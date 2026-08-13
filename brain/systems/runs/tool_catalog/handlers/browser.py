"""Browser orchestration tool handlers."""

from __future__ import annotations

from brain.contracts.thread_references import resolve_idea_thread_reference
from brain.systems.runs.tool_catalog.handlers.common import *


def _browser_state_is_error(state: dict | None) -> bool:
    if not isinstance(state, dict):
        return False
    return bool(state.get("last_error")) or str(state.get("status") or "").lower() == "error"


async def _record_browser_preview_artifact(state: dict, *, source_tool: str) -> None:
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
    await _persist_execution_artifacts_async([artifact])


async def _record_browser_saved_artifact(result: dict, *, source_tool: str) -> None:
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
    download_url = saved.get("download_url") or saved.get("downloadUrl")
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
    if download_url:
        artifact["download_url"] = download_url
    await _persist_execution_artifacts_async([artifact])


async def _record_browser_snapshot_artifact(result: dict, *, source_tool: str) -> None:
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
    await _persist_execution_artifacts_async([artifact])


def _browser_session_context() -> tuple[str, str | None, int | None]:
    thread_reference = getattr(_agent_context, "idea_id", None)
    idea_id = resolve_idea_thread_reference(thread_reference).require_idea_id(
        operation="Browser tools"
    )
    user_id = getattr(_agent_context, "user_id", None)
    run = getattr(_agent_context, "run", None)
    run_id = getattr(run, "run_id", None) if run else None
    return idea_id, str(user_id) if user_id else None, run_id


_TOOL_RESULT_CONTENT_KEY = "_tool_result_content"


def _clean_browser_payload(value):
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            if key == _TOOL_RESULT_CONTENT_KEY:
                continue
            if key == "image_url" and isinstance(item, str) and item.startswith("data:image/"):
                cleaned["image_attached"] = True
                continue
            cleaned[key] = _clean_browser_payload(item)
        return cleaned
    if isinstance(value, list):
        return [_clean_browser_payload(item) for item in value]
    return value


def _browser_image_block(result: dict) -> dict | None:
    frame = result.get("frame") if isinstance(result, dict) else None
    if not isinstance(frame, dict):
        return None
    image_url = frame.get("image_url")
    if not isinstance(image_url, str) or not image_url.startswith("data:image/") or "," not in image_url:
        return None
    header, data = image_url.split(",", 1)
    media_type = header.removeprefix("data:").split(";", 1)[0] or "image/png"
    if not data.strip():
        return None
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": data,
        },
    }


def _browser_result_for_model(result: dict, *, source_tool: str) -> dict:
    cleaned = _clean_browser_payload(result)
    if not isinstance(cleaned, dict):
        cleaned = {"result": cleaned}
    image_block = _browser_image_block(result)
    if image_block:
        cleaned["observation"] = (
            "Screenshot attached. Use it to choose viewport coordinates and verify visible state; "
            "use discover/extract only when DOM/text detail is needed."
        )
    cleaned["source_tool"] = source_tool
    blocks = [{"type": "text", "text": json.dumps(cleaned, default=str, sort_keys=True)}]
    if image_block:
        blocks.append(image_block)
    cleaned[_TOOL_RESULT_CONTENT_KEY] = blocks
    return cleaned


async def _observe_browser_session(session_id: str, action_result: dict, *, source_tool: str) -> dict:
    from brain.platform.browser import browser_sessions

    try:
        observed = await browser_sessions.command(session_id, "observe", {})
    except Exception as exc:
        fallback = dict(action_result) if isinstance(action_result, dict) else {"result": action_result}
        fallback["observation_error"] = str(exc)
        return fallback
    observed["action_result"] = _clean_browser_payload(action_result)
    return _browser_result_for_model(observed, source_tool=source_tool)


_BROWSER_DISCOVER_DEFAULT_SELECTOR = "a,button,input,textarea,select,[role='button']"

