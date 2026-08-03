"""Connector contract for bounded, incremental knowledge enumeration."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class KnowledgeDraft:
    """A source row ready for connector-agnostic indexing.

    ``distill`` is the slice-2 seam for conversational sources. Slice-1
    structural connectors emit their already-distilled fields directly.
    """

    source: str
    kind: str
    source_ref: str
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
    "KnowledgeConnector",
    "KnowledgeDraft",
    "KnowledgeEnumeration",
]
