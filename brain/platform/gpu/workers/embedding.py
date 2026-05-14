"""Embedding worker — Qwen3-Embedding-8B via SentenceTransformer."""

import asyncio
import logging
import os
import time

from brain.platform.gpu.config import WorkerManifest
from brain.platform.gpu.device import default_dtype, empty_device_cache, select_device
from brain.platform.gpu.worker_protocol import BaseWorker

logger = logging.getLogger("brain.platform.gpu.worker.embedding")

QUERY_PREFIX = "Instruct: Retrieve semantically similar memory entries\nQuery: "
DEFAULT_NON_CUDA_FALLBACK_MODEL = "all-MiniLM-L6-v2"


def _configured_embedding_dim() -> int:
    raw_dim = os.environ.get("EMBEDDING_DIM")
    if raw_dim:
        try:
            return int(raw_dim)
        except ValueError:
            logger.warning("Invalid EMBEDDING_DIM=%r; falling back to kernel config", raw_dim)

    try:
        from brain.kernel import config as cfg
        return int(getattr(cfg, "EMBEDDING_DIM", 2000))
    except Exception:
        return 2000


class EmbeddingWorker(BaseWorker):
    """Loads SentenceTransformer on GPU, serves /infer for embed requests."""

    def __init__(self, manifest: WorkerManifest):
        super().__init__(manifest)
        # Respect configured vector size so worker output matches DB schema.
        self.truncate_dim = _configured_embedding_dim()
        self.model = None
        self.device = "cpu"
        self.loaded_model_path = manifest.model_path

    def _model_path_for_device(self, device: str) -> tuple[str, str]:
        if device == "cuda":
            return self.manifest.model_path, device
        if os.environ.get("GPU_ALLOW_LARGE_EMBEDDING_ON_NON_CUDA", "").lower() in {"1", "true", "yes", "on"}:
            return self.manifest.model_path, device
        fallback_model = (
            os.environ.get("GPU_EMBEDDING_FALLBACK_MODEL")
            or os.environ.get("EMBEDDING_CPU_MODEL")
            or DEFAULT_NON_CUDA_FALLBACK_MODEL
        )
        logger.warning(
            "CUDA is unavailable; loading lightweight embedding fallback %s on CPU instead of %s",
            fallback_model,
            self.manifest.model_path,
        )
        return fallback_model, "cpu"

    def load_model(self):
        import torch
        from sentence_transformers import SentenceTransformer

        t0 = time.time()
        self.device = select_device(torch, "GPU_EMBEDDING_DEVICE", "EMBEDDING_DEVICE")
        self.loaded_model_path, self.device = self._model_path_for_device(self.device)
        dtype = default_dtype(torch, self.device)
        logger.info(f"Using device: {self.device}, dtype: {dtype}")

        def _load(device: str, model_dtype):
            return SentenceTransformer(
                self.loaded_model_path,
                device=device,
                model_kwargs={"torch_dtype": model_dtype},
            )

        try:
            self.model = _load(self.device, dtype)
        except torch.cuda.OutOfMemoryError:
            logger.warning("OOM on first load attempt — clearing cache and retrying")
            empty_device_cache(torch, self.device)
            time.sleep(2)
            self.model = _load(self.device, dtype)
        except Exception:
            if self.device == "cpu":
                raise
            logger.exception("Failed to load on %s — falling back to CPU", self.device)
            empty_device_cache(torch, self.device)
            self.device = "cpu"
            self.loaded_model_path = (
                os.environ.get("GPU_EMBEDDING_FALLBACK_MODEL")
                or os.environ.get("EMBEDDING_CPU_MODEL")
                or DEFAULT_NON_CUDA_FALLBACK_MODEL
            )
            self.model = _load(self.device, torch.float32)
        elapsed = time.time() - t0
        device_mb = torch.cuda.memory_allocated() / 1024**2 if self.device == "cuda" else 0
        logger.info(f"Model loaded in {elapsed:.1f}s — device={self.device} allocated={device_mb:.0f}MB")

    def unload_model(self):
        if self.model is not None:
            del self.model
            self.model = None
            try:
                import torch
                empty_device_cache(torch, self.device)
            except Exception:
                pass
            logger.info("Model unloaded, GPU memory freed")

    async def handle_request(self, data: dict) -> dict:
        import torch

        texts = data.get("texts", [])
        mode = data.get("mode", "document")

        if not texts:
            return {"error": "texts required"}

        t0 = time.time()

        if mode == "query":
            texts = [QUERY_PREFIX + t for t in texts]

        batch_size = max(1, self.manifest.max_batch_size)
        while True:
            try:
                embeddings = self.model.encode(
                    texts,
                    normalize_embeddings=True,
                    truncate_dim=self.truncate_dim,
                    show_progress_bar=False,
                    batch_size=batch_size,
                )
                result = self._fit_embedding_dim(embeddings).tolist()
                break
            except torch.cuda.OutOfMemoryError:
                empty_device_cache(torch, self.device)
                if batch_size <= 1:
                    raise
                batch_size = max(1, batch_size // 2)
                logger.warning("Embedding OOM — retrying request with batch_size=%s", batch_size)
                await asyncio.sleep(1)
            finally:
                empty_device_cache(torch, self.device)

        elapsed = time.time() - t0
        return {
            "embeddings": result,
            "dims": self.truncate_dim,
            "count": len(texts),
            "elapsed_ms": round(elapsed * 1000, 1),
        }

    def _fit_embedding_dim(self, embeddings):
        import numpy as np

        arr = np.asarray(embeddings, dtype=np.float32)
        if arr.ndim == 1:
            arr = arr.reshape(1, -1)
        dims = arr.shape[1]
        if dims == self.truncate_dim:
            return arr
        if dims > self.truncate_dim:
            return arr[:, :self.truncate_dim]
        padded = np.zeros((arr.shape[0], self.truncate_dim), dtype=np.float32)
        padded[:, :dims] = arr
        norms = np.linalg.norm(padded, axis=1, keepdims=True)
        np.divide(padded, norms, out=padded, where=norms > 0)
        return padded


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True)
    parser.add_argument("--model-path", required=True)
    parser.add_argument("--socket", required=True)
    args = parser.parse_args()

    manifest = WorkerManifest(name=args.name, model_path=args.model_path, vram_mb=5000,
                              worker_module="brain.platform.gpu.workers.embedding")
    worker = EmbeddingWorker(manifest)
    worker.run(socket_path=args.socket)
