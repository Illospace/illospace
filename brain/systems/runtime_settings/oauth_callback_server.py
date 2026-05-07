from __future__ import annotations

import logging
import os
import threading
import time
from html import escape
from json import dumps
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse, urlunparse

logger = logging.getLogger(__name__)

CALLBACK_HOST = "localhost"
CALLBACK_PORT = 1455
CALLBACK_PATH = "/auth/callback"
CALLBACK_REDIRECT_URI = f"http://{CALLBACK_HOST}:{CALLBACK_PORT}{CALLBACK_PATH}"
CALLBACK_TTL_SEC = 30 * 60


@dataclass(frozen=True)
class CallbackServerStatus:
    available: bool
    redirect_uri: str = CALLBACK_REDIRECT_URI
    detail: str | None = None


@dataclass(frozen=True)
class CallbackTarget:
    return_url: str
    created_at: float


class _CallbackHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


_lock = threading.RLock()
_server: _CallbackHTTPServer | None = None
_thread: threading.Thread | None = None
_targets: dict[str, CallbackTarget] = {}
_last_error: str | None = None


def ensure_callback_server() -> CallbackServerStatus:
    if os.getenv("ILLO_OPENAI_OAUTH_CALLBACK_SERVER", "1").strip().lower() in {"0", "false", "no", "off"}:
        return CallbackServerStatus(available=False, detail="Local callback bridge is disabled.")

    global _last_error, _server, _thread
    with _lock:
        if _server is not None and _thread is not None and _thread.is_alive():
            return CallbackServerStatus(available=True)

        try:
            server = _CallbackHTTPServer((CALLBACK_HOST, CALLBACK_PORT), _CallbackRequestHandler)
        except OSError as exc:
            _last_error = f"Could not listen on {CALLBACK_REDIRECT_URI}: {exc}"
            logger.info("openai_oauth_callback_server_unavailable", extra={"error": _last_error})
            return CallbackServerStatus(available=False, detail=_last_error)

        thread = threading.Thread(target=server.serve_forever, name="openai-oauth-callback", daemon=True)
        thread.start()
        _server = server
        _thread = thread
        _last_error = None
        return CallbackServerStatus(available=True)


def register_callback_target(state: str, return_url: str) -> None:
    clean_state = (state or "").strip()
    clean_return_url = (return_url or "").strip()
    if not clean_state or not clean_return_url:
        return
    with _lock:
        _cleanup_locked()
        _targets[clean_state] = CallbackTarget(return_url=clean_return_url, created_at=time.time())


def clear_callback_target(state: str | None) -> None:
    clean_state = (state or "").strip()
    if not clean_state:
        return
    with _lock:
        _targets.pop(clean_state, None)


def _callback_target(state: str | None) -> CallbackTarget | None:
    clean_state = (state or "").strip()
    if not clean_state:
        return None
    with _lock:
        _cleanup_locked()
        return _targets.get(clean_state)


def _cleanup_locked() -> None:
    cutoff = time.time() - CALLBACK_TTL_SEC
    expired = [state for state, target in _targets.items() if target.created_at < cutoff]
    for state in expired:
        _targets.pop(state, None)


def _redirect_url(return_url: str, query: str) -> str:
    parsed = urlparse(return_url)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path or CALLBACK_PATH, "", query, ""))


class _CallbackRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.rstrip("/") != CALLBACK_PATH:
            self._send_html(HTTPStatus.NOT_FOUND, "OpenAI callback not found.", "Return to System and start sign-in again.")
            return

        params = parse_qs(parsed.query)
        state = (params.get("state") or [""])[0]
        target = _callback_target(state)
        if target is None:
            self._send_callback_fallback(state=state, callback_url=self._request_url())
            return

        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", _redirect_url(target.return_url, parsed.query))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        logger.debug("openai_oauth_callback_server_request", extra={"message": format % args})

    def _request_url(self) -> str:
        host = (self.headers.get("Host") or f"{CALLBACK_HOST}:{CALLBACK_PORT}").strip()
        return f"http://{host}{self.path}"

    def _send_callback_fallback(self, *, state: str, callback_url: str) -> None:
        payload = {
            "type": "illo:openai-oauth",
            "status": "callback",
            "state": state or "",
            "callback": callback_url,
        }
        payload_json = (
            dumps(payload, separators=(",", ":"))
            .replace("<", "\\u003c")
            .replace(">", "\\u003e")
            .replace("&", "\\u0026")
        )
        title = "OpenAI sign-in returned here."
        detail = (
            "Illo could not match this callback in the local bridge. "
            "If the System tab does not finish automatically, copy this callback URL "
            "and paste it into the manual callback field."
        )
        body = f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{escape(title)}</title>
  </head>
  <body>
    <main style="font-family: system-ui, sans-serif; max-width: 680px; margin: 12vh auto; line-height: 1.5;">
      <h1>{escape(title)}</h1>
      <p>{escape(detail)}</p>
      <label for="callback-url" style="display:block; font-weight:600; margin-bottom:8px;">Callback URL</label>
      <textarea id="callback-url" readonly rows="5" style="box-sizing:border-box; width:100%; font: 13px ui-monospace, SFMono-Regular, Menlo, monospace;">{escape(callback_url)}</textarea>
      <button id="copy-callback" type="button" style="margin-top:12px; padding:8px 12px;">Copy callback URL</button>
      <p id="copy-status" aria-live="polite" style="color:#555;"></p>
    </main>
    <script>
      const payload = {payload_json};
      try {{
        if (window.opener && !window.opener.closed) {{
          window.opener.postMessage(payload, "*");
        }}
      }} catch {{}}
      try {{
        const channel = new BroadcastChannel("illo:openai-oauth");
        channel.postMessage(payload);
        channel.close();
      }} catch {{}}
      document.getElementById("copy-callback")?.addEventListener("click", async () => {{
        const text = document.getElementById("callback-url")?.value || window.location.href;
        try {{
          await navigator.clipboard.writeText(text);
          document.getElementById("copy-status").textContent = "Copied.";
        }} catch {{
          document.getElementById("callback-url")?.select();
          document.getElementById("copy-status").textContent = "Select and copy the URL above.";
        }}
      }});
    </script>
  </body>
</html>"""
        encoded = body.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def _send_html(self, status: HTTPStatus, title: str, detail: str) -> None:
        body = f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{title}</title>
  </head>
  <body>
    <main style="font-family: system-ui, sans-serif; max-width: 520px; margin: 12vh auto; line-height: 1.5;">
      <h1>{title}</h1>
      <p>{detail}</p>
    </main>
  </body>
</html>"""
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)


__all__ = [
    "CALLBACK_REDIRECT_URI",
    "CallbackServerStatus",
    "clear_callback_target",
    "ensure_callback_server",
    "register_callback_target",
]
