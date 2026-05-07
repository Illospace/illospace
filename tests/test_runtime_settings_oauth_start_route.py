from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from starlette.testclient import TestClient

from brain.app.api.deps import rate_limit
from brain.systems.runtime_settings import router as runtime_router


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(runtime_router.router)
    app.dependency_overrides[runtime_router._runtime_user] = lambda: SimpleNamespace(id="user-1")
    app.dependency_overrides[rate_limit] = lambda: None
    return TestClient(app, raise_server_exceptions=False)


def _oauth_response(mode: str = "server") -> dict[str, object]:
    return {
        "url": "https://auth.openai.com/oauth/authorize?state=state-123",
        "state": "state-123",
        "redirect_uri": "http://testserver/auth/callback",
        "expires_in_seconds": 1800,
        "callback_available": True,
        "callback_detail": None,
        "callback_mode": mode,
    }


def test_openai_oauth_start_accepts_empty_post_body():
    with patch("brain.systems.runtime_settings.router.start_openai_oauth", return_value=_oauth_response()) as start:
        response = _client().post("/api/runtime-settings/connection/openai/oauth/start")

    assert response.status_code == 200
    assert response.json()["state"] == "state-123"
    assert start.call_args.kwargs["callback_mode"] == "auto"


def test_openai_oauth_start_accepts_valid_callback_mode_json():
    with patch("brain.systems.runtime_settings.router.start_openai_oauth", return_value=_oauth_response("local_bridge")) as start:
        response = _client().post(
            "/api/runtime-settings/connection/openai/oauth/start",
            json={"callback_mode": " local_bridge "},
        )

    assert response.status_code == 200
    assert start.call_args.kwargs["callback_mode"] == "local_bridge"


def test_openai_oauth_start_falls_back_for_unrecognized_body():
    cases = [
        {"callback_mode": "server_callback"},
        {"callback_mode": {"callback_mode": "server"}},
        {"callback_mode": ["server"]},
        {"callback_mode": True},
        {"callback_mode": None},
        {"callbackMode": "server"},
        ["server"],
        "server",
    ]
    for payload in cases:
        with patch("brain.systems.runtime_settings.router.start_openai_oauth", return_value=_oauth_response()) as start:
            response = _client().post("/api/runtime-settings/connection/openai/oauth/start", json=payload)

        assert response.status_code == 200
        assert start.call_args.kwargs["callback_mode"] == "auto"


def test_openai_oauth_start_falls_back_for_malformed_json():
    with patch("brain.systems.runtime_settings.router.start_openai_oauth", return_value=_oauth_response()) as start:
        response = _client().post(
            "/api/runtime-settings/connection/openai/oauth/start",
            content="{not json",
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 200
    assert start.call_args.kwargs["callback_mode"] == "auto"
