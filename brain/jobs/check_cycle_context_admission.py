"""Read-only deploy gate for enabled Cycle context admission."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Sequence

from sqlalchemy import select

from brain.platform.db.models.cycle import (
    Cycle,
    CycleGuidance,
    CycleOutputTarget,
    CycleRun,
)
from brain.platform.db.repositories.unit_of_work import UnitOfWork
from brain.platform.providers.model_policy import (
    async_get_default_model,
    async_get_default_thinking,
    infer_provider_from_model,
)
from brain.systems.context.errors import ContextFloorExceedsBudgetError
from brain.systems.context.window_policy import ContextWindowPolicy
from brain.systems.cycles.prompts import cycle_run_message
from brain.systems.runs.direct_agent import _build_reasoning_effort
from brain.systems.runs.direct_loop.request import build_system_blocks
from brain.systems.runs.recipes.fast import (
    build_fast_system_prompt,
    fast_agent_tools_for_request,
)

DEFAULT_FIXTURE_PATH = (
    Path(__file__).resolve().parents[2]
    / "tests"
    / "fixtures"
    / "enabled_cycles_context_admission.json"
)
DEFAULT_HEADROOM_WARNING_RATIO = 0.80


@dataclass(frozen=True, slots=True)
class CycleAdmissionSpec:
    cycle_id: int
    name: str
    prompt: str
    model: str
    thinking: str
    timezone_name: str = "UTC"
    target_idea_id: str | None = None
    guidance_snapshot: list[dict[str, Any]] = field(default_factory=list)
    output_targets_snapshot: list[dict[str, Any]] = field(default_factory=list)
    context_snapshot: dict[str, Any] = field(default_factory=dict)


def _cycle_message(spec: CycleAdmissionSpec) -> str:
    scheduled_for = datetime(2026, 1, 1, tzinfo=timezone.utc)
    idea_id = spec.target_idea_id or f"cycle-context-gate-{spec.cycle_id}"
    idea = SimpleNamespace(id=idea_id, title=spec.name)
    cycle = SimpleNamespace(
        id=spec.cycle_id,
        name=spec.name,
        prompt=spec.prompt,
        timezone=spec.timezone_name,
        model_override=spec.model,
        thinking_override=spec.thinking,
    )
    run = SimpleNamespace(
        id=-abs(spec.cycle_id),
        revision_id=None,
        scheduled_for=scheduled_for,
        guidance_snapshot=spec.guidance_snapshot,
        output_targets_snapshot=spec.output_targets_snapshot,
        context_snapshot=spec.context_snapshot,
    )
    return cycle_run_message(idea, cycle, run)


def check_cycle_context_admission(
    spec: CycleAdmissionSpec,
    *,
    warning_ratio: float = DEFAULT_HEADROOM_WARNING_RATIO,
) -> dict[str, Any]:
    """Measure one Cycle with the Fast recipe's real prompt and tool scaffold."""

    metadata = {"source": "cycle", "origin": "cycle", "cycle_id": spec.cycle_id}
    target_ref = {"kind": "cortex_idea", "idea_id": spec.target_idea_id}
    tools = fast_agent_tools_for_request(
        metadata=metadata,
        target_ref=target_ref,
        thread_id=str(spec.target_idea_id or f"cycle:{spec.cycle_id}"),
    )
    system = build_system_blocks(None, build_fast_system_prompt(), False)
    reasoning_effort, max_output_tokens = _build_reasoning_effort(spec.thinking)
    policy = ContextWindowPolicy.resolve(
        model=spec.model,
        provider=infer_provider_from_model(spec.model),
        reasoning_effort=reasoning_effort,
        max_output_tokens=max_output_tokens,
        tools=tools,
    )
    try:
        admission = policy.admit(
            [{"role": "user", "content": _cycle_message(spec)}],
            system=system,
            tools=tools,
            session_id=f"cycle-context-gate-{spec.cycle_id}",
            phase="deploy_gate",
        )
    except ContextFloorExceedsBudgetError as exc:
        return {
            "cycle_id": spec.cycle_id,
            "cycle_name": spec.name,
            "status": "failed",
            "model": spec.model,
            "thinking": spec.thinking,
            "floor": exc.floor,
            "ceiling": exc.ceiling,
            "tools": exc.tools,
            "diagnostic": str(exc),
        }

    ratio = admission.floor_tokens / admission.budget.auto_compact_threshold_tokens
    return {
        "cycle_id": spec.cycle_id,
        "cycle_name": spec.name,
        "status": "warning" if ratio > warning_ratio else "passed",
        "model": spec.model,
        "thinking": spec.thinking,
        "floor": admission.floor_tokens,
        "ceiling": admission.budget.auto_compact_threshold_tokens,
        "tools": admission.tool_count,
        "headroom_ratio": round(1.0 - ratio, 6),
    }


