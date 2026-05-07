from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlparse

import pytest


def _b64url_json(data: dict) -> str:
    raw = json.dumps(data, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _make_jwt(payload: dict) -> str:
    header = {"alg": "none", "typ": "JWT"}
    return ".".join([_b64url_json(header), _b64url_json(payload), "sig"])


def test_parse_codex_auth_payload_extracts_nested_tokens_and_jwt_metadata():
    from brain.platform.integrations.openai_codex_auth import parse_codex_auth_payload, encode_codex_auth_payload

    id_token = _make_jwt(
        {
            "https://api.openai.com/profile.email": "alex@example.com",
            "https://api.openai.com/auth": {
                "account_id": "acct_123",
                "plan_type": "chatgpt-plus",
            },
            "exp": 2_222_222_222,
        }
    )
    payload = {
        "auth_mode": "chatgpt",
        "last_refresh": 1_700_000_000,
        "tokens": {
            "access_token": "access-123",
            "refresh_token": "refresh-123",
            "id_token": id_token,
        },
    }

    cred = parse_codex_auth_payload(payload, source="payload")

    assert cred.auth_mode == "chatgpt"
    assert cred.access_token == "access-123"
    assert cred.refresh_token == "refresh-123"
    assert cred.id_token == id_token
    assert cred.account_id == "acct_123"
    assert cred.email == "alex@example.com"
    assert cred.plan_type == "chatgpt-plus"
    assert cred.expires_at == 2_222_222_222
    assert cred.last_refresh == 1_700_000_000
    assert cred.source == "payload"

    encoded = encode_codex_auth_payload(cred)
    assert encoded["auth_mode"] == "chatgpt"
    assert encoded["tokens"]["account_id"] == "acct_123"
    assert encoded["tokens"]["id_token"] == id_token


def test_load_codex_auth_json_prefers_codex_home_over_home(monkeypatch, tmp_path):
    from brain.platform.integrations.openai_codex_auth import load_codex_auth_json

    codex_home = tmp_path / "codex-home"
    home_dir = tmp_path / "home"
    codex_home.mkdir()
    (home_dir / ".codex").mkdir(parents=True)

    preferred = codex_home / "auth.json"
    fallback = home_dir / ".codex" / "auth.json"
    preferred.write_text(json.dumps({
        "auth_mode": "chatgpt",
        "tokens": {"access_token": "preferred-access"},
    }))
    fallback.write_text(json.dumps({
        "auth_mode": "chatgpt",
        "tokens": {"access_token": "fallback-access"},
    }))

    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home_dir))

    cred = load_codex_auth_json()

    assert cred is not None
    assert cred.access_token == "preferred-access"
    assert cred.external_source_path == str(preferred)
    assert cred.source == "codex_auth_json"


def test_parse_codex_jwt_claims_decodes_profile_and_auth_claims():
    from brain.platform.integrations.openai_codex_auth import parse_codex_jwt_claims

    token = _make_jwt(
        {
            "https://api.openai.com/profile.email": "alex@example.com",
            "https://api.openai.com/auth": {"account_id": "acct_456", "plan_type": "team"},
            "exp": 2_222_222_333,
        }
    )

    claims = parse_codex_jwt_claims(token)
    assert claims["https://api.openai.com/profile.email"] == "alex@example.com"
    assert claims["https://api.openai.com/auth"]["account_id"] == "acct_456"
    assert claims["exp"] == 2_222_222_333


def test_refresh_codex_access_token_preserves_missing_refresh_and_derives_account(monkeypatch):
    from brain.platform.integrations.openai_codex_auth import refresh_codex_access_token

    response_payload = {
        "access_token": "new-access-123",
        "id_token": _make_jwt(
            {
                "https://api.openai.com/profile.email": "alex@example.com",
                "https://api.openai.com/auth": {
                    "account_id": "acct_refresh_1",
                    "plan_type": "chatgpt-plus",
                },
                "exp": 2_333_333_333,
            }
        ),
        "expires_in": 1800,
    }

    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = response_payload
    fake_response.text = "ok"

    post_calls = []

    def fake_post(url, **kwargs):
        post_calls.append((url, kwargs))
        return fake_response

    monkeypatch.setattr("brain.platform.integrations.openai_codex_auth.httpx.post", fake_post)

    cred = refresh_codex_access_token("refresh-original", issuer="https://auth.openai.com", client_id="codex_cli", timeout=3.5)

    assert post_calls[0][0] == "https://auth.openai.com/oauth/token"
    assert post_calls[0][1]["json"]["grant_type"] == "refresh_token"
    assert post_calls[0][1]["json"]["refresh_token"] == "refresh-original"
    assert cred.access_token == "new-access-123"
    assert cred.refresh_token == "refresh-original"
    assert cred.account_id == "acct_refresh_1"
    assert cred.email == "alex@example.com"
    assert cred.plan_type == "chatgpt-plus"
    assert cred.expires_at == 2_333_333_333
    assert cred.source == "refresh:https://auth.openai.com"
    assert cred.last_refresh is not None


