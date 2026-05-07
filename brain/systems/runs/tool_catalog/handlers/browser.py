"""Browser orchestration tool handlers."""

from __future__ import annotations

from brain.systems.runs.tool_catalog.handlers.common import *


def _browser_state_is_error(state: dict | None) -> bool:
    if not isinstance(state, dict):
        return False
    return bool(state.get("last_error")) or str(state.get("status") or "").lower() == "error"


def _record_browser_preview_artifact(state: dict, *, source_tool: str) -> None:
    if not isinstance(state, dict):
        return
    if _browser_state_is_error(state):
        return
    session_id = str(state.get("id") or state.get("session_id") or "").strip()
    current_url = str(state.get("current_url") or state.get("url") or "").strip()
    if not session_id and not current_url:
        return
    artifact = {
        "type": "browser_preview",
        "source_tool": source_tool,
        "status": state.get("status") or "observed",
        "session_id": session_id,
        "url": current_url,
        "page_title": state.get("page_title") or state.get("title"),
        "viewport_width": state.get("viewport_width"),
        "viewport_height": state.get("viewport_height"),
    }
    _persist_execution_artifacts([artifact])


def _record_browser_saved_artifact(result: dict, *, source_tool: str) -> None:
    if not isinstance(result, dict):
        return
    saved = result.get("artifact")
    if not isinstance(saved, dict):
        return
    kind = str(saved.get("kind") or "").strip().lower()
    if kind not in {"screenshot", "pdf"}:
        return
    state = result.get("state") if isinstance(result.get("state"), dict) else {}
    if _browser_state_is_error(state):
        return
    artifact_type = "browser_screenshot" if kind == "screenshot" else "browser_preview"
    artifact = {
        "type": artifact_type,
        "source_tool": source_tool,
        "status": "saved",
        "session_id": result.get("session_id") or state.get("id") or state.get("session_id"),
        "url": saved.get("url"),
        "filename": saved.get("filename"),
        "size": saved.get("size"),
        "page_url": state.get("current_url"),
        "page_title": state.get("page_title"),
    }
    _persist_execution_artifacts([artifact])


def _record_browser_snapshot_artifact(result: dict, *, source_tool: str) -> None:
    if not isinstance(result, dict):
        return
    frame = result.get("frame") if isinstance(result.get("frame"), dict) else {}
    state = result.get("state") if isinstance(result.get("state"), dict) else {}
    if not frame and not state:
        return
    if _browser_state_is_error(state):
        return
    artifact = {
        "type": "browser_preview",
        "source_tool": source_tool,
        "status": "captured",
        "session_id": result.get("session_id") or state.get("id") or state.get("session_id"),
        "url": state.get("current_url"),
        "page_title": state.get("page_title"),
        "screenshot_sha1": frame.get("sha1"),
        "width": frame.get("width"),
        "height": frame.get("height"),
    }
    _persist_execution_artifacts([artifact])


def _browser_session_context() -> tuple[str, str | None, int | None]:
    idea_id = getattr(_agent_context, "idea_id", None)
    if not idea_id:
        raise ValueError("No idea_id in context — browser tools only work during cortex runs")
    user_id = getattr(_agent_context, "user_id", None)
    run = getattr(_agent_context, "run", None)
    run_id = getattr(run, "run_id", None) if run else None
    return str(idea_id), str(user_id) if user_id else None, run_id


def _run_browser_async(coro):
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    raise RuntimeError(
        "Browser tools are not supported from an already-running event loop in this sync handler context"
    )


