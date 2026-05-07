from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import pytest


def _browser_harness_launch_is_blocked(exc: BaseException) -> bool:
    text = str(exc)
    markers = [
        "Browser Harness is not installed",
        "No Chrome/Chromium executable found",
        "Chrome exited early",
        "Timed out waiting for Chrome CDP endpoint",
        "MachPortRendezvousServer",
        "Permission denied",
    ]
    return any(marker in text for marker in markers)


@pytest.mark.asyncio
@pytest.mark.requires_browser
async def test_browser_runtime_smoke(monkeypatch, tmp_path):
    import brain.platform.browser.service as browser_service
    from brain.platform.browser.service import BrowserSessionRuntime, BrowserSessionService

    upload_root = tmp_path / "uploads"
    upload_root.mkdir()
    monkeypatch.setattr(browser_service, "UPLOAD_DIR", upload_root)
    monkeypatch.setattr(browser_service, "WORKSPACE_BROWSER_STATE_DIR", str(tmp_path / ".browser-state"))
    monkeypatch.setattr(browser_service, "publish_safe", lambda *args, **kwargs: None)

    smoke_dir = upload_root / "smoke"
    smoke_dir.mkdir()
    attachment = smoke_dir / "demo.txt"
    attachment.write_text("browser upload smoke", encoding="utf-8")

    service = BrowserSessionService()
    service.nav_timeout_ms = 8000
    service.action_timeout_ms = 8000
    record = SimpleNamespace(
        id=str(uuid.uuid4()),
        idea_id="idea-smoke",
        user_id="user-123",
        run_id=None,
        viewport_width=1280,
        viewport_height=800,
        status="starting",
        current_url=None,
        page_title=None,
        storage_mode="idea",
        allow_downloads=True,
        allow_file_uploads=True,
        last_error=None,
    )
    runtime = BrowserSessionRuntime(service, record)

    async def fake_persist_state(**updates):
        for key, value in updates.items():
            if hasattr(runtime, key):
                setattr(runtime, key, value)

    runtime._persist_state = fake_persist_state  # type: ignore[method-assign]
    runtime._emit_state = lambda reason: None  # type: ignore[assignment]

    try:
        try:
            await runtime.start()
        except Exception as exc:
            if _browser_harness_launch_is_blocked(exc):
                pytest.skip(f"Browser Harness launch unavailable in this environment: {exc}")
            raise

        html = """
        <!doctype html>
        <html>
          <head><title>Browser Harness Smoke</title></head>
          <body>
            <h1>Browser Harness smoke</h1>
            <input id="field" />
            <input id="upload" type="file" />
            <button id="submit" onclick="document.querySelector('#output').textContent = document.querySelector('#field').value">Submit</button>
            <a id="download" download="report.txt" href="data:text/plain,downloaded-from-browser">Download</a>
            <output id="output"></output>
          </body>
        </html>
        """
        data_url = "data:text/html;charset=utf-8," + quote(html)
        info = await runtime._run_json(f"""
goto_url({json.dumps(data_url)})
wait_for_load(5)
result = page_info()
""")
        await runtime._apply_page_info(info, status="ready")

        extract = await runtime.extract(mode="text", max_chars=2000)
        discover = await runtime.discover(selector="button,a,input", max_results=10)
        await runtime.type_text("hello from smoke", selector="#field")
        typed_value = await runtime._run_json(
            'result = js("return document.querySelector(\\"#field\\").value")'
        )

        await runtime.upload_attachment(selector="#upload", attachment_url="/static/uploads/smoke/demo.txt")
        uploaded_name = await runtime._run_json(
            'result = js("const el = document.querySelector(\\"#upload\\"); return el.files && el.files[0] ? el.files[0].name : \\"\\";")'
        )

        await runtime.click(selector="#submit")
        clicked_value = await runtime._run_json(
            'result = js("return document.querySelector(\\"#output\\").textContent")'
        )
        await runtime.click(selector="#download")

        screenshot = await runtime.save_screenshot(full_page=True)
        pdf = await runtime.print_pdf()
        snapshot = await runtime.snapshot()
        await runtime.close(reason="browser_smoke_complete")

        assert "Browser Harness smoke" in extract["content"]
        assert discover["count"] >= 4
        assert typed_value == "hello from smoke"
        assert uploaded_name == "demo.txt"
        assert clicked_value == "hello from smoke"
        assert len(runtime._downloads) == 1
        assert runtime._downloads[0].filename == "report.txt"
        assert screenshot["artifact"]["kind"] == "screenshot"
        assert pdf["artifact"]["kind"] == "pdf"
        assert len(runtime._artifacts) == 2
        assert snapshot["frame"]["image_url"].startswith("data:image/png;base64,")
        assert runtime._harness_chrome_dir.exists()
        assert Path(runtime._harness_bin or "").exists()
    finally:
        if not runtime._closed:
            await runtime.close(reason="browser_smoke_cleanup")
