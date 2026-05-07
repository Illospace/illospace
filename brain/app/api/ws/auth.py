"""Signed, short-lived authentication tokens for WebSocket handshakes."""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from itsdangerous import BadData, URLSafeSerializer

from brain.app.api.config import SECRET_KEY

WS_TOKEN_TTL_SECONDS = 60
_WS_TOKEN_SALT = "illo-brain-ws-auth-v1"
_WS_TOKEN_TYPE = "ws-auth"


class WsTokenError(ValueError):
    """Raised when a WebSocket token cannot authenticate a socket."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class WsTokenClaims:
    user_id: str
    org_id: str
    expires_at: datetime
    session_id: str
    tab_id: str | None = None
    principal_type: str = "human"
    permissions: tuple[str, ...] = ()

    @property
    def expires_at_unix(self) -> int:
        return int(self.expires_at.timestamp())


def ensure_ws_session_id(session: dict[str, Any]) -> str:
    """Return a stable random id scoped to the current signed HTTP session."""
    existing = str(session.get("ws_session_id") or "").strip()
    if existing:
        return existing
    session_id = secrets.token_urlsafe(18)
    session["ws_session_id"] = session_id
    return session_id


def create_ws_token(
    user: Mapping[str, Any],
    *,
    session_id: str,
    tab_id: str | None = None,
    ttl_seconds: int = WS_TOKEN_TTL_SECONDS,
    now: datetime | None = None,
) -> tuple[str, WsTokenClaims]:
    user_id = _require_text(user.get("id"), "WS_TOKEN_USER_REQUIRED")
    org_id = _require_text(user.get("org_id"), "WS_TOKEN_ORG_REQUIRED")
    session_id = _require_text(session_id, "WS_TOKEN_SESSION_REQUIRED")
    issued_at = _now(now)
    expires_at = issued_at + timedelta(seconds=ttl_seconds)
    normalized_tab_id = _optional_text(tab_id)
    principal_type = _optional_text(user.get("principal_type")) or "human"
    permissions = _normalize_permissions(user.get("permissions"))
    claims = WsTokenClaims(
        user_id=user_id,
        org_id=org_id,
        expires_at=expires_at,
        session_id=session_id,
        tab_id=normalized_tab_id,
        principal_type=principal_type,
        permissions=permissions,
    )
    payload = {
        "typ": _WS_TOKEN_TYPE,
        "ver": 1,
        "user_id": claims.user_id,
        "org_id": claims.org_id,
        "principal_type": claims.principal_type,
        "permissions": list(claims.permissions),
        "exp": claims.expires_at_unix,
        "iat": int(issued_at.timestamp()),
        "session_id": claims.session_id,
    }
    if normalized_tab_id:
        payload["tab_id"] = normalized_tab_id
    return _serializer().dumps(payload), claims


def verify_ws_token(token: str, *, now: datetime | None = None) -> WsTokenClaims:
    token = _require_text(token, "WS_TOKEN_REQUIRED")
    try:
        payload = _serializer().loads(token)
    except BadData as exc:
        raise WsTokenError("WS_TOKEN_INVALID") from exc

    if not isinstance(payload, dict) or payload.get("typ") != _WS_TOKEN_TYPE:
        raise WsTokenError("WS_TOKEN_INVALID")

    try:
        expires_at_unix = int(payload["exp"])
    except (KeyError, TypeError, ValueError) as exc:
        raise WsTokenError("WS_TOKEN_INVALID") from exc

    expires_at = datetime.fromtimestamp(expires_at_unix, tz=timezone.utc)
    if expires_at <= _now(now):
        raise WsTokenError("WS_TOKEN_EXPIRED")

    return WsTokenClaims(
        user_id=_require_text(payload.get("user_id"), "WS_TOKEN_INVALID"),
        org_id=_require_text(payload.get("org_id"), "WS_TOKEN_INVALID"),
        expires_at=expires_at,
        session_id=_require_text(payload.get("session_id"), "WS_TOKEN_INVALID"),
        tab_id=_optional_text(payload.get("tab_id")),
        principal_type=_optional_text(payload.get("principal_type")) or "human",
        permissions=_normalize_permissions(payload.get("permissions")),
    )


def validate_auth_frame_claims(
    frame: Mapping[str, Any],
    claims: WsTokenClaims,
) -> None:
    """Reject spoofed legacy identity fields when clients still send them."""
    for key, expected in (
        ("user_id", claims.user_id),
        ("org_id", claims.org_id),
        ("session_id", claims.session_id),
    ):
        supplied = _optional_text(frame.get(key))
        if supplied is not None and supplied != expected:
            raise WsTokenError("WS_TOKEN_CLAIM_MISMATCH")


def _serializer() -> URLSafeSerializer:
    return URLSafeSerializer(SECRET_KEY, salt=_WS_TOKEN_SALT)


def _now(value: datetime | None = None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _require_text(value: Any, code: str) -> str:
    text = _optional_text(value)
    if text is None:
        raise WsTokenError(code)
    return text


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize_permissions(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple, set)):
        return ()
    seen: set[str] = set()
    ordered: list[str] = []
    for permission in value:
        if permission is None:
            continue
        normalized = str(permission)
        if normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return tuple(ordered)
