"""FastAPI dependencies — DB session, rate limiting."""
from __future__ import annotations

import hashlib
import ipaddress
import time
from collections import defaultdict
from typing import Generator

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from brain.platform.db import SessionFactory
from brain.app.api.config import RATE_LIMIT, RATE_LIMIT_GLOBAL, RATE_WINDOW


def get_db() -> Generator[Session, None, None]:
    """Yield a SQLAlchemy session, auto-close after request."""
    session = SessionFactory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


_rate_store: dict[str, list[float]] = defaultdict(list)


def _is_internal(addr: str | None) -> bool:
    raw = (addr or "").strip().strip("[]")
    if not raw:
        return False
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        return False
    return ip.is_loopback or ip in ipaddress.ip_network("100.64.0.0/10")


def rate_limit(request: Request) -> None:
    """Dependency that enforces rate limiting on API routes."""
    addr = request.client.host if request.client else "unknown"
    if _is_internal(addr):
        return
    now = time.time()
    principal = _rate_limit_principal(request, addr)
    route = _rate_limit_route(request)
    buckets = (
        (f"route:{principal}:{route}", RATE_LIMIT),
        (f"global:{principal}", RATE_LIMIT_GLOBAL),
    )

    pruned: list[tuple[str, list[float]]] = []
    retry_after = 0
    for key, limit in buckets:
        hits = [t for t in _rate_store[key] if now - t < RATE_WINDOW]
        pruned.append((key, hits))
        if len(hits) >= limit:
            oldest = hits[0]
            retry_after = max(retry_after, int(RATE_WINDOW - (now - oldest)) + 1)

    if retry_after > 0:
        for key, hits in pruned:
            _rate_store[key] = hits
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded. Retry after {retry_after}s.",
            headers={"Retry-After": str(retry_after)},
        )

    for key, hits in pruned:
        hits.append(now)
        _rate_store[key] = hits


def _rate_limit_principal(request: Request, addr: str) -> str:
    user_id = _session_user_id(request)
    if user_id:
        return f"user:{user_id}"

    auth_header = request.headers.get("Authorization", "")
    if not isinstance(auth_header, str):
        auth_header = ""
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
        if token:
            digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]
            return f"bearer:{digest}"

    return f"ip:{addr}"


def _session_user_id(request: Request) -> str | None:
    try:
        session = request.session if hasattr(request, "session") else None
        user_id = session.get("user_id") if session is not None else None
    except Exception:
        return None
    text = str(user_id or "").strip()
    return text or None


def _rate_limit_route(request: Request) -> str:
    method = str(getattr(request, "method", "GET") or "GET").upper()
    route = None
    try:
        route = getattr(request.scope.get("route"), "path", None)
    except Exception:
        route = None
    path = route or getattr(getattr(request, "url", None), "path", None) or "/"
    return f"{method} {path}"
