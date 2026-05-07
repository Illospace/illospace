import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch


def _mock_request(payload: dict):
    req = MagicMock()
    req.json = AsyncMock(return_value=payload)
    return req


def test_add_api_key_trusts_failed_setup_token_verification():
    from brain.app.api.routers.cortex import add_api_key

    request = _mock_request({"provider": "anthropic", "label": "default", "api_key": "sk-ant-oat01-test-token"})
    user = {"id": "user-1"}

    with patch("brain.app.api.routers.cortex._auth_keys._verify_provider_api_key", side_effect=RuntimeError("401 invalid x-api-key")), \
         patch("brain.app.api.routers.cortex._auth_keys._should_trust_failed_key_verification", return_value=True), \
         patch("brain.systems.vault.set_api_key", return_value=42) as mock_set:
        resp = asyncio.run(add_api_key(request, user))

    assert resp["id"] == 42
    assert resp["status"] == "stored"
    assert resp["verified"] is False
    assert "401 invalid x-api-key" in resp["verify_error"]
    mock_set.assert_called_once()


def test_set_org_main_key_trusts_failed_setup_token_verification():
    from brain.app.api.routers.cortex import set_org_main_key

    request = _mock_request({"provider": "anthropic", "api_key": "sk-ant-oat01-test-token"})
    user = {"id": "user-1", "org_id": "org-1", "role": "owner"}

    mock_session = MagicMock()
    mock_uow = MagicMock()
    mock_uow.__enter__ = MagicMock(return_value=mock_uow)
    mock_uow.__exit__ = MagicMock(return_value=False)
    mock_uow.session = mock_session

    with patch("brain.app.api.routers.cortex._auth_keys._verify_provider_api_key", side_effect=RuntimeError("401 invalid x-api-key")), \
         patch("brain.app.api.routers.cortex._auth_keys._should_trust_failed_key_verification", return_value=True), \
         patch("brain.systems.vault._encrypt", return_value=b"enc"), \
         patch("brain.app.api.routers.cortex._auth_keys.UnitOfWork", return_value=mock_uow):
        resp = asyncio.run(set_org_main_key(request, user))

    assert resp["status"] == "org_key_stored"
    assert resp["verified"] is False
    assert "401 invalid x-api-key" in resp["verify_error"]
    mock_session.execute.assert_called_once()


def test_parse_provider_connect_token_accepts_openai_codex_payload():
    from brain.app.api.routers.cortex._key_utils import parse_provider_connect_token

    payload = json.dumps({
        "auth_mode": "chatgpt",
        "tokens": {
            "access_token": "eyJhbGciOiJub25lIn0.eyJleHAiOjE5MDAwMDAwMDAsImh0dHBzOi8vYXBpLm9wZW5haS5jb20vYXV0aCI6eyJjaGF0Z3B0X2FjY291bnRfaWQiOiJhY2N0XzEyMyJ9fQ.sig",
            "refresh_token": "refresh-123",
            "account_id": "acct_123",
        },
    })

    token, method = parse_provider_connect_token(payload, "openai")

    parsed = json.loads(token)
    assert method == "chatgpt"
    assert parsed["auth_mode"] == "chatgpt"
    assert parsed["tokens"]["account_id"] == "acct_123"


def test_parse_provider_connect_token_accepts_openai_api_key():
    from brain.app.api.routers.cortex._key_utils import parse_provider_connect_token

    token, method = parse_provider_connect_token("sk-proj-test-123", "openai")

    assert token == "sk-proj-test-123"
    assert method == "api_key"


def test_parse_provider_connect_token_requires_explicit_provider():
    import pytest

    from brain.app.api.routers.cortex._key_utils import parse_provider_connect_token

    with pytest.raises(ValueError, match="Unsupported provider"):
        parse_provider_connect_token("sk-proj-test-123", "")
