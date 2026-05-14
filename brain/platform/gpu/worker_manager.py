"""Worker lifecycle manager: start, stop, drain, evict, crash-recover."""

import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from brain.platform.async_io import popen_sync, run_subprocess_sync
from brain.platform.gpu.config import WorkerManifest
from brain.platform.gpu.vram import VRAMBookkeeper

logger = logging.getLogger("brain.platform.gpu.manager")

# How long to wait before auto-recovering a "failed" worker (seconds).
FAILED_RECOVERY_INTERVAL = 120


def _backoff_seconds(attempt: int) -> int:
    """Exponential backoff: 1, 2, 4, 8, 16, 30 (capped)."""
    return min(2 ** attempt, 30)


class WorkerManager:
    """Manages worker subprocess lifecycles."""

    def __init__(self, vram: VRAMBookkeeper, socket_dir: str = "/tmp",
                 max_restarts: int = 5, restart_window: int = 300,
                 reclaim_conflicting_processes: bool = True):
        self.vram = vram
        self.socket_dir = socket_dir
        self.max_restarts = max_restarts
        self.restart_window = restart_window
        self.reclaim_conflicting_processes = reclaim_conflicting_processes
        self.workers: dict[str, dict] = {}

    def register(self, manifest: WorkerManifest):
        """Register a worker manifest (does not start it)."""
        self.workers[manifest.name] = {
            "manifest": manifest,
            "process": None,
            "status": "registered",
            "last_activity": time.time(),
            "failure_count": 0,
            "first_failure_time": 0.0,
            "failed_at": 0.0,
            "loading_since": 0.0,
            "ready_since": 0.0,
            "last_crash_output": "",
            "restart_after": 0.0,
            "socket_path": os.path.join(self.socket_dir, f"gpu_worker_{manifest.name}.sock"),
        }

    def sync_manifests(self, manifests: list[WorkerManifest]):
        """Upsert live worker manifests from current config."""
        for manifest in manifests:
            if manifest.name in self.workers:
                self.workers[manifest.name]["manifest"] = manifest
            else:
                self.register(manifest)

    def cleanup_orphaned_workers(self):
        """Kill stray worker processes from prior GPU server instances."""
        for w in self.workers.values():
            self._kill_matching_workers(w)

    def cleanup_conflicting_gpu_processes(self) -> list[int]:
        """Terminate known non-managed model processes that commonly hold VRAM.

        This intentionally targets Illo/LLM runtimes we know about rather than
        arbitrary CUDA users on the machine.
        """
        if not self.reclaim_conflicting_processes:
            return []

        tracked_pids = {
            getattr(w.get("process"), "pid", None)
            for w in self.workers.values()
            if w.get("process") is not None
        }
        tracked_pids.discard(None)
        current_pid = os.getpid()
        killed: list[int] = []

        for pid, command in self._find_conflicting_gpu_processes():
            if pid == current_pid or pid in tracked_pids:
                continue
            logger.warning("Terminating GPU memory consumer pid %s before model startup: %s", pid, command[:200])
            try:
                os.kill(pid, signal.SIGTERM)
                killed.append(pid)
            except ProcessLookupError:
                continue
            except Exception as exc:
                logger.warning("Failed to terminate GPU consumer pid %s: %s", pid, exc)

        if killed:
            time.sleep(2)
            for pid in killed:
                if not self._pid_is_alive(pid):
                    continue
                logger.warning("GPU memory consumer pid %s survived SIGTERM; killing", pid)
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    continue
                except Exception as exc:
                    logger.warning("Failed to kill GPU consumer pid %s: %s", pid, exc)

        return killed

    def reset_failure_state(self, name: str):
        """Reset failure counters. Called on manual restart or recovery."""
        w = self.workers.get(name)
        if w:
            w["failure_count"] = 0
            w["first_failure_time"] = 0.0
            w["failed_at"] = 0.0
            w["loading_since"] = 0.0
            w["ready_since"] = 0.0
            w["restart_after"] = 0.0
            w["last_crash_output"] = ""

    def _record_failure(self, name: str, *, crash_output: str = "") -> bool:
        """Track a worker startup/runtime failure and schedule recovery."""
        w = self.workers.get(name)
        if not w:
            return False

        now = time.time()
        if crash_output:
            w["last_crash_output"] = crash_output[:2000]

        if not w["first_failure_time"] or now - w["first_failure_time"] > self.restart_window:
            w["failure_count"] = 0
            w["first_failure_time"] = now

        w["failure_count"] += 1
        w["process"] = None
        w["status"] = "stopped"
        w["failed_at"] = 0.0
        w["loading_since"] = 0.0
        w["ready_since"] = 0.0

        if w["failure_count"] > self.max_restarts:
            logger.error(
                f"Worker '{name}' failed {w['failure_count']} times in {self.restart_window}s "
                f"— marking FAILED (will auto-recover in {FAILED_RECOVERY_INTERVAL}s)"
            )
            w["status"] = "failed"
            w["failed_at"] = now
            w["restart_after"] = 0.0
            return False

        delay = _backoff_seconds(w["failure_count"] - 1)
        w["restart_after"] = now + delay
        logger.warning(f"Restarting worker '{name}' in {delay}s (attempt {w['failure_count']}/{self.max_restarts})")
        return False

    def eviction_order(self) -> list[dict]:
        """Return workers sorted by eviction priority: lowest priority first, then oldest idle."""
        candidates = [
            w for w in self.workers.values()
            if w["status"] in ("ready", "busy")
        ]
        candidates.sort(key=lambda w: (w["manifest"].priority, -w["last_activity"]))
        return candidates

    def start_worker(self, name: str) -> bool:
        """Start a worker subprocess. Returns True if started successfully."""
        w = self.workers.get(name)
        if not w:
            logger.error(f"Unknown worker: {name}")
            return False

        if self._is_process_alive(w.get("process")):
            if w["status"] not in ("ready", "busy", "loading"):
                w["status"] = "ready"
            logger.info(f"Worker '{name}' already running (pid {w['process'].pid})")
            return True

        w["process"] = None
        self._kill_matching_workers(w)

        manifest = w["manifest"]

        if not self.vram.has_space(manifest.vram_mb, refresh=True):
            if self.reclaim_conflicting_processes:
                self.cleanup_conflicting_gpu_processes()
                self.vram.reconcile()

        if not self.vram.has_space(manifest.vram_mb):
            freed = self._evict_for_space(
                manifest.vram_mb,
                exclude=name,
                target_priority=manifest.priority,
            )
            if not freed:
                logger.error(f"Cannot start {name}: not enough VRAM ({self.vram.free_mb}MB free, need {manifest.vram_mb}MB)")
                return False

        if os.path.exists(w["socket_path"]):
            os.unlink(w["socket_path"])

        w["status"] = "loading"
        w["loading_since"] = time.time()
        w["ready_since"] = 0.0
        logger.info(f"Starting worker '{name}' (VRAM: {manifest.vram_mb}MB)")

        try:
            # Use repo root as CWD so `brain` package is importable
            repo_root = str(Path(__file__).resolve().parents[3])

            # Write worker output to a log file instead of a pipe.
            # Captured pipes deadlock when the buffer fills (64KB) and nobody
            # reads — this was a common cause of worker hangs.
            log_dir = os.path.join(repo_root, "logs")
            os.makedirs(log_dir, exist_ok=True)
            log_path = os.path.join(log_dir, f"gpu_worker_{name}.log")
            log_file = open(log_path, "a")

            popen = popen_sync(
                [sys.executable, "-m", manifest.worker_module,
                 "--name", manifest.name,
                 "--model-path", manifest.model_path,
                 "--socket", w["socket_path"]],
                start_new_session=True,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                cwd=repo_root,
            )
            proc = _ManagedProcess(popen.pid, popen)
            w["process"] = proc
            w["_log_file"] = log_file
            w["_log_path"] = log_path
        except Exception as e:
            logger.error(f"Failed to start worker '{name}': {e}")
            self._close_log(w)
            return self._record_failure(name, crash_output=str(e))

        deadline = time.time() + manifest.load_timeout
        while time.time() < deadline:
            if os.path.exists(w["socket_path"]):
                w["status"] = "ready"
                w["last_activity"] = time.time()
                w["loading_since"] = 0.0
                w["ready_since"] = time.time()
                w["failure_count"] = 0
                w["first_failure_time"] = 0.0
                w["failed_at"] = 0.0
                w["restart_after"] = 0.0
                w["last_crash_output"] = ""
                self.vram.allocate(name, manifest.vram_mb)
                logger.info(f"Worker '{name}' ready ({time.time() - (deadline - manifest.load_timeout):.1f}s)")
                return True
            if proc.poll() is not None:
                crash_output = self._read_crash_output(w)
                exit_code = getattr(proc, "returncode", None)
                logger.error(f"Worker '{name}' exited during load (code {exit_code}): {crash_output[:500]}")
                self._close_log(w)
                return self._record_failure(name, crash_output=crash_output)
            time.sleep(0.5)

        logger.error(f"Worker '{name}' failed to start within {manifest.load_timeout}s")
        self._kill_worker(name)
        self._close_log(w)
        return self._record_failure(name, crash_output=f"startup timeout after {manifest.load_timeout}s")

    def stop_worker(self, name: str, drain_timeout: int = 30):
        """Gracefully stop a worker: drain → SIGTERM → wait → SIGKILL."""
        w = self.workers.get(name)
        if not w or not w["process"]:
            return

        proc = w["process"]
        w["status"] = "draining"
        logger.info(f"Draining worker '{name}' (timeout: {drain_timeout}s)")
        time.sleep(min(drain_timeout, 5))

        w["status"] = "unloading"
        try:
            self._terminate_process(proc)
            proc.wait(timeout=10)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            self._kill_worker(name)

        w["status"] = "stopped"
        w["process"] = None
        w["loading_since"] = 0.0
        w["ready_since"] = 0.0
        self.vram.release(name)
        self._close_log(w)

        if os.path.exists(w["socket_path"]):
            os.unlink(w["socket_path"])

        logger.info(f"Worker '{name}' stopped")

    def _kill_worker(self, name: str):
        """Force-kill a worker subprocess."""
        w = self.workers.get(name)
        if w and w["process"]:
            proc = w["process"]
            try:
                self._kill_process(proc)
                proc.wait(timeout=5)
            except Exception:
                pass

    def _is_process_alive(self, proc) -> bool:
        return proc is not None and proc.poll() is None

    def _kill_matching_workers(self, w: dict):
        """Kill any stray processes matching this worker's module + socket."""
        tracked_pid = getattr(w.get("process"), "pid", None)
        pids = self._find_matching_worker_pids(w)
        for pid in pids:
            if tracked_pid and pid == tracked_pid:
                continue
            logger.warning(
                "Killing orphan worker '%s' with pid %s before start/recovery",
                w["manifest"].name,
                pid,
            )
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                continue
            except Exception as exc:
                logger.warning("Failed to kill orphan worker pid %s: %s", pid, exc)
        if pids and os.path.exists(w["socket_path"]):
            try:
                os.unlink(w["socket_path"])
            except FileNotFoundError:
                pass

    def _find_matching_worker_pids(self, w: dict) -> list[int]:
        """Find worker processes by module name and socket path."""
        try:
            result = run_subprocess_sync(
                ["ps", "-axo", "pid=,command="],
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
        except Exception:
            return []

        if result.returncode != 0:
            return []

        expected = (w["manifest"].worker_module, "--socket", w["socket_path"])
        matches: list[int] = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                pid_str, command = line.split(None, 1)
            except ValueError:
                continue
            if all(part in command for part in expected):
                try:
                    matches.append(int(pid_str))
                except ValueError:
                    continue
        return matches

    def _find_conflicting_gpu_processes(self) -> list[tuple[int, str]]:
        """Find known model-serving processes outside this manager."""
        try:
            result = run_subprocess_sync(
                ["ps", "-axo", "pid=,command="],
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
        except Exception:
            return []

        if result.returncode != 0:
            return []

        matches: list[tuple[int, str]] = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                pid_str, command = line.split(None, 1)
                pid = int(pid_str)
            except ValueError:
                continue
            if self._is_conflicting_gpu_command(command):
                matches.append((pid, command))
        return matches

    @staticmethod
    def _is_conflicting_gpu_command(command: str) -> bool:
        lowered = command.lower()
        if "ollama" in lowered and "runner" in lowered:
            return True
        if "ollama_llama_server" in lowered:
            return True
        if "brain.platform.gpu.workers." in lowered:
            return True
        if "embed_server/server.py" in lowered or "-m embed_server" in lowered:
            return True
        return False

    @staticmethod
    def _pid_is_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def _close_log(self, w: dict):
        """Close the worker's log file handle if open."""
        log_file = w.pop("_log_file", None)
        if log_file:
            try:
                log_file.close()
            except Exception:
                pass

    def _read_crash_output(self, w: dict) -> str:
        """Read the last 2KB from the worker's log file for crash diagnostics."""
        log_path = w.get("_log_path")
        if not log_path or not os.path.exists(log_path):
            return ""
        try:
            with open(log_path, "r") as f:
                f.seek(0, 2)  # end
                size = f.tell()
                f.seek(max(0, size - 2048))
                return f.read().strip()
        except Exception:
            return ""

    def _evict_for_space(self, needed_mb: int, exclude: str = "", target_priority: int | None = None) -> bool:
        """Evict lowest-priority idle workers until enough VRAM is free."""
        candidates = self.eviction_order()
        for w in candidates:
            if w["manifest"].name == exclude:
                continue
            if target_priority is not None and w["manifest"].priority >= target_priority:
                continue
            if self.vram.has_space(needed_mb):
                return True
            logger.warning(f"Evicting worker '{w['manifest'].name}' to free {w['manifest'].vram_mb}MB")
            self.stop_worker(w["manifest"].name)
        return self.vram.has_space(needed_mb, refresh=True)

    def check_workers(self):
        """Poll worker processes. Detect crashes and trigger restart.

        Also schedules auto-recovery for workers stuck in "failed" state
        after FAILED_RECOVERY_INTERVAL — the failure counter is reset so
        the worker gets a fresh set of restart attempts.
        """
        now = time.time()
        for name, w in self.workers.items():
            # Auto-recover "failed" workers after cooldown
            if w["status"] == "failed":
                failed_at = w.get("failed_at", 0)
                if failed_at and now - failed_at >= FAILED_RECOVERY_INTERVAL:
                    logger.info(f"Worker '{name}' auto-recovering after {FAILED_RECOVERY_INTERVAL}s cooldown")
                    self.reset_failure_state(name)
                    w["status"] = "stopped"
                    w["restart_after"] = now + 1
                continue

            if w["process"] is None:
                continue
            if w["process"].poll() is not None:
                exit_code = getattr(w["process"], "returncode", None)
                crash_output = self._read_crash_output(w)
                logger.warning(f"Worker '{name}' crashed (exit code {exit_code})")
                if crash_output:
                    for line in crash_output[-500:].splitlines()[-5:]:
                        logger.warning(f"  [{name}] {line}")
                self.vram.release(name)
                self._close_log(w)

                if os.path.exists(w["socket_path"]):
                    os.unlink(w["socket_path"])
                self._record_failure(name, crash_output=crash_output)

    def _terminate_process(self, proc):
        """Terminate a worker process group, falling back to the process."""
        self._signal_process(proc, signal.SIGTERM)

    def _kill_process(self, proc):
        """Kill a worker process group, falling back to the process."""
        self._signal_process(proc, signal.SIGKILL)

    def _signal_process(self, proc, sig: int):
        pid = getattr(proc, "pid", None)
        if not pid:
            return
        try:
            os.killpg(pid, sig)
            return
        except ProcessLookupError:
            return
        except Exception:
            pass

        try:
            if sig == signal.SIGTERM:
                proc.terminate()
            else:
                proc.kill()
        except ProcessLookupError:
            return


class _ManagedProcess:
    """Small process wrapper that also works for externally killed children."""

    def __init__(self, pid: int, popen: subprocess.Popen | None = None):
        self.pid = pid
        self._popen = popen

    def poll(self):
        if self._popen is not None:
            return self._popen.poll()
        try:
            os.kill(self.pid, 0)
            return None
        except ProcessLookupError:
            return -1

    @property
    def returncode(self):
        if self._popen is not None:
            return self._popen.returncode
        return self.poll()

    def wait(self, timeout: float | None = None):
        if self._popen is not None:
            return self._popen.wait(timeout=timeout)

        deadline = None if timeout is None else time.time() + timeout
        while True:
            code = self.poll()
            if code is not None:
                return code
            if deadline is not None and time.time() >= deadline:
                raise subprocess.TimeoutExpired(cmd=f"pid:{self.pid}", timeout=timeout)
            time.sleep(0.1)

    def terminate(self):
        os.kill(self.pid, signal.SIGTERM)

    def kill(self):
        os.kill(self.pid, signal.SIGKILL)
