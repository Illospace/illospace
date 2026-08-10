"""Canonical codec for headless worker workspace identities.

One headless worker has two spellings of the same identity: a thread id
(``headless-worker:{run_id}:{digest}``) and the workspace directory name
derived from it (``headless-worker-{run_id}-{digest}``). Every producer and
consumer goes through this module, so the format is stated once.
"""

from __future__ import annotations


_HEADLESS_WORKER_NAME = "headless-worker"
_THREAD_PREFIX = f"{_HEADLESS_WORKER_NAME}:"
_DIRECTORY_PREFIX = f"{_HEADLESS_WORKER_NAME}-"


def _parse_components(
    value: str, *, prefix: str, separator: str
) -> tuple[int, str] | None:
    """Split one spelling into ``(run_id, digest)``, or ``None`` when unrelated."""

    if not value.startswith(prefix):
        return None
    parent_run_id, found_separator, digest = value.removeprefix(prefix).partition(
        separator
    )
    if not found_separator or not parent_run_id.isdigit() or not digest:
        return None
    parsed_run_id = int(parent_run_id)
    if parsed_run_id <= 0:
        return None
    return parsed_run_id, digest


def build_headless_worker_thread_id(parent_run_id: int, digest: str) -> str:
    """Build the durable thread identity for one headless worker."""

    return f"{_THREAD_PREFIX}{parent_run_id}:{digest}"


def parse_headless_worker_thread_id(value: str) -> tuple[int, str] | None:
    """Parse a thread id, or return ``None`` when it is not one."""

    return _parse_components(value, prefix=_THREAD_PREFIX, separator=":")


def parse_headless_worker_directory_name(value: str) -> tuple[int, str] | None:
    """Parse a workspace directory name, or return ``None`` when it is not one."""

    return _parse_components(value, prefix=_DIRECTORY_PREFIX, separator="-")


def headless_worker_directory_name(thread_id: str) -> str | None:
    """Derive the workspace directory name from a headless worker thread id."""

    identity = parse_headless_worker_thread_id(thread_id)
    if identity is None:
        return None
    parent_run_id, digest = identity
    return f"{_DIRECTORY_PREFIX}{parent_run_id}-{digest}"


def is_headless_worker_directory_candidate(value: str) -> bool:
    """Return whether a name belongs to the GC's existing scan namespace.

    Deliberately broader than :func:`parse_headless_worker_directory_name`: the
    GC counts and refuses malformed names inside the prefix it owns, so it must
    recognise them before it can judge them.
    """

    return value.startswith(_DIRECTORY_PREFIX)


__all__ = [
    "build_headless_worker_thread_id",
    "headless_worker_directory_name",
    "is_headless_worker_directory_candidate",
    "parse_headless_worker_directory_name",
    "parse_headless_worker_thread_id",
]
