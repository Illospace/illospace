"""Canonical codec for headless worker workspace identities."""

from __future__ import annotations


_HEADLESS_WORKER_NAME = "headless-worker"
_THREAD_PREFIX = f"{_HEADLESS_WORKER_NAME}:"
_DIRECTORY_PREFIX = f"{_HEADLESS_WORKER_NAME}-"


def build_headless_worker_thread_id(parent_run_id: int, digest: str) -> str:
    """Build the durable thread identity for one headless worker."""

    return f"{_THREAD_PREFIX}{parent_run_id}:{digest}"


def headless_worker_directory_name(thread_id: str) -> str | None:
    """Derive the workspace directory name from a headless worker thread id."""

    identity = parse_headless_worker_identity(thread_id)
    if identity is None or not thread_id.startswith(_THREAD_PREFIX):
        return None
    parent_run_id, digest = identity
    return f"{_DIRECTORY_PREFIX}{parent_run_id}-{digest}"


def parse_headless_worker_identity(value: str) -> tuple[int, str] | None:
    """Parse a thread id or directory name, or return ``None`` when unrelated."""

    if value.startswith(_THREAD_PREFIX):
        remainder = value.removeprefix(_THREAD_PREFIX)
        separator = ":"
    elif value.startswith(_DIRECTORY_PREFIX):
        remainder = value.removeprefix(_DIRECTORY_PREFIX)
        separator = "-"
    else:
        return None

    parent_run_id, found_separator, digest = remainder.partition(separator)
    if not found_separator or not parent_run_id.isdigit() or not digest:
        return None
    parsed_run_id = int(parent_run_id)
    if parsed_run_id <= 0:
        return None
    return parsed_run_id, digest


def is_headless_worker_directory_candidate(value: str) -> bool:
    """Return whether a name belongs to the GC's existing scan namespace."""

    return value.startswith(_DIRECTORY_PREFIX)


__all__ = [
    "build_headless_worker_thread_id",
    "headless_worker_directory_name",
    "is_headless_worker_directory_candidate",
    "parse_headless_worker_identity",
]
