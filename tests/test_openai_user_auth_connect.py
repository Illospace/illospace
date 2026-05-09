import json
import threading
import time
from types import SimpleNamespace
from urllib.request import urlopen
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from brain.platform.integrations.openai_codex_auth import OpenAICodexCredential
from brain.systems.runtime_settings.schemas import RuntimeConnectionRead


class _RequestStub:
    def __init__(self, *, headers=None, base_url="http://127.0.0.1:5176/", session=None):
        self.headers = headers or {}
        self.base_url = base_url
        self.session = session or {}


def test_start_openai_oauth_stashes_pkce_state_in_runtime_settings_session():
    from brain.systems.runtime_settings.auth import OPENAI_OAUTH_SESSION_KEY, start_openai_oauth
    from brain.systems.runtime_settings.oauth_callback_server import CallbackServerStatus

    request = _RequestStub(headers={"origin": "http://127.0.0.1:5176"}, session={})

    with patch(
        "brain.systems.runtime_settings.auth.ensure_callback_server",
        return_value=CallbackServerStatus(available=True),
    ), patch(
        "brain.systems.runtime_settings.auth.build_codex_oauth_authorize_url",
        return_value=("https://auth.openai.com/oauth/authorize?state=state-123", "state-123", "verifier-123"),
    ), patch("brain.systems.runtime_settings.auth.register_callback_target") as register_target:
        result = start_openai_oauth(request=request)

    assert result["url"] == "https://auth.openai.com/oauth/authorize?state=state-123"
    assert result["redirect_uri"] == "http://localhost:1455/auth/callback"
    assert result["state"] == "state-123"
    assert result["callback_available"] is True
    assert request.session[OPENAI_OAUTH_SESSION_KEY]["state"] == "state-123"
    assert request.session[OPENAI_OAUTH_SESSION_KEY]["code_verifier"] == "verifier-123"
    assert request.session[OPENAI_OAUTH_SESSION_KEY]["created_at"] is not None
    assert request.session[OPENAI_OAUTH_SESSION_KEY]["return_url"] == "http://127.0.0.1:5176/auth/callback"
    register_target.assert_called_once_with("state-123", "http://127.0.0.1:5176/auth/callback")


def test_start_openai_oauth_uses_localhost_fallback_for_public_base_url_by_default():
    from brain.systems.runtime_settings.auth import OPENAI_OAUTH_SESSION_KEY, start_openai_oauth

    request = _RequestStub(
        headers={"origin": "https://illo.example.com"},
        base_url="https://illo.example.com/",
        session={},
    )

    with patch(
        "brain.systems.runtime_settings.auth.ensure_callback_server",
    ) as ensure_callback, patch(
        "brain.systems.runtime_settings.auth.build_codex_oauth_authorize_url",
        return_value=("https://auth.openai.com/oauth/authorize?state=state-123", "state-123", "verifier-123"),
    ) as mock_build, patch("brain.systems.runtime_settings.auth.register_callback_target") as register_target:
        result = start_openai_oauth(request=request)

    assert result["redirect_uri"] == "http://localhost:1455/auth/callback"
    assert result["callback_available"] is False
    assert result["callback_mode"] == "local_bridge"
    assert "localhost:1455" in str(result["callback_detail"])
    assert "paste" in str(result["callback_detail"]).lower()
    assert request.session[OPENAI_OAUTH_SESSION_KEY]["redirect_uri"] == "http://localhost:1455/auth/callback"
    assert request.session[OPENAI_OAUTH_SESSION_KEY]["return_url"] == "https://illo.example.com/auth/callback"
    assert request.session[OPENAI_OAUTH_SESSION_KEY]["callback_mode"] == "local_bridge"
    mock_build.assert_called_once_with(redirect_uri="http://localhost:1455/auth/callback")
    ensure_callback.assert_not_called()
    register_target.assert_not_called()


def test_start_openai_oauth_disables_local_bridge_for_compose_loopback_origin(monkeypatch):
    from brain.systems.runtime_settings.auth import OPENAI_OAUTH_SESSION_KEY, start_openai_oauth

    monkeypatch.setenv("ILLO_OPENAI_OAUTH_LOCAL_BRIDGE", "0")
    request = _RequestStub(
        headers={"origin": "http://localhost:8080"},
        base_url="http://localhost:8080/",
        session={},
    )

    with patch(
        "brain.systems.runtime_settings.auth.ensure_callback_server",
    ) as ensure_callback, patch(
        "brain.systems.runtime_settings.auth.build_codex_oauth_authorize_url",
        return_value=("https://auth.openai.com/oauth/authorize?state=state-123", "state-123", "verifier-123"),
    ) as mock_build, patch("brain.systems.runtime_settings.auth.register_callback_target") as register_target:
        result = start_openai_oauth(request=request)

    assert result["redirect_uri"] == "http://localhost:1455/auth/callback"
    assert result["callback_available"] is False
    assert result["callback_mode"] == "local_bridge"
    assert "browser-local callback" in str(result["callback_detail"])
    assert "paste" in str(result["callback_detail"]).lower()
    assert request.session[OPENAI_OAUTH_SESSION_KEY]["redirect_uri"] == "http://localhost:1455/auth/callback"
    assert request.session[OPENAI_OAUTH_SESSION_KEY]["return_url"] == "http://localhost:8080/auth/callback"
    mock_build.assert_called_once_with(redirect_uri="http://localhost:1455/auth/callback")
    ensure_callback.assert_not_called()
    register_target.assert_not_called()


