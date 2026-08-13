"""Shared normalization for AgentRun thread references."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any
from uuid import UUID


THREAD_DISCUSSION_THREAD_PREFIX = "thread-discussion:"
INBOUND_THREAD_PREFIX = "inbound:"


class IdeaThreadReferenceKind(str, Enum):
    """How an AgentRun thread reference relates to an Idea."""

    IDEA = "idea"
    INBOUND = "inbound"
    NOT_IDEA_BACKED = "not_idea_backed"


class IdeaBackedThreadRequiredError(ValueError):
    """Raised when an operation needs an Idea-backed AgentRun thread."""


@dataclass(frozen=True)
class IdeaThreadReference:
    """The normalized Idea identity, or why the thread has none."""

    kind: IdeaThreadReferenceKind
    idea_id: str | None = None

    def require_idea_id(self, *, operation: str) -> str:
        if self.idea_id is not None:
            return self.idea_id
        if self.kind is IdeaThreadReferenceKind.INBOUND:
            raise IdeaBackedThreadRequiredError(
                f"{operation} need an idea-backed thread; this run is a headless inbound submission"
            )
        raise IdeaBackedThreadRequiredError(
            f"{operation} need an idea-backed thread with a valid UUID idea_id"
        )


def resolve_idea_thread_reference(value: Any) -> IdeaThreadReference:
    """Resolve a run thread reference without sending invalid UUIDs to storage."""

    text = str(value or "").strip()
    if text.startswith(THREAD_DISCUSSION_THREAD_PREFIX):
        text = text.removeprefix(THREAD_DISCUSSION_THREAD_PREFIX).strip()
    elif text.startswith(INBOUND_THREAD_PREFIX):
        parts = text.split(":")
        if len(parts) == 3:
            try:
                UUID(parts[1])
                UUID(parts[2])
            except (TypeError, ValueError):
                pass
            else:
                return IdeaThreadReference(IdeaThreadReferenceKind.INBOUND)

    try:
        idea_id = str(UUID(text))
    except (TypeError, ValueError, AttributeError):
        return IdeaThreadReference(IdeaThreadReferenceKind.NOT_IDEA_BACKED)
    return IdeaThreadReference(IdeaThreadReferenceKind.IDEA, idea_id)


__all__ = [
    "IdeaBackedThreadRequiredError",
    "IdeaThreadReference",
    "IdeaThreadReferenceKind",
    "THREAD_DISCUSSION_THREAD_PREFIX",
    "resolve_idea_thread_reference",
]
