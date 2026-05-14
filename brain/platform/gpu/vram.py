"""VRAM bookkeeping and nvidia-smi queries."""

import logging
import subprocess

from brain.platform.async_io import run_subprocess_sync

logger = logging.getLogger("brain.platform.gpu.vram")


def query_gpu_total_mb() -> int | None:
    try:
        result = run_subprocess_sync(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return int(result.stdout.strip().split("\n")[0])
    except Exception:
        pass
    return None


def query_gpu_used_mb() -> int | None:
    try:
        result = run_subprocess_sync(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return int(result.stdout.strip().split("\n")[0])
    except Exception:
        pass
    return None


class VRAMBookkeeper:
    def __init__(self, total_mb: int):
        self.total_mb = total_mb
        self._allocations: dict[str, int] = {}
        self._actual_used_override: int | None = None

    @property
    def allocated_mb(self) -> int:
        return sum(self._allocations.values())

    @property
    def free_mb(self) -> int:
        if self._actual_used_override is not None:
            return self.total_mb - self._actual_used_override
        return self.total_mb - self.allocated_mb

    def allocate(self, worker_name: str, mb: int):
        self._allocations[worker_name] = mb
        self._actual_used_override = None
        logger.info(f"VRAM allocated: {worker_name}={mb}MB (free: {self.free_mb}MB)")

    def release(self, worker_name: str):
        removed = self._allocations.pop(worker_name, 0)
        self._actual_used_override = None
        if removed:
            logger.info(f"VRAM released: {worker_name}={removed}MB (free: {self.free_mb}MB)")

    def has_space(self, needed_mb: int, *, refresh: bool = False) -> bool:
        if refresh:
            self.reconcile()
        return self.free_mb >= needed_mb

    def reconcile(self) -> bool:
        actual_used = query_gpu_used_mb()
        if actual_used is None:
            return False
        drift = abs(actual_used - self.allocated_mb)
        if drift > 500:
            logger.warning(
                f"VRAM drift detected: bookkeeping={self.allocated_mb}MB, "
                f"actual={actual_used}MB (drift={drift}MB). Correcting."
            )
            self.total_mb = query_gpu_total_mb() or self.total_mb
            self._actual_used_override = actual_used
            return True
        return False