_BROWSER_ACTION_HELP: dict[str, dict[str, object]] = {
    "open": {
        "required": [],
        "optional": ["url", "viewport_width", "viewport_height", "storage_mode", "allow_downloads", "allow_file_uploads"],
        "effect": "create or reuse the browser session for this thought; set allow_downloads=true before tasks that may download files",
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
    "observe": {"required": [], "optional": [], "effect": "capture the current viewport as a model-visible screenshot"},
    "extract": {"required": [], "optional": ["selector", "mode", "max_chars"], "effect": "read text, HTML, or markdown"},
    "discover": {"required": [], "optional": ["selector", "max_results"], "effect": "find likely interactive elements"},
    "upload_attachment": {
        "required": ["selector", "attachment_url"],
        "optional": [],
        "effect": "upload a Cortex attachment into a file input",
    },
    "snapshot": {"required": [], "optional": ["persist", "title"], "effect": "capture the current viewport and optionally persist it into the thought"},
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


async def _handle_browser(
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
    try:
        return await _dispatch_browser_action(
            action,
            operation=operation,
            url=url,
            viewport_width=viewport_width,
            viewport_height=viewport_height,
            storage_mode=storage_mode,
            allow_downloads=allow_downloads,
            allow_file_uploads=allow_file_uploads,
            selector=selector,
            x=x,
            y=y,
            text=text,
            press_enter=press_enter,
            key=key,
            index=index,
            wait_until=wait_until,
            timeout_ms=timeout_ms,
            mode=mode,
            max_chars=max_chars,
            max_results=max_results,
            attachment_url=attachment_url,
            persist=persist,
            title=title,
            full_page=full_page,
            landscape=landscape,
        )
    except Exception as exc:
        return _tool_failure_payload("browser", exc)


async def _dispatch_browser_action(
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
        return await _handle_browser_session_open(
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
        return await _handle_browser_navigate(url)
    if normalized == "click":
        if not selector and (x is None or y is None):
            return _missing_browser_args(normalized, "selector or x/y")
        return await _handle_browser_click(selector=selector, x=x, y=y)
    if normalized == "type":
        if text is None:
            return _missing_browser_args(normalized, "text")
        return await _handle_browser_type(text=text, selector=selector, press_enter=press_enter)
    if normalized == "key":
        if not key:
            return _missing_browser_args(normalized, "key")
        return await _handle_browser_key(key)
    if normalized == "back":
        return await _handle_browser_back()
    if normalized == "forward":
        return await _handle_browser_forward()
    if normalized == "new_tab":
        return await _handle_browser_new_tab(url=url)
    if normalized == "switch_tab":
        if index is None:
            return _missing_browser_args(normalized, "index")
        return await _handle_browser_switch_tab(index=index)
    if normalized == "close_tab":
        return await _handle_browser_close_tab(index=index)
    if normalized == "list_tabs":
        return await _handle_browser_list_tabs()
    if normalized == "wait":
        return await _handle_browser_wait(selector=selector, wait_until=wait_until, timeout_ms=timeout_ms)
    if normalized == "observe":
        return await _handle_browser_observe()
    if normalized == "extract":
        return await _handle_browser_extract(selector=selector, mode=mode, max_chars=max_chars)
    if normalized == "discover":
        return await _handle_browser_discover(
            selector=selector or _BROWSER_DISCOVER_DEFAULT_SELECTOR,
            max_results=max_results,
        )
    if normalized == "upload_attachment":
        if not selector or not attachment_url:
            return _missing_browser_args(normalized, "selector", "attachment_url")
        return await _handle_browser_upload_attachment(selector=selector, attachment_url=attachment_url)
    if normalized == "snapshot":
        return await _handle_browser_snapshot(persist=persist, title=title)
    if normalized == "save_screenshot":
        return await _handle_browser_save_screenshot(full_page=full_page)
    if normalized == "print_pdf":
        return await _handle_browser_print_pdf(landscape=landscape)
    if normalized == "close":
        return await _handle_browser_close()

    return {
        "error": f"Unknown browser action: {action}",
        "available_actions": sorted(_BROWSER_ACTION_HELP),
    }


async def _handle_browser_session_open(
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
    runtime = await browser_sessions.create_or_get_session(
        idea_id=idea_id,
        user_id=user_id,
        run_id=run_id,
        url=url,
        viewport_width=viewport_width,
        viewport_height=viewport_height,
        storage_mode=storage_mode,
        allow_downloads=allow_downloads,
        allow_file_uploads=allow_file_uploads,
    )
    await runtime.ensure_streaming(user_id)
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
    await _record_browser_preview_artifact(state, source_tool="browser_session_open")
    try:
        observed = await runtime.observe()
        observed["action_result"] = _clean_browser_payload(state)
        return _browser_result_for_model(observed, source_tool="browser_session_open")
    except Exception as exc:
        state["observation_error"] = str(exc)
        return state


async def _get_active_browser_session_id(idea_id: str) -> str:
    from brain.platform.browser import browser_sessions

    record = await browser_sessions.get_active_session_record_async(idea_id)
    if record is None:
        raise ValueError("No active browser session for this thought — call browser_session_open first")
    return str(record.id)


async def _handle_browser_navigate(url: str) -> dict:
    from brain.platform.browser import browser_sessions

    idea_id, _, _ = _browser_session_context()
    session_id = await _get_active_browser_session_id(idea_id)
    result = await browser_sessions.command(session_id, "navigate", {"url": url})
    await _record_browser_preview_artifact(result, source_tool="browser_navigate")
    return await _observe_browser_session(session_id, result, source_tool="browser_navigate")


async def _handle_browser_click(selector: str | None = None, x: float | None = None, y: float | None = None) -> dict:
    from brain.platform.browser import browser_sessions

    idea_id, _, _ = _browser_session_context()
    session_id = await _get_active_browser_session_id(idea_id)
    result = await browser_sessions.command(
        session_id,
        "click",
        {"selector": selector, "x": x, "y": y},
    )
    return await _observe_browser_session(session_id, result, source_tool="browser_click")


async def _handle_browser_type(text: str, selector: str | None = None, press_enter: bool = False) -> dict:
    from brain.platform.browser import browser_sessions

    idea_id, _, _ = _browser_session_context()
    session_id = await _get_active_browser_session_id(idea_id)
    result = await browser_sessions.command(
        session_id,
        "type",
        {"text": text, "selector": selector, "press_enter": press_enter},
    )
    return await _observe_browser_session(session_id, result, source_tool="browser_type")


async def _handle_browser_key(key: str) -> dict:
    from brain.platform.browser import browser_sessions

    idea_id, _, _ = _browser_session_context()
    session_id = await _get_active_browser_session_id(idea_id)
    result = await browser_sessions.command(
        session_id,
        "key",
        {"key": key},
    )
    return await _observe_browser_session(session_id, result, source_tool="browser_key")


async def _handle_browser_back() -> dict:
    from brain.platform.browser import browser_sessions

    idea_id, _, _ = _browser_session_context()
    session_id = await _get_active_browser_session_id(idea_id)
    result = await browser_sessions.command(
        session_id,
        "back",
        {},
    )
    return await _observe_browser_session(session_id, result, source_tool="browser_back")


async def _handle_browser_forward() -> dict:
    from brain.platform.browser import browser_sessions

    idea_id, _, _ = _browser_session_context()
    session_id = await _get_active_browser_session_id(idea_id)
    result = await browser_sessions.command(
        session_id,
        "forward",
        {},
    )
    return await _observe_browser_session(session_id, result, source_tool="browser_forward")


async def _handle_browser_new_tab(url: str | None = None) -> dict:
    from brain.platform.browser import browser_sessions

    idea_id, _, _ = _browser_session_context()
    session_id = await _get_active_browser_session_id(idea_id)
    result = await browser_sessions.command(
        session_id,
        "new_tab",
        {"url": url},
    )
    await _record_browser_preview_artifact(result, source_tool="browser_new_tab")
    return await _observe_browser_session(session_id, result, source_tool="browser_new_tab")


async def _handle_browser_switch_tab(index: int) -> dict:
    from brain.platform.browser import browser_sessions

    idea_id, _, _ = _browser_session_context()
    session_id = await _get_active_browser_session_id(idea_id)
    result = await browser_sessions.command(
        session_id,
        "switch_tab",
        {"index": index},
    )
    return await _observe_browser_session(session_id, result, source_tool="browser_switch_tab")


async def _handle_browser_close_tab(index: int | None = None) -> dict:
    from brain.platform.browser import browser_sessions

    idea_id, _, _ = _browser_session_context()
    payload = {} if index is None else {"index": index}
    session_id = await _get_active_browser_session_id(idea_id)
    result = await browser_sessions.command(
        session_id,
        "close_tab",
        payload,
    )
    return await _observe_browser_session(session_id, result, source_tool="browser_close_tab")


async def _handle_browser_list_tabs() -> dict:
    from brain.platform.browser import browser_sessions

    idea_id, _, _ = _browser_session_context()
    return await browser_sessions.command(
        await _get_active_browser_session_id(idea_id),
        "list_tabs",
        {},
    )


async def _handle_browser_wait(selector: str | None = None, wait_until: str = "load", timeout_ms: int = 10000) -> dict:
    from brain.platform.browser import browser_sessions

    idea_id, _, _ = _browser_session_context()
    session_id = await _get_active_browser_session_id(idea_id)
    result = await browser_sessions.command(
        session_id,
        "wait",
        {"selector": selector, "wait_until": wait_until, "timeout_ms": timeout_ms},
    )
    return await _observe_browser_session(session_id, result, source_tool="browser_wait")


async def _handle_browser_observe() -> dict:
    from brain.platform.browser import browser_sessions

    idea_id, _, _ = _browser_session_context()
    session_id = await _get_active_browser_session_id(idea_id)
    result = await browser_sessions.command(session_id, "observe", {})
    return _browser_result_for_model(result, source_tool="browser_observe")


async def _handle_browser_extract(selector: str | None = None, mode: str = "text", max_chars: int = 6000) -> dict:
    from brain.platform.browser import browser_sessions

    idea_id, _, _ = _browser_session_context()
    return await browser_sessions.command(
        await _get_active_browser_session_id(idea_id),
        "extract",
        {"selector": selector, "mode": mode, "max_chars": max_chars},
    )


async def _handle_browser_discover(
    selector: str = "a,button,input,textarea,select,[role='button']",
    max_results: int = 40,
) -> dict:
    from brain.platform.browser import browser_sessions

    idea_id, _, _ = _browser_session_context()
    return await browser_sessions.command(
        await _get_active_browser_session_id(idea_id),
        "discover",
        {"selector": selector, "max_results": max_results},
    )


async def _handle_browser_upload_attachment(selector: str, attachment_url: str) -> dict:
    from brain.platform.browser import browser_sessions

    idea_id, _, _ = _browser_session_context()
    session_id = await _get_active_browser_session_id(idea_id)
    result = await browser_sessions.command(
        session_id,
        "upload_attachment",
        {"selector": selector, "attachment_url": attachment_url},
    )
    return await _observe_browser_session(session_id, result, source_tool="browser_upload_attachment")


async def _handle_browser_snapshot(persist: bool = False, title: str | None = None) -> dict:
    from brain.platform.browser import browser_sessions

    idea_id, _, _ = _browser_session_context()
    session_id = await _get_active_browser_session_id(idea_id)
    result = await browser_sessions.command(
        session_id,
        "snapshot",
        {"persist": persist, "title": title},
    )
    await _record_browser_snapshot_artifact(result, source_tool="browser_snapshot")
    return _browser_result_for_model(result, source_tool="browser_snapshot")


async def _handle_browser_save_screenshot(full_page: bool = True) -> dict:
    from brain.platform.browser import browser_sessions

    idea_id, _, _ = _browser_session_context()
    result = await browser_sessions.command(
        await _get_active_browser_session_id(idea_id),
        "save_screenshot",
        {"full_page": full_page},
    )
    await _record_browser_saved_artifact(result, source_tool="browser_save_screenshot")
    return result


async def _handle_browser_print_pdf(landscape: bool = False) -> dict:
    from brain.platform.browser import browser_sessions

    idea_id, _, _ = _browser_session_context()
    result = await browser_sessions.command(
        await _get_active_browser_session_id(idea_id),
        "print_pdf",
        {"landscape": landscape},
    )
    await _record_browser_saved_artifact(result, source_tool="browser_print_pdf")
    return result


async def _handle_browser_close() -> dict:
    from brain.platform.browser import browser_sessions

    idea_id, _, _ = _browser_session_context()
    return await browser_sessions.command(
        await _get_active_browser_session_id(idea_id),
        "close",
        {"reason": "agent_closed"},
    )

__all__ = [name for name in globals() if not name.startswith("__")]
