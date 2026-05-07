"""Helpers for parsing and refreshing Codex/OpenAI ChatGPT auth data.

This module is intentionally pure and small:
- it parses Codex CLI-style `auth.json` payloads
- it extracts useful metadata from JWT claims without verifying signatures
- it prepares refresh requests for Illo without wiring into runtime code

Expected storage shape is close to Codex CLI:
- top-level `auth_mode`
- top-level `OPENAI_API_KEY` for API-key auth
- nested `tokens` object for ChatGPT/Codex auth
- top-level `last_refresh`

The parser is flexible enough to handle either nested or top-level token
fields so it can tolerate small shape differences across Codex-style auth
payloads without becoming brittle.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

_AUTH_FILENAME = "auth.json"
_DEFAULT_CLI_HOME = ".codex"
_DEFAULT_OAUTH_ISSUER = "https://auth.openai.com"
_DEFAULT_OAUTH_REDIRECT_URI = "http://localhost:1455/auth/callback"
_DEFAULT_REFRESH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"


@dataclass
class OpenAICodexCredential:
    """Codex/OpenAI auth state normalized into a small, portable record."""

    access_token: str | None = None
    refresh_token: str | None = None
    id_token: str | None = None
    account_id: str | None = None
    email: str | None = None
    plan_type: str | None = None
    expires_at: int | float | None = None
    last_refresh: int | float | None = None
    source: str = ""
    external_source_path: str | None = None
    auth_mode: str | None = None


def parse_codex_jwt_claims(token: str | bytes | None) -> dict[str, Any]:
    """Decode the payload of an unverified JWT.

    The helper intentionally ignores signatures because Codex auth caches are
    local trust material, and Illo only needs claims to prepare refreshes and
    surface metadata.
    """

    if not token:
        return {}
    if isinstance(token, bytes):
        try:
            token = token.decode("utf-8")
        except Exception:
            return {}
    token = token.strip()
    parts = token.split(".")
    if len(parts) < 2:
        return {}

    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
        data = json.loads(decoded.decode("utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def parse_codex_auth_payload(payload: str | bytes | dict[str, Any], *, source: str = "") -> OpenAICodexCredential:
    """Parse auth.json-style payloads into a normalized credential record."""

    data = _load_payload_mapping(payload)
    tokens = _mapping(data.get("tokens"))

    access_token = _first_text(
        tokens.get("access_token"),
        tokens.get("accessToken"),
        data.get("access_token"),
        data.get("accessToken"),
        data.get("OPENAI_API_KEY"),
    )
    refresh_token = _first_text(
        tokens.get("refresh_token"),
        tokens.get("refreshToken"),
        data.get("refresh_token"),
        data.get("refreshToken"),
    )
    id_token = _first_text(
        tokens.get("id_token"),
        tokens.get("idToken"),
        data.get("id_token"),
        data.get("idToken"),
    )

    account_id = _first_text(
        tokens.get("account_id"),
        tokens.get("accountId"),
        data.get("account_id"),
        data.get("accountId"),
    )
    email = _first_text(
        data.get("email"),
        data.get("profile.email"),
        tokens.get("email"),
        tokens.get("profile.email"),
    )
    plan_type = _first_text(
        data.get("plan_type"),
        data.get("planType"),
        tokens.get("plan_type"),
        tokens.get("planType"),
    )

    expires_at = _coerce_timestamp(
        tokens.get("expires_at"),
        tokens.get("expiresAt"),
        data.get("expires_at"),
        data.get("expiresAt"),
    )
    last_refresh = _coerce_timestamp(
        data.get("last_refresh"),
        data.get("lastRefresh"),
    )

    auth_mode = _first_text(data.get("auth_mode"), data.get("authMode"))

    claims = _merged_token_claims(id_token, access_token)
    email = email or _extract_email_from_claims(claims)
    plan_type = plan_type or _extract_plan_from_claims(claims)
    account_id = account_id or _extract_account_id_from_claims(claims)
    expires_at = expires_at or _extract_expiry_from_claims(claims)

    if not auth_mode:
        if data.get("OPENAI_API_KEY"):
            auth_mode = "api_key"
        elif access_token or refresh_token or id_token or account_id or email or plan_type or expires_at or last_refresh:
            auth_mode = "chatgpt"

    return OpenAICodexCredential(
        access_token=access_token,
        refresh_token=refresh_token,
        id_token=id_token,
        account_id=account_id,
        email=email,
        plan_type=plan_type,
        expires_at=expires_at,
        last_refresh=last_refresh,
        source=source,
        auth_mode=auth_mode,
    )


def encode_codex_auth_payload(cred: OpenAICodexCredential) -> dict[str, Any]:
    """Encode a normalized credential back into a Codex-style payload."""

    payload: dict[str, Any] = {}

    auth_mode = cred.auth_mode
    if not auth_mode and (
        cred.refresh_token
        or cred.id_token
        or cred.account_id
        or cred.email
        or cred.plan_type
        or cred.expires_at
        or cred.last_refresh
    ):
        auth_mode = "chatgpt"

    if auth_mode:
        payload["auth_mode"] = auth_mode

    if cred.last_refresh is not None:
        payload["last_refresh"] = cred.last_refresh

    if auth_mode == "api_key" and cred.access_token:
        payload["OPENAI_API_KEY"] = cred.access_token
        return payload

    tokens: dict[str, Any] = {}
    if cred.access_token:
        tokens["access_token"] = cred.access_token
    if cred.refresh_token:
        tokens["refresh_token"] = cred.refresh_token
    if cred.id_token:
        tokens["id_token"] = cred.id_token
    if cred.account_id:
        tokens["account_id"] = cred.account_id
    if cred.email:
        tokens["email"] = cred.email
    if cred.plan_type:
        tokens["plan_type"] = cred.plan_type
    if cred.expires_at is not None:
        tokens["expires_at"] = cred.expires_at

    if tokens:
        payload["tokens"] = tokens
    return payload


def load_codex_auth_json(path: str | Path | None = None) -> OpenAICodexCredential | None:
    """Load a Codex auth cache from disk.

    Search order when `path` is not provided:
    1. `$CODEX_HOME/auth.json`
    2. `~/.codex/auth.json`
    """

    if path is not None:
        candidate = Path(path)
        if candidate.exists() and candidate.is_dir():
            candidate = candidate / _AUTH_FILENAME
        return _load_auth_file(candidate)

    candidates: list[Path] = []
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home:
        candidates.append(Path(codex_home) / _AUTH_FILENAME)
    candidates.append(Path.home() / _DEFAULT_CLI_HOME / _AUTH_FILENAME)

    for candidate in candidates:
        cred = _load_auth_file(candidate)
        if cred is not None:
            return cred
    return None


def refresh_codex_access_token(
    refresh_token: str,
    *,
    issuer: str = _DEFAULT_OAUTH_ISSUER,
    client_id: str = _DEFAULT_REFRESH_CLIENT_ID,
    timeout: float = 15.0,
) -> OpenAICodexCredential:
    """Refresh a Codex access token using the OAuth refresh flow.

    The return value is a full normalized credential record. If the refresh
    response omits a new refresh token, the input refresh token is preserved.
    """

    token_url = f"{issuer.rstrip('/')}/oauth/token"
    response = httpx.post(
        token_url,
        json={
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": refresh_token,
        },
        headers={"Content-Type": "application/json"},
        timeout=timeout,
    )
    if response.status_code != 200:
        raise RuntimeError(f"Token refresh failed ({response.status_code}): {response.text[:200]}")

    data = response.json()
    if not isinstance(data, dict):
        raise RuntimeError("Token refresh failed: response was not a JSON object")

    cred = parse_codex_auth_payload(data, source=f"refresh:{issuer.rstrip('/')}")
    cred.refresh_token = cred.refresh_token or refresh_token
    cred.source = cred.source or f"refresh:{issuer.rstrip('/')}"
    cred.last_refresh = cred.last_refresh or _now_seconds()

    if cred.expires_at is None:
        expires_at = _coerce_timestamp(data.get("expires_at"), data.get("expiresAt"))
        if expires_at is None and data.get("expires_in") is not None:
            try:
                expires_at = _now_seconds() + float(data["expires_in"])
            except (TypeError, ValueError):
                expires_at = None
        cred.expires_at = expires_at

    return cred


def build_codex_oauth_authorize_url(
    *,
    issuer: str = _DEFAULT_OAUTH_ISSUER,
    client_id: str = _DEFAULT_REFRESH_CLIENT_ID,
    redirect_uri: str = _DEFAULT_OAUTH_REDIRECT_URI,
    state: str | None = None,
    code_verifier: str | None = None,
) -> tuple[str, str, str]:
    """Build a ChatGPT/Codex PKCE authorize URL.

    Returns `(authorize_url, state, code_verifier)`.
    """

    oauth_state = (state or secrets.token_urlsafe(24)).strip()
    if not oauth_state:
        oauth_state = secrets.token_urlsafe(24)

    verifier = (code_verifier or _make_pkce_verifier()).strip()
    if not verifier:
        verifier = _make_pkce_verifier()

    challenge = _make_pkce_challenge(verifier)
    query = urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": "openid profile email offline_access",
            "state": oauth_state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "id_token_add_organizations": "true",
            "codex_cli_simplified_flow": "true",
        }
    )
    url = f"{issuer.rstrip('/')}/oauth/authorize?{query}"
    return url, oauth_state, verifier


def parse_codex_oauth_callback(callback: str) -> tuple[str, str | None]:
    """Extract the authorization code and optional state from a pasted callback."""

    raw = (callback or "").strip()
    if not raw:
        raise ValueError("Paste the callback URL or authorization code")

    if "://" not in raw and "code=" not in raw and "&" not in raw and "?" not in raw:
        return raw, None

    candidate = raw
    if "://" not in candidate:
        candidate = f"http://localhost/?{candidate.lstrip('?')}"
    elif "?" not in candidate and "#" in candidate:
        candidate = candidate.replace("#", "?", 1)

    parsed = urlparse(candidate)
    query = parse_qs(parsed.query or parsed.fragment)
    if parsed.netloc == "auth.openai.com" and parsed.path.rstrip("/") == "/error":
        raise ValueError("OpenAI returned an authorization error page before redirecting to localhost. Start the flow again.")
    error_code = _first_text(*(query.get("error") or []))
    error_description = _first_text(*(query.get("error_description") or []))
    if error_code:
        if error_description:
            raise ValueError(f"{error_code}: {error_description}")
        raise ValueError(error_code)

    code = _first_text(*(query.get("code") or []))
    state = _first_text(*(query.get("state") or []))
    if not code:
        raise ValueError("Callback did not include an authorization code")
    return code, state


def exchange_codex_authorization_code(
    code: str,
    *,
    code_verifier: str,
    issuer: str = _DEFAULT_OAUTH_ISSUER,
    client_id: str = _DEFAULT_REFRESH_CLIENT_ID,
    redirect_uri: str = _DEFAULT_OAUTH_REDIRECT_URI,
    timeout: float = 15.0,
) -> OpenAICodexCredential:
    """Exchange a PKCE authorization code for ChatGPT/Codex tokens."""

    auth_code = (code or "").strip()
    verifier = (code_verifier or "").strip()
    if not auth_code:
        raise ValueError("Missing authorization code")
    if not verifier:
        raise ValueError("Missing PKCE code verifier")

    token_url = f"{issuer.rstrip('/')}/oauth/token"
    response = httpx.post(
        token_url,
        json={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": auth_code,
            "code_verifier": verifier,
            "redirect_uri": redirect_uri,
        },
        headers={"Content-Type": "application/json"},
        timeout=timeout,
    )
    if response.status_code != 200:
        raise RuntimeError(f"Token exchange failed ({response.status_code}): {response.text[:200]}")

    data = response.json()
    if not isinstance(data, dict):
        raise RuntimeError("Token exchange failed: response was not a JSON object")

    cred = parse_codex_auth_payload(data, source=f"oauth:{issuer.rstrip('/')}")
    cred.last_refresh = cred.last_refresh or _now_seconds()

    if cred.expires_at is None:
        expires_at = _coerce_timestamp(data.get("expires_at"), data.get("expiresAt"))
        if expires_at is None and data.get("expires_in") is not None:
            try:
                expires_at = _now_seconds() + float(data["expires_in"])
            except (TypeError, ValueError):
                expires_at = None
        cred.expires_at = expires_at

    return cred


def _load_auth_file(path: Path) -> OpenAICodexCredential | None:
    if not path.is_file():
        return None
    try:
        cred = parse_codex_auth_payload(path.read_text(), source="codex_auth_json")
    except Exception:
        return None
    cred.external_source_path = str(path)
    if not _credential_has_useful_data(cred):
        return None
    return cred


def _credential_has_useful_data(cred: OpenAICodexCredential) -> bool:
    return any(
        value is not None and value != ""
        for value in (
            cred.access_token,
            cred.refresh_token,
            cred.id_token,
            cred.account_id,
            cred.email,
            cred.plan_type,
            cred.expires_at,
            cred.last_refresh,
            cred.auth_mode,
        )
    )


def _load_payload_mapping(payload: str | bytes | dict[str, Any]) -> dict[str, Any]:
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8")
    if not isinstance(payload, str):
        raise TypeError(f"Unsupported payload type: {type(payload)!r}")
    data = json.loads(payload)
    if not isinstance(data, dict):
        raise ValueError("Codex auth payload must decode to a JSON object")
    return data


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _first_text(*values: Any) -> str | None:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str):
            stripped = value.strip()
            if stripped:
                return stripped
            continue
        if isinstance(value, (int, float)):
            return str(value)
    return None


def _coerce_timestamp(*values: Any) -> int | float | None:
    for value in values:
        if value is None:
            continue
        if isinstance(value, (int, float)):
            return value
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                continue
            try:
                iso_value = stripped.replace("Z", "+00:00")
                parsed = datetime.fromisoformat(iso_value)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return parsed.timestamp()
            except ValueError:
                pass
            try:
                number = float(stripped)
            except ValueError:
                continue
            return int(number) if number.is_integer() else number
    return None


def _merged_token_claims(id_token: str | None, access_token: str | None) -> dict[str, Any]:
    claims = parse_codex_jwt_claims(id_token)
    access_claims = parse_codex_jwt_claims(access_token)
    if access_claims:
        claims = {**access_claims, **claims}
    return claims


def _extract_email_from_claims(claims: Mapping[str, Any]) -> str | None:
    profile_email = _first_text(claims.get("https://api.openai.com/profile.email"))
    if profile_email:
        return profile_email
    profile_claim = _mapping(claims.get("https://api.openai.com/profile"))
    profile_email = _first_text(profile_claim.get("email"))
    if profile_email:
        return profile_email
    auth_claim = _mapping(claims.get("https://api.openai.com/auth"))
    return _first_text(
        auth_claim.get("email"),
        auth_claim.get("profile.email"),
        claims.get("email"),
    )


def _extract_plan_from_claims(claims: Mapping[str, Any]) -> str | None:
    auth_claim = _mapping(claims.get("https://api.openai.com/auth"))
    return _first_text(
        auth_claim.get("chatgpt_plan_type"),
        auth_claim.get("plan_type"),
        auth_claim.get("planType"),
        auth_claim.get("plan"),
        claims.get("plan_type"),
        claims.get("planType"),
        claims.get("plan"),
    )


def _extract_account_id_from_claims(claims: Mapping[str, Any]) -> str | None:
    auth_claim = _mapping(claims.get("https://api.openai.com/auth"))
    return _first_text(
        auth_claim.get("chatgpt_account_id"),
        auth_claim.get("account_id"),
        auth_claim.get("accountId"),
        auth_claim.get("workspace_id"),
        auth_claim.get("workspaceId"),
        auth_claim.get("org_id"),
        auth_claim.get("organization_id"),
        claims.get("account_id"),
        claims.get("accountId"),
        claims.get("workspace_id"),
        claims.get("workspaceId"),
        claims.get("org_id"),
        claims.get("organization_id"),
    )


def _extract_expiry_from_claims(claims: Mapping[str, Any]) -> int | float | None:
    auth_claim = _mapping(claims.get("https://api.openai.com/auth"))
    expiry = _coerce_timestamp(
        auth_claim.get("expires_at"),
        auth_claim.get("expiresAt"),
        auth_claim.get("exp"),
        claims.get("expires_at"),
        claims.get("expiresAt"),
        claims.get("exp"),
    )
    return expiry


def _now_seconds() -> float:
    return time.time()


def _make_pkce_verifier() -> str:
    return secrets.token_urlsafe(48)


def _make_pkce_challenge(code_verifier: str) -> str:
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


__all__ = [
    "OpenAICodexCredential",
    "build_codex_oauth_authorize_url",
    "encode_codex_auth_payload",
    "exchange_codex_authorization_code",
    "load_codex_auth_json",
    "parse_codex_auth_payload",
    "parse_codex_oauth_callback",
    "parse_codex_jwt_claims",
    "refresh_codex_access_token",
]
