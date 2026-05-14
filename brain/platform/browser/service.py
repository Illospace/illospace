"""Server-side browser session runtime for Cortex thoughts."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import json
import logging
import os
import platform
import re
import shutil
import socket
import sys
import tempfile
from pathlib import Path
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import unquote, urlparse

import httpx
from sqlalchemy import select

from brain.kernel.common.time import utcnow as _shared_utcnow

from brain.systems.cortex.events import publish_safe
from brain.systems.cortex.resources.telemetry import build_browser_resource_summary
from brain.systems.cortex.upload_preview import public_static_upload_url, static_upload_url_for
from brain.platform.async_io import copy_file, ensure_dir, glob_paths, iter_dir, path_exists, path_is_file, path_stat
from brain.platform.db.models.browser import BrowserSession
from brain.platform.db.models.idea import Idea, VisualBlock
from brain.platform.db.repositories.unit_of_work import UnitOfWork
from brain.app.web.research import _assert_safe_url

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
BRAIN_ROOT = PROJECT_ROOT / "brain"
WORKSPACE_BROWSER_STATE_DIR = os.environ.get(
    "ILLO_BROWSER_STATE_DIR",
    str(BRAIN_ROOT / ".browser-state"),
)
UPLOAD_DIR = BRAIN_ROOT / "uploads"
HARNESS_RESULT_MARKER = "__ILLO_BROWSER_HARNESS_RESULT__"


def _utcnow() -> datetime:
    return _shared_utcnow()


def _normalize_url(url: str) -> str:
    value = (url or "").strip()
    if not value:
        raise ValueError("URL is required")
    if "://" not in value:
        value = f"https://{value}"
    return value


def _browser_harness_ipc_dir(session_id: str) -> Path:
    root = os.environ.get("ILLO_BROWSER_IPC_ROOT")
    ipc_root = Path(root) if root else Path("/tmp" if sys.platform != "win32" else tempfile.gettempdir())
    return ipc_root / "illo-bh" / _browser_harness_short_name(session_id, fallback="session")


def _browser_harness_short_name(value: str | None, *, fallback: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]+", "-", value or fallback).strip("-") or fallback
    digest = hashlib.sha1((value or safe).encode("utf-8")).hexdigest()[:12]
    return f"{safe[:8]}-{digest}"


def _browser_harness_state_base() -> Path:
    root = os.environ.get("ILLO_BROWSER_HARNESS_ROOT")
    if root:
        return Path(root)
    if sys.platform.startswith("linux"):
        return Path(tempfile.gettempdir()) / "illo-bh"
    return Path(WORKSPACE_BROWSER_STATE_DIR).parent / ".browser-harness"


async def _record_browser_harness_tool_call(
    *,
    run_id: int | None,
    idea_id: str,
    session_id: str | None,
    url: str | None,
    action: str,
    status: str,
    detail: str | None = None,
) -> None:
    if run_id is None:
        return
    try:
        from brain.systems.runs.events import async_record_tool_call

        args = {
            "action": action,
            "provider": "browser-harness",
            "session_id": session_id,
            "url": url,
        }
        result = {
            "status": status,
            "provider": "browser-harness",
            "session_id": session_id,
            "url": url,
        }
        if detail:
            result["detail"] = detail
        await async_record_tool_call(
            int(run_id),
            str(idea_id),
            "browser_harness",
            args,
            json.dumps(result, default=str),
            source="browser_harness",
        )
    except Exception:
        logger.debug("Failed to record Browser Harness tool trace", exc_info=True)


class BrowserCapabilityError(RuntimeError):
    """Raised when browser automation is unavailable."""


@dataclass
class BrowserFrame:
    image_url: str
    sha1: str
    width: int
    height: int
    captured_at: str


@dataclass
class BrowserAction:
    at: str
    action: str
    detail: str | None = None


@dataclass
class BrowserDownload:
    at: str
    filename: str
    url: str
    download_url: str | None = None
    size: int | None = None


@dataclass
class BrowserArtifact:
    at: str
    kind: str
    filename: str
    url: str
    download_url: str | None = None
    size: int | None = None


@dataclass
class BrowserConsoleEntry:
    at: str
    level: str
    text: str
    location: str | None = None


@dataclass
class BrowserRequestFailure:
    at: str
    method: str
    url: str
    error_text: str | None = None
    resource_type: str | None = None


class BrowserSessionRuntime:
    """Browser Harness-backed runtime for a persisted Cortex browser session."""

    def __init__(self, service: "BrowserSessionService", record: BrowserSession):
        self._record = record
        self.service = service
        self.session_id = str(record.id)
        self.idea_id = str(record.idea_id)
        self.org_id = str(getattr(record, "_idea_org_id", "") or getattr(record, "org_id", "") or "") or None
        self.user_id = str(record.user_id) if record.user_id else None
        self.run_id = record.run_id
        self.viewport_width = int(record.viewport_width or 1280)
        self.viewport_height = int(record.viewport_height or 800)
        self.storage_mode = getattr(record, "storage_mode", "ephemeral") or "ephemeral"
        self.allow_downloads = bool(getattr(record, "allow_downloads", False))
        self.allow_file_uploads = bool(getattr(record, "allow_file_uploads", True))
        self.status = record.status or "starting"
        self.current_url = record.current_url or None
        self.page_title = record.page_title or None
        self.last_error = record.last_error or None
        self._action_lock = asyncio.Lock()
        self._watchers: set[str] = set()
        self._stream_task: asyncio.Task | None = None
        self._idle_close_task: asyncio.Task | None = None
        self._dirty = asyncio.Event()
        self._closed = False
        self._last_frame_sha1: str | None = None
        self._force_next_stream_frame = False
        self._actions: list[BrowserAction] = []
        self._downloads: list[BrowserDownload] = []
        self._artifacts: list[BrowserArtifact] = []
        self._console_messages: list[BrowserConsoleEntry] = []
        self._request_failures: list[BrowserRequestFailure] = []
        self._download_dir = UPLOAD_DIR / "browser-downloads" / self.session_id
        self._artifact_dir = UPLOAD_DIR / "browser-artifacts" / self.session_id
        short_session = _browser_harness_short_name(self.session_id, fallback="session")
        short_idea = _browser_harness_short_name(self.idea_id, fallback="idea")
        harness_base = _browser_harness_state_base()
        self._harness_name = f"illo-{short_session}"
        self._harness_root = harness_base / "s" / short_session
        self._harness_ipc_dir = _browser_harness_ipc_dir(self.session_id)
        self._harness_workspace = self._harness_root / "w"
        self._harness_download_dir = self._harness_root / "d"
        self._harness_chrome_dir = (
            harness_base / "p" / short_idea
            if self.storage_mode == "idea"
            else self._harness_root / "c"
        )
        self._harness_bin = self._find_browser_harness_executable()
        self._chrome_process: asyncio.subprocess.Process | None = None
        self._chrome_executable: str | None = None
        self._cdp_port: int | None = None
        self._cdp_url: str | None = None
        self._tabs: list[dict[str, Any]] = []
        self._current_tab_id: str | None = None
        self._known_download_paths: set[str] = set()
        self.resource_summary = build_browser_resource_summary(
            mode="cold",
            warm_start_used=False,
            reason="browser harness dedicated Chrome session",
            browser_version="browser-harness+chrome-cdp",
            context_mode=self.storage_mode,
            profile_key=f"idea:{self.idea_id}:user:{self.user_id or 'anon'}",
        )

    def _find_browser_harness_executable(self) -> str | None:
        candidates = [
            os.environ.get("ILLO_BROWSER_HARNESS_BIN"),
            str(Path(sys.executable).with_name("browser-harness")),
            shutil.which("browser-harness"),
        ]
        for candidate in candidates:
            if candidate and Path(candidate).exists():
                return str(candidate)
        return None

    async def _configure_downloads(self) -> None:
        if not self.allow_downloads:
            return
        await ensure_dir(self._harness_download_dir)
        self._known_download_paths = await self._download_snapshot()
        await self._run_json(f"""
