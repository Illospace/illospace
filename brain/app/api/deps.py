"""FastAPI dependencies — DB session, rate limiting."""
from __future__ import annotations

import ipaddress
import time
from collections import defaultdict
from typing import Generator

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from brain.platform.db import SessionFactory
from brain.app.api.config import RATE_LIMIT, RATE_WINDOW


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
    hits = _rate_store[addr]
    _rate_store[addr] = [t for t in hits if now - t < RATE_WINDOW]
    if len(_rate_store[addr]) >= RATE_LIMIT:
        oldest = _rate_store[addr][0]
        retry_after = max(1, int(RATE_WINDOW - (now - oldest)) + 1)
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded. Retry after {retry_after}s.",
            headers={"Retry-After": str(retry_after)},
        )
    _rate_store[addr].append(now)
