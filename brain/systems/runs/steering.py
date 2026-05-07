"""First-class run steering."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class SteeringMessage:
    run_id: int
    content: str
    user_id: str | None = None
    created_at: datetime | None = None

    def normalized(self) -> "SteeringMessage":
        return SteeringMessage(
            run_id=self.run_id,
            content=" ".join(self.content.split()),
            user_id=self.user_id,
            created_at=self.created_at or datetime.now(timezone.utc),
        )


class SteeringInbox:
    def __init__(self) -> None:
        self._messages: dict[int, list[SteeringMessage]] = {}

    def append(self, message: SteeringMessage) -> None:
        normalized = message.normalized()
        if not normalized.content:
            return
        self._messages.setdefault(normalized.run_id, []).append(normalized)

    def drain(self, run_id: int) -> list[SteeringMessage]:
        return self._messages.pop(run_id, [])


__all__ = ["SteeringInbox", "SteeringMessage"]