from pathlib import Path
DL = {json.dumps(str(self._harness_download_dir))}
Path(DL).mkdir(parents=True, exist_ok=True)
cdp("Browser.setDownloadBehavior", behavior="allow", downloadPath=DL, eventsEnabled=True)
result = page_info()
""")

    async def _download_snapshot(self) -> set[str]:
        if not await path_exists(self._harness_download_dir):
            return set()
        paths = await iter_dir(self._harness_download_dir)
        snapshot: set[str] = set()
        for path in paths:
            if await path_is_file(path) and not path.name.endswith(".crdownload"):
                snapshot.add(str(path.resolve()))
        return snapshot

    async def _capture_new_downloads(self, *, wait_for_completion: bool = False) -> None:
        if not self.allow_downloads:
            return
        await ensure_dir(self._harness_download_dir)
        deadline = asyncio.get_running_loop().time() + (5.0 if wait_for_completion else 0.3)
        current = await self._download_snapshot()
        while wait_for_completion and asyncio.get_running_loop().time() < deadline:
            incomplete = await glob_paths(self._harness_download_dir, "*.crdownload")
            current = await self._download_snapshot()
            if current - self._known_download_paths and not incomplete:
                break
            await asyncio.sleep(0.2)
        for raw_path in sorted(current - self._known_download_paths):
            source = Path(raw_path)
            if not await path_exists(source):
                continue
            await ensure_dir(self._download_dir)
            filename = self._sanitize_download_filename(source.name)
            target = self._allocate_unique_path(self._download_dir, filename)
            try:
                await copy_file(source, target)
            except FileNotFoundError:
                continue
            public_url = static_upload_url_for("browser-downloads", self.session_id, target.name)
            self._record_action("download", target.name)
            stat = await path_stat(target)
            self._record_download(
                filename=target.name,
                url=public_url,
                download_url=public_static_upload_url(public_url),
                size=stat.st_size,
            )
            self._emit_state("download")
        self._known_download_paths = current

    def _update_tabs_from_list(self, tabs: list[dict[str, Any]] | None) -> None:
        normalized: list[dict[str, Any]] = []
        for idx, tab in enumerate(tabs or []):
            title = self._clean_harness_title(tab.get("title"))
            normalized.append({
                "index": idx,
                "targetId": tab.get("targetId"),
                "url": tab.get("url"),
                "title": title,
                "active": False,
            })
        if normalized:
            active_index = 0
            for idx, tab in enumerate(normalized):
                if self._current_tab_id and tab.get("targetId") == self._current_tab_id:
                    active_index = idx
                    break
                if self.current_url and tab.get("url") == self.current_url:
                    active_index = idx
            for idx, tab in enumerate(normalized):
                tab["active"] = idx == active_index
            self._current_tab_id = normalized[active_index].get("targetId") or self._current_tab_id
        self._tabs = normalized

    def _update_single_tab_from_page_info(self) -> None:
        if not self.current_url and not self.page_title:
            return
        if self._tabs:
            idx = self._current_tab_index()
            self._tabs[idx] = {
                **self._tabs[idx],
                "url": self.current_url,
                "title": self.page_title,
                "active": True,
            }
            for other_idx, tab in enumerate(self._tabs):
                tab["active"] = other_idx == idx
            return
        self._tabs = [{
            "index": 0,
            "targetId": self._current_tab_id,
            "url": self.current_url,
            "title": self.page_title,
            "active": True,
        }]

    def _clean_harness_title(self, value: Any) -> str | None:
        if value is None:
            return None
        return str(value).removeprefix("\U0001F7E2 ").strip() or None

    def _current_tab_index(self) -> int:
        for idx, tab in enumerate(self._tabs):
            if tab.get("active"):
                return idx
        return 0

    def _tab_summaries_sync(self) -> list[dict[str, Any]]:
        return [
            {
                "index": idx,
                "url": tab.get("url"),
                "title": tab.get("title"),
                "active": bool(tab.get("active")),
            }
            for idx, tab in enumerate(self._tabs)
        ]

    async def ensure_streaming(self, user_id: str | None = None, *, force_frame: bool = False) -> None:
        await self.start()
        new_watcher = bool(user_id and str(user_id) not in self._watchers)
        if user_id:
            self._watchers.add(str(user_id))
        if force_frame or new_watcher:
            # A session open can capture the first frame before the websocket
            # subscriber is attached. Force the next streamed frame so SHA
            # dedupe cannot leave a new panel with state but no image.
            self._force_next_stream_frame = True
        self._cancel_idle_close()
        if self._stream_task and not self._stream_task.done():
            self._dirty.set()
            return
        self._stream_task = asyncio.get_running_loop().create_task(
            self._stream_loop(),
            name=f"browser-stream-{self.session_id}",
        )
        self._dirty.set()

    async def unsubscribe(self, user_id: str | None = None) -> None:
        if user_id:
            self._watchers.discard(str(user_id))
        if self._watchers:
            return
        if self._stream_task and not self._stream_task.done():
            self._stream_task.cancel()
            try:
                await self._stream_task
            except asyncio.CancelledError:
                pass
        self._schedule_idle_close()

    async def start(self) -> None:
        if self.status == "ready" and self._cdp_url and self._chrome_process and self._chrome_process.returncode is None:
            return
        if not self._harness_bin:
            raise BrowserCapabilityError(
                "Browser Harness is not installed or not on PATH. "
                "Install https://github.com/browser-use/browser-harness and ensure `browser-harness` is available."
            )
        await ensure_dir(self._harness_root)
        await ensure_dir(self._harness_ipc_dir)
        await ensure_dir(self._harness_workspace)
        await ensure_dir(self._harness_download_dir)
        await self._ensure_chrome_process()
        info = await self._page_info()
        await self._apply_page_info(info, status="ready")
        await self._configure_downloads()
        self._emit_state("ready")

    async def navigate(self, url: str) -> dict[str, Any]:
        async with self._action_lock:
            await self.start()
            target = _assert_safe_url(url)
            self._record_action("navigate", target)
            self.status = "navigating"
            await self._persist_state(status="navigating", current_url=target, last_error=None)
            self._emit_state("navigating")
            info = await self._run_json(
                f"""
goto_url({json.dumps(target)})
wait_for_load({self.service.nav_timeout_ms / 1000:.3f})
result = page_info()
"""
            )
            await self._apply_page_info(info, status="ready")
            self._dirty.set()
            return self.state_payload()

    async def click(self, *, selector: str | None = None, x: float | None = None, y: float | None = None) -> dict[str, Any]:
        async with self._action_lock:
            await self.start()
            if selector:
                self._record_action("click", selector)
                code = f"""
selector = {json.dumps(selector)}
rect = js('''
const el = document.querySelector(''' + json.dumps(selector) + ''');
if (!el) throw new Error("No element for selector: " + ''' + json.dumps(selector) + ''');
el.scrollIntoView({{block: "center", inline: "center"}});
const r = el.getBoundingClientRect();
return {{x: r.left + r.width / 2, y: r.top + r.height / 2}};
''')
click_at_xy(rect["x"], rect["y"])
wait(0.15)
result = page_info()
"""
            elif x is not None and y is not None:
                self._record_action("click", f"{int(x)},{int(y)}")
                code = f"""
click_at_xy({float(x)}, {float(y)})
wait(0.15)
result = page_info()
"""
            else:
                raise ValueError("click requires a selector or x/y coordinates")
            info = await self._run_json(code)
            await self._apply_page_info(info)
            await self._capture_new_downloads(wait_for_completion=True)
            self._dirty.set()
            return self.state_payload()

    async def type_text(self, text: str, selector: str | None = None, press_enter: bool = False) -> dict[str, Any]:
        async with self._action_lock:
            await self.start()
            self._record_action("type", selector or f"{len(text)} chars")
            if selector:
                focus_code = f"""