def _handle_browser_session_open(
    url: str | None = None,
    viewport_width: int = 1280,
    viewport_height: int = 800,
    storage_mode: str = "ephemeral",
    allow_downloads: bool = False,
    allow_file_uploads: bool = True,
) -> dict:
    from brain.platform.browser import browser_sessions
    from brain.systems.cortex.resources.telemetry import build_browser_resource_summary

    idea_id, user_id, run_id = _browser_session_context()
    runtime = _run_browser_async(browser_sessions.create_or_get_session(
        idea_id=idea_id,
        user_id=user_id,
        run_id=run_id,
        url=url,
        viewport_width=viewport_width,
        viewport_height=viewport_height,
        storage_mode=storage_mode,
        allow_downloads=allow_downloads,
        allow_file_uploads=allow_file_uploads,
    ))
    _run_browser_async(runtime.ensure_streaming(user_id))
    state = runtime.state_payload()
    resource_summary = getattr(runtime, "resource_summary", None)
    if resource_summary is None:
        resource_summary = build_browser_resource_summary(
            mode="cold",
            warm_start_used=False,
            reason="browser session open fallback",
        )
        try:
            runtime.resource_summary = resource_summary
        except Exception:
            pass
    state.setdefault("resource_summary", resource_summary)
    _record_browser_preview_artifact(state, source_tool="browser_session_open")
    return state


def _get_active_browser_session_id(idea_id: str) -> str:
    from brain.platform.browser import browser_sessions

    record = browser_sessions.get_active_session_record(idea_id)
    if record is None:
        raise ValueError("No active browser session for this thought — call browser_session_open first")
    return str(record.id)


def _handle_browser_navigate(url: str) -> dict:
    from brain.platform.browser import browser_sessions

    idea_id, _, _ = _browser_session_context()
    result = _run_browser_async(browser_sessions.command(_get_active_browser_session_id(idea_id), "navigate", {"url": url}))
    _record_browser_preview_artifact(result, source_tool="browser_navigate")
    return result


def _handle_browser_click(selector: str | None = None, x: float | None = None, y: float | None = None) -> dict:
    from brain.platform.browser import browser_sessions

    idea_id, _, _ = _browser_session_context()
    return _run_browser_async(browser_sessions.command(
        _get_active_browser_session_id(idea_id),
        "click",
        {"selector": selector, "x": x, "y": y},
    ))


def _handle_browser_type(text: str, selector: str | None = None, press_enter: bool = False) -> dict:
    from brain.platform.browser import browser_sessions

    idea_id, _, _ = _browser_session_context()
    return _run_browser_async(browser_sessions.command(
        _get_active_browser_session_id(idea_id),
        "type",
        {"text": text, "selector": selector, "press_enter": press_enter},
    ))


def _handle_browser_key(key: str) -> dict:
    from brain.platform.browser import browser_sessions

    idea_id, _, _ = _browser_session_context()
    return _run_browser_async(browser_sessions.command(
        _get_active_browser_session_id(idea_id),
        "key",
        {"key": key},
    ))


def _handle_browser_back() -> dict:
    from brain.platform.browser import browser_sessions

    idea_id, _, _ = _browser_session_context()
    return _run_browser_async(browser_sessions.command(
        _get_active_browser_session_id(idea_id),
        "back",
        {},
    ))


def _handle_browser_forward() -> dict:
    from brain.platform.browser import browser_sessions

    idea_id, _, _ = _browser_session_context()
    return _run_browser_async(browser_sessions.command(
        _get_active_browser_session_id(idea_id),
        "forward",
        {},
    ))


def _handle_browser_new_tab(url: str | None = None) -> dict:
    from brain.platform.browser import browser_sessions

    idea_id, _, _ = _browser_session_context()
    result = _run_browser_async(browser_sessions.command(
        _get_active_browser_session_id(idea_id),
        "new_tab",
        {"url": url},
    ))
    _record_browser_preview_artifact(result, source_tool="browser_new_tab")
    return result


def _handle_browser_switch_tab(index: int) -> dict:
    from brain.platform.browser import browser_sessions

    idea_id, _, _ = _browser_session_context()
    return _run_browser_async(browser_sessions.command(
        _get_active_browser_session_id(idea_id),
        "switch_tab",
        {"index": index},
    ))


