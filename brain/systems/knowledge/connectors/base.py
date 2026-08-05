"""Connector contract for bounded, incremental knowledge enumeration."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Mapping, Protocol

from sqlalchemy import and_, or_
from sqlalchemy.ext.asyncio import AsyncSession

from brain.systems.knowledge.scope import (
    KNOWLEDGE_SCOPE_EXTRA_KEY,
    KnowledgeScope,
)


@dataclass(frozen=True)
class KnowledgeDraft:
    """A source row ready for connector-agnostic indexing.

    ``distill`` is the slice-2 seam for conversational sources. Slice-1
    structural connectors emit their already-distilled fields directly.
    """

    source: str
    kind: str
    source_ref: str
    scope: KnowledgeScope
    title: str
    summary: str
    resolution: str | None = None
    entities: list[Any] = field(default_factory=list)
    raw_text: str = ""
    extra: dict[str, Any] = field(default_factory=dict)
    source_created_at: datetime | None = None
    source_updated_at: datetime | None = None
    archived_at: datetime | None = None
    distill: bool = False


def _cursor_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


@dataclass(frozen=True)
class UpdatedAtCursor:
    """Stable ``(updated_at, id)`` cursor shared by structural connectors."""

    updated_at: datetime | None
    row_id: int

    @classmethod
    def from_mapping(cls, cursor: Mapping[str, Any]) -> UpdatedAtCursor:
        return cls(
            updated_at=_cursor_datetime(cursor.get("updated_at")),
            row_id=max(0, int(cursor.get("id") or 0)),
        )

    def changed_after(self, updated_at_column: Any, id_column: Any) -> Any | None:
        if self.updated_at is None:
            return None
        return or_(
            updated_at_column > self.updated_at,
            and_(
                updated_at_column == self.updated_at,
                id_column > self.row_id,
            ),
        )

    def advanced_to(self, updated_at: datetime, row_id: int) -> dict[str, Any]:
        return {
            "updated_at": _utc_iso(updated_at),
            "id": row_id,
        }


class EnumerationFailureKind(StrEnum):
    """How the sync service must account for an enumeration failure."""

    TRANSIENT = "transient"
    CONFIGURATION = "configuration"


@dataclass(frozen=True)
class EnumerationFailure:
    """One source scope that could not be enumerated."""

    scope: str
    message: str
    kind: EnumerationFailureKind = EnumerationFailureKind.TRANSIENT
    reason_code: str | None = None
    remediation: str | None = None


@dataclass(frozen=True)
class KnowledgeEnumeration:
    """The complete result of one bounded connector enumeration."""

    drafts: list[KnowledgeDraft]
    cursor: dict[str, Any]
    failures: tuple[EnumerationFailure, ...] = ()
    skipped: int = 0


class KnowledgeConnector(Protocol):
    """One bounded source enumerator with a durable resume cursor."""

    source_key: str

    async def enumerate_changed(
        self,
        session: AsyncSession,
        cursor: dict[str, Any],
    ) -> KnowledgeEnumeration: ...


__all__ = [
    "EnumerationFailure",
    "EnumerationFailureKind",
    "KNOWLEDGE_SCOPE_EXTRA_KEY",
    "KnowledgeConnector",
    "KnowledgeDraft",
    "KnowledgeEnumeration",
    "KnowledgeScope",
    "UpdatedAtCursor",
]