selector = {json.dumps(selector)}
rect = js('''
const el = document.querySelector(''' + json.dumps(selector) + ''');
if (!el) throw new Error("No element for selector: " + ''' + json.dumps(selector) + ''');
el.scrollIntoView({{block: "center", inline: "center"}});
el.focus();
if ("value" in el) el.value = "";
const r = el.getBoundingClientRect();
return {{x: r.left + r.width / 2, y: r.top + r.height / 2}};
''')
click_at_xy(rect["x"], rect["y"])
type_text({json.dumps(text)})
"""
            else:
                focus_code = f"type_text({json.dumps(text)})\n"
            enter_code = "press_key('Enter')\n" if press_enter else ""
            info = await self._run_json(f"""
{focus_code}
{enter_code}
wait(0.15)
result = page_info()
""")
            await self._apply_page_info(info)
            self._dirty.set()
            return self.state_payload()

    async def press_key(self, key: str) -> dict[str, Any]:
        async with self._action_lock:
            await self.start()
            self._record_action("key", key)
            info = await self._run_json(f"""
press_key({json.dumps(key)})
wait(0.15)
result = page_info()
""")
            await self._apply_page_info(info)
            await self._capture_new_downloads(wait_for_completion=True)
            self._dirty.set()
            return self.state_payload()

    async def scroll(self, delta_x: float = 0, delta_y: float = 0) -> dict[str, Any]:
        async with self._action_lock:
            await self.start()
            self._record_action("scroll", f"{int(delta_x)},{int(delta_y)}")
            info = await self._run_json(f"""
info = page_info()
scroll(info["w"] / 2, info["h"] / 2, dy={float(delta_y)}, dx={float(delta_x)})
wait(0.15)
result = page_info()
""")
            await self._apply_page_info(info)
            self._dirty.set()
            return self.state_payload()

    async def refresh(self) -> dict[str, Any]:
        async with self._action_lock:
            await self.start()
            self._record_action("refresh")
            info = await self._run_json(f"""
cdp("Page.reload")
wait_for_load({self.service.nav_timeout_ms / 1000:.3f})
result = page_info()
""")
            await self._apply_page_info(info)
            self._dirty.set()
            return self.state_payload()

    async def go_back(self) -> dict[str, Any]:
        return await self._history_action("back", -1)

    async def go_forward(self) -> dict[str, Any]:
        return await self._history_action("forward", 1)

    async def _history_action(self, action: str, offset: int) -> dict[str, Any]:
        async with self._action_lock:
            await self.start()
            self._record_action(action)
            info = await self._run_json(f"""
history = cdp("Page.getNavigationHistory")
idx = history.get("currentIndex", 0) + ({offset})
entries = history.get("entries", [])
if 0 <= idx < len(entries):
    cdp("Page.navigateToHistoryEntry", entryId=entries[idx]["id"])
    wait_for_load({self.service.nav_timeout_ms / 1000:.3f})
result = page_info()
""")
            await self._apply_page_info(info)
            self._dirty.set()
            return self.state_payload()

    async def new_tab(self, url: str | None = None) -> dict[str, Any]:
        async with self._action_lock:
            await self.start()
            self._record_action("new_tab", url)
            target = _assert_safe_url(url) if url else "about:blank"
            info = await self._run_json(f"""
new_tab({json.dumps(target)})
wait_for_load({self.service.nav_timeout_ms / 1000:.3f})
result = page_info()
""")
            await self._apply_page_info(info)
            await self.list_tabs()
            self._dirty.set()
            return self.state_payload()

    async def list_tabs(self) -> dict[str, Any]:
        await self.start()
        tabs = await self._run_json("result = list_tabs(include_chrome=False)")
        self._update_tabs_from_list(tabs or [])
        return {
            "session_id": self.session_id,
            "current_index": self._current_tab_index(),
            "tabs": self._tab_summaries_sync(),
        }

    async def switch_tab(self, index: int) -> dict[str, Any]:
        async with self._action_lock:
            await self.start()
            self._record_action("switch_tab", str(index))
            info = await self._run_json(f"""
tabs = list_tabs(include_chrome=False)
if {int(index)} < 0 or {int(index)} >= len(tabs):
    raise RuntimeError("Invalid tab index: {int(index)}")
switch_tab(tabs[{int(index)}]["targetId"])
result = page_info()
""")
            await self._apply_page_info(info)
            await self.list_tabs()
            self._dirty.set()
            return self.state_payload()

    async def close_tab(self, index: int | None = None) -> dict[str, Any]:
        async with self._action_lock:
            await self.start()
            target = self._current_tab_index() if index is None else int(index)
            self._record_action("close_tab", str(target))
            info = await self._run_json(f"""
tabs = list_tabs(include_chrome=False)
if len(tabs) <= 1:
    raise RuntimeError("Cannot close the last remaining tab")
if {target} < 0 or {target} >= len(tabs):
    raise RuntimeError("Invalid tab index: {target}")
cdp("Target.closeTarget", targetId=tabs[{target}]["targetId"])
wait(0.2)
tabs = list_tabs(include_chrome=False)
if tabs:
    switch_tab(tabs[min({target}, len(tabs) - 1)]["targetId"])
result = page_info()
""")
            await self._apply_page_info(info)
            await self.list_tabs()
            self._dirty.set()
            return self.state_payload()

    async def wait(self, *, selector: str | None = None, timeout_ms: int | None = None, wait_until: str = "load") -> dict[str, Any]:
        async with self._action_lock:
            await self.start()
            timeout = max(250, min(timeout_ms or self.service.action_timeout_ms, 60000))
            self._record_action("wait", selector or wait_until)
            if selector:
                code = f"""
deadline = time.time() + {timeout / 1000:.3f}
selector = {json.dumps(selector)}
while time.time() < deadline:
    if js("return Boolean(document.querySelector(" + json.dumps(selector) + "))"):
        break
    wait(0.2)
else:
    raise RuntimeError("Timed out waiting for selector: " + selector)
result = page_info()
"""
            else:
                code = f"""
wait_for_load({timeout / 1000:.3f})
result = page_info()
"""
            info = await self._run_json(code)
            await self._apply_page_info(info)
            await self._capture_new_downloads(wait_for_completion=True)
            self._dirty.set()
            return self.state_payload()

    async def extract(self, *, selector: str | None = None, mode: str = "text", max_chars: int = 6000) -> dict[str, Any]:
        async with self._action_lock:
            await self.start()
            mode = (mode or "text").strip().lower()
            max_chars = max(200, min(max_chars, 50000))
            self._record_action("extract", f"{mode}:{selector or 'page'}")
            if selector:
                extract_value = "el.outerHTML" if mode == "html" else "el.innerText || el.textContent || ''"
                extract_expr = (
                    f"const el = document.querySelector({json.dumps(selector)});"
                    "if (!el) return '';"
                    f"return {extract_value};"
                )
            else:
                extract_expr = (
                    "return document.documentElement.outerHTML;"
                    if mode == "html"
                    else "return document.body ? document.body.innerText : document.documentElement.innerText;"
                )
            content = await self._run_json(f"result = js({json.dumps(extract_expr)})")
            return {
                "session_id": self.session_id,
                "url": self.current_url,
                "title": self.page_title,
                "mode": mode,
                "selector": selector,
                "content": str(content)[:max_chars],
                "truncated": len(str(content)) > max_chars,
            }

    async def discover(self, *, selector: str = "a,button,input,textarea,select,[role='button']", max_results: int = 40) -> dict[str, Any]:
        async with self._action_lock:
            await self.start()
            limit = max(1, min(max_results, 200))
            self._record_action("discover", selector)
            discover_expr = f"""
