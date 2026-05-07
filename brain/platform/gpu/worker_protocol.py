"""Base worker protocol: FastAPI on UDS, signal handling, health."""

import abc
import asyncio
import logging
import os
import signal
import time
from enum import Enum

from brain.platform.gpu.config import WorkerManifest

logger = logging.getLogger("brain.platform.gpu.worker")


class WorkerState(Enum):
    REGISTERED = "registered"
    LOADING = "loading"
    READY = "ready"
    BUSY = "busy"
    DRAINING = "draining"
    UNLOADING = "unloading"
    STOPPED = "stopped"
    FAILED = "failed"


class BaseWorker(abc.ABC):
    """Base class for GPU model workers.

    Subclasses implement load_model(), unload_model(), and handle_request().
    The base class provides health tracking, state management, and the
    FastAPI app for serving on a Unix domain socket.
    """

    def __init__(self, manifest: WorkerManifest):
        self.manifest = manifest
        self.state = WorkerState.REGISTERED
        self.last_activity = time.time()
        self.requests_total = 0
        self._start_time = time.time()
        # Serialize inference — one request at a time prevents VRAM spikes
        # from concurrent requests accumulating intermediate tensors.
        self._infer_lock = asyncio.Lock()

    @abc.abstractmethod
    def load_model(self):
        """Load the model onto GPU. Called once at startup."""

    @abc.abstractmethod
    def unload_model(self):
        """Unload the model from GPU. Called on shutdown/eviction."""

    @abc.abstractmethod
    async def handle_request(self, data: dict) -> dict:
        """Process a single inference request. Return JSON-serializable dict."""

    def touch_activity(self):
        self.last_activity = time.time()
        self.requests_total += 1

    def get_health(self) -> dict:
        return {
            "name": self.manifest.name,
            "status": self.state.value,
            "vram_mb": self.manifest.vram_mb,
            "idle_s": round(time.time() - self.last_activity),
            "requests_total": self.requests_total,
            "uptime_s": round(time.time() - self._start_time),
        }

    def build_app(self):
        """Build a FastAPI app for this worker."""
        from fastapi import FastAPI, Request
        from fastapi.responses import JSONResponse

        app = FastAPI(title=f"GPU Worker: {self.manifest.name}")
        worker = self

        @app.get("/health")
        async def health():
            return worker.get_health()

        @app.post("/infer")
        async def infer(request: Request):
            if worker.state == WorkerState.DRAINING:
                return JSONResponse(
                    status_code=503,
                    content={"error": "draining", "message": "Worker is shutting down"},
                )
            if worker.state != WorkerState.READY:
                return JSONResponse(
                    status_code=503,
                    content={"error": "not_ready", "message": f"Worker state: {worker.state.value}"},
                )

            # Serialize inference to prevent concurrent VRAM allocation.
            # Multiple in-flight requests accumulate intermediate tensors
            # and cause OOM — the #1 reason workers crash.
            async with worker._infer_lock:
                worker.state = WorkerState.BUSY
                try:
                    data = await request.json()
                    worker.touch_activity()
                    result = await worker.handle_request(data)
                    return result
                except Exception as exc:
                    error_msg = str(exc)
                    logger.error(f"[worker:{worker.manifest.name}] Inference error: {error_msg}")
                    # Attempt CUDA recovery — clear leaked tensors
                    try:
                        import torch
                        if torch.cuda.is_available():
                            torch.cuda.empty_cache()
                    except Exception:
                        pass
                    return JSONResponse(
                        status_code=500,
                        content={"error": "inference_error", "message": error_msg[:500]},
                    )
                finally:
                    if worker.state == WorkerState.BUSY:
                        worker.state = WorkerState.READY

        return app

    def run(self, socket_path: str):
        """Load model, start uvicorn on UDS, handle signals."""
        import uvicorn

        self.state = WorkerState.LOADING
        logger.info(f"[worker:{self.manifest.name}] Loading model from {self.manifest.model_path}")
        try:
            self.load_model()
        except Exception as e:
            logger.error(f"[worker:{self.manifest.name}] Failed to load model: {e}")
            self.state = WorkerState.FAILED
            raise

        self.state = WorkerState.READY
        self.last_activity = time.time()
        logger.info(f"[worker:{self.manifest.name}] Model loaded, serving on {socket_path}")

        app = self.build_app()

        def _shutdown(signum, frame):
            logger.info(f"[worker:{self.manifest.name}] Received signal {signum}, shutting down")
            self.state = WorkerState.UNLOADING
            self.unload_model()
            self.state = WorkerState.STOPPED
            raise SystemExit(0)

        signal.signal(signal.SIGTERM, _shutdown)

        uvicorn.run(app, uds=socket_path, log_level="warning")
