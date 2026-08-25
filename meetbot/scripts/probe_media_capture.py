"""Probe Chromium media capture with the meetbot browser configuration.

This standalone probe exists to settle #847. It launches Chromium with the
exact flags and context options the engine uses, then calls getUserMedia and
prints a JSON verdict.

getUserMedia exists only in a secure context, and a data: page is NOT one —
probing there rejects with a TypeError that proves nothing about permissions.
The probe therefore serves its page from 127.0.0.1, which the spec treats as
a potentially trustworthy origin, and reports a missing mediaDevices API as
its own distinct outcome.
"""

from __future__ import annotations

import asyncio
import http.server
import json
import threading
from typing import Any

from meetbot.config import MeetbotConfig


_PROBE_HTML = """<!doctype html>
<meta charset="utf-8">
<script>
window.mediaCaptureVerdict = (async () => {
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    return {
      outcome: "no-mediadevices",
      error_name: null,
      device_labels: [],
      secure_context: window.isSecureContext,
    };
  }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: true,
      video: true,
    });
    const deviceLabels = stream.getTracks().map((track) => track.label);
    stream.getTracks().forEach((track) => track.stop());
    return {
      outcome: "resolved",
      error_name: null,
      device_labels: deviceLabels,
      secure_context: window.isSecureContext,
    };
  } catch (error) {
    return {
      outcome: "rejected",
      error_name: error?.name ?? "Error",
      device_labels: [],
      secure_context: window.isSecureContext,
    };
  }
})();
</script>
"""


class _ProbeHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - http.server contract
        body = _PROBE_HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        return


async def _probe() -> dict[str, Any]:
    from playwright.async_api import async_playwright

    config = MeetbotConfig.from_env()
    context_options: dict[str, object] = {
        "viewport": {"width": 1280, "height": 720},
        "permissions": [],
        "locale": config.ui_locale,
        "extra_http_headers": {
            "Accept-Language": f"{config.ui_locale},en;q=0.9",
        },
    }
    if config.storage_state_path.is_file():
        context_options["storage_state"] = str(config.storage_state_path)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _ProbeHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=False,
                args=[
                    "--use-fake-ui-for-media-stream",
                    "--use-fake-device-for-media-stream",
                    "--disable-dev-shm-usage",
                    "--no-sandbox",
                    f"--lang={config.ui_locale}",
                ],
            )
            context = await browser.new_context(**context_options)
            try:
                page = await context.new_page()
                await page.goto(
                    f"http://127.0.0.1:{port}/probe", wait_until="load"
                )
                return await page.evaluate("window.mediaCaptureVerdict")
            finally:
                await context.close()
                await browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)


def main() -> None:
    print(json.dumps(asyncio.run(_probe()), sort_keys=True))


if __name__ == "__main__":
    main()