const nodes = Array.from(document.querySelectorAll({json.dumps(selector)})).slice(0, {limit});
return nodes.map((el, idx) => {{
  const rect = el.getBoundingClientRect();
  const text = (el.innerText || el.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 120);
  const id = el.id || null;
  const cls = (typeof el.className === 'string' ? el.className : '').trim().split(/\\s+/).filter(Boolean).slice(0, 4);
  let suggested = el.tagName.toLowerCase();
  if (id) suggested += `#${{id}}`;
  else if (cls.length) suggested += `.${{cls.join('.')}}`;
  return {{
    index: idx,
    tag: el.tagName.toLowerCase(),
    type: el.getAttribute('type'),
    role: el.getAttribute('role'),
    text,
    aria_label: el.getAttribute('aria-label'),
    name: el.getAttribute('name'),
    href: el.getAttribute('href'),
    suggested_selector: suggested,
    bounds: {{x: Math.round(rect.x), y: Math.round(rect.y), width: Math.round(rect.width), height: Math.round(rect.height)}}
  }};
}});
"""
            items = await self._run_json(f"result = js({json.dumps(discover_expr)})")
            return {
                "session_id": self.session_id,
                "url": self.current_url,
                "title": self.page_title,
                "selector": selector,
                "count": len(items or []),
                "elements": items or [],
            }

    async def upload_attachment(self, *, selector: str, attachment_url: str) -> dict[str, Any]:
        async with self._action_lock:
            await self.start()
            if not self.allow_file_uploads:
                raise ValueError("File uploads are disabled for this browser session")
            local_path = self._resolve_attachment_path(attachment_url)
            self._record_action("upload_attachment", f"{selector}:{local_path.name}")
            info = await self._run_json(f"""
upload_file({json.dumps(selector)}, {json.dumps(str(local_path))})
wait(0.15)
result = page_info()
""")
            await self._apply_page_info(info)
            self._dirty.set()
            return self.state_payload()

    async def snapshot(self, *, persist: bool = False, title: str | None = None) -> dict[str, Any]:
        self._record_action("snapshot", title or ("persist" if persist else None))
        frame = await self._capture_frame(force=True)
        if persist:
            html = (
                "<img "
                f"src=\"{frame.image_url}\" "
                "style=\"display:block;width:100%;height:auto;border-radius:12px;\" "
                f"alt=\"{title or self.page_title or self.current_url or 'Browser snapshot'}\""
                " />"
            )
            async with UnitOfWork() as uow:
                block = VisualBlock(
                    idea_id=self.idea_id,
                    content_type="preview",
                    title=title or self.page_title or "Browser snapshot",
                    content=html,
                    display_mode="canvas",
                    run_id=self.run_id,
                )
                uow.session.add(block)
                await uow.session.flush()
                block_id = block.id
            publish_safe("visual_reply", {
                "idea_id": self.idea_id,
                "block": {
                    "id": block_id,
                    "idea_id": self.idea_id,
                    "run_id": self.run_id,
                    "content_type": "preview",
                    "title": title or self.page_title or "Browser snapshot",
                    "content": html,
                    "display_mode": "canvas",
                    "position_after": None,
                    "created_at": _utcnow().isoformat(),
                },
            })
        return {
            "session_id": self.session_id,
            "frame": frame.__dict__,
            "state": self.state_payload(),
        }

    async def observe(self) -> dict[str, Any]:
        frame = await self._capture_frame(force=True)
        return {
            "session_id": self.session_id,
            "frame": frame.__dict__,
            "state": self.state_payload(),
        }

    async def save_screenshot(self, *, full_page: bool = True) -> dict[str, Any]:
        async with self._action_lock:
            await self.start()
            self._record_action("save_screenshot", "full_page" if full_page else "viewport")
            await ensure_dir(self._artifact_dir)
            target = self._allocate_unique_path(self._artifact_dir, self._artifact_filename("screenshot", "png"))
            await self._run_json(f"""
capture_screenshot({json.dumps(str(target))}, full={bool(full_page)})
result = {{"ok": True}}
""")
            stat = await path_stat(target) if await path_exists(target) else None
            artifact = self._record_artifact(
                kind="screenshot",
                filename=target.name,
                url=static_upload_url_for("browser-artifacts", self.session_id, target.name),
                size=stat.st_size if stat else None,
            )
            self._emit_state("artifact")
            return {"session_id": self.session_id, "artifact": artifact.__dict__, "state": self.state_payload()}

    async def print_pdf(self, *, landscape: bool = False) -> dict[str, Any]:
        async with self._action_lock:
            await self.start()
            self._record_action("print_pdf", "landscape" if landscape else "portrait")
            await ensure_dir(self._artifact_dir)
            target = self._allocate_unique_path(self._artifact_dir, self._artifact_filename("page", "pdf"))
            await self._run_json(f"""
payload = cdp("Page.printToPDF", printBackground=True, landscape={bool(landscape)})
import base64
open({json.dumps(str(target))}, "wb").write(base64.b64decode(payload["data"]))
result = {{"ok": True}}
""")
            stat = await path_stat(target) if await path_exists(target) else None
            artifact = self._record_artifact(
                kind="pdf",
                filename=target.name,
                url=static_upload_url_for("browser-artifacts", self.session_id, target.name),
                size=stat.st_size if stat else None,
            )
            self._emit_state("artifact")
            return {"session_id": self.session_id, "artifact": artifact.__dict__, "state": self.state_payload()}

    async def close(self, *, reason: str = "closed") -> None:
        if self._closed:
            return
        self._closed = True
        self._cancel_idle_close()
        if self._stream_task and not self._stream_task.done():
            self._stream_task.cancel()
            try:
                await self._stream_task
            except asyncio.CancelledError:
                pass
        await self._stop_harness_daemon()
        await self._stop_chrome_process()
        self.status = "closed"
        await self._persist_state(status="closed", active=False, closed_at=_utcnow(), last_error=reason if reason != "closed" else None)
        publish_safe("browser_session_closed", {
            "session_id": self.session_id,
            "idea_id": self.idea_id,
            "org_id": self.org_id,
            "reason": reason,
        })

    async def _stream_loop(self) -> None:
        while not self._closed:
            try:
                await asyncio.wait_for(self._dirty.wait(), timeout=self.service.keepalive_sec)
                self._dirty.clear()
                force_frame = self._force_next_stream_frame
                self._force_next_stream_frame = False
                await self._capture_frame(force=force_frame)
            except asyncio.TimeoutError:
                if self._watchers:
                    await self._capture_frame()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("browser stream failure session=%s: %s", self.session_id, e)
                await self._handle_runtime_error(str(e))
                return

    async def _capture_frame(self, *, force: bool = False) -> BrowserFrame:
        await self.start()
        payload = await self._run_json(
            """
