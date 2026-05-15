"""Scout recipe used by Deep before escalation."""

from __future__ import annotations

from dataclasses import dataclass

from brain.systems.runs.engine import RunRecipeResult, RunRuntime
from brain.systems.runs.recipes.base import BaseRunRecipe


ESCALATION_WORDS = frozenset({"build", "fix", "implement", "investigate", "refactor", "test", "verify"})


@dataclass(frozen=True)
class ScoutHandoff:
    summary: str
    should_escalate: bool
    reasons: tuple[str, ...]

    def to_payload(self) -> dict:
        return {
            "summary": self.summary,
            "should_escalate": self.should_escalate,
            "reasons": list(self.reasons),
        }


def scout_request(message: str) -> ScoutHandoff:
    text = message.strip()
    lowered = text.lower()
    reasons: list[str] = []
    if len(text) >= 240:
        reasons.append("long request")
    matched_words = sorted(word for word in ESCALATION_WORDS if word in lowered)
    if matched_words:
        reasons.append("actionable engineering request: " + ", ".join(matched_words))
    should_escalate = bool(reasons)
    summary = "Scout recommends Deep escalation." if should_escalate else "Scout sees a direct-answer request."
    return ScoutHandoff(summary=summary, should_escalate=should_escalate, reasons=tuple(reasons))


class ScoutRecipe(BaseRunRecipe):
    name = "scout"

    async def execute(self, runtime: RunRuntime) -> RunRecipeResult:
        await runtime.activity("Scouting")
        handoff = scout_request(runtime.request.message)
        output = handoff.summary
        await runtime.text_delta(output)
        return RunRecipeResult(output=output)


__all__ = ["ScoutHandoff", "ScoutRecipe", "scout_request"]
