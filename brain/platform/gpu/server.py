"""Unified GPU Model Server — main process.

Routes HTTP requests to worker subprocesses via Unix domain sockets.
Manages worker lifecycle, VRAM, crash recovery, and API fallback.
"""

import json
import logging
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

import httpx

from brain.platform.gpu.config import ServerConfig, build_worker_manifests
from brain.platform.gpu.vram import VRAMBookkeeper, query_gpu_total_mb
from brain.platform.gpu.worker_manager import WorkerManager, FAILED_RECOVERY_INTERVAL, _backoff_seconds
from brain.platform.async_io import sync_http_client

logger = logging.getLogger("brain.platform.gpu")

ROUTES = {
    "/embed": "embedding",
    "/generate": "llm",
}


def _parse_fallback_policy(value: str | None) -> str:
    if value in ("auto", "local-only", "api-only"):
        return value
    return "local-only"


def _should_fallback(policy: str, worker_status: str, has_api_config: bool) -> bool:
    if not has_api_config:
        return False
    if policy == "api-only":
        return True
    if policy == "local-only":
        return False
    return worker_status in ("failed", "stopped", "loading")


class GPUServer:
    """Main GPU server: HTTP router + worker manager."""

    def __init__(self, config: ServerConfig | None = None):
        self.config = config or ServerConfig()
        total = query_gpu_total_mb() or 32000
        self.vram = VRAMBookkeeper(total_mb=total)
        self.manager = WorkerManager(
            self.vram,
            socket_dir=self.config.socket_dir,
            max_restarts=self.config.max_restart_attempts,
            restart_window=self.config.restart_window,
            reclaim_conflicting_processes=self.config.reclaim_conflicting_processes,
        )

    def route_for_endpoint(self, path: str) -> str | None:
        for prefix, worker in ROUTES.items():
            if path.startswith(prefix):
                return worker
        return None

    def sync_worker_manifests(self):
        manifests = build_worker_manifests()
        self.manager.sync_manifests(manifests)
        return manifests

    def aggregate_health(self) -> dict:
        self.sync_worker_manifests()
        workers_health = {}
        now = time.time()
        for name, w in self.manager.workers.items():
            info = {
                "status": w["status"],
                "vram_mb": w["manifest"].vram_mb,
                "failure_count": w.get("failure_count", 0),
                "log_path": w.get("_log_path"),
            }
            if w.get("loading_since"):
                info["loading_for_s"] = max(0, round(now - w["loading_since"]))
                info["load_timeout_s"] = w["manifest"].load_timeout
            if w.get("ready_since"):
                info["ready_for_s"] = max(0, round(now - w["ready_since"]))
            if w.get("restart_after", 0):
                info["restart_in_s"] = max(0, round(w["restart_after"] - now))
            if w.get("last_crash_output") and w["status"] != "failed":
                info["last_crash_output"] = w.get("last_crash_output", "")[:500]
            if w["status"] == "failed":
                info["last_crash_output"] = w.get("last_crash_output", "")[:500]
                failed_at = w.get("failed_at", 0)
                if failed_at:
                    remaining = max(0, int(FAILED_RECOVERY_INTERVAL - (time.time() - failed_at)))
                    info["auto_recovery_in_s"] = remaining
            workers_health[name] = info

        statuses = [w["status"] for w in self.manager.workers.values()]
        if not statuses:
            status = "down"
        elif all(s == "ready" for s in statuses):
            status = "ok"
        elif any(s == "ready" for s in statuses):
            status = "degraded"
        else:
            status = "down"

        embedding_ready = workers_health.get("embedding", {}).get("status") == "ready"

        return {
            "status": status,
            "server": "alive",
            "embedding_ready": embedding_ready,
            "gpu_total_mb": self.vram.total_mb,
            "gpu_free_mb": self.vram.free_mb,
            "workers": workers_health,
        }

    def proxy_to_worker(self, worker_name: str, data: bytes, timeout: int = 30) -> tuple[int, dict]:
        w = self.manager.workers.get(worker_name)
        if not w:
            return 404, {"error": "unknown_worker", "message": f"No worker named '{worker_name}'"}

        if w["status"] not in ("ready", "busy"):
            detail = f"Worker '{worker_name}' is {w['status']}"
            if w.get("last_crash_output"):
                detail += f" | recent crash: {w['last_crash_output'][:200]}"
            return 503, {
                "error": "worker_unavailable",
                "message": detail,
            }

        sock_path = w["socket_path"]
        try:
            transport = httpx.HTTPTransport(uds=sock_path)
            with sync_http_client(transport=transport, timeout=timeout) as client:
                resp = client.post(
                    "http://localhost/infer",
                    content=data,
                    headers={"Content-Type": "application/json"},
                )
                w["last_activity"] = time.time()
                return resp.status_code, resp.json()
        except Exception as e:
            detail = str(e)
            if w.get("last_crash_output"):
                detail += f" | recent crash: {w['last_crash_output'][:200]}"
            return 502, {"error": "worker_error", "message": detail}

    def setup_workers(self):
        manifests = self.sync_worker_manifests()
        self.manager.cleanup_orphaned_workers()
        self.manager.cleanup_conflicting_gpu_processes()

        def _preload(name: str):
            ok = self.manager.start_worker(name)
            if not ok:
                logger.warning(f"Worker '{name}' failed initial load")

        for manifest in sorted(manifests, key=lambda item: item.priority, reverse=True):
            if manifest.preload:
                _preload(manifest.name)

    def start_poll_loop(self):
        def _loop():
            while True:
                time.sleep(self.config.poll_interval)
                self.manager.check_workers()

                now = time.time()
                for name, w in self.manager.workers.items():
                    # Retry stopped workers that have a scheduled restart
                    if w["status"] == "stopped" and w.get("restart_after", 0) <= now and w.get("restart_after", 0) > 0:
                        logger.info(f"Restarting worker '{name}' (attempt {w['failure_count']})")
                        self.manager.start_worker(name)

                    # Also pick up preload workers that are stopped with no
                    # restart scheduled — this catches edge cases where
                    # restart_after was cleared but the worker never came up
                    if (w["status"] == "stopped"
                            and w["manifest"].preload
                            and w.get("restart_after", 0) == 0
                            and w.get("failure_count", 0) < self.manager.max_restarts):
                        delay = _backoff_seconds(w.get("failure_count", 0))
                        w["restart_after"] = now + delay
                        logger.info(f"Worker '{name}' is stopped but should be running — scheduling restart in {delay}s")

                    idle_timeout = w["manifest"].idle_timeout
                    if (idle_timeout > 0
                            and w["status"] == "ready"
                            and now - w["last_activity"] >= idle_timeout):
                        logger.info(f"Worker '{name}' idle for {idle_timeout}s — unloading to free GPU")
                        self.manager.stop_worker(name)

        t = threading.Thread(target=_loop, daemon=True)
        t.start()

    def start_reconciliation_loop(self):
        def _loop():
            while True:
                time.sleep(self.config.reconciliation_interval)
                self.vram.reconcile()

        t = threading.Thread(target=_loop, daemon=True)
        t.start()


