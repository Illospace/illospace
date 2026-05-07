"""Shared Cortex API key normalization, verification, and storage helpers."""

from __future__ import annotations

import json
import logging
import re

from sqlalchemy import text

from brain.platform.db.repositories.unit_of_work import UnitOfWork
from brain.platform.integrations.anthropic_adapter import build_auth_adapter, create_message_with_token, is_oauth_token
from brain.platform.integrations.llm import _import_openai_sdk
from brain.platform.integrations.openai_codex_auth import (
    encode_codex_auth_payload,
    parse_codex_auth_payload,
)

logger = logging.getLogger(__name__)

VALID_PROVIDERS = {"anthropic", "openai", "google"}


def normalize_provider_api_key(raw_value: str, provider: str) -> str:
    """Normalize a pasted credential into a provider token string."""
    token = (raw_value or "").strip()
    original_len = len(token)
    if provider == "openai":
        try:
            cred = parse_codex_auth_payload(token, source="cortex_connect")
            if cred.auth_mode == "chatgpt" and (cred.access_token or cred.refresh_token or cred.account_id):
                return json.dumps(encode_codex_auth_payload(cred))
            if cred.auth_mode == "api_key" and cred.access_token:
                return json.dumps(encode_codex_auth_payload(cred))
        except Exception:
            try:
                payload = json.loads(token)
                extracted = (
                    payload.get("OPENAI_API_KEY")
                    or payload.get("openai_api_key")
                    or payload.get("api_key")
                    or payload.get("key")
                )
                if extracted:
                    token = str(extracted).strip()
            except (json.JSONDecodeError, TypeError, AttributeError):
                pass
        token = token.strip().strip('"').strip("'").strip()
        return token

    if provider != "anthropic":
        return token

    try:
        creds = json.loads(token)
        access = creds.get("a") or creds.get("accessToken") or creds.get("access", "")
        if access:
            return str(access).strip()
    except (json.JSONDecodeError, TypeError, AttributeError):
        pass

    for pattern in (r'(sk-ant-oat01-\S+)', r'(sk-ant-[A-Za-z0-9._-]+)'):
        match = re.search(pattern, token)
        if match:
            token = match.group(1)
            break

    token = token.strip().strip('"').strip("'").strip()
    logger.info(
        "auth.normalize_key provider=%s original_len=%s normalized_len=%s prefix=%s suffix=%s",
        provider,
        original_len,
        len(token),
        token[:18] if token else "",
        token[-16:] if token else "",
    )
    return token


def verify_provider_api_key(api_key: str, provider: str) -> None:
    """Verify a provider API key by making a tiny model call when supported."""
    if provider == "openai":
        try:
            cred = parse_codex_auth_payload(api_key, source="cortex_verify")
        except Exception:
            cred = None

        if cred and cred.auth_mode == "chatgpt" and cred.access_token and cred.account_id:
            return

        openai = _import_openai_sdk()
        key = cred.access_token if cred and cred.access_token else api_key
        client = openai.OpenAI(api_key=key)
        try:
            client.models.list()
            return
        except Exception as e:
            logger.warning(
                "auth.verify_key_failed provider=%s token_prefix=%s token_suffix=%s error=%s",
                provider,
                api_key[:18] if api_key else "",
                api_key[-16:] if api_key else "",
                str(e),
            )
            raise

    if provider != "anthropic":
        return

    adapter = build_auth_adapter(api_key, timeout=15)
    client_token = getattr(adapter.client, "auth_token", None) or getattr(adapter.client, "api_key", None) or ""
    logger.info(
        "auth.verify_key provider=%s oauth=%s client_auth_token=%s client_api_key=%s "
        "token_prefix=%s token_suffix=%s extra_headers=%s",
        provider,
        adapter.is_oauth,
        bool(getattr(adapter.client, "auth_token", None)),
        bool(getattr(adapter.client, "api_key", None)),
        client_token[:18] if client_token else "",
        client_token[-16:] if client_token else "",
        sorted(adapter.extra_headers.keys()),
    )

    try:
        resp = create_message_with_token(
            api_key,
            timeout=15,
            model="claude-sonnet-4-6",
            max_tokens=5,
            messages=[{"role": "user", "content": "hi"}],
        )
        if not resp.content:
            raise RuntimeError("API returned empty response")
    except Exception as e:
        logger.warning(
            "auth.verify_key_failed provider=%s oauth=%s token_prefix=%s token_suffix=%s error=%s",
            provider,
            adapter.is_oauth,
            api_key[:18] if api_key else "",
            api_key[-16:] if api_key else "",
            str(e),
        )
        raise


