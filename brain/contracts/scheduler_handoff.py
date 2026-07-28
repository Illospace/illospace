"""Typed transport contract for scheduler-to-AgentRun handoffs."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

AGENT_RUN_COMPLETION_MODE = "agent_run"
HANDOFF_TYPE = "scheduler.detached_agent_run"
HANDOFF_VERSION = 1
HANDOFF_STATUS_DISPATCHED = "dispatched"
_HANDOFF_FIELDS = frozenset(
    {
        "type",
        "version",
        "status",
        "scheduler_agent_run_id",
    }
)


class DetachedAgentRunHandoffError(ValueError):
    """Raised when a declared detached-run command emits an invalid handoff."""


@dataclass(frozen=True)
class DetachedAgentRunHandoff:
    """Validated scheduler-to-AgentRun dispatch envelope."""

    agent_run_id: int

    def __post_init__(self) -> None:
        if (
            isinstance(self.agent_run_id, bool)
            or not isinstance(self.agent_run_id, int)
            or self.agent_run_id <= 0
        ):
            raise DetachedAgentRunHandoffError(
                "scheduler_agent_run_id must be a positive integer"
            )

    def as_payload(self) -> dict[str, Any]:
        return {
            "type": HANDOFF_TYPE,
            "version": HANDOFF_VERSION,
            "status": HANDOFF_STATUS_DISPATCHED,
            "scheduler_agent_run_id": self.agent_run_id,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> DetachedAgentRunHandoff:
        fields = frozenset(payload)
        if fields != _HANDOFF_FIELDS:
            missing = sorted(_HANDOFF_FIELDS - fields)
            unexpected = sorted(fields - _HANDOFF_FIELDS)
            details = []
            if missing:
                details.append(f"missing fields: {', '.join(missing)}")
            if unexpected:
                details.append(f"unexpected fields: {', '.join(unexpected)}")
            raise DetachedAgentRunHandoffError(
                "invalid detached AgentRun handoff envelope"
                + (f" ({'; '.join(details)})" if details else "")
            )
        if payload["type"] != HANDOFF_TYPE:
            raise DetachedAgentRunHandoffError(
                f"handoff type must be {HANDOFF_TYPE!r}"
            )
        if (
            isinstance(payload["version"], bool)
            or not isinstance(payload["version"], int)
            or payload["version"] != HANDOFF_VERSION
        ):
            raise DetachedAgentRunHandoffError(
                f"handoff version must be {HANDOFF_VERSION}"
            )
        if payload["status"] != HANDOFF_STATUS_DISPATCHED:
            raise DetachedAgentRunHandoffError(
                f"handoff status must be {HANDOFF_STATUS_DISPATCHED!r}"
            )
        agent_run_id = payload["scheduler_agent_run_id"]
        if isinstance(agent_run_id, bool) or not isinstance(agent_run_id, int):
            raise DetachedAgentRunHandoffError(
                "scheduler_agent_run_id must be a positive integer"
            )
        return cls(agent_run_id=agent_run_id)


def emit_detached_agent_run_handoff(agent_run_id: int) -> str:
    """Serialize the one supported detached AgentRun handoff envelope."""
    handoff = DetachedAgentRunHandoff(agent_run_id=agent_run_id)
    return json.dumps(handoff.as_payload(), sort_keys=True)


def parse_detached_agent_run_handoff(stdout: str | None) -> DetachedAgentRunHandoff:
    """Parse and fully validate a declared detached-run command's stdout."""
    for line in reversed(str(stdout or "").splitlines()):
        try:
            payload = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue
        if (
            payload.get("type") != HANDOFF_TYPE
            and "scheduler_agent_run_id" not in payload
        ):
            continue
        return DetachedAgentRunHandoff.from_payload(payload)
    raise DetachedAgentRunHandoffError(
        "declared detached AgentRun command did not emit a handoff envelope"
    )
