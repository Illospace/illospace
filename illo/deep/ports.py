"""Keep the deep lane independent of the sandbox package at import time."""

from typing import Protocol


class SandboxResult(Protocol):
    exit_code: int
    stdout: str
    stderr: str
    truncated: bool
    timed_out: bool
    duration_ms: int


class Sandbox(Protocol):
    async def run(
        self, *, run_id: int, command: str, timeout_s: float = 30, cwd: str | None = None
    ) -> SandboxResult: ...
    async def cleanup(self, run_id: int) -> None: ...


def sandbox_error_text(exc) -> str:
    """Only the sandbox's safe exception may supply text to the model."""
    try:
        from illo.sandbox.client import SandboxError
    except ImportError:
        SandboxError = ()
    if isinstance(exc, SandboxError):
        return str(exc)
    if isinstance(exc, TimeoutError):
        return "The sandbox command reached its time limit."
    return "The sandbox is busy or unavailable. Try again later."


async def refresh_repos(ctx):
    if getattr(ctx, "sandbox", None) is None:
        return
    try:
        from illo.sandbox.repos import ensure_fresh
    except ImportError:
        return
    await ensure_fresh(ctx, max_age_minutes=15)
