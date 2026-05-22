"""Serialized Project root mutation helpers."""
from __future__ import annotations

from collections.abc import Iterable
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Iterator
import fcntl

from brain.systems.cortex.project_context.drafts import PROJECT_HISTORY_DIR


LOCK_FILE = "root.lock"


def _lock_path(root: Path) -> Path:
    path = Path(root).expanduser()
    base = path if not path.exists() or path.is_dir() else path.parent
    return base / PROJECT_HISTORY_DIR / LOCK_FILE


@contextmanager
def project_root_lock(root: Path) -> Iterator[None]:
    """Hold an exclusive lock for one local Project root."""

    path = _lock_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def project_root_locks(roots: Iterable[Path]) -> Iterator[None]:
    """Hold exclusive locks for local Project roots in deterministic order."""

    ordered = sorted({str(Path(root).expanduser()) for root in roots})
    with ExitStack() as stack:
        for root in ordered:
            stack.enter_context(project_root_lock(Path(root)))
        yield


__all__ = ["project_root_lock", "project_root_locks"]