def test_build_codex_oauth_authorize_url_includes_pkce_and_codex_params():
    from brain.platform.integrations.openai_codex_auth import build_codex_oauth_authorize_url

    url, state, verifier = build_codex_oauth_authorize_url(
        state="state-123",
        code_verifier="verifier-123",
    )

    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    expected_challenge = base64.urlsafe_b64encode(hashlib.sha256(b"verifier-123").digest()).decode("ascii").rstrip("=")

    assert parsed.scheme == "https"
    assert parsed.netloc == "auth.openai.com"
    assert parsed.path == "/oauth/authorize"
    assert state == "state-123"
    assert verifier == "verifier-123"
    assert query["response_type"] == ["code"]
    assert query["state"] == ["state-123"]
    assert query["redirect_uri"] == ["http://localhost:1455/auth/callback"]
    assert query["scope"] == ["openid profile email offline_access"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"] == [expected_challenge]
    assert query["codex_cli_simplified_flow"] == ["true"]


def test_parse_codex_oauth_callback_accepts_url_and_surfaces_errors():
    from brain.platform.integrations.openai_codex_auth import parse_codex_oauth_callback

    code, state = parse_codex_oauth_callback("http://localhost:1455/auth/callback?code=auth-code-123&state=state-xyz")
    assert code == "auth-code-123"
    assert state == "state-xyz"

    with pytest.raises(ValueError) as excinfo:
        parse_codex_oauth_callback("http://localhost:1455/auth/callback?error=access_denied&error_description=Nope")

    assert "access_denied" in str(excinfo.value)

    with pytest.raises(ValueError) as excinfo:
        parse_codex_oauth_callback("https://auth.openai.com/error?payload=eyJrZXkiOiAidmFsdWUifQ")

    assert "authorization error page" in str(excinfo.value)


def test_exchange_codex_authorization_code_posts_pkce_payload(monkeypatch):
    from brain.platform.integrations.openai_codex_auth import exchange_codex_authorization_code

    response_payload = {
        "access_token": "new-access-123",
        "refresh_token": "refresh-123",
        "id_token": _make_jwt(
            {
                "https://api.openai.com/profile.email": "alex@example.com",
                "https://api.openai.com/auth": {
                    "account_id": "acct_exchange_1",
                    "plan_type": "chatgpt-plus",
                },
                "exp": 2_444_444_444,
            }
        ),
        "expires_in": 1200,
    }

    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = response_payload
    fake_response.text = "ok"

    post_calls = []

    def fake_post(url, **kwargs):
        post_calls.append((url, kwargs))
        return fake_response

    monkeypatch.setattr("brain.platform.integrations.openai_codex_auth.httpx.post", fake_post)

    cred = exchange_codex_authorization_code(
        "auth-code-123",
        code_verifier="verifier-123",
        client_id="codex_cli",
        timeout=3.5,
    )

    assert post_calls[0][0] == "https://auth.openai.com/oauth/token"
    assert post_calls[0][1]["json"]["grant_type"] == "authorization_code"
    assert post_calls[0][1]["json"]["code"] == "auth-code-123"
    assert post_calls[0][1]["json"]["code_verifier"] == "verifier-123"
    assert post_calls[0][1]["json"]["redirect_uri"] == "http://localhost:1455/auth/callback"
    assert cred.access_token == "new-access-123"
    assert cred.refresh_token == "refresh-123"
    assert cred.account_id == "acct_exchange_1"
    assert cred.email == "alex@example.com"
    assert cred.plan_type == "chatgpt-plus"
    assert cred.expires_at == 2_444_444_444
    assert cred.source == "oauth:https://auth.openai.com"
    assert cred.last_refresh is not None
