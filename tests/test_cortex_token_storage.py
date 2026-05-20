import json
from unittest.mock import AsyncMock, MagicMock, patch

from tests.test_api_key_endpoints import _AsyncSession


def _mock_request(payload: dict):
    req = MagicMock()
    req.json = AsyncMock(return_value=payload)
    return req


async def test_add_api_key_trusts_failed_setup_token_verification():
    from brain.app.api.routers.cortex import add_api_key

    request = _mock_request({"provider": "anthropic", "label": "default", "api_key": "sk-ant-oat01-test-token"})
    user = {"id": "user-1", "org_id": "org-1", "role": "owner"}

    with patch("brain.app.api.routers.cortex._auth_keys._verify_provider_api_key", side_effect=RuntimeError("401 invalid x-api-key")), \
         patch("brain.app.api.routers.cortex._auth_keys._should_trust_failed_key_verification", return_value=True), \
         patch("brain.systems.vault._encrypt", return_value=b"enc"):
        session = MagicMock()
        session.scalars.return_value.first.return_value = None
        session.add.side_effect = lambda key: setattr(key, "id", 42)
        resp = await add_api_key(request, user, db=_AsyncSession(session))

    assert resp["id"] == 42
    assert resp["status"] == "org_key_stored"
    assert resp["verified"] is False
    assert "401 invalid x-api-key" in resp["verify_error"]
    session.add.assert_called_once()


async def test_set_org_main_key_trusts_failed_setup_token_verification():
    from brain.app.api.routers.cortex import set_org_main_key

    request = _mock_request({"provider": "anthropic", "api_key": "sk-ant-oat01-test-token"})
    user = {"id": "user-1", "org_id": "org-1", "role": "owner"}

    mock_session = MagicMock()

    with patch("brain.app.api.routers.cortex._auth_keys._verify_provider_api_key", side_effect=RuntimeError("401 invalid x-api-key")), \
         patch("brain.app.api.routers.cortex._auth_keys._should_trust_failed_key_verification", return_value=True), \
         patch("brain.systems.vault._encrypt", return_value=b"enc"):
        resp = await set_org_main_key(request, user, db=_AsyncSession(mock_session))

    assert resp["status"] == "org_key_stored"
    assert resp["verified"] is False
    assert "401 invalid x-api-key" in resp["verify_error"]
    assert resp["id"] is not None


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