def test_start_openai_oauth_requires_opt_in_for_server_callback_for_public_base_url():
    from brain.systems.runtime_settings.auth import OPENAI_OAUTH_SESSION_KEY, start_openai_oauth

    request = _RequestStub(
        headers={"origin": "https://illo.example.com"},
        base_url="https://illo.example.com/",
        session={},
    )

    with patch(
        "brain.systems.runtime_settings.auth.ensure_callback_server",
    ) as ensure_callback, patch(
        "brain.systems.runtime_settings.auth.build_codex_oauth_authorize_url",
        return_value=("https://auth.openai.com/oauth/authorize?state=state-123", "state-123", "verifier-123"),
    ) as mock_build, patch("brain.systems.runtime_settings.auth.register_callback_target") as register_target:
        result = start_openai_oauth(request=request, callback_mode="server")

    assert result["redirect_uri"] == "http://localhost:1455/auth/callback"
    assert result["callback_mode"] == "local_bridge"
    assert request.session[OPENAI_OAUTH_SESSION_KEY]["redirect_uri"] == "http://localhost:1455/auth/callback"
    mock_build.assert_called_once_with(redirect_uri="http://localhost:1455/auth/callback")
    ensure_callback.assert_not_called()
    register_target.assert_not_called()


def test_start_openai_oauth_can_opt_in_server_callback_for_public_base_url(monkeypatch):
    from brain.systems.runtime_settings.auth import OPENAI_OAUTH_SESSION_KEY, start_openai_oauth

    monkeypatch.setenv("ILLO_OPENAI_OAUTH_SERVER_CALLBACK", "1")
    request = _RequestStub(
        headers={"origin": "https://illo.example.com"},
        base_url="https://illo.example.com/",
        session={},
    )

    with patch(
        "brain.systems.runtime_settings.auth.ensure_callback_server",
    ) as ensure_callback, patch(
        "brain.systems.runtime_settings.auth.build_codex_oauth_authorize_url",
        return_value=("https://auth.openai.com/oauth/authorize?state=state-123", "state-123", "verifier-123"),
    ) as mock_build, patch("brain.systems.runtime_settings.auth.register_callback_target") as register_target:
        result = start_openai_oauth(request=request, callback_mode="server")

    assert result["redirect_uri"] == "https://illo.example.com/auth/callback"
    assert result["callback_available"] is True
    assert result["callback_mode"] == "server"
    assert "Illo server" in str(result["callback_detail"])
    assert request.session[OPENAI_OAUTH_SESSION_KEY]["redirect_uri"] == "https://illo.example.com/auth/callback"
    assert request.session[OPENAI_OAUTH_SESSION_KEY]["return_url"] == "https://illo.example.com/auth/callback"
    assert request.session[OPENAI_OAUTH_SESSION_KEY]["callback_mode"] == "server"
    mock_build.assert_called_once_with(redirect_uri="https://illo.example.com/auth/callback")
    ensure_callback.assert_not_called()
    register_target.assert_not_called()


def test_start_openai_oauth_can_force_localhost_fallback_for_public_base_url():
    from brain.systems.runtime_settings.auth import OPENAI_OAUTH_SESSION_KEY, start_openai_oauth

    request = _RequestStub(
        headers={"origin": "https://illo.example.com"},
        base_url="https://illo.example.com/",
        session={},
    )

    with patch(
        "brain.systems.runtime_settings.auth.ensure_callback_server",
    ) as ensure_callback, patch(
        "brain.systems.runtime_settings.auth.build_codex_oauth_authorize_url",
        return_value=("https://auth.openai.com/oauth/authorize?state=state-123", "state-123", "verifier-123"),
    ) as mock_build, patch("brain.systems.runtime_settings.auth.register_callback_target") as register_target:
        result = start_openai_oauth(request=request, callback_mode="local_bridge")

    assert result["redirect_uri"] == "http://localhost:1455/auth/callback"
    assert result["callback_available"] is False
    assert result["callback_mode"] == "local_bridge"
    assert "localhost:1455" in str(result["callback_detail"])
    assert "browser" in str(result["callback_detail"]).lower()
    assert request.session[OPENAI_OAUTH_SESSION_KEY]["redirect_uri"] == "http://localhost:1455/auth/callback"
    assert request.session[OPENAI_OAUTH_SESSION_KEY]["return_url"] == "https://illo.example.com/auth/callback"
    mock_build.assert_called_once_with(redirect_uri="http://localhost:1455/auth/callback")
    ensure_callback.assert_not_called()
    register_target.assert_not_called()


