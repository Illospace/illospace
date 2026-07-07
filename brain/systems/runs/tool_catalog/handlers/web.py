"""Web orchestration tool handlers."""

from __future__ import annotations

from brain.systems.runs.tool_catalog.handlers.common import *

# Substrings that indicate web_search failed only because no search provider
# API key is configured — a config gap, not a transient/unexpected error.
_UNCONFIGURED_MARKERS = ("not configured",)


def _is_unconfigured_search_error(message: str) -> bool:
    lowered = (message or "").lower()
    return any(marker in lowered for marker in _UNCONFIGURED_MARKERS)


async def _handle_web_search(query: str, provider: str | None = None, limit: int = 5) -> dict:
    from brain.app.web import WebResearchError, web_search

    try:
        return await web_search(
            query,
            provider=provider,
            limit=limit,
            runtime_secret_context=_current_runtime_secret_context(),
        )
    except WebResearchError as exc:
        message = str(exc)
        if _is_unconfigured_search_error(message):
            return {
                "error": "web_search is not configured (no search API key set)",
                "unavailable": True,
            }
        return {"error": f"web_search failed: {message}"}
    except Exception as exc:
        logger.debug("web_search failed unexpectedly: %s", exc)
        return {"error": f"web_search failed: {exc}"}


async def _handle_web_fetch(url: str, extract_mode: str = "markdown", max_chars: int = 12000) -> dict:
    from brain.app.web import WebResearchError, web_fetch

    try:
        return await web_fetch(url, extract_mode=extract_mode, max_chars=max_chars)
    except WebResearchError as exc:
        return {"error": f"web_fetch failed: {exc}"}
    except Exception as exc:
        logger.debug("web_fetch failed unexpectedly: %s", exc)
        return {"error": f"web_fetch failed: {exc}"}


# ── Execution Tool Handlers ──────────────────────────────────
# These give agents the ability to read/write files, run commands,
# and search codebases — the hands that match the brain.

__all__ = [name for name in globals() if not name.startswith("__")]
