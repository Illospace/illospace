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

_BROWSER_DISCOVER_DEFAULT_SELECTOR = "a,button,input,textarea,select,[role='button']"

_BROWSER_ACTION_HELP: dict[str, dict[str, object]] = {
    "open": {
        "required": [],
        "optional": ["url", "viewport_width", "viewport_height", "storage_mode", "allow_downloads", "allow_file_uploads"],
        "effect": "create or reuse the browser session for this thought",
        "aliases": ["session_open"],
    },
    "navigate": {"required": ["url"], "optional": [], "effect": "navigate the active browser tab"},
    "click": {"required": ["selector or x/y"], "optional": [], "effect": "click an element or viewport point"},
    "type": {"required": ["text"], "optional": ["selector", "press_enter"], "effect": "type text into the page"},
    "key": {"required": ["key"], "optional": [], "effect": "press a keyboard key"},
    "back": {"required": [], "optional": [], "effect": "go back in browser history"},
    "forward": {"required": [], "optional": [], "effect": "go forward in browser history"},
    "new_tab": {"required": [], "optional": ["url"], "effect": "open a new tab"},
    "switch_tab": {"required": ["index"], "optional": [], "effect": "switch active tab by index"},
    "close_tab": {"required": [], "optional": ["index"], "effect": "close a tab, defaulting to the current tab"},
    "list_tabs": {"required": [], "optional": [], "effect": "read open tabs"},
    "wait": {"required": [], "optional": ["selector", "wait_until", "timeout_ms"], "effect": "wait for page state or selector"},
    "extract": {"required": [], "optional": ["selector", "mode", "max_chars"], "effect": "read text, HTML, or markdown"},
    "discover": {"required": [], "optional": ["selector", "max_results"], "effect": "find likely interactive elements"},
    "upload_attachment": {
        "required": ["selector", "attachment_url"],
        "optional": [],
        "effect": "upload a Cortex attachment into a file input",
    },
    "snapshot": {"required": [], "optional": ["persist", "title"], "effect": "capture the current viewport"},
    "save_screenshot": {"required": [], "optional": ["full_page"], "effect": "save a PNG screenshot artifact"},
    "print_pdf": {"required": [], "optional": ["landscape"], "effect": "save the current page as a PDF artifact"},
    "close": {"required": [], "optional": [], "effect": "close the active browser session"},
}


def _browser_help(operation: str | None = None) -> dict:
    requested = str(operation or "").strip().lower()
    if requested == "session_open":
        requested = "open"
    if requested:
        detail = _BROWSER_ACTION_HELP.get(requested)
        if detail is None:
            return {
                "tool": "browser",
                "error": f"Unknown browser action: {operation}",
                "available_actions": sorted(_BROWSER_ACTION_HELP),
            }
        return {"tool": "browser", "action": requested, **detail}
    return {
        "tool": "browser",
        "usage": "Call browser with action set to one of these sub-actions. Use operation with action=help for one sub-action.",
        "actions": _BROWSER_ACTION_HELP,
    }


def _missing_browser_args(action: str, *names: str) -> dict:
    return {"error": f"browser action '{action}' requires: {', '.join(names)}"}


def _handle_browser(
    action: str,
    operation: str | None = None,
    url: str | None = None,
    viewport_width: int = 1280,
    viewport_height: int = 800,
    storage_mode: str = "ephemeral",
    allow_downloads: bool = False,
    allow_file_uploads: bool = True,
    selector: str | None = None,
    x: float | None = None,
    y: float | None = None,
    text: str | None = None,
    press_enter: bool = False,
    key: str | None = None,
    index: int | None = None,
    wait_until: str = "load",
    timeout_ms: int = 10000,
    mode: str = "text",
    max_chars: int = 6000,
    max_results: int = 40,
    attachment_url: str | None = None,
    persist: bool = False,
    title: str | None = None,
    full_page: bool = True,
    landscape: bool = False,
) -> dict:
    normalized = str(action or "").strip().lower()
    if normalized == "session_open":
        normalized = "open"
    if normalized == "help":
        return _browser_help(operation)
    if not normalized:
        return {"error": "browser requires: action", "available_actions": sorted(_BROWSER_ACTION_HELP)}

    if normalized == "open":
        return _handle_browser_session_open(
            url=url,
            viewport_width=viewport_width,
            viewport_height=viewport_height,
            storage_mode=storage_mode,
            allow_downloads=allow_downloads,
            allow_file_uploads=allow_file_uploads,
        )
    if normalized == "navigate":
        if not url:
            return _missing_browser_args(normalized, "url")
        return _handle_browser_navigate(url)
    if normalized == "click":
        if not selector and (x is None or y is None):
            return _missing_browser_args(normalized, "selector or x/y")
        return _handle_browser_click(selector=selector, x=x, y=y)
    if normalized == "type":
        if text is None:
            return _missing_browser_args(normalized, "text")
        return _handle_browser_type(text=text, selector=selector, press_enter=press_enter)
    if normalized == "key":
        if not key:
            return _missing_browser_args(normalized, "key")
        return _handle_browser_key(key)
    if normalized == "back":
        return _handle_browser_back()
    if normalized == "forward":
        return _handle_browser_forward()
    if normalized == "new_tab":
        return _handle_browser_new_tab(url=url)
    if normalized == "switch_tab":
        if index is None:
            return _missing_browser_args(normalized, "index")
        return _handle_browser_switch_tab(index=index)
    if normalized == "close_tab":
        return _handle_browser_close_tab(index=index)
    if normalized == "list_tabs":
        return _handle_browser_list_tabs()
    if normalized == "wait":
        return _handle_browser_wait(selector=selector, wait_until=wait_until, timeout_ms=timeout_ms)
    if normalized == "extract":
        return _handle_browser_extract(selector=selector, mode=mode, max_chars=max_chars)
    if normalized == "discover":
        return _handle_browser_discover(
            selector=selector or _BROWSER_DISCOVER_DEFAULT_SELECTOR,
            max_results=max_results,
        )
    if normalized == "upload_attachment":
        if not selector or not attachment_url:
            return _missing_browser_args(normalized, "selector", "attachment_url")
        return _handle_browser_upload_attachment(selector=selector, attachment_url=attachment_url)
    if normalized == "snapshot":
        return _handle_browser_snapshot(persist=persist, title=title)
    if normalized == "save_screenshot":
        return _handle_browser_save_screenshot(full_page=full_page)
    if normalized == "print_pdf":
        return _handle_browser_print_pdf(landscape=landscape)
    if normalized == "close":
        return _handle_browser_close()

    return {
        "error": f"Unknown browser action: {action}",
        "available_actions": sorted(_BROWSER_ACTION_HELP),
    }


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
