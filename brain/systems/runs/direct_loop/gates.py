"""Deterministic gates for agent tool access."""

from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass
class GateState:
    """Tracks deterministic gates that constrain tool access."""

    brain: bool = False
    skills: bool = False
    continuation_corrections: int = 0


def check_gate_violations(
    tool_name: str,
    block_id: str,
    gates: GateState,
    tool_handlers: dict,
    *,
    gated_tool_names: frozenset[str],
) -> dict | None:
    """Check if a tool call is blocked by gates. Returns error tool_result or None."""
    if tool_name in gated_tool_names and not gates.brain:
        if "brain_guardrails" in tool_handlers:
            try:
                guardrails = tool_handlers["brain_guardrails"]()
                gates.brain = True
                return {
                    "type": "tool_result",
                    "tool_use_id": block_id,
                    "content": (
                        f"[Brain gate] {tool_name} blocked — auto-fetched guardrails:\n"
                        + json.dumps(guardrails, default=str)
                        + "\n\nReview these, then retry."
                    ),
                    "is_error": True,
                }
            except Exception:
                pass
        if not gates.brain:
            return {
                "type": "tool_result",
                "tool_use_id": block_id,
                "content": f"[Brain gate] {tool_name} blocked — consult brain first.",
                "is_error": True,
            }

    return None