class RequestHandler(BaseHTTPRequestHandler):
    server_instance: GPUServer = None

    def do_GET(self):
        srv = self.server_instance
        if self.path == "/health":
            self._respond(200, srv.aggregate_health())
        elif self.path == "/models":
            srv.sync_worker_manifests()
            models = []
            for name, w in srv.manager.workers.items():
                models.append({
                    "name": name, "status": w["status"],
                    "vram_mb": w["manifest"].vram_mb,
                })
            self._respond(200, models)
        else:
            self._respond(404, {"error": "not_found"})

    def do_POST(self):
        srv = self.server_instance
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b"{}"

        if self.path.startswith("/models/") and self.path.endswith("/load"):
            name = self.path.split("/")[2]
            srv.sync_worker_manifests()
            if name not in srv.manager.workers:
                self._respond(404, {"error": "unknown_worker", "message": f"No worker named '{name}'"})
                return
            srv.manager.reset_failure_state(name)
            ok = srv.manager.start_worker(name)
            self._respond(200 if ok else 500, {"ok": ok})
            return

        if self.path.startswith("/models/") and self.path.endswith("/unload"):
            name = self.path.split("/")[2]
            srv.manager.stop_worker(name)
            self._respond(200, {"ok": True})
            return

        worker_name = srv.route_for_endpoint(self.path)
        if not worker_name:
            self._respond(404, {"error": "not_found"})
            return

        policy = _parse_fallback_policy(self.headers.get("X-Fallback-Policy"))
        w = srv.manager.workers.get(worker_name, {})
        has_api = bool(w.get("manifest", type("M", (), {"api_fallback": {}})()).api_fallback)
        worker_status = w.get("status", "stopped")

        if _should_fallback(policy, worker_status, has_api):
            self._respond(501, {"error": "api_fallback_not_implemented"})
            return

        # Auto-chunk embed batches
        if worker_name == "embedding":
            try:
                data = json.loads(body)
                max_batch = w.get("manifest", type("M", (), {"max_batch_size": 64})()).max_batch_size
                texts = data.get("texts", [])
                if len(texts) > max_batch:
                    all_embeddings = []
                    for i in range(0, len(texts), max_batch):
                        chunk = {"texts": texts[i:i + max_batch], "mode": data.get("mode", "document")}
                        status, result = srv.proxy_to_worker(
                            worker_name,
                            json.dumps(chunk).encode(),
                            timeout=srv.config.embedding_request_timeout,
                        )
                        if status != 200:
                            self._respond(status, result)
                            return
                        all_embeddings.extend(result.get("embeddings", []))
                    self._respond(200, {
                        "embeddings": all_embeddings,
                        "dims": result.get("dims", 2000),
                        "count": len(all_embeddings),
                    })
                    return
            except (json.JSONDecodeError, KeyError):
                pass

        request_timeout = (
            srv.config.llm_request_timeout
            if worker_name == "llm"
            else srv.config.embedding_request_timeout
        )
        status, result = srv.proxy_to_worker(worker_name, body, timeout=request_timeout)
        self._respond(status, result)

    def _respond(self, code: int, data):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def log_message(self, format, *args):
        if "/health" not in str(args):
            logger.debug(f"{args[0]}")


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    )

    config = ServerConfig()
    srv = GPUServer(config)

    logger.info(f"GPU Server starting on {config.host}:{config.port}")
    logger.info(f"GPU VRAM: {srv.vram.total_mb}MB total")

    # Bind the HTTP server FIRST — fail fast if the port is in use.
    # Loading models onto GPU takes 10+ seconds; if we do that first and
    # then the port bind fails, the models sit on GPU with no server
    # managing them, wasting VRAM until the orphan workers are killed.
    class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    RequestHandler.server_instance = srv
    try:
        httpd = ThreadedHTTPServer((config.host, config.port), RequestHandler)
    except OSError as e:
        logger.error(f"Cannot bind {config.host}:{config.port}: {e}")
        logger.error("Is another GPU server still running? Kill it first.")
        raise SystemExit(1)

    logger.info(f"HTTP server bound to {config.host}:{config.port}")

    srv.start_poll_loop()
    srv.start_reconciliation_loop()

    # Serve health/model endpoints immediately while workers warm in the
    # background. Binding the port is not enough: without this thread, clients
    # can connect but /health stalls until preload finishes or times out.
    setup_thread = threading.Thread(target=srv.setup_workers, name="gpu-worker-setup", daemon=True)
    setup_thread.start()

    logger.info(f"Serving on {config.host}:{config.port}")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        for name in list(srv.manager.workers):
            srv.manager.stop_worker(name)
        httpd.shutdown()


if __name__ == "__main__":
    main()
