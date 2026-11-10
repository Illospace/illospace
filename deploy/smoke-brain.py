"""A model-free fixture for deployment tests before the brain area merges."""

import hmac
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        """Do not put a run token in a test log."""

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}')

    def do_POST(self):
        supplied = self.headers.get("X-Illo-Brain-Secret", "")
        expected = os.environ["ILLO_BRAIN_SECRET"]
        if not hmac.compare_digest(supplied, expected):
            self.send_error(401)
            return
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path != "/run" or request["runtime"] != "echo":
            self.send_error(400)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()
        for event in ({"type": "started"}, {"type": "final", "text": request["user_message"]}):
            self.wfile.write(json.dumps(event).encode() + b"\n")


ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