import base64
path = capture_screenshot()
data = open(path, "rb").read()
result = {"image": base64.b64encode(data).decode("ascii"), "info": page_info()}
""",
            timeout=max(10.0, self.service.action_timeout_ms / 1000),
        )
        raw = base64.b64decode(payload["image"])
        sha1 = hashlib.sha1(raw).hexdigest()
        info = payload.get("info") or {}
        if not force and sha1 == self._last_frame_sha1:
            return BrowserFrame(
                image_url="",
                sha1=sha1,
                width=int(info.get("w") or self.viewport_width),
                height=int(info.get("h") or self.viewport_height),
                captured_at=_utcnow().isoformat(),
            )
        self._last_frame_sha1 = sha1
        frame = BrowserFrame(
            image_url="data:image/png;base64," + payload["image"],
            sha1=sha1,
            width=int(info.get("w") or self.viewport_width),
            height=int(info.get("h") or self.viewport_height),
            captured_at=_utcnow().isoformat(),
        )
        await self._apply_page_info(info)
        await self._persist_state(last_frame_at=_utcnow())
        publish_safe("browser_session_frame", {
            "session_id": self.session_id,
            "idea_id": self.idea_id,
            "org_id": self.org_id,
            "state": self.state_payload(),
            "frame": frame.__dict__,
        })
        return frame

    async def capture_visible_frame(self, *, reason: str = "visible") -> BrowserFrame | None:
        try:
            return await self._capture_frame(force=True)
        except Exception as exc:
            logger.warning(
                "browser visible frame capture failed session=%s reason=%s: %s",
                self.session_id,
                reason,
                exc,
            )
            return None

    async def _ensure_chrome_process(self) -> None:
        if self._chrome_process and self._chrome_process.returncode is None and self._cdp_url:
            return
        chrome_candidates = self._chrome_executable_candidates()
        if not chrome_candidates:
            raise BrowserCapabilityError(
                "No Chrome/Chromium executable found for Browser Harness. "
                "Run `./ops/install-browser-runtime.sh venv/bin/python3` to install repo-local Chrome for Testing, "
                "or set ILLO_BROWSER_CHROME_BIN to a custom executable path."
            )

        errors: list[str] = []
        for chrome in chrome_candidates:
            self._cdp_port = self._allocate_port()
            self._cdp_url = f"http://127.0.0.1:{self._cdp_port}"
            self._chrome_executable = chrome
            await ensure_dir(self._harness_chrome_dir)
            self._ensure_chrome_sidecar_permissions(chrome)
            try:
                self._chrome_process = await asyncio.create_subprocess_exec(
                    *self._chrome_launch_args(chrome),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                    env=self._chrome_launch_env(),
                )
            except OSError as exc:
                errors.append(f"Browser Harness Chrome failed to launch ({chrome}): {exc}")
                self._chrome_process = None
                self._chrome_executable = None
                self._cdp_port = None
                self._cdp_url = None
                continue
            try:
                await self._wait_for_cdp()
                return
            except BrowserCapabilityError as exc:
                errors.append(str(exc))
                await self._stop_chrome_process()
                self._chrome_executable = None
                self._cdp_port = None
                self._cdp_url = None

        detail = " | ".join(errors[-3:]) if errors else "unknown Chrome startup failure"
        raise BrowserCapabilityError(f"Unable to start Browser Harness Chrome: {detail}")

    def _chrome_launch_args(self, chrome: str) -> list[str]:
        crash_dir = self._harness_root / "crash-dumps"
        crash_dir.mkdir(parents=True, exist_ok=True)
        args = [
            chrome,
            "--remote-debugging-address=127.0.0.1",
            f"--remote-debugging-port={self._cdp_port}",
            f"--user-data-dir={self._harness_chrome_dir}",
            f"--window-size={self.viewport_width},{self.viewport_height}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            "--disable-client-side-phishing-detection",
            "--disable-component-update",
            "--disable-default-apps",
            "--disable-domain-reliability",
            "--disable-sync",
            "--disable-extensions",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-breakpad",
            "--disable-features=OptimizationHints,OptimizationTargetPrediction,OptimizationGuideModelDownloading,MediaRouter,DialMediaRouteProvider,Translate,AutofillServerCommunication",
            f"--crash-dumps-dir={crash_dir}",
            "--metrics-recording-only",
            "--disable-setuid-sandbox",
            "--no-sandbox",
            "--enable-automation",
            "--no-service-autorun",
            "--password-store=basic",
            "--use-mock-keychain",
            "--test-type",
        ]
        args.append("--headless=new")
        args.append("about:blank")
        return args

    def _chrome_sidecar_executables(self, chrome: str) -> list[Path]:
        executable = Path(chrome)
        candidates: list[Path] = [
            executable,
            executable.with_name("chrome_crashpad_handler"),
            executable.with_name("chrome_sandbox"),
        ]
        contents_root = next((parent for parent in executable.parents if parent.name == "Contents"), None)
        if contents_root is not None:
            frameworks_root = contents_root / "Frameworks"
            if frameworks_root.exists():
                candidates.extend(path for path in frameworks_root.glob("**/Helpers/*") if path.is_file())
                candidates.extend(path for path in frameworks_root.glob("**/*.app/Contents/MacOS/*") if path.is_file())
        deduped: list[Path] = []
        seen: set[Path] = set()
        for path in candidates:
            if path in seen:
                continue
            deduped.append(path)
            seen.add(path)
        return deduped

    def _ensure_chrome_sidecar_permissions(self, chrome: str) -> None:
        for path in self._chrome_sidecar_executables(chrome):
            if not path.exists() or not path.is_file():
                continue
            try:
                path.chmod(path.stat().st_mode | 0o100)
            except OSError:
                logger.debug("Failed to chmod Chrome sidecar executable: %s", path, exc_info=True)

    def _harness_log_tail(self, limit: int = 2000) -> str:
        try:
            text = (self._harness_ipc_dir / "bu.log").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        return text[-limit:].strip()

    def _chrome_launch_env(self) -> dict[str, str]:
        env = os.environ.copy()
        if not sys.platform.startswith("linux"):
            return env

        env_root = self._harness_root / "linux-env"
        paths = {
            "HOME": env_root / "home",
            "XDG_CONFIG_HOME": env_root / "config",
            "XDG_CACHE_HOME": env_root / "cache",
            "XDG_DATA_HOME": env_root / "data",
            "XDG_RUNTIME_DIR": env_root / "runtime",
            "TMPDIR": env_root / "tmp",
        }
        for key, path in paths.items():
            path.mkdir(parents=True, exist_ok=True)
            env[key] = str(path)
        try:
            paths["XDG_RUNTIME_DIR"].chmod(0o700)
        except OSError:
            pass
        env.setdefault("LANG", "C.UTF-8")
        return env

    def _find_chrome_executable(self) -> str | None:
        candidates = self._chrome_executable_candidates()
        return candidates[0] if candidates else None

    def _chrome_executable_candidates(self) -> list[str]:
        runtime_root = Path(os.environ.get("ILLO_BROWSER_RUNTIME_DIR", PROJECT_ROOT / ".runtime" / "browser"))
        chrome_for_testing_dir = Path(
            os.environ.get("ILLO_BROWSER_CHROME_FOR_TESTING_DIR", runtime_root / "chrome-for-testing")
        )
        chrome_for_testing_candidates: list[Path] = []
        if sys.platform.startswith("linux"):
            chrome_for_testing_candidates.append(chrome_for_testing_dir / "chrome-linux64" / "chrome")
        elif sys.platform == "darwin":
            mac_key = "mac-arm64" if platform.machine().lower() in {"arm64", "aarch64"} else "mac-x64"
            chrome_for_testing_candidates.append(
                chrome_for_testing_dir
                / f"chrome-{mac_key}"
                / "Google Chrome for Testing.app"
                / "Contents"
                / "MacOS"
                / "Google Chrome for Testing"
            )
        explicit_chrome = os.environ.get("ILLO_BROWSER_CHROME_BIN")
        if explicit_chrome:
            return [str(Path(explicit_chrome))] if Path(explicit_chrome).exists() else []
        candidates = [
            *chrome_for_testing_candidates,
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            shutil.which("google-chrome"),
            shutil.which("google-chrome-stable"),
            shutil.which("chromium"),
            shutil.which("chromium-browser"),
            shutil.which("microsoft-edge"),
        ]
        existing: list[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            if candidate and Path(candidate).exists():
                normalized = str(candidate)
                if normalized not in seen:
                    existing.append(normalized)
                    seen.add(normalized)
        return existing

    def _allocate_port(self) -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    async def _wait_for_cdp(self) -> None:
        assert self._cdp_url is not None
        deadline = asyncio.get_running_loop().time() + 20
        last_error: Exception | None = None
        timeout = httpx.Timeout(1.0, connect=1.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            while asyncio.get_running_loop().time() < deadline:
                if self._chrome_process and self._chrome_process.returncode is not None:
                    stderr = ""
                    if self._chrome_process.stderr:
                        try:
                            stderr = self._summarize_chrome_stderr(
                                (await self._chrome_process.stderr.read()).decode("utf-8", "replace")
                            )
                        except Exception:
                            stderr = ""
                    chrome = f" ({self._chrome_executable})" if self._chrome_executable else ""
                    code = self._chrome_process.returncode
                    detail = stderr or f"exit code {code}"
                    raise BrowserCapabilityError(f"Browser Harness Chrome exited early{chrome}: {detail}".strip())
                try:
                    response = await client.get(f"{self._cdp_url}/json/version")
                    response.raise_for_status()
                    return
                except Exception as exc:
                    last_error = exc
                    await asyncio.sleep(0.2)
        raise BrowserCapabilityError(f"Timed out waiting for Chrome CDP endpoint {self._cdp_url}: {last_error}")

    def _summarize_chrome_stderr(self, stderr: str) -> str:
        text = "\n".join(line.rstrip() for line in stderr.splitlines() if line.strip())
        if len(text) <= 4000:
            return text
        return f"{text[:1800]}\n...\n{text[-1800:]}"

    async def _page_info(self) -> dict[str, Any]:
        return await self._run_json("result = page_info()")

    async def _apply_page_info(self, info: dict[str, Any], status: str | None = None) -> None:
        if not isinstance(info, dict):
            info = {}
        self.current_url = info.get("url") or self.current_url
        raw_title = info.get("title") or self.page_title
        self.page_title = self._clean_harness_title(raw_title) if raw_title else raw_title
        if info.get("w"):
            self.viewport_width = int(info.get("w"))
        if info.get("h"):
            self.viewport_height = int(info.get("h"))
        if status:
            self.status = status
        self._update_single_tab_from_page_info()
        await self._persist_state(
            status=self.status,
            current_url=self.current_url,
            page_title=self.page_title,
            last_error=None,
        )
        self._emit_state(self.status)

    async def _run_json(self, code: str, *, timeout: float | None = None) -> Any:
        if not self._harness_bin:
            raise BrowserCapabilityError(
                "Browser Harness is not installed for this service. Run `./ops/install-browser-runtime.sh venv/bin/python3` "
                "or install the browser-harness console script into the Illo venv."
            )
        await self._ensure_chrome_process()
        marker = HARNESS_RESULT_MARKER
        wrapped = f"""
