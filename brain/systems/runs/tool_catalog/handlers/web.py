"""Web orchestration tool handlers."""

from __future__ import annotations

from brain.systems.runs.tool_catalog.handlers.common import *

def _handle_web_search(query: str, provider: str | None = None, limit: int = 5) -> dict:
    from brain.app.web import web_search

    return web_search(
        query,
        provider=provider,
        limit=limit,
        runtime_secret_context=_current_runtime_secret_context(),
    )


def _handle_web_fetch(url: str, extract_mode: str = "markdown", max_chars: int = 12000) -> dict:
    from brain.app.web import web_fetch

    return web_fetch(url, extract_mode=extract_mode, max_chars=max_chars)


# ── Execution Tool Handlers ──────────────────────────────────
# These give agents the ability to read/write files, run commands,
# and search codebases — the hands that match the brain.

__all__ = [name for name in globals() if not name.startswith("__")]
