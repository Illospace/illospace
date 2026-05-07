from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from brain.app.api.auth import get_current_user
from brain.app.api.main import app
from brain.app.api.ws.auth import create_ws_token


class _FakeUOW:
    def __init__(self):
        self.session = SimpleNamespace()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _auth_user():
    return {"id": "user-123", "org_id": "org-123", "name": "Alex"}


def _ws_token(user_id: str = "user-123", org_id: str = "org-123") -> str:
    token, _ = create_ws_token(
        {"id": user_id, "org_id": org_id},
        session_id=f"session-{user_id}",
    )
    return token


def _allow_ws_browser_session(monkeypatch, ws_router, *, allowed_org_id: str = "org-123", active: bool = True):
    def fake_get_session_record_for_org(session_id: str, *, org_id: str | None):
        if org_id != allowed_org_id:
            return None
        if not active:
            return None
        return SimpleNamespace(id=session_id, idea_id="idea-123", active=True, _idea_org_id=org_id)

    monkeypatch.setattr(
        ws_router.browser_sessions,
        "get_session_record_for_org",
        fake_get_session_record_for_org,
    )


def test_create_browser_session_endpoint(monkeypatch):
    from brain.app.api.routers.cortex import _browser

    created_at = datetime.now(timezone.utc)
    runtime = SimpleNamespace(
        session_id="sess-1",
        idea_id="idea-1",
        user_id="user-123",
        run_id=None,
        status="ready",
        current_url="https://example.com",
        page_title="Example",
        viewport_width=1280,
        viewport_height=800,
        storage_mode="idea",
        allow_downloads=True,
        allow_file_uploads=False,
        last_error=None,
    )

    async def fake_create_or_get_session(**kwargs):
        assert kwargs["idea_id"] == "idea-1"
        assert kwargs["url"] == "https://example.com"
        assert kwargs["storage_mode"] == "idea"
        assert kwargs["allow_downloads"] is True
        assert kwargs["allow_file_uploads"] is False
        return runtime

    monkeypatch.setattr(_browser, "UnitOfWork", _FakeUOW)
    monkeypatch.setattr(_browser, "_validate_idea_org_orm", lambda session, idea_id, org_id: True)
    monkeypatch.setattr(_browser.browser_sessions, "create_or_get_session", fake_create_or_get_session)
    monkeypatch.setattr(
        _browser,
        "_get_browser_session_or_404",
        lambda session_id: SimpleNamespace(created_at=created_at),
    )
    app.dependency_overrides[get_current_user] = _auth_user

    client = TestClient(app)
    try:
        response = client.post(
            "/api/cortex/ideas/idea-1/browser/session",
            json={
                "url": "https://example.com",
                "storage_mode": "idea",
                "allow_downloads": True,
                "allow_file_uploads": False,
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == "sess-1"
    assert body["current_url"] == "https://example.com"
    assert body["status"] == "ready"
    assert body["storage_mode"] == "idea"
    assert body["allow_downloads"] is True
    assert body["allow_file_uploads"] is False


def test_browser_tool_handlers_record_preview_and_screenshot_artifacts(monkeypatch):
    from brain.systems.runs.tool_catalog.handlers import browser as browser_handlers

    persisted: list[dict] = []
    monkeypatch.setattr(
        browser_handlers,
        "_persist_execution_artifacts",
        lambda artifacts, run_id=None: persisted.extend(artifacts),
    )

    browser_handlers._record_browser_preview_artifact(
        {
            "id": "sess-preview",
            "status": "ready",
            "current_url": "https://www.google.com/search?q=nice+joke",
            "page_title": "Google Search",
            "viewport_width": 1280,
            "viewport_height": 800,
        },
        source_tool="browser_session_open",
    )
    browser_handlers._record_browser_saved_artifact(
        {
            "session_id": "sess-preview",
            "artifact": {
                "kind": "screenshot",
                "filename": "screenshot.png",
                "url": "/static/uploads/browser-artifacts/sess-preview/screenshot.png",
                "size": 1234,
            },
            "state": {
                "current_url": "https://www.google.com/search?q=nice+joke",
                "page_title": "Google Search",
            },
        },
        source_tool="browser_save_screenshot",
    )

    assert persisted == [
        {
            "type": "browser_preview",
            "source_tool": "browser_session_open",
            "status": "ready",
            "session_id": "sess-preview",
            "url": "https://www.google.com/search?q=nice+joke",
            "page_title": "Google Search",
            "viewport_width": 1280,
            "viewport_height": 800,
        },
        {
            "type": "browser_screenshot",
            "source_tool": "browser_save_screenshot",
            "status": "saved",
            "session_id": "sess-preview",
            "url": "/static/uploads/browser-artifacts/sess-preview/screenshot.png",
            "filename": "screenshot.png",
            "size": 1234,
            "page_url": "https://www.google.com/search?q=nice+joke",
            "page_title": "Google Search",
        },
    ]


def test_browser_tool_handlers_do_not_record_failed_preview(monkeypatch):
    from brain.systems.runs.tool_catalog.handlers import browser as browser_handlers

    persisted: list[dict] = []
    monkeypatch.setattr(
        browser_handlers,
        "_persist_execution_artifacts",
        lambda artifacts, run_id=None: persisted.extend(artifacts),
    )

    browser_handlers._record_browser_preview_artifact(
        {
            "id": "sess-error",
            "status": "error",
            "current_url": "https://www.google.com",
            "last_error": "Chrome exited early",
        },
        source_tool="browser_session_open",
    )

    browser_handlers._record_browser_snapshot_artifact(
        {
            "session_id": "sess-error",
            "frame": {
                "sha1": "frame-sha",
                "width": 1280,
                "height": 800,
            },
            "state": {
                "id": "sess-error",
                "status": "error",
                "current_url": "https://www.google.com",
                "last_error": "Chrome exited early",
            },
        },
        source_tool="browser_snapshot",
    )
    browser_handlers._record_browser_saved_artifact(
        {
            "session_id": "sess-error",
            "artifact": {
                "kind": "screenshot",
                "filename": "screenshot.png",
                "url": "/static/uploads/browser-artifacts/sess-error/screenshot.png",
                "size": 1234,
            },
            "state": {
                "id": "sess-error",
                "status": "error",
                "current_url": "https://www.google.com",
                "last_error": "Chrome exited early",
            },
        },
        source_tool="browser_save_screenshot",
    )

    assert persisted == []


def test_get_browser_session_endpoint(monkeypatch):
    from brain.app.api.routers.cortex import _browser

    created_at = datetime.now(timezone.utc)
    record = SimpleNamespace(
        id="sess-2",
        idea_id="idea-2",
        user_id="user-123",
        run_id=None,
        status="ready",
        current_url="https://illo.ai",
        page_title="Illo",
        viewport_width=1440,
        viewport_height=900,
        storage_mode="ephemeral",
        allow_downloads=False,
        allow_file_uploads=True,
        last_error=None,
        active=True,
        last_frame_at=None,
        closed_at=None,
        created_at=created_at,
    )

    monkeypatch.setattr(_browser, "UnitOfWork", _FakeUOW)
    monkeypatch.setattr(_browser, "_validate_idea_org_orm", lambda session, idea_id, org_id: True)
    monkeypatch.setattr(_browser.browser_sessions, "get_active_session_record", lambda idea_id: record)
    app.dependency_overrides[get_current_user] = _auth_user

    client = TestClient(app)
    try:
        response = client.get("/api/cortex/ideas/idea-2/browser/session")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == "sess-2"
    assert body["viewport_width"] == 1440
    assert body["storage_mode"] == "ephemeral"
    assert body["allow_file_uploads"] is True


def test_snapshot_and_close_browser_session_endpoints(monkeypatch):
    from brain.app.api.routers.cortex import _browser

    calls: list[tuple[str, str, dict]] = []

    async def fake_command(session_id: str, action: str, payload: dict):
        calls.append((session_id, action, payload))
        if action == "snapshot":
            return {"session_id": session_id, "ok": True}
        return {"closed": True}

    record = SimpleNamespace(idea_id="idea-3")
    monkeypatch.setattr(_browser, "UnitOfWork", _FakeUOW)
    monkeypatch.setattr(_browser, "_validate_idea_org_orm", lambda session, idea_id, org_id: True)
    monkeypatch.setattr(_browser, "_get_browser_session_or_404", lambda session_id: record)
    monkeypatch.setattr(_browser.browser_sessions, "get_session_record_for_org", lambda session_id, *, org_id: record)
    monkeypatch.setattr(_browser.browser_sessions, "command", fake_command)
    app.dependency_overrides[get_current_user] = _auth_user

    client = TestClient(app)
    try:
        snap = client.post("/api/cortex/browser/session/sess-3/snapshot", json={"persist": True, "title": "Snap"})
        close = client.delete("/api/cortex/browser/session/sess-3")
    finally:
        app.dependency_overrides.clear()

    assert snap.status_code == 200
    assert close.status_code == 200
    assert calls[0] == ("sess-3", "snapshot", {"persist": True, "title": "Snap"})
    assert calls[1] == ("sess-3", "close", {"reason": "user_closed"})


def test_ws_browser_commands(monkeypatch):
    from brain.app.api.routers import ws as ws_router

    subscribed: list[tuple[str, str]] = []
    commands: list[tuple[str, str, dict]] = []

    async def fake_subscribe(session_id: str, user_id: str | None):
        subscribed.append((session_id, user_id or ""))
        return {"id": session_id}

    async def fake_command(session_id: str, action: str, payload: dict):
        commands.append((session_id, action, payload))
        return {"current_url": payload.get("url", "https://example.com"), "status": "ready"}

    monkeypatch.setattr(ws_router.browser_sessions, "subscribe", fake_subscribe)
    monkeypatch.setattr(ws_router.browser_sessions, "command", fake_command)
    _allow_ws_browser_session(monkeypatch, ws_router)

    client = TestClient(app)
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "connected"
        ws.send_json({"type": "auth", "token": _ws_token()})
        assert ws.receive_json()["type"] == "authenticated"

        ws.send_json({"type": "browser_subscribe", "session_id": "sess-9"})
        ws.send_json({"type": "browser_navigate", "session_id": "sess-9", "url": "https://example.com"})

        msg = ws.receive_json()
        assert msg["type"] == "browser_session_delta"
        assert msg["session_id"] == "sess-9"

    assert subscribed == [("sess-9", "user-123")]
    assert commands == [("sess-9", "navigate", {"url": "https://example.com"})]


def test_ws_browser_subscribe_rejects_cross_org(monkeypatch):
    from brain.app.api.routers import ws as ws_router

    subscribed: list[tuple[str, str]] = []

    async def fake_subscribe(session_id: str, user_id: str | None):
        subscribed.append((session_id, user_id or ""))
        return {"id": session_id}

    monkeypatch.setattr(ws_router.browser_sessions, "subscribe", fake_subscribe)
    _allow_ws_browser_session(monkeypatch, ws_router, allowed_org_id="org-owner")

    client = TestClient(app)
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "connected"
        ws.send_json({"type": "auth", "token": _ws_token(org_id="org-attacker")})
        assert ws.receive_json()["type"] == "authenticated"

        ws.send_json({"type": "browser_subscribe", "session_id": "sess-cross"})

        msg = ws.receive_json()
        assert msg["type"] == "browser_session_error"
        assert msg["session_id"] == "sess-cross"
        assert msg["code"] == "BROWSER_SESSION_FORBIDDEN"

    assert subscribed == []


def test_ws_browser_subscribe_rejects_inactive_session_with_browser_error(monkeypatch):
    from brain.app.api.routers import ws as ws_router

    subscribed: list[tuple[str, str]] = []

    async def fake_subscribe(session_id: str, user_id: str | None):
        subscribed.append((session_id, user_id or ""))
        return {"id": session_id}

    monkeypatch.setattr(ws_router.browser_sessions, "subscribe", fake_subscribe)
    _allow_ws_browser_session(monkeypatch, ws_router, active=False)

    client = TestClient(app)
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "connected"
        ws.send_json({"type": "auth", "token": _ws_token()})
        assert ws.receive_json()["type"] == "authenticated"

        ws.send_json({"type": "browser_subscribe", "session_id": "sess-inactive"})

        msg = ws.receive_json()
        assert msg["type"] == "browser_session_error"
        assert msg["session_id"] == "sess-inactive"
        assert msg["code"] == "BROWSER_SESSION_FORBIDDEN"

    assert subscribed == []


def test_ws_browser_command_rejects_cross_org_without_calling_command(monkeypatch):
    from brain.app.api.routers import ws as ws_router

    commands: list[tuple[str, str, dict]] = []

    async def fake_command(session_id: str, action: str, payload: dict):
        commands.append((session_id, action, payload))
        return {"status": "ready"}

    monkeypatch.setattr(ws_router.browser_sessions, "command", fake_command)
    _allow_ws_browser_session(monkeypatch, ws_router, allowed_org_id="org-owner")

    client = TestClient(app)
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "connected"
        ws.send_json({"type": "auth", "token": _ws_token(org_id="org-attacker")})
        assert ws.receive_json()["type"] == "authenticated"

        ws.send_json({"type": "browser_navigate", "session_id": "sess-cross", "url": "https://example.com"})

        msg = ws.receive_json()
        assert msg["type"] == "browser_session_error"
        assert msg["session_id"] == "sess-cross"
        assert msg["code"] == "BROWSER_SESSION_FORBIDDEN"

    assert commands == []


@pytest.mark.asyncio
async def test_browser_service_runs_wait_and_extract():
    from brain.platform.browser.service import BrowserSessionService

    service = BrowserSessionService()
    calls = []

    class FakeRuntime:
        async def wait(self, **kwargs):
            calls.append(("wait", kwargs))
            return {"status": "ready"}

        async def extract(self, **kwargs):
            calls.append(("extract", kwargs))
            return {"content": "hello"}

        async def discover(self, **kwargs):
            calls.append(("discover", kwargs))
            return {"elements": [{"suggested_selector": "button#submit"}]}

    fake_runtime = FakeRuntime()

    async def fake_get_or_restore(session_id: str):
        assert session_id == "sess-10"
        return fake_runtime

    service.get_or_restore_runtime = fake_get_or_restore  # type: ignore[method-assign]

    wait_result = await service.command("sess-10", "wait", {"selector": "#app", "timeout_ms": 5000})
    extract_result = await service.command("sess-10", "extract", {"mode": "text", "max_chars": 100})
    discover_result = await service.command("sess-10", "discover", {"max_results": 5})

    assert wait_result["status"] == "ready"
    assert extract_result["content"] == "hello"
    assert discover_result["elements"][0]["suggested_selector"] == "button#submit"
    assert calls == [
        ("wait", {"selector": "#app", "timeout_ms": 5000, "wait_until": "load"}),
        ("extract", {"selector": None, "mode": "text", "max_chars": 100}),
        ("discover", {"selector": "a,button,input,textarea,select,[role='button']", "max_results": 5}),
    ]


@pytest.mark.asyncio
async def test_browser_service_runs_upload_attachment():
    from brain.platform.browser.service import BrowserSessionService

    service = BrowserSessionService()
    calls = []

    class FakeRuntime:
        async def upload_attachment(self, **kwargs):
            calls.append(("upload_attachment", kwargs))
            return {"status": "ready", "downloads": []}

    fake_runtime = FakeRuntime()

    async def fake_get_or_restore(session_id: str):
        assert session_id == "sess-upload"
        return fake_runtime

    service.get_or_restore_runtime = fake_get_or_restore  # type: ignore[method-assign]

    result = await service.command(
        "sess-upload",
        "upload_attachment",
        {"selector": "input[type='file']", "attachment_url": "/static/uploads/demo.txt"},
    )

    assert result["status"] == "ready"
    assert calls == [
        ("upload_attachment", {"selector": "input[type='file']", "attachment_url": "/static/uploads/demo.txt"}),
    ]


@pytest.mark.asyncio
async def test_browser_service_records_browser_harness_resource_summary(monkeypatch):
    from brain.platform.browser.service import BrowserSessionService, BrowserSessionRuntime
    from brain.platform.db.models.browser import BrowserSession

    service = BrowserSessionService()

    async def fake_start(self):
        return None

    service._load_active_session = lambda idea_id: None  # type: ignore[method-assign]
    monkeypatch.setattr(BrowserSessionRuntime, "start", fake_start)

    record = BrowserSession(
        idea_id="idea-harness",
        user_id="user-123",
        run_id=77,
        current_url="https://example.com",
        viewport_width=1280,
        viewport_height=800,
        storage_mode="ephemeral",
        allow_downloads=False,
        allow_file_uploads=True,
        status="starting",
    )

    class _Session:
        def add(self, obj):
            obj.id = "sess-harness"

        def flush(self):
            return None

        def refresh(self, obj):
            return None

        def get(self, model, session_id):
            return record

    class _UOW:
        def __enter__(self):
            self.session = _Session()
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr("brain.platform.browser.service.UnitOfWork", lambda: _UOW())

    runtime = await service.create_or_get_session(
        idea_id="idea-harness",
        user_id="user-123",
        run_id=77,
        url=None,
        storage_mode="ephemeral",
    )

    browser_summary = runtime.resource_summary["browser"]
    assert browser_summary["mode"] == "cold"
    assert browser_summary["warm_start_used"] is False
    assert browser_summary["reason"] == "browser harness dedicated Chrome session"
    assert browser_summary["browser_version"] == "browser-harness+chrome-cdp"
    assert browser_summary["context_mode"] == "ephemeral"


@pytest.mark.asyncio
async def test_browser_service_marks_start_failure_before_reraising(monkeypatch):
    from brain.platform.browser.service import BrowserSessionService, BrowserSessionRuntime
    from brain.platform.db.models.browser import BrowserSession

    service = BrowserSessionService()
    service._load_active_session = lambda idea_id: None  # type: ignore[method-assign]
    handled_errors: list[tuple[str, str]] = []

    async def fake_start(self):
        raise RuntimeError("chrome missing")

    async def fake_handle_error(self, message: str):
        handled_errors.append((self.session_id, message))
        self.status = "error"
        self.last_error = message

    monkeypatch.setattr(BrowserSessionRuntime, "start", fake_start)
    monkeypatch.setattr(BrowserSessionRuntime, "_handle_runtime_error", fake_handle_error)

    record = BrowserSession(
        idea_id="idea-start-fail",
        user_id="user-123",
        run_id=77,
        current_url=None,
        viewport_width=1280,
        viewport_height=800,
        storage_mode="ephemeral",
        allow_downloads=False,
        allow_file_uploads=True,
        status="starting",
    )

    class _Session:
        def add(self, obj):
            obj.id = "sess-start-fail"

        def flush(self):
            return None

        def refresh(self, obj):
            return None

    class _UOW:
        def __enter__(self):
            self.session = _Session()
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr("brain.platform.browser.service.UnitOfWork", lambda: _UOW())
    monkeypatch.setattr("brain.platform.browser.service.BrowserSession", lambda **kwargs: record)

    with pytest.raises(RuntimeError, match="chrome missing"):
        await service.create_or_get_session(
            idea_id="idea-start-fail",
            user_id="user-123",
            run_id=77,
            storage_mode="ephemeral",
        )

    assert handled_errors == [("sess-start-fail", "chrome missing")]
    assert service._runtimes["sess-start-fail"].status == "error"


@pytest.mark.asyncio
async def test_browser_service_recycles_dirty_active_session_before_creating_new_one(monkeypatch):
    from brain.platform.browser.service import BrowserSessionService, BrowserSessionRuntime
    from brain.platform.db.models.browser import BrowserSession

    service = BrowserSessionService()
    dirty_record = BrowserSession(
        idea_id="idea-dirty",
        user_id="user-123",
        run_id=41,
        current_url="https://example.com",
        viewport_width=1280,
        viewport_height=800,
        storage_mode="ephemeral",
        allow_downloads=False,
        allow_file_uploads=True,
        status="error",
        last_error="boom",
        active=True,
    )
    dirty_record.id = "sess-old"

    retired: list[tuple[str, str]] = []

    async def fake_retire(session_id: str, reason: str) -> None:
        retired.append((session_id, reason))

    service._load_active_session = lambda idea_id: dirty_record  # type: ignore[method-assign]
    service._retire_browser_session = fake_retire  # type: ignore[method-assign]

    async def fake_start(self):
        self.status = "ready"
        return None

    monkeypatch.setattr(BrowserSessionRuntime, "start", fake_start)

    class _Session:
        def add(self, obj):
            obj.id = "sess-new"

        def flush(self):
            return None

        def refresh(self, obj):
            return None

        def get(self, model, session_id):
            return dirty_record

    class _UOW:
        def __enter__(self):
            self.session = _Session()
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr("brain.platform.browser.service.UnitOfWork", lambda: _UOW())

    runtime = await service.create_or_get_session(
        idea_id="idea-dirty",
        user_id="user-123",
        run_id=42,
        url=None,
        storage_mode="ephemeral",
    )

    assert retired == [("sess-old", "browser session has a recorded error")]
    assert runtime.session_id == "sess-new"
    assert runtime.status == "ready"


@pytest.mark.asyncio
async def test_browser_service_captures_visible_frame_when_agent_opens_session(monkeypatch):
    from brain.platform.browser.service import BrowserSessionService, BrowserSessionRuntime

    service = BrowserSessionService()
    service._load_active_session = lambda idea_id: None  # type: ignore[method-assign]
    captures: list[tuple[str, str, str | None]] = []

    async def fake_start(self):
        self.status = "ready"
        return None

    async def fake_navigate(self, url):
        self.current_url = url
        self.page_title = "YouTube"
        return self.state_payload()

    async def fake_capture_visible_frame(self, *, reason="visible"):
        captures.append((self.session_id, reason, self.current_url))
        return None

    tool_traces = []

    monkeypatch.setattr(BrowserSessionRuntime, "start", fake_start)
    monkeypatch.setattr(BrowserSessionRuntime, "navigate", fake_navigate)
    monkeypatch.setattr(BrowserSessionRuntime, "capture_visible_frame", fake_capture_visible_frame)
    monkeypatch.setattr(
        "brain.platform.browser.service._record_browser_harness_tool_call",
        lambda **kwargs: tool_traces.append(kwargs),
    )

    class _Session:
        def add(self, obj):
            obj.id = "sess-visible"

        def flush(self):
            return None

        def refresh(self, obj):
            return None

    class _UOW:
        def __enter__(self):
            self.session = _Session()
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr("brain.platform.browser.service.UnitOfWork", lambda: _UOW())

    runtime = await service.create_or_get_session(
        idea_id="idea-visible",
        user_id="user-123",
        run_id=99,
        url="https://www.youtube.com",
        storage_mode="ephemeral",
    )

    assert runtime.session_id == "sess-visible"
    assert captures == [("sess-visible", "created", "https://www.youtube.com")]
    assert tool_traces == [{
        "run_id": 99,
        "idea_id": "idea-visible",
        "session_id": "sess-visible",
        "url": "https://www.youtube.com",
        "action": "open",
        "status": "ready",
    }]


@pytest.mark.asyncio
async def test_browser_frame_event_includes_session_state(monkeypatch):
    import base64

    import brain.platform.browser.service as browser_service
    from brain.platform.browser.service import BrowserSessionRuntime, BrowserSessionService

    service = BrowserSessionService()
    record = SimpleNamespace(
        id="sess-frame",
        idea_id="idea-frame",
        user_id="user-123",
        run_id=101,
        viewport_width=1280,
        viewport_height=800,
        status="ready",
        current_url="about:blank",
        page_title=None,
        storage_mode="ephemeral",
        allow_downloads=False,
        allow_file_uploads=True,
        last_error=None,
        _idea_org_id="org-frame",
    )
    runtime = BrowserSessionRuntime(service, record)
    events = []

    async def fake_start():
        return None

    async def fake_run_json(script, timeout=None):
        return {
            "image": base64.b64encode(b"frame-bytes").decode("ascii"),
            "info": {"url": "https://example.com", "title": "Example", "w": 100, "h": 50},
        }

    async def fake_persist_state(**updates):
        return None

    runtime.start = fake_start  # type: ignore[method-assign]
    runtime._run_json = fake_run_json  # type: ignore[method-assign]
    runtime._persist_state = fake_persist_state  # type: ignore[method-assign]
    monkeypatch.setattr(browser_service, "publish_safe", lambda event_type, data: events.append((event_type, data)))

    frame = await runtime._capture_frame(force=True)

    frame_events = [payload for event_type, payload in events if event_type == "browser_session_frame"]
    assert frame.image_url.startswith("data:image/png;base64,")
    assert frame_events
    assert frame_events[-1]["session_id"] == "sess-frame"
    assert frame_events[-1]["org_id"] == "org-frame"
    assert frame_events[-1]["state"]["current_url"] == "https://example.com"
    assert frame_events[-1]["state"]["org_id"] == "org-frame"
    assert frame_events[-1]["state"]["page_title"] == "Example"


@pytest.mark.asyncio
async def test_browser_service_runs_export_commands():
    from brain.platform.browser.service import BrowserSessionService

    service = BrowserSessionService()
    calls = []

    class FakeRuntime:
        async def save_screenshot(self, **kwargs):
            calls.append(("save_screenshot", kwargs))
            return {"artifact": {"kind": "screenshot", "filename": "shot.png"}}

        async def print_pdf(self, **kwargs):
            calls.append(("print_pdf", kwargs))
            return {"artifact": {"kind": "pdf", "filename": "page.pdf"}}

    fake_runtime = FakeRuntime()

    async def fake_get_or_restore(session_id: str):
        assert session_id == "sess-export"
        return fake_runtime

    service.get_or_restore_runtime = fake_get_or_restore  # type: ignore[method-assign]

    screenshot = await service.command("sess-export", "save_screenshot", {"full_page": False})
    pdf = await service.command("sess-export", "print_pdf", {"landscape": True})

    assert screenshot["artifact"]["kind"] == "screenshot"
    assert pdf["artifact"]["kind"] == "pdf"
    assert calls == [
        ("save_screenshot", {"full_page": False}),
        ("print_pdf", {"landscape": True}),
    ]


@pytest.mark.asyncio
async def test_browser_runtime_unsubscribe_schedules_idle_close(monkeypatch):
    from brain.platform.browser.service import BrowserSessionRuntime, BrowserSessionService

    service = BrowserSessionService()
    record = SimpleNamespace(
        id="sess-11",
        idea_id="idea-11",
        user_id="user-123",
        run_id=None,
        viewport_width=1280,
        viewport_height=800,
        status="ready",
        current_url=None,
        page_title=None,
        last_error=None,
    )
    runtime = BrowserSessionRuntime(service, record)
    runtime._watchers = {"user-123"}
    scheduled = []

    def fake_schedule():
        scheduled.append(True)

    monkeypatch.setattr(runtime, "_schedule_idle_close", fake_schedule)
    await runtime.unsubscribe("user-123")
    assert scheduled == [True]


def test_browser_runtime_action_log_is_bounded():
    from brain.platform.browser.service import BrowserSessionRuntime, BrowserSessionService

    service = BrowserSessionService()
    record = SimpleNamespace(
        id="sess-log",
        idea_id="idea-log",
        user_id="user-123",
        run_id=None,
        viewport_width=1280,
        viewport_height=800,
        status="ready",
        current_url=None,
        page_title=None,
        last_error=None,
    )
    runtime = BrowserSessionRuntime(service, record)

    for i in range(35):
        runtime._record_action("test", f"item-{i}")

    payload = runtime.state_payload()
    assert len(payload["actions"]) == 30
    assert payload["actions"][0]["detail"] == "item-5"
    assert payload["actions"][-1]["detail"] == "item-34"


def test_browser_runtime_state_payload_includes_resource_summary():
    from brain.platform.browser.service import BrowserSessionRuntime, BrowserSessionService

    service = BrowserSessionService()
    record = SimpleNamespace(
        id="sess-resource",
        idea_id="idea-resource",
        user_id="user-123",
        run_id=5,
        viewport_width=1280,
        viewport_height=800,
        status="ready",
        current_url=None,
        page_title=None,
        last_error=None,
    )
    runtime = BrowserSessionRuntime(service, record)

    payload = runtime.state_payload()
    assert payload["resource_summary"]["browser"]["mode"] == "cold"
    assert payload["resource_summary"]["browser"]["warm_start_used"] is False


def test_browser_runtime_artifacts_and_diagnostics_are_bounded():
    from brain.platform.browser.service import BrowserSessionRuntime, BrowserSessionService

    service = BrowserSessionService()
    record = SimpleNamespace(
        id="sess-bounds",
        idea_id="idea-bounds",
        user_id="user-123",
        run_id=None,
        viewport_width=1280,
        viewport_height=800,
        status="ready",
        current_url="https://example.com",
        page_title="Example",
        last_error=None,
    )
    runtime = BrowserSessionRuntime(service, record)

    for i in range(30):
        runtime._record_artifact("screenshot", f"shot-{i}.png", f"/static/uploads/shot-{i}.png", size=i)
        runtime._record_console_entry("error", f"console-{i}")
        runtime._record_request_failure("GET", f"https://example.com/{i}", error_text=f"failure-{i}")

    payload = runtime.state_payload()
    assert len(payload["artifacts"]) == 20
    assert payload["artifacts"][0]["filename"] == "shot-10.png"
    assert payload["artifacts"][-1]["filename"] == "shot-29.png"
    assert len(payload["console_messages"]) == 25
    assert payload["console_messages"][0]["text"] == "console-5"
    assert payload["console_messages"][-1]["text"] == "console-29"
    assert len(payload["request_failures"]) == 25
    assert payload["request_failures"][0]["url"] == "https://example.com/5"
    assert payload["request_failures"][-1]["url"] == "https://example.com/29"


def test_browser_runtime_tab_payload_uses_harness_tab_state():
    from brain.platform.browser.service import BrowserSessionRuntime, BrowserSessionService

    service = BrowserSessionService()
    record = SimpleNamespace(
        id="sess-wire",
        idea_id="idea-wire",
        user_id="user-123",
        run_id=None,
        viewport_width=1280,
        viewport_height=800,
        status="ready",
        current_url=None,
        page_title=None,
        storage_mode="ephemeral",
        allow_downloads=True,
        allow_file_uploads=True,
        last_error=None,
    )
    runtime = BrowserSessionRuntime(service, record)
    runtime._update_tabs_from_list([
        {"targetId": "target-1", "title": "Example", "url": "https://example.com"},
        {"targetId": "target-2", "title": "Docs", "url": "https://docs.example.com"},
    ])

    payload = runtime.state_payload()
    assert payload["current_tab_index"] == 0
    assert payload["tabs"] == [
        {"index": 0, "url": "https://example.com", "title": "Example", "active": True},
        {"index": 1, "url": "https://docs.example.com", "title": "Docs", "active": False},
    ]


def test_browser_runtime_resolves_attachment_path(monkeypatch, tmp_path):
    import brain.platform.browser.service as browser_service
    from brain.platform.browser.service import BrowserSessionRuntime, BrowserSessionService

    upload_root = tmp_path / "uploads"
    upload_root.mkdir()
    target = upload_root / "folder" / "demo file.txt"
    target.parent.mkdir()
    target.write_text("hello")

    monkeypatch.setattr(browser_service, "UPLOAD_DIR", upload_root)

    service = BrowserSessionService()
    record = SimpleNamespace(
        id="sess-attach",
        idea_id="idea-attach",
        user_id="user-123",
        run_id=None,
        viewport_width=1280,
        viewport_height=800,
        status="ready",
        current_url=None,
        page_title=None,
        storage_mode="ephemeral",
        allow_downloads=False,
        allow_file_uploads=True,
        last_error=None,
    )
    runtime = BrowserSessionRuntime(service, record)

    resolved = runtime._resolve_attachment_path("https://illo.ai/static/uploads/folder/demo%20file.txt?token=abc")
    assert resolved == target.resolve()

    with pytest.raises(ValueError, match="escapes upload root"):
        runtime._resolve_attachment_path("/static/uploads/../secret.txt")


def test_browser_runtime_uses_short_ipc_dir_for_uuid_sessions(monkeypatch):
    from brain.platform.browser.service import BrowserSessionRuntime, BrowserSessionService

    monkeypatch.setenv("ILLO_BROWSER_IPC_ROOT", "/tmp")
    service = BrowserSessionService()
    record = SimpleNamespace(
        id="8da46698-1d8d-4c7e-9163-5a82422b6bc4",
        idea_id="idea-browser-ipc",
        user_id="user-123",
        run_id=None,
        viewport_width=1280,
        viewport_height=800,
        status="starting",
        current_url=None,
        page_title=None,
        storage_mode="ephemeral",
        allow_downloads=False,
        allow_file_uploads=True,
        last_error=None,
    )

    runtime = BrowserSessionRuntime(service, record)
    socket_path = runtime._harness_ipc_dir / "bu.sock"

    assert str(runtime._harness_ipc_dir).startswith("/tmp/illo-bh/")
    assert "8da46698-1d8d-4c7e-9163-5a82422b6bc4" not in str(runtime._harness_ipc_dir)
    assert len(str(socket_path)) < 100


@pytest.mark.asyncio
async def test_browser_runtime_close_tab_defaults_to_active_tab(monkeypatch):
    from brain.platform.browser.service import BrowserSessionRuntime, BrowserSessionService

    service = BrowserSessionService()
    record = SimpleNamespace(
        id="sess-close-active-tab",
        idea_id="idea-close-active-tab",
        user_id="user-123",
        run_id=None,
        viewport_width=1280,
        viewport_height=800,
        status="ready",
        current_url="https://two.example",
        page_title="Two",
        storage_mode="ephemeral",
        allow_downloads=False,
        allow_file_uploads=True,
        last_error=None,
    )
    runtime = BrowserSessionRuntime(service, record)
    runtime._tabs = [
        {"targetId": "target-1", "url": "https://one.example", "title": "One", "active": False},
        {"targetId": "target-2", "url": "https://two.example", "title": "Two", "active": True},
    ]
    captured_code: list[str] = []

    async def fake_start():
        return None

    async def fake_run_json(code: str, **kwargs):
        captured_code.append(code)
        return {"url": "https://one.example", "title": "One", "w": 1280, "h": 800}

    async def fake_persist_state(**updates):
        for key, value in updates.items():
            if hasattr(runtime, key):
                setattr(runtime, key, value)

    async def fake_list_tabs():
        return runtime.state_payload()

    runtime.start = fake_start  # type: ignore[method-assign]
    runtime._run_json = fake_run_json  # type: ignore[method-assign]
    runtime._persist_state = fake_persist_state  # type: ignore[method-assign]
    runtime._emit_state = lambda reason: None  # type: ignore[assignment]
    runtime.list_tabs = fake_list_tabs  # type: ignore[method-assign]

    await runtime.close_tab()

    assert "target = \"1\"" not in "".join(captured_code)
    assert "tabs[1]" in captured_code[0]
    assert "min(1, len(tabs) - 1)" in captured_code[0]


@pytest.mark.asyncio
async def test_browser_service_close_removes_runtime():
    from brain.platform.browser.service import BrowserSessionService

    service = BrowserSessionService()
    closed_reasons = []

    class FakeRuntime:
        async def close(self, *, reason: str = "closed"):
            closed_reasons.append(reason)

    service._runtimes["sess-close"] = FakeRuntime()

    async def fake_get_or_restore(session_id: str):
        return service._runtimes[session_id]

    service.get_or_restore_runtime = fake_get_or_restore  # type: ignore[method-assign]

    result = await service.command("sess-close", "close", {"reason": "user_closed"})

    assert result == {"closed": True, "session_id": "sess-close"}
    assert closed_reasons == ["user_closed"]
    assert "sess-close" not in service._runtimes


@pytest.mark.asyncio
async def test_browser_service_marks_command_failure_before_reraising():
    from brain.platform.browser.service import BrowserSessionService

    service = BrowserSessionService()
    handled_errors: list[str] = []

    class FakeRuntime:
        _closed = False

        async def navigate(self, url: str):
            raise RuntimeError(f"cannot navigate to {url}")

        async def _handle_runtime_error(self, message: str):
            handled_errors.append(message)

    fake_runtime = FakeRuntime()

    async def fake_get_or_restore(session_id: str):
        assert session_id == "sess-command-fail"
        return fake_runtime

    service.get_or_restore_runtime = fake_get_or_restore  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="cannot navigate"):
        await service.command("sess-command-fail", "navigate", {"url": "https://example.com"})

    assert handled_errors == ["cannot navigate to https://example.com"]


@pytest.mark.asyncio
async def test_browser_service_missing_executable_has_actionable_error(monkeypatch):
    from brain.platform.browser.service import BrowserCapabilityError, BrowserSessionRuntime, BrowserSessionService

    service = BrowserSessionService()
    record = SimpleNamespace(
        id="sess-missing",
        idea_id="idea-missing",
        user_id="user-123",
        run_id=None,
        viewport_width=1280,
        viewport_height=800,
        status="starting",
        current_url=None,
        page_title=None,
        storage_mode="ephemeral",
        allow_downloads=False,
        allow_file_uploads=True,
        last_error=None,
    )
    runtime = BrowserSessionRuntime(service, record)
    runtime._harness_bin = None

    with pytest.raises(BrowserCapabilityError, match="Browser Harness is not installed"):
        await runtime.start()


def test_browser_runtime_prefers_repo_local_chrome_for_testing(monkeypatch, tmp_path):
    import platform
    import sys

    from brain.platform.browser.service import BrowserSessionRuntime, BrowserSessionService

    if sys.platform == "darwin":
        mac_key = "mac-arm64" if platform.machine().lower() in {"arm64", "aarch64"} else "mac-x64"
        chrome = (
            tmp_path
            / f"chrome-{mac_key}"
            / "Google Chrome for Testing.app"
            / "Contents"
            / "MacOS"
            / "Google Chrome for Testing"
        )
    else:
        chrome = tmp_path / "chrome-linux64" / "chrome"
    chrome.parent.mkdir(parents=True)
    chrome.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.delenv("ILLO_BROWSER_CHROME_BIN", raising=False)
    monkeypatch.setenv("ILLO_BROWSER_CHROME_FOR_TESTING_DIR", str(tmp_path))

    service = BrowserSessionService()
    record = SimpleNamespace(
        id="sess-cft",
        idea_id="idea-cft",
        user_id="user-123",
        run_id=None,
        viewport_width=1280,
        viewport_height=800,
        status="starting",
        current_url=None,
        page_title=None,
        storage_mode="ephemeral",
        allow_downloads=False,
        allow_file_uploads=True,
        last_error=None,
    )
    runtime = BrowserSessionRuntime(service, record)

    assert runtime._find_chrome_executable() == str(chrome)


def test_browser_runtime_uses_short_linux_chrome_state_paths(monkeypatch, tmp_path):
    import sys

    from brain.platform.browser.service import BrowserSessionRuntime, BrowserSessionService

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("ILLO_BROWSER_HARNESS_ROOT", str(tmp_path / "bh"))

    service = BrowserSessionService()
    record = SimpleNamespace(
        id="8da46698-1d8d-4c7e-9163-5a82422b6bc4",
        idea_id="4f79322e-4dfa-49ea-a702-8649331e8699",
        user_id="user-123",
        run_id=None,
        viewport_width=1280,
        viewport_height=800,
        status="starting",
        current_url=None,
        page_title=None,
        storage_mode="idea",
        allow_downloads=False,
        allow_file_uploads=True,
        last_error=None,
    )
    runtime = BrowserSessionRuntime(service, record)

    assert runtime._harness_root.parent == tmp_path / "bh" / "s"
    assert runtime._harness_chrome_dir.parent == tmp_path / "bh" / "p"
    assert "8da46698-1d8d-4c7e-9163-5a82422b6bc4" not in str(runtime._harness_root)
    assert "4f79322e-4dfa-49ea-a702-8649331e8699" not in str(runtime._harness_chrome_dir)
    assert len(runtime._harness_root.name) <= 21
    assert len(runtime._harness_chrome_dir.name) <= 21


def test_browser_runtime_repairs_chrome_sidecar_permissions(tmp_path):
    from brain.platform.browser.service import BrowserSessionRuntime, BrowserSessionService

    chrome = tmp_path / "chrome-linux64" / "chrome"
    crashpad = chrome.with_name("chrome_crashpad_handler")
    sandbox = chrome.with_name("chrome_sandbox")
    chrome.parent.mkdir(parents=True)
    for path in (chrome, crashpad, sandbox):
        path.write_text("#!/bin/sh\n", encoding="utf-8")
        path.chmod(0o600)

    service = BrowserSessionService()
    record = SimpleNamespace(
        id="sess-perms",
        idea_id="idea-perms",
        user_id="user-123",
        run_id=None,
        viewport_width=1280,
        viewport_height=800,
        status="starting",
        current_url=None,
        page_title=None,
        storage_mode="ephemeral",
        allow_downloads=False,
        allow_file_uploads=True,
        last_error=None,
    )
    runtime = BrowserSessionRuntime(service, record)

    runtime._ensure_chrome_sidecar_permissions(str(chrome))

    assert chrome.stat().st_mode & 0o100
    assert crashpad.stat().st_mode & 0o100
    assert sandbox.stat().st_mode & 0o100


@pytest.mark.asyncio
async def test_browser_runtime_launches_chrome_with_server_safe_env(monkeypatch, tmp_path):
    import asyncio
    import sys

    import brain.platform.browser.service as browser_service
    from brain.platform.browser.service import BrowserSessionRuntime, BrowserSessionService

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(browser_service, "WORKSPACE_BROWSER_STATE_DIR", str(tmp_path / ".browser-state"))
    chrome = tmp_path / "chrome-linux64" / "chrome"
    chrome.parent.mkdir(parents=True)
    chrome.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv("ILLO_BROWSER_CHROME_FOR_TESTING_DIR", str(tmp_path))
    monkeypatch.delenv("ILLO_BROWSER_CHROME_BIN", raising=False)

    service = BrowserSessionService()
    record = SimpleNamespace(
        id="sess-linux-env",
        idea_id="idea-linux-env",
        user_id="user-123",
        run_id=None,
        viewport_width=1280,
        viewport_height=800,
        status="starting",
        current_url=None,
        page_title=None,
        storage_mode="ephemeral",
        allow_downloads=False,
        allow_file_uploads=True,
        last_error=None,
    )
    runtime = BrowserSessionRuntime(service, record)
    captured: dict[str, object] = {}

    class FakeProcess:
        returncode = None
        stderr = None

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["args"] = args
        captured["env"] = kwargs.get("env")
        return FakeProcess()

    async def fake_wait_for_cdp():
        return None

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    runtime._wait_for_cdp = fake_wait_for_cdp  # type: ignore[method-assign]

    await runtime._ensure_chrome_process()

    args = list(captured["args"])
    env = captured["env"]
    assert args[0] == str(chrome)
    assert "--remote-debugging-address=127.0.0.1" in args
    assert "--disable-gpu" in args
    assert "--disable-breakpad" in args
    assert "--disable-component-update" in args
    assert any("OptimizationGuideModelDownloading" in str(arg) for arg in args)
    assert any(str(arg).startswith("--crash-dumps-dir=") for arg in args)
    assert isinstance(env, dict)
    assert env["HOME"].startswith(str(runtime._harness_root))
    assert env["XDG_CONFIG_HOME"].startswith(str(runtime._harness_root))
    assert env["XDG_CACHE_HOME"].startswith(str(runtime._harness_root))
    assert env["XDG_RUNTIME_DIR"].startswith(str(runtime._harness_root))
    assert Path(env["XDG_RUNTIME_DIR"]).stat().st_mode & 0o777 == 0o700


@pytest.mark.asyncio
async def test_browser_runtime_falls_back_when_preferred_chrome_cannot_launch(monkeypatch):
    import asyncio

    from brain.platform.browser.service import BrowserSessionRuntime, BrowserSessionService

    service = BrowserSessionService()
    record = SimpleNamespace(
        id="sess-chrome-fallback",
        idea_id="idea-chrome-fallback",
        user_id="user-123",
        run_id=None,
        viewport_width=1280,
        viewport_height=800,
        status="starting",
        current_url=None,
        page_title=None,
        storage_mode="ephemeral",
        allow_downloads=False,
        allow_file_uploads=True,
        last_error=None,
    )
    runtime = BrowserSessionRuntime(service, record)
    launched: list[str] = []

    class FakeProcess:
        returncode = None
        stderr = None

    async def fake_create_subprocess_exec(*args, **kwargs):
        launched.append(str(args[0]))
        if args[0] == "/bad/chrome":
            raise OSError("permission denied")
        return FakeProcess()

    async def fake_wait_for_cdp():
        return None

    runtime._chrome_executable_candidates = lambda: ["/bad/chrome", "/good/chrome"]  # type: ignore[method-assign]
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    runtime._wait_for_cdp = fake_wait_for_cdp  # type: ignore[method-assign]

    await runtime._ensure_chrome_process()

    assert launched == ["/bad/chrome", "/good/chrome"]
    assert runtime._chrome_executable == "/good/chrome"


def test_browser_runtime_stderr_summary_keeps_crash_root_cause():
    from brain.platform.browser.service import BrowserSessionRuntime, BrowserSessionService

    service = BrowserSessionService()
    record = SimpleNamespace(
        id="sess-stderr",
        idea_id="idea-stderr",
        user_id="user-123",
        run_id=None,
        viewport_width=1280,
        viewport_height=800,
        status="starting",
        current_url=None,
        page_title=None,
        storage_mode="ephemeral",
        allow_downloads=False,
        allow_file_uploads=True,
        last_error=None,
    )
    runtime = BrowserSessionRuntime(service, record)
    stderr = "chrome_crashpad_handler: --database is required\n" + "\n".join(
        f"stack frame {idx}" for idx in range(500)
    )

    summary = runtime._summarize_chrome_stderr(stderr)

    assert "chrome_crashpad_handler: --database is required" in summary
    assert "stack frame 499" in summary
    assert len(summary) < len(stderr)


def test_ws_browser_history_commands(monkeypatch):
    from brain.app.api.routers import ws as ws_router

    commands: list[tuple[str, str, dict]] = []

    async def fake_subscribe(session_id: str, user_id: str | None):
        return {"id": session_id}

    async def fake_command(session_id: str, action: str, payload: dict):
        commands.append((session_id, action, payload))
        return {"status": "ready"}

    monkeypatch.setattr(ws_router.browser_sessions, "subscribe", fake_subscribe)
    monkeypatch.setattr(ws_router.browser_sessions, "command", fake_command)
    _allow_ws_browser_session(monkeypatch, ws_router)

    client = TestClient(app)
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "connected"
        ws.send_json({"type": "auth", "token": _ws_token()})
        assert ws.receive_json()["type"] == "authenticated"
        ws.send_json({"type": "browser_back", "session_id": "sess-9"})
        ws.send_json({"type": "browser_forward", "session_id": "sess-9"})
        assert ws.receive_json()["type"] == "browser_session_delta"
        assert ws.receive_json()["type"] == "browser_session_delta"

    assert commands == [
        ("sess-9", "back", {}),
        ("sess-9", "forward", {}),
    ]


def test_ws_browser_export_commands(monkeypatch):
    from brain.app.api.routers import ws as ws_router

    commands: list[tuple[str, str, dict]] = []

    async def fake_subscribe(session_id: str, user_id: str | None):
        return {"id": session_id}

    async def fake_command(session_id: str, action: str, payload: dict):
        commands.append((session_id, action, payload))
        return {"artifact": {"kind": "screenshot"}}

    monkeypatch.setattr(ws_router.browser_sessions, "subscribe", fake_subscribe)
    monkeypatch.setattr(ws_router.browser_sessions, "command", fake_command)
    _allow_ws_browser_session(monkeypatch, ws_router)

    client = TestClient(app)
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "connected"
        ws.send_json({"type": "auth", "token": _ws_token()})
        assert ws.receive_json()["type"] == "authenticated"
        ws.send_json({"type": "browser_save_screenshot", "session_id": "sess-9", "full_page": False})
        ws.send_json({"type": "browser_print_pdf", "session_id": "sess-9", "landscape": True})
        assert ws.receive_json()["type"] == "browser_session_delta"
        assert ws.receive_json()["type"] == "browser_session_delta"

    assert commands == [
        ("sess-9", "save_screenshot", {"full_page": False}),
        ("sess-9", "print_pdf", {"landscape": True}),
    ]


def test_ws_browser_inspection_commands(monkeypatch):
    from brain.app.api.routers import ws as ws_router

    commands: list[tuple[str, str, dict]] = []

    async def fake_subscribe(session_id: str, user_id: str | None):
        return {"id": session_id}

    async def fake_command(session_id: str, action: str, payload: dict):
        commands.append((session_id, action, payload))
        if action == "discover":
            return {"elements": [{"suggested_selector": "button#submit"}]}
        return {"content": "page text"}

    monkeypatch.setattr(ws_router.browser_sessions, "subscribe", fake_subscribe)
    monkeypatch.setattr(ws_router.browser_sessions, "command", fake_command)
    _allow_ws_browser_session(monkeypatch, ws_router)

    client = TestClient(app)
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "connected"
        ws.send_json({"type": "auth", "token": _ws_token()})
        assert ws.receive_json()["type"] == "authenticated"
        ws.send_json({"type": "browser_discover", "session_id": "sess-9", "max_results": 5})
        ws.send_json({"type": "browser_extract", "session_id": "sess-9", "mode": "text", "max_chars": 1000})
        assert ws.receive_json()["type"] == "browser_session_delta"
        assert ws.receive_json()["type"] == "browser_session_delta"

    assert commands == [
        ("sess-9", "discover", {"max_results": 5}),
        ("sess-9", "extract", {"mode": "text", "max_chars": 1000}),
    ]


@pytest.mark.asyncio
async def test_browser_service_runs_tab_commands():
    from brain.platform.browser.service import BrowserSessionService

    service = BrowserSessionService()
    calls = []

    class FakeRuntime:
        async def new_tab(self, url=None):
            calls.append(("new_tab", {"url": url}))
            return {"current_tab_index": 1}

        async def switch_tab(self, index: int):
            calls.append(("switch_tab", {"index": index}))
            return {"current_tab_index": index}

        async def close_tab(self, index=None):
            calls.append(("close_tab", {"index": index}))
            return {"current_tab_index": 0}

        async def list_tabs(self):
            calls.append(("list_tabs", {}))
            return {"tabs": [{"index": 0, "active": True}]}

    fake_runtime = FakeRuntime()

    async def fake_get_or_restore(session_id: str):
        assert session_id == "sess-tabs"
        return fake_runtime

    service.get_or_restore_runtime = fake_get_or_restore  # type: ignore[method-assign]

    new_tab = await service.command("sess-tabs", "new_tab", {"url": "https://example.com"})
    switched = await service.command("sess-tabs", "switch_tab", {"index": 0})
    listed = await service.command("sess-tabs", "list_tabs", {})
    closed = await service.command("sess-tabs", "close_tab", {})

    assert new_tab["current_tab_index"] == 1
    assert switched["current_tab_index"] == 0
    assert listed["tabs"][0]["active"] is True
    assert closed["current_tab_index"] == 0
    assert calls == [
        ("new_tab", {"url": "https://example.com"}),
        ("switch_tab", {"index": 0}),
        ("list_tabs", {}),
        ("close_tab", {"index": None}),
    ]
