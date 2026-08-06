"""Typed coordination declarations for outbound replies."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping


class ReplyCoordinationAction(str, Enum):
    WAIT = "wait"
    REFERENCE = "reference"
    HANDOFF = "handoff"


REPLY_COORDINATION_INPUT_SCHEMA = {
    "type": "object",
    "description": (
        "Required when admission context lists an active same-thread run. Declare "
        "whether this reply will wait, reference one listed run, or hand the question off."
    ),
    "properties": {
        "action": {
            "type": "string",
            "enum": [action.value for action in ReplyCoordinationAction],
        },
        "run_id": {
            "type": "integer",
            "description": "Active run id; required when action is reference.",
        },
    },
    "required": ["action"],
}


@dataclass(frozen=True)
class ReplyCoordination:
    """A reply's declared handling of active same-thread work."""

    action: ReplyCoordinationAction
    run_id: int | None = None

    @classmethod
    def from_value(cls, value: Any) -> "ReplyCoordination | None":
        if value is None:
            return None
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            return None
        try:
            action = ReplyCoordinationAction(
                str(value.get("action") or "").strip().lower()
            )
        except ValueError:
            return None
        raw_run_id = value.get("run_id")
        try:
            run_id = int(raw_run_id) if raw_run_id not in (None, "") else None
        except (TypeError, ValueError):
            return None
        if action is ReplyCoordinationAction.REFERENCE and run_id is None:
            return None
        return cls(action=action, run_id=run_id)

    def cache_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"action": self.action.value}
        if self.run_id is not None:
            payload["run_id"] = self.run_id
        return payload


__all__ = [
    "REPLY_COORDINATION_INPUT_SCHEMA",
    "ReplyCoordination",
    "ReplyCoordinationAction",
]