def load_fixture_specs(path: Path = DEFAULT_FIXTURE_PATH) -> list[CycleAdmissionSpec]:
    payload = json.loads(path.read_text())
    specs: list[CycleAdmissionSpec] = []
    for item in payload.get("cycles", []):
        prompt_chars = max(0, int(item.get("prompt_chars") or 0))
        guidance_chars = max(0, int(item.get("guidance_chars") or 0))
        specs.append(
            CycleAdmissionSpec(
                cycle_id=int(item["cycle_id"]),
                name=str(item["name"]),
                prompt="P" * prompt_chars,
                model=str(item["model"]),
                thinking=str(item["thinking"]),
                timezone_name=str(item.get("timezone") or "UTC"),
                guidance_snapshot=(
                    [{"guidance": "G" * guidance_chars}] if guidance_chars else []
                ),
            )
        )
    return specs


async def load_live_specs() -> list[CycleAdmissionSpec]:
    """Read every enabled Cycle and its current persisted memory without writes."""

    specs: list[CycleAdmissionSpec] = []
    async with UnitOfWork() as uow:
        cycles = (
            await uow.session.scalars(
                select(Cycle)
                .where(Cycle.enabled.is_(True), Cycle.deleted_at.is_(None))
                .order_by(Cycle.id.asc())
            )
        ).all()
        for cycle in cycles:
            latest_run = await uow.session.scalar(
                select(CycleRun)
                .where(CycleRun.cycle_id == int(cycle.id))
                .order_by(CycleRun.scheduled_for.desc(), CycleRun.id.desc())
                .limit(1)
            )
            guidances = (
                await uow.session.scalars(
                    select(CycleGuidance)
                    .where(
                        CycleGuidance.cycle_id == int(cycle.id),
                        CycleGuidance.is_active.is_(True),
                    )
                    .order_by(CycleGuidance.created_at.asc(), CycleGuidance.id.asc())
                )
            ).all()
            output_targets = (
                await uow.session.scalars(
                    select(CycleOutputTarget)
                    .where(
                        CycleOutputTarget.cycle_id == int(cycle.id),
                        CycleOutputTarget.is_active.is_(True),
                    )
                    .order_by(CycleOutputTarget.created_at.asc(), CycleOutputTarget.id.asc())
                )
            ).all()
            model = str(cycle.model_override or "").strip() or await async_get_default_model(
                uow.session,
                include_provider_prefix=True,
                user_id=str(cycle.user_id),
                org_id=str(cycle.org_id or "") or None,
            )
            thinking = str(cycle.thinking_override or "").strip() or await async_get_default_thinking(
                uow.session,
                user_id=str(cycle.user_id),
                org_id=str(cycle.org_id or "") or None,
            )
            specs.append(
                CycleAdmissionSpec(
                    cycle_id=int(cycle.id),
                    name=str(cycle.name),
                    prompt=str(cycle.prompt),
                    model=model,
                    thinking=thinking,
                    timezone_name=str(cycle.timezone or "UTC"),
                    target_idea_id=str(cycle.target_idea_id or "") or None,
                    guidance_snapshot=[
                        {
                            "id": item.id,
                            "guidance": item.guidance,
                            "rationale": item.rationale,
                        }
                        for item in guidances
                    ],
                    output_targets_snapshot=[
                        {
                            "id": item.id,
                            "target_type": item.target_type,
                            "target_id": item.target_id,
                            "label": item.label,
                            "config": dict(item.config or {}),
                        }
                        for item in output_targets
                    ],
                    context_snapshot=dict(getattr(latest_run, "context_snapshot", None) or {}),
                )
            )
        await uow.session.rollback()
    return specs


def evaluate_specs(specs: Sequence[CycleAdmissionSpec]) -> dict[str, Any]:
    results = [check_cycle_context_admission(spec) for spec in specs]
    return {
        "ok": bool(results) and all(item["status"] != "failed" for item in results),
        "cycle_count": len(results),
        "results": results,
    }


async def _async_main(args: argparse.Namespace) -> int:
    specs = await load_live_specs() if args.live else load_fixture_specs(args.fixtures)
    report = evaluate_specs(specs)
    print(json.dumps(report, sort_keys=True))
    return 0 if report["ok"] else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--live", action="store_true", help="Read enabled Cycles from the live DB")
    source.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURE_PATH)
    return asyncio.run(_async_main(parser.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CycleAdmissionSpec",
    "check_cycle_context_admission",
    "evaluate_specs",
    "load_fixture_specs",
    "load_live_specs",
]