def _handle_browser_close_tab(index: int | None = None) -> dict:
    from brain.platform.browser import browser_sessions

    idea_id, _, _ = _browser_session_context()
    payload = {} if index is None else {"index": index}
    return _run_browser_async(browser_sessions.command(
        _get_active_browser_session_id(idea_id),
        "close_tab",
        payload,
    ))


def _handle_browser_list_tabs() -> dict:
    from brain.platform.browser import browser_sessions

    idea_id, _, _ = _browser_session_context()
    return _run_browser_async(browser_sessions.command(
        _get_active_browser_session_id(idea_id),
        "list_tabs",
        {},
    ))


def _handle_browser_wait(selector: str | None = None, wait_until: str = "load", timeout_ms: int = 10000) -> dict:
    from brain.platform.browser import browser_sessions

    idea_id, _, _ = _browser_session_context()
    return _run_browser_async(browser_sessions.command(
        _get_active_browser_session_id(idea_id),
        "wait",
        {"selector": selector, "wait_until": wait_until, "timeout_ms": timeout_ms},
    ))


def _handle_browser_extract(selector: str | None = None, mode: str = "text", max_chars: int = 6000) -> dict:
    from brain.platform.browser import browser_sessions

    idea_id, _, _ = _browser_session_context()
    return _run_browser_async(browser_sessions.command(
        _get_active_browser_session_id(idea_id),
        "extract",
        {"selector": selector, "mode": mode, "max_chars": max_chars},
    ))


def _handle_browser_discover(
    selector: str = "a,button,input,textarea,select,[role='button']",
    max_results: int = 40,
) -> dict:
    from brain.platform.browser import browser_sessions

    idea_id, _, _ = _browser_session_context()
    return _run_browser_async(browser_sessions.command(
        _get_active_browser_session_id(idea_id),
        "discover",
        {"selector": selector, "max_results": max_results},
    ))


def _handle_browser_upload_attachment(selector: str, attachment_url: str) -> dict:
    from brain.platform.browser import browser_sessions

    idea_id, _, _ = _browser_session_context()
    return _run_browser_async(browser_sessions.command(
        _get_active_browser_session_id(idea_id),
        "upload_attachment",
        {"selector": selector, "attachment_url": attachment_url},
    ))


def _handle_browser_snapshot(persist: bool = False, title: str | None = None) -> dict:
    from brain.platform.browser import browser_sessions

    idea_id, _, _ = _browser_session_context()
    result = _run_browser_async(browser_sessions.command(
        _get_active_browser_session_id(idea_id),
        "snapshot",
        {"persist": persist, "title": title},
    ))
    _record_browser_snapshot_artifact(result, source_tool="browser_snapshot")
    return result


def _handle_browser_save_screenshot(full_page: bool = True) -> dict:
    from brain.platform.browser import browser_sessions

    idea_id, _, _ = _browser_session_context()
    result = _run_browser_async(browser_sessions.command(
        _get_active_browser_session_id(idea_id),
        "save_screenshot",
        {"full_page": full_page},
    ))
    _record_browser_saved_artifact(result, source_tool="browser_save_screenshot")
    return result


def _handle_browser_print_pdf(landscape: bool = False) -> dict:
    from brain.platform.browser import browser_sessions

    idea_id, _, _ = _browser_session_context()
    result = _run_browser_async(browser_sessions.command(
        _get_active_browser_session_id(idea_id),
        "print_pdf",
        {"landscape": landscape},
    ))
    _record_browser_saved_artifact(result, source_tool="browser_print_pdf")
    return result


def _handle_browser_close() -> dict:
    from brain.platform.browser import browser_sessions

    idea_id, _, _ = _browser_session_context()
    return _run_browser_async(browser_sessions.command(
        _get_active_browser_session_id(idea_id),
        "close",
        {"reason": "agent_closed"},
    ))

__all__ = [name for name in globals() if not name.startswith("__")]