def test_exchange_openai_oauth_stores_chatgpt_session_payload():
    from brain.systems.runtime_settings.auth import OPENAI_OAUTH_SESSION_KEY, exchange_openai_oauth

    request = _RequestStub(
        session={
            OPENAI_OAUTH_SESSION_KEY: {
                "state": "state-123",
                "code_verifier": "verifier-123",
                "created_at": time.time(),
                "redirect_uri": "http://localhost:1455/auth/callback",
            }
        },
    )
    user = SimpleNamespace(id="user-1", org_id="org-1", role="owner")
    callback = "http://localhost:1455/auth/callback?code=auth-code-123&state=state-123"
    cred = OpenAICodexCredential(
        access_token="access-token-123",
        refresh_token="refresh-token-123",
        account_id="acct_123",
        email="reda@example.com",
        plan_type="chatgpt-plus",
        auth_mode="chatgpt",
    )

    with patch(
        "brain.systems.runtime_settings.auth.exchange_codex_authorization_code",
        return_value=cred,
    ) as mock_exchange, patch(
        "brain.systems.runtime_settings.auth.store_openai_connection",
        return_value=RuntimeConnectionRead(
            status="connected",
            setup_required=False,
            method="chatgpt",
            source="user_default",
            label="Codex / ChatGPT",
        ),
    ) as mock_store:
        result = exchange_openai_oauth(request=request, user=user, callback=callback)

    assert result.status == "connected"
    assert result.method == "chatgpt"
    assert OPENAI_OAUTH_SESSION_KEY not in request.session
    mock_exchange.assert_called_once_with(
        "auth-code-123",
        code_verifier="verifier-123",
        redirect_uri="http://localhost:1455/auth/callback",
    )

    stored_payload = json.loads(mock_store.call_args.args[1])
    assert stored_payload["auth_mode"] == "chatgpt"
    assert stored_payload["tokens"]["account_id"] == "acct_123"
    assert stored_payload["tokens"]["refresh_token"] == "refresh-token-123"
    assert mock_store.call_args.args[0] is user
    assert mock_store.call_args.kwargs["label"] == "Codex / ChatGPT"


def test_exchange_openai_oauth_uses_session_redirect_uri_for_server_callback():
    from brain.systems.runtime_settings.auth import OPENAI_OAUTH_SESSION_KEY, exchange_openai_oauth

    request = _RequestStub(
        session={
            OPENAI_OAUTH_SESSION_KEY: {
                "state": "state-123",
                "code_verifier": "verifier-123",
                "created_at": time.time(),
                "redirect_uri": "https://illo.example.com/auth/callback",
            }
        },
    )
    user = SimpleNamespace(id="user-1", org_id="org-1", role="owner")
    callback = "https://illo.example.com/auth/callback?code=auth-code-123&state=state-123"
    cred = OpenAICodexCredential(
        access_token="access-token-123",
        refresh_token="refresh-token-123",
        account_id="acct_123",
        email="reda@example.com",
        plan_type="chatgpt-plus",
        auth_mode="chatgpt",
    )

    with patch(
        "brain.systems.runtime_settings.auth.exchange_codex_authorization_code",
        return_value=cred,
    ) as mock_exchange, patch(
        "brain.systems.runtime_settings.auth.store_openai_connection",
        return_value=RuntimeConnectionRead(
            status="connected",
            setup_required=False,
            method="chatgpt",
            source="user_default",
            label="Codex / ChatGPT",
        ),
    ):
        result = exchange_openai_oauth(request=request, user=user, callback=callback)

    assert result.status == "connected"
    mock_exchange.assert_called_once_with(
        "auth-code-123",
        code_verifier="verifier-123",
        redirect_uri="https://illo.example.com/auth/callback",
    )


def test_exchange_openai_oauth_rejects_state_mismatch():
    from brain.systems.runtime_settings.auth import OPENAI_OAUTH_SESSION_KEY, exchange_openai_oauth

    request = _RequestStub(
        session={
            OPENAI_OAUTH_SESSION_KEY: {
                "state": "state-123",
                "code_verifier": "verifier-123",
                "created_at": time.time(),
            }
        },
    )
    user = SimpleNamespace(id="user-1", org_id="org-1", role="member")
    callback = "http://localhost:1455/auth/callback?code=auth-code-123&state=wrong-state"

    with pytest.raises(HTTPException) as excinfo:
        exchange_openai_oauth(request=request, user=user, callback=callback)

    assert excinfo.value.status_code == 400
    assert "state mismatch" in excinfo.value.detail.lower()


def test_callback_bridge_unknown_state_returns_copyable_callback_page():
    from brain.systems.runtime_settings.oauth_callback_server import _CallbackHTTPServer, _CallbackRequestHandler

    server = _CallbackHTTPServer(("127.0.0.1", 0), _CallbackRequestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        callback_url = f"http://{host}:{port}/auth/callback?code=auth-code-123&state=missing-state"
        with urlopen(callback_url, timeout=5) as response:  # noqa: S310 - local test server.
            body = response.read().decode("utf-8")
            status = response.status
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert status == 200
    assert "OpenAI sign-in returned here" in body
    assert "auth-code-123" in body
    assert "missing-state" in body
    assert '"type":"illo:openai-oauth"' in body
    assert '"status":"callback"' in body
    assert "copy-callback" in body
