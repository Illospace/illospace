"""Explicit context required for durable memory writes.

Memory reads use ``MemoryVisibilityContext``. Writes need a stricter shape so
production code cannot accidentally create global or unowned memories.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import logging
import os
from typing import Any, Mapping, Sequence
import warnings

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.db.repositories.memory_visibility import (
    VALID_MEMORY_VISIBILITIES,
    MemoryVisibilityContext,
)

logger = logging.getLogger(__name__)


class MemoryWriteContextError(ValueError):
    """Raised when a memory insert lacks safe ownership/scope context."""


@dataclass(frozen=True)
class MemoryWriteContext:
    """Provenance, ownership, and visibility for a memory insert."""

    user_id: str
    org_id: str | None = None
    visibility: str = "private"
    source: str = "conversation"
    conversation_id: str | None = None
    idea_id: str | None = None
    run_id: int | str | None = None
    session_id: str | None = None
    confidence: float | None = None
    evidence: Mapping[str, Any] | Sequence[Any] | None = field(default_factory=dict)

    def __post_init__(self) -> None:
        user_id = _clean(self.user_id)
        source = _clean(self.source) or "conversation"
        visibility = _clean(self.visibility) or "private"
        if not user_id:
            raise MemoryWriteContextError("MemoryWriteContext.user_id is required")
        if visibility not in VALID_MEMORY_VISIBILITIES:
            allowed = ", ".join(VALID_MEMORY_VISIBILITIES)
            raise MemoryWriteContextError(
                f"Invalid memory visibility {visibility!r}; expected one of: {allowed}"
            )
        org_id = _clean(self.org_id)
        if visibility in {"team", "org"} and not org_id:
            raise MemoryWriteContextError(
                f"Memory visibility {visibility!r} requires org_id"
            )
        confidence = self.confidence
        if confidence is not None:
            confidence = max(0.0, min(1.0, float(confidence)))
        object.__setattr__(self, "user_id", user_id)
        object.__setattr__(self, "org_id", org_id)
        object.__setattr__(self, "visibility", visibility)
        object.__setattr__(self, "source", source[:50])
        object.__setattr__(self, "conversation_id", _clean(self.conversation_id))
        object.__setattr__(self, "idea_id", _clean(self.idea_id))
        object.__setattr__(self, "run_id", self.run_id)
        object.__setattr__(self, "session_id", _clean(self.session_id))
        object.__setattr__(self, "confidence", confidence)
        object.__setattr__(self, "evidence", self.evidence or {})

    def with_defaults(
        self,
        *,
        source: str | None = None,
        session_id: str | None = None,
        confidence: float | None = None,
    ) -> "MemoryWriteContext":
        """Return a copy with caller defaults only where the context is blank."""

        updates: dict[str, Any] = {}
        if source and not self.source:
            updates["source"] = source
        if session_id and not self.session_id:
            updates["session_id"] = session_id
        if confidence is not None and self.confidence is None:
            updates["confidence"] = confidence
        return replace(self, **updates) if updates else self

    def as_visibility_context(self) -> MemoryVisibilityContext:
        return MemoryVisibilityContext(user_id=self.user_id, org_id=self.org_id)

    def source_session(self) -> str | None:
        """Session/conversation provenance for source records."""

        return _truncate(self.session_id or self.conversation_id, 100)

    def source_ref(self) -> str | None:
        """Compact provenance reference for reconstructive source records."""

        parts: list[str] = []
        if self.run_id is not None:
            parts.append(f"run:{self.run_id}")
        if self.idea_id:
            parts.append(f"idea:{self.idea_id}")
        if self.conversation_id:
            parts.append(f"conversation:{self.conversation_id}")
        if self.session_id:
            parts.append(f"session:{self.session_id}")
        return _truncate(";".join(parts) or None, 120)


def require_memory_write_context(context: MemoryWriteContext | None) -> MemoryWriteContext:
    if not isinstance(context, MemoryWriteContext):
        raise MemoryWriteContextError(
            "Production memory inserts require an explicit MemoryWriteContext"
        )
    return context


async def dangerously_build_dev_test_memory_write_context(
    *,
    session: AsyncSession | None = None,
    source: str = "legacy_add_memory",
    source_session: str | None = None,
    visibility: str = "private",
) -> MemoryWriteContext:
    """Compatibility shim for old local/test callers while migration completes.

    This is intentionally loud and intentionally unavailable in production.
    Production callers must pass ``MemoryWriteContext`` explicitly.
    """

    env = os.getenv("ILLO_ENV", "development").strip().lower()
    if env == "production":
        raise MemoryWriteContextError(
            "Contextless add_memory() is disabled in production; pass MemoryWriteContext"
        )

    message = (
        "DEV/TEST COMPATIBILITY ONLY: contextless add_memory() created a "
        "MemoryWriteContext. Pass MemoryWriteContext explicitly before using this path "
        "in production."
    )
    logger.warning(message)
    warnings.warn(message, RuntimeWarning, stacklevel=2)

    user_id = _clean(os.getenv("ILLO_DEV_MEMORY_USER_ID"))
    org_id = _clean(os.getenv("ILLO_DEV_MEMORY_ORG_ID"))
    if (not user_id or not org_id) and session is not None:
        row = await _first_user_row(session)
        if row:
            user_id = user_id or _clean(row.get("id"))
            org_id = org_id or _clean(row.get("org_id"))

    if not user_id:
        raise MemoryWriteContextError(
            "Contextless dev/test memory write could not resolve a user. "
            "Set ILLO_DEV_MEMORY_USER_ID or pass MemoryWriteContext."
        )

    normalized_visibility = visibility if visibility in VALID_MEMORY_VISIBILITIES else "private"
    if normalized_visibility in {"team", "org"} and not org_id:
        normalized_visibility = "private"

    return MemoryWriteContext(
        user_id=user_id,
        org_id=org_id,
        visibility=normalized_visibility,
        source=source,
        session_id=source_session,
        evidence={"compatibility_shim": "contextless_add_memory_dev_test"},
    )


async def _first_user_row(session: AsyncSession) -> dict[str, Any] | None:
    try:
        row = (
            await session.execute(
            text("SELECT id, org_id FROM users ORDER BY created_at NULLS LAST, id LIMIT 1")
            )
        ).mappings().first()
        return dict(row) if row else None
    except Exception:
        logger.debug("Could not resolve dev/test memory context from first user", exc_info=True)
        return None


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text_value = str(value).strip()
    return text_value or None


def _truncate(value: str | None, limit: int) -> str | None:
    if not value:
        return None
    return value[:limit]
