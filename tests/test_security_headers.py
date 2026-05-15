"""Tests for security headers middleware added to FastAPI."""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Patch runtime side effects before importing app.
with patch("brain.systems.runs.cortex.ensure_schema"), \
     patch("brain.systems.runs.cortex.start_runner"):
    from brain.app.api.main import app

from starlette.testclient import TestClient

client = TestClient(app, raise_server_exceptions=False)


class TestSecurityHeaders:
    """Verify security headers are set on responses."""

    def test_docs_security_headers(self):
        resp = client.get("/api/docs")
        csp = resp.headers["Content-Security-Policy"]
        assert "default-src 'self'" in csp
        assert "script-src" in csp
        assert "connect-src 'self' ws: wss:" in csp
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"
        assert resp.headers.get("X-Frame-Options") == "SAMEORIGIN"
        assert resp.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"
        assert "camera=()" in resp.headers.get("Permissions-Policy", "")
        assert resp.headers.get("X-XSS-Protection") == "1; mode=block"

    def test_headers_on_api_routes(self):
        """Security headers should be on API routes too, not just docs."""
        resp = client.get("/api/health")
        assert "Content-Security-Policy" in resp.headers
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"