def should_trust_failed_key_verification(provider: str, api_key: str) -> bool:
    """Setup tokens are stored on user intent even if live verification fails."""
    return provider == "anthropic" and is_oauth_token(api_key)


def store_org_api_key(
    org_id: str,
    provider: str,
    encrypted_key: bytes,
    *,
    label: str | None = None,
    uow_factory=UnitOfWork,
) -> None:
    """Upsert an org API key."""
    with uow_factory() as uow:
        if label is None:
            uow.session.execute(
                text("""
                    INSERT INTO org_api_keys (org_id, provider, encrypted_key)
                    VALUES (:org_id, :provider, :encrypted)
                    ON CONFLICT (org_id, provider) DO UPDATE SET
                        encrypted_key = EXCLUDED.encrypted_key
                """),
                {"org_id": org_id, "provider": provider, "encrypted": encrypted_key},
            )
        else:
            uow.session.execute(
                text("""
                    INSERT INTO org_api_keys (org_id, provider, encrypted_key, label)
                    VALUES (:org_id, :provider, :encrypted, :label)
                    ON CONFLICT (org_id, provider) DO UPDATE SET
                        encrypted_key = EXCLUDED.encrypted_key,
                        label = EXCLUDED.label
                """),
                {"org_id": org_id, "provider": provider, "encrypted": encrypted_key, "label": label},
            )


def parse_anthropic_connect_token(raw_token: str) -> tuple[str, str]:
    """Parse a pasted Anthropic setup token or API key from raw auth-connect input."""
    token = (raw_token or "").strip()
    if not token:
        raise ValueError("Paste your credentials")

    try:
        creds = json.loads(token)
        access = creds.get("a") or creds.get("accessToken") or creds.get("access", "")
        if access:
            return str(access).strip(), "oauth"
    except (json.JSONDecodeError, TypeError):
        pass

    match = re.search(r'(sk-ant-(?:oat01|api03)-\S+)', token)
    if match:
        token = match.group(1)
    token = token.strip().strip('"').strip("'").strip()
    if not token.startswith(("sk-ant-oat01-", "sk-ant-api03-")):
        raise ValueError("Invalid token format — expected setup-token or API key")
    return token, ("setup_token" if is_oauth_token(token) else "api_key")


def parse_provider_connect_token(raw_token: str, provider: str) -> tuple[str, str]:
    """Parse provider credentials pasted into the generic auth-connect flow."""
    provider = (provider or "").strip().lower()
    if provider == "anthropic":
        return parse_anthropic_connect_token(raw_token)
    if provider != "openai":
        raise ValueError("Unsupported provider")

    token = normalize_provider_api_key(raw_token, provider)
    if not token:
        raise ValueError("Paste your credentials")

    if provider == "openai":
        try:
            cred = parse_codex_auth_payload(token, source="cortex_connect")
        except Exception:
            cred = None
        if cred and cred.auth_mode == "chatgpt" and cred.access_token and cred.account_id:
            return json.dumps(encode_codex_auth_payload(cred)), "chatgpt"
        if cred and cred.auth_mode == "api_key" and cred.access_token:
            return json.dumps(encode_codex_auth_payload(cred)), "api_key"
        if not token.startswith("sk-"):
            raise ValueError("Invalid format — expected an OpenAI API key or Codex auth JSON")
        return token, "api_key"

    return token, "api_key"