import json
import time
result = None
{code}
print({json.dumps(marker)} + json.dumps(result))
"""
        env = os.environ.copy()
        env.update({
            "BU_NAME": self._harness_name,
            "BU_CDP_URL": self._cdp_url or "",
            "BH_TMP_DIR": str(self._harness_ipc_dir),
            "BH_AGENT_WORKSPACE": str(self._harness_workspace),
        })
        process = await asyncio.create_subprocess_exec(
            self._harness_bin,
            "-c",
            wrapped,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout or max(40.0, self.service.nav_timeout_ms / 1000 + 5),
            )
        except asyncio.TimeoutError as exc:
            process.kill()
            stdout, stderr = await process.communicate()
            out = stdout.decode("utf-8", "replace")
            err = stderr.decode("utf-8", "replace")
            detail = (err or out or self._harness_log_tail() or "").strip()
            message = "Browser Harness command timed out"
            if detail:
                message = f"{message}: {detail[-2000:]}"
            raise BrowserCapabilityError(message) from exc
        out = stdout.decode("utf-8", "replace")
        err = stderr.decode("utf-8", "replace")
        if process.returncode:
            detail = (err or out or self._harness_log_tail() or "Browser Harness command failed").strip()
            raise BrowserCapabilityError(detail)
        for line in reversed(out.splitlines()):
            if line.startswith(marker):
                return json.loads(line[len(marker):])
        raise BrowserCapabilityError(f"Browser Harness command did not return a JSON result. stdout={out[-1000:]} stderr={err[-1000:]}")

    async def _stop_harness_daemon(self) -> None:
        if not self._harness_bin:
            return
        env = os.environ.copy()
        env.update({
            "BU_NAME": self._harness_name,
            "BU_CDP_URL": self._cdp_url or "",
            "BH_TMP_DIR": str(self._harness_ipc_dir),
            "BH_AGENT_WORKSPACE": str(self._harness_workspace),
        })
        try:
            process = await asyncio.create_subprocess_exec(
                self._harness_bin,
                "--reload",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                env=env,
            )
            await asyncio.wait_for(process.communicate(), timeout=5)
        except Exception:
            logger.debug("Failed to stop Browser Harness daemon for session=%s", self.session_id)

    async def _stop_chrome_process(self) -> None:
        if not self._chrome_process:
            return
        if self._chrome_process.returncode is None:
            self._chrome_process.terminate()
            try:
                await asyncio.wait_for(self._chrome_process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._chrome_process.kill()
                await self._chrome_process.wait()
        self._chrome_process = None

    async def _persist_state(self, **updates: Any) -> None:
        async with UnitOfWork() as uow:
            record = await uow.session.get(BrowserSession, self.session_id)
            if not record:
                return
            for key, value in updates.items():
                setattr(record, key, value)
                setattr(self._record, key, value)
            # Keep runtime copy in sync for fields we expose frequently.
            for key, value in updates.items():
                if hasattr(self, key):
                    setattr(self, key, value)

    async def _handle_runtime_error(self, message: str) -> None:
        self.last_error = message
        self.status = "error"
        await self._persist_state(status="error", last_error=message)
        publish_safe("browser_session_error", {
            "session_id": self.session_id,
            "idea_id": self.idea_id,
            "org_id": self.org_id,
            "error": message,
        })

    def state_payload(self) -> dict[str, Any]:
        return {
            "id": self.session_id,
            "idea_id": self.idea_id,
            "org_id": self.org_id,
            "run_id": self.run_id,
            "status": self.status,
            "current_url": self.current_url,
            "page_title": self.page_title,
            "viewport_width": self.viewport_width,
            "viewport_height": self.viewport_height,
            "storage_mode": self.storage_mode,
            "allow_downloads": self.allow_downloads,
            "allow_file_uploads": self.allow_file_uploads,
            "last_error": self.last_error,
            "watchers": len(self._watchers),
            "tabs": self._tab_summaries_sync(),
            "current_tab_index": self._current_tab_index(),
            "actions": [a.__dict__ for a in self._actions],
            "downloads": [d.__dict__ for d in self._downloads],
            "artifacts": [a.__dict__ for a in self._artifacts],
            "console_messages": [m.__dict__ for m in self._console_messages],
            "request_failures": [r.__dict__ for r in self._request_failures],
            "resource_summary": self.resource_summary,
        }

    def _emit_state(self, reason: str) -> None:
        publish_safe("browser_session_state", {
            "session_id": self.session_id,
            "idea_id": self.idea_id,
            "org_id": self.org_id,
            "reason": reason,
            "state": self.state_payload(),
        })

    def _mark_dirty(self) -> None:
        try:
            self._dirty.set()
        except Exception:
            pass

    def _record_action(self, action: str, detail: str | None = None) -> None:
        self._actions.append(BrowserAction(at=_utcnow().isoformat(), action=action, detail=detail))
        if len(self._actions) > 30:
            self._actions = self._actions[-30:]

    def _record_download(
        self,
        filename: str,
        url: str,
        download_url: str | None = None,
        size: int | None = None,
    ) -> None:
        self._downloads.append(BrowserDownload(
            at=_utcnow().isoformat(),
            filename=filename,
            url=url,
            download_url=download_url or public_static_upload_url(url),
            size=size,
        ))
        if len(self._downloads) > 20:
            self._downloads = self._downloads[-20:]

    def _record_artifact(
        self,
        kind: str,
        filename: str,
        url: str,
        download_url: str | None = None,
        size: int | None = None,
    ) -> BrowserArtifact:
        artifact = BrowserArtifact(
            at=_utcnow().isoformat(),
            kind=kind,
            filename=filename,
            url=url,
            download_url=download_url or public_static_upload_url(url),
            size=size,
        )
        self._artifacts.append(artifact)
        if len(self._artifacts) > 20:
            self._artifacts = self._artifacts[-20:]
        return artifact

    def _record_console_entry(self, level: str, text: str, location: str | None = None) -> None:
        self._console_messages.append(
            BrowserConsoleEntry(
                at=_utcnow().isoformat(),
                level=level,
                text=text[:500],
                location=location,
            )
        )
        if len(self._console_messages) > 25:
            self._console_messages = self._console_messages[-25:]

    def _record_request_failure(
        self,
        method: str,
        url: str,
        error_text: str | None = None,
        resource_type: str | None = None,
    ) -> None:
        self._request_failures.append(
            BrowserRequestFailure(
                at=_utcnow().isoformat(),
                method=method,
                url=url[:500],
                error_text=(error_text or "")[:300] or None,
                resource_type=resource_type,
            )
        )
        if len(self._request_failures) > 25:
            self._request_failures = self._request_failures[-25:]

    def _resolve_attachment_path(self, attachment_url: str) -> Path:
        value = (attachment_url or "").strip()
        if not value:
            raise ValueError("attachment_url is required")
        parsed = urlparse(value)
        if parsed.scheme or parsed.netloc:
            value = parsed.path
        prefix = "/static/uploads/"
        if not value.startswith(prefix):
            raise ValueError("Only Cortex uploaded attachments are supported")
        relative = unquote(value[len(prefix):].strip("/"))
        candidate = (UPLOAD_DIR / relative).resolve()
        upload_root = UPLOAD_DIR.resolve()
        if not str(candidate).startswith(str(upload_root) + os.sep) and candidate != upload_root:
            raise ValueError("Attachment path escapes upload root")
        if not candidate.exists() or not candidate.is_file():
            raise ValueError(f"Attachment not found: {attachment_url}")
        return candidate

    def _sanitize_download_filename(self, filename: str) -> str:
        cleaned = Path((filename or "").strip()).name
        cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", cleaned).strip(".-")
        return cleaned or "download.bin"

    def _slugify(self, value: str | None) -> str:
        cleaned = re.sub(r"^https?://", "", (value or "").strip(), flags=re.I)
        cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", cleaned).strip(".-")
        return cleaned[:60] or "page"

    def _artifact_filename(self, kind: str, ext: str) -> str:
        stamp = _utcnow().strftime("%Y%m%d-%H%M%S")
        slug = self._slugify(self.page_title or self.current_url or kind)
        return self._sanitize_download_filename(f"{kind}-{slug}-{stamp}.{ext}")

    def _allocate_unique_path(self, directory: Path, filename: str) -> Path:
        candidate = directory / filename
        if not candidate.exists():
            return candidate
        stem = candidate.stem or "download"
        suffix = candidate.suffix
        for idx in range(2, 1000):
            rotated = directory / f"{stem}-{idx}{suffix}"
            if not rotated.exists():
                return rotated
        raise RuntimeError("Unable to allocate a unique download filename")

    def _schedule_idle_close(self) -> None:
        self._cancel_idle_close()
        self._idle_close_task = asyncio.get_running_loop().create_task(
            self._idle_close_after_delay(),
            name=f"browser-idle-close-{self.session_id}",
        )

    def _cancel_idle_close(self) -> None:
        if self._idle_close_task and not self._idle_close_task.done():
            self._idle_close_task.cancel()
        self._idle_close_task = None

    async def _idle_close_after_delay(self) -> None:
        try:
            await asyncio.sleep(self.service.idle_ttl_sec)
            if not self._watchers and not self._closed:
                await self.close(reason="idle_timeout")
        except asyncio.CancelledError:
            raise

class BrowserSessionService:
    """Registry and lifecycle manager for thought browser sessions."""

    def __init__(self) -> None:
        self._runtime_lock = asyncio.Lock()
        self._runtimes: dict[str, BrowserSessionRuntime] = {}
        self.keepalive_sec = float(os.environ.get("ILLO_BROWSER_FRAME_KEEPALIVE_SEC", "1.5"))
        self.jpeg_quality = int(os.environ.get("ILLO_BROWSER_JPEG_QUALITY", "55"))
        self.nav_timeout_ms = int(os.environ.get("ILLO_BROWSER_NAV_TIMEOUT_MS", "20000"))
        self.action_timeout_ms = int(os.environ.get("ILLO_BROWSER_ACTION_TIMEOUT_MS", "10000"))
        self.idle_ttl_sec = float(os.environ.get("ILLO_BROWSER_IDLE_TTL_SEC", "180"))

    def _session_recycle_reason(
        self,
        record: BrowserSession,
        *,
        user_id: str | None,
        storage_mode: str,
        viewport_width: int,
        viewport_height: int,
        allow_downloads: bool,
        allow_file_uploads: bool,
    ) -> str | None:
        if not getattr(record, "active", False):
            return "browser session is inactive"
        if getattr(record, "last_error", None):
            return "browser session has a recorded error"
        if (record.status or "").lower() not in {"starting", "ready"}:
            return f"browser session status is {record.status or 'unknown'}"
        if str(getattr(record, "user_id", "") or "") != str(user_id or ""):
            return "browser session user changed"
        if (getattr(record, "storage_mode", None) or "ephemeral") != storage_mode:
            return "browser session storage mode changed"
        if bool(getattr(record, "allow_downloads", False)) != bool(allow_downloads):
            return "browser session download policy changed"
        if bool(getattr(record, "allow_file_uploads", True)) != bool(allow_file_uploads):
            return "browser session upload policy changed"
        if int(getattr(record, "viewport_width", viewport_width) or viewport_width) != int(viewport_width):
            return "browser session viewport width changed"
        if int(getattr(record, "viewport_height", viewport_height) or viewport_height) != int(viewport_height):
            return "browser session viewport height changed"
        return None

    async def _retire_browser_session(self, session_id: str, reason: str) -> None:
        runtime = self._runtimes.pop(session_id, None)
        if runtime is not None and not getattr(runtime, "_closed", False):
            try:
                await runtime.close(reason=reason)
                return
            except Exception as exc:
                logger.debug("Failed to close recycled browser session %s: %s", session_id, exc)
        async with UnitOfWork() as uow:
            record = await uow.session.get(BrowserSession, session_id)
            if record:
                record.status = "closed"
                record.active = False
                record.closed_at = _utcnow()
                record.last_error = reason

    def get_idea_org_id(self, idea_id: str) -> str | None:
        for runtime in self._runtimes.values():
            if runtime.idea_id == str(idea_id) and not runtime._closed:
                return runtime.org_id
        return None

    async def get_idea_org_id_async(self, idea_id: str) -> str | None:
        try:
            async with UnitOfWork() as uow:
                org_id = await uow.session.scalar(
                    select(Idea.org_id).where(Idea.id == str(idea_id))
                )
        except Exception as exc:
            logger.debug("Failed to resolve browser session idea org idea=%s: %s", idea_id, exc)
            return None
        return str(org_id) if org_id else None

    def get_session_record_for_org(
        self,
        session_id: str,
        *,
        org_id: str | None,
    ) -> BrowserSession | None:
        normalized_session_id = str(session_id or "").strip()
        if not normalized_session_id:
            return None
        normalized_org_id = str(org_id).strip() if org_id else None
        runtime = self._runtimes.get(normalized_session_id)
        if runtime is None or runtime._closed:
            return None
        if normalized_org_id and runtime.org_id != normalized_org_id:
            return None
        record = runtime._record
        if not getattr(record, "active", True):
            return None
        setattr(record, "_idea_org_id", runtime.org_id)
        return record

    async def get_session_record_for_org_async(
        self,
        session_id: str,
        *,
        org_id: str | None,
    ) -> BrowserSession | None:
        normalized_session_id = str(session_id or "").strip()
        if not normalized_session_id:
            return None
        normalized_org_id = str(org_id).strip() if org_id else None
        async with UnitOfWork() as uow:
            stmt = select(BrowserSession).where(BrowserSession.id == normalized_session_id)
            if normalized_org_id:
                stmt = (
                    stmt.join(Idea, BrowserSession.idea_id == Idea.id)
                    .where(Idea.org_id == normalized_org_id)
                )
            result = await uow.session.scalars(stmt)
            record = result.first()
            if record is None or not getattr(record, "active", False):
                return None
            idea_org_id = normalized_org_id
            if idea_org_id is None:
                idea_org_id = await uow.session.scalar(
                    select(Idea.org_id).where(Idea.id == str(record.idea_id))
                )
            setattr(record, "_idea_org_id", str(idea_org_id) if idea_org_id else None)
            return record

    async def create_or_get_session(
        self,
        *,
        idea_id: str,
        user_id: str | None,
        run_id: int | None = None,
        url: str | None = None,
        viewport_width: int = 1280,
        viewport_height: int = 800,
        storage_mode: str = "ephemeral",
        allow_downloads: bool = False,
        allow_file_uploads: bool = True,
    ) -> BrowserSessionRuntime:
        async with self._runtime_lock:
            record = await self.get_active_session_record_async(idea_id)
            org_id = await self.get_idea_org_id_async(idea_id)
            if record is not None:
                setattr(record, "_idea_org_id", org_id)
            created_session = False
            if record is not None:
                recycle_reason = self._session_recycle_reason(
                    record,
                    user_id=user_id,
                    storage_mode=storage_mode,
                    viewport_width=viewport_width,
                    viewport_height=viewport_height,
                    allow_downloads=allow_downloads,
                    allow_file_uploads=allow_file_uploads,
                )
                if recycle_reason is not None:
                    await self._retire_browser_session(str(record.id), recycle_reason)
                    record = None
            resource_summary = build_browser_resource_summary(
                mode="cold",
                warm_start_used=False,
                reason="browser harness dedicated Chrome session",
                browser_version="browser-harness+chrome-cdp",
                context_mode=storage_mode,
                profile_key=f"idea:{idea_id}:user:{user_id or 'anon'}",
            )
            if record is None:
                created_session = True
                async with UnitOfWork() as uow:
                    record = BrowserSession(
                        idea_id=idea_id,
                        user_id=user_id,
                        run_id=run_id,
                        current_url=url,
                        viewport_width=viewport_width,
                        viewport_height=viewport_height,
                        storage_mode=storage_mode,
                        allow_downloads=allow_downloads,
                        allow_file_uploads=allow_file_uploads,
                        status="starting",
                    )
                    uow.session.add(record)
                    await uow.session.flush()
                    await uow.session.refresh(record)
                    setattr(record, "_idea_org_id", org_id)
                publish_safe("browser_session_state", {
                    "session_id": str(record.id),
                    "idea_id": idea_id,
                    "org_id": org_id,
                    "reason": "created",
                    "state": {
                        "id": str(record.id),
                        "idea_id": idea_id,
                        "org_id": org_id,
                        "run_id": run_id,
                        "status": "starting",
                        "current_url": url,
                        "page_title": None,
                        "viewport_width": viewport_width,
                        "viewport_height": viewport_height,
                        "storage_mode": storage_mode,
                        "allow_downloads": allow_downloads,
                        "allow_file_uploads": allow_file_uploads,
                        "last_error": None,
                        "watchers": 0,
                        "resource_summary": resource_summary,
                    },
                })
            runtime = self._runtimes.get(str(record.id))
            if runtime is None or runtime._closed:
                runtime = BrowserSessionRuntime(self, record)
                self._runtimes[str(record.id)] = runtime
            runtime.resource_summary = resource_summary
        try:
            await runtime.start()
            if url:
                current_url = str(runtime.current_url or "").strip()
                if created_session or not current_url or current_url == "about:blank" or current_url.startswith("chrome://"):
                    await runtime.new_tab(url)
                else:
                    await runtime.navigate(url)
            await runtime.capture_visible_frame(reason="created" if created_session else "opened")
        except Exception as exc:
            await _record_browser_harness_tool_call(
                run_id=run_id,
                idea_id=idea_id,
                session_id=getattr(runtime, "session_id", None),
                url=url,
                action="open",
                status="error",
                detail=str(exc),
            )
            await runtime._handle_runtime_error(str(exc))
            raise
        await _record_browser_harness_tool_call(
            run_id=run_id,
            idea_id=idea_id,
            session_id=runtime.session_id,
            url=runtime.current_url or url,
            action="open",
            status="ready",
        )
        return runtime

    async def get_or_restore_runtime(self, session_id: str) -> BrowserSessionRuntime:
        runtime = self._runtimes.get(session_id)
        if runtime is not None and runtime._closed:
            self._runtimes.pop(session_id, None)
            runtime = None
        if runtime is not None:
            return runtime
        async with UnitOfWork() as uow:
            record = await uow.session.get(BrowserSession, session_id)
            if record is not None:
                org_id = await uow.session.scalar(
                    select(Idea.org_id).where(Idea.id == str(record.idea_id))
                )
                setattr(record, "_idea_org_id", str(org_id) if org_id else None)
        if not record or not record.active:
            raise KeyError(f"Unknown or inactive browser session: {session_id}")
        runtime = BrowserSessionRuntime(self, record)
        self._runtimes[session_id] = runtime
        return runtime

    def get_active_session_record(self, idea_id: str) -> BrowserSession | None:
        for runtime in self._runtimes.values():
            if runtime.idea_id == str(idea_id) and not runtime._closed:
                record = runtime._record
                if getattr(record, "active", True):
                    return record
        return None

    async def get_active_session_record_async(self, idea_id: str) -> BrowserSession | None:
        record = self._load_active_session(idea_id)
        if inspect.isawaitable(record):
            return await record
        return record

    async def subscribe(self, session_id: str, user_id: str | None) -> dict[str, Any]:
        runtime = await self.get_or_restore_runtime(session_id)
        await runtime.ensure_streaming(user_id, force_frame=True)
        runtime._emit_state("subscribed")
        runtime._dirty.set()
        return runtime.state_payload()

    async def unsubscribe(self, session_id: str, user_id: str | None) -> None:
        runtime = await self.get_or_restore_runtime(session_id)
        await runtime.unsubscribe(user_id)

    async def command(self, session_id: str, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        runtime = await self.get_or_restore_runtime(session_id)
        try:
            if action == "navigate":
                return await runtime.navigate(payload.get("url", ""))
            if action == "click":
                return await runtime.click(
                    selector=payload.get("selector"),
                    x=payload.get("x"),
                    y=payload.get("y"),
                )
            if action == "type":
                return await runtime.type_text(
                    payload.get("text", ""),
                    selector=payload.get("selector"),
                    press_enter=bool(payload.get("press_enter")),
                )
            if action == "key":
                return await runtime.press_key(payload.get("key", "Enter"))
            if action == "scroll":
                return await runtime.scroll(
                    delta_x=float(payload.get("delta_x", 0)),
                    delta_y=float(payload.get("delta_y", 0)),
                )
            if action == "refresh":
                return await runtime.refresh()
            if action == "new_tab":
                return await runtime.new_tab(payload.get("url"))
            if action == "switch_tab":
                return await runtime.switch_tab(int(payload.get("index", 0)))
            if action == "close_tab":
                raw_index = payload.get("index")
                return await runtime.close_tab(None if raw_index is None else int(raw_index))
            if action == "list_tabs":
                return await runtime.list_tabs()
            if action == "back":
                return await runtime.go_back()
            if action == "forward":
                return await runtime.go_forward()
            if action == "wait":
                return await runtime.wait(
                    selector=payload.get("selector"),
                    timeout_ms=payload.get("timeout_ms"),
                    wait_until=payload.get("wait_until", "load"),
                )
            if action == "extract":
                return await runtime.extract(
                    selector=payload.get("selector"),
                    mode=payload.get("mode", "text"),
                    max_chars=int(payload.get("max_chars", 6000)),
                )
            if action == "discover":
                return await runtime.discover(
                    selector=payload.get("selector", "a,button,input,textarea,select,[role='button']"),
                    max_results=int(payload.get("max_results", 40)),
                )
            if action == "upload_attachment":
                return await runtime.upload_attachment(
                    selector=payload.get("selector", ""),
                    attachment_url=payload.get("attachment_url", ""),
                )
            if action == "save_screenshot":
                return await runtime.save_screenshot(full_page=bool(payload.get("full_page", True)))
            if action == "print_pdf":
                return await runtime.print_pdf(landscape=bool(payload.get("landscape", False)))
            if action == "snapshot":
                return await runtime.snapshot(
                    persist=bool(payload.get("persist")),
                    title=payload.get("title"),
                )
            if action == "observe":
                return await runtime.observe()
            if action == "close":
                await runtime.close(reason=payload.get("reason", "closed"))
                async with self._runtime_lock:
                    self._runtimes.pop(session_id, None)
                return {"closed": True, "session_id": session_id}
            raise ValueError(f"Unsupported browser action: {action}")
        except Exception as exc:
            if action != "close" and not runtime._closed:
                await runtime._handle_runtime_error(str(exc))
            raise

    async def _load_active_session(self, idea_id: str) -> BrowserSession | None:
        async with UnitOfWork() as uow:
            result = await uow.session.scalars(
                select(BrowserSession)
                .where(
                    BrowserSession.idea_id == idea_id,
                    BrowserSession.active.is_(True),
                )
                .order_by(BrowserSession.created_at.desc())
            )
            return result.first()


browser_sessions = BrowserSessionService()
