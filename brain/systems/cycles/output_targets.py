"""Cycle output target policy."""
from __future__ import annotations

from dataclasses import dataclass, field

from brain.platform.db.models.cycle import Cycle
from brain.systems.cycles.common import (
    CYCLE_LEDGER_OUTPUT_TARGET_TYPE,
    THREAD_OUTPUT_TARGET_TYPE,
    json_dict,
)


@dataclass(frozen=True)
class CycleOutputTargetSpec:
    target_type: str
    target_id: str | None
    label: str
    rationale: str
    config: dict = field(default_factory=dict)
    source_type: str = "system"

    def snapshot(self) -> dict:
        return {
            "target_type": self.target_type,
            "target_id": self.target_id,
            "label": self.label,
            "config": json_dict(self.config),
            "source_type": self.source_type,
            "rationale": self.rationale,
            "is_active": True,
        }


def default_output_target_specs(
    cycle: Cycle,
    *,
    source_type: str = "system",
    ledger_rationale: str = "Implicit durable Cycle memory target.",
    thread_rationale: str = "Implicit display thread target.",
) -> list[CycleOutputTargetSpec]:
    specs = [
        CycleOutputTargetSpec(
            target_type=CYCLE_LEDGER_OUTPUT_TARGET_TYPE,
            target_id=str(cycle.id),
            label="Cycle ledger",
            source_type=source_type,
            rationale=ledger_rationale,
        )
    ]
    if cycle.target_idea_id:
        specs.append(
            CycleOutputTargetSpec(
                target_type=THREAD_OUTPUT_TARGET_TYPE,
                target_id=str(cycle.target_idea_id),
                label="Cycle thread",
                source_type=source_type,
                rationale=thread_rationale,
            )
        )
    return specs
