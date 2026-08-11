"""Scheduler program step definitions."""
from __future__ import annotations

import shlex
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

from brain.contracts.scheduler_handoff import AGENT_RUN_COMPLETION_MODE
from brain.platform.db.models.scheduler import SchedulerJob, SchedulerRun

DEFAULT_STEP_KIND = "single"
DEFAULT_COMPLETION_MODE = "command"
WRAPPER_STEP_KEY = "nightly_wrapper"
ILLO_EXTERNAL_HEARTBEAT_COMMAND = [
    "python3",
    "-m",
    "brain.jobs.pipelines.illo_heartbeat",
]


def _python_one_liner(code: str) -> list[str]:
    return ["python3", "-c", code]


def nightly_heuristic_review_command() -> list[str]:
    """Run the async heuristic review from a synchronous scheduler process."""
    return _python_one_liner(
        "import asyncio; "
        "from brain.systems.feedback.heuristics import nightly_heuristic_review; "
        "r = asyncio.run(nightly_heuristic_review()); "
        "print(f'Nightly heuristic review ran: pruned={r[\"pruned\"]}, "
        "skills_updated={r[\"skills_updated\"]}')"
    )


def nightly_meta_evolution_command() -> list[str]:
    """Run async meta-evolution from a synchronous scheduler process."""
    return _python_one_liner(
        "import asyncio; "
        "from brain.systems.feedback.meta_evolution import run_meta_evolution; "
        "stats = asyncio.run(run_meta_evolution()); "
        "print(f'Insights: {stats[\"insights_total\"]}, "
        "Regressions: {stats[\"regressions\"]}, "
        "Adjustments: {len(stats[\"adjustments\"])}')"
    )


@dataclass(frozen=True)
class StepSpec:
    step_key: str
    command: list[str]
    description: str = ""


@dataclass(frozen=True)
class SingleCommandProgram:
    """Canonical metadata shared by both scheduler step representations."""

    command: tuple[str, ...]
    step_key: str
    description: str
    completion_mode: str = DEFAULT_COMPLETION_MODE

    def build_step_plan(
        self,
        job: SchedulerJob,
        *,
        program_key: str,
    ) -> list[dict[str, object]]:
        return [
            {
                "step_key": self.step_key,
                "sequence_no": 1,
                "kind": DEFAULT_STEP_KIND,
                "handler_ref": job.handler_ref,
                "payload": {"program": program_key},
                "command": list(self.command),
            }
        ]

    def build_step_specs(self) -> list[StepSpec]:
        return [
            StepSpec(
                self.step_key,
                list(self.command),
                self.description,
            )
        ]


# Programs whose plan and StepSpec projections share one representation.
SINGLE_COMMAND_PROGRAM_REGISTRY: dict[str, SingleCommandProgram] = {
    "host_capacity": SingleCommandProgram(
        command=("python3", "-m", "brain.jobs.pipelines.host_capacity"),
        step_key="host_capacity",
        description=(
            "Measure filesystem capacity and the largest workspace consumers, "
            "then evaluate the active storage policy"
        ),
    ),
    "workspace_gc": SingleCommandProgram(
        command=("python3", "-m", "brain.jobs.pipelines.workspace_gc"),
        step_key="workspace_gc",
        description=(
            "Reclaim headless-worker workspaces past the active storage-policy retention "
            "window when parent runs are terminal or absent from the database"
        ),
    ),
    "cortex_canvas_occupancy": SingleCommandProgram(
        command=("python3", "-m", "brain.jobs.pipelines.cortex_occupancy"),
        step_key="cortex_canvas_occupancy",
        description="Move quiet emerged thoughts from the canvas into history",
    ),
    "knowledge_index_sync": SingleCommandProgram(
        command=("python3", "-m", "brain.jobs.pipelines.knowledge_index_sync"),
        step_key="knowledge_index_sync",
        description="Incrementally sync the Illo knowledge index",
    ),
    "uwear_aws_health_scan": SingleCommandProgram(
        command=("python3", "-m", "brain.jobs.pipelines.aws_health_scan"),
        step_key="uwear_aws_health_scan",
        description="Uwear AWS production health scan",
        completion_mode=AGENT_RUN_COMPLETION_MODE,
    ),
    "uwear_staging_promotion_pr": SingleCommandProgram(
        command=("python3", "-m", "brain.jobs.pipelines.staging_promotion_pr"),
        step_key="uwear_staging_promotion_pr",
        description="Ensure Uwear staging promotion pull requests exist",
    ),
}


def scheduler_program_completion_mode(job: SchedulerJob) -> str:
    """Return the completion lifecycle declared by an exact-key program."""
    definition = SINGLE_COMMAND_PROGRAM_REGISTRY.get(job.program_key)
    if definition is None:
        return DEFAULT_COMPLETION_MODE
    return definition.completion_mode


@dataclass(frozen=True)
class DivergentSchedulerProgram:
    """Exact-key builders for programs with distinct plan and StepSpec shapes."""

    plan_builder: Callable[
        [SchedulerJob, dict[str, object]],
        list[dict[str, object]],
    ]
    step_specs_builder: Callable[[SchedulerJob, SchedulerRun], list[StepSpec]]


@dataclass(frozen=True)
class NightlyProgramStep:
    """Metadata for one command exposed by the legacy StepSpec API."""

    step_key: str
    description: str | None = None


NightlyCommandFactory = Callable[[date], Sequence[Sequence[str]]]


@dataclass(frozen=True)
class NightlyStepDefinition:
    """One nightly phase and every representation derived from it."""

    step_key: str
    command_factory: NightlyCommandFactory
    description: str
    budget_hint: dict[str, object]
    program_steps: tuple[NightlyProgramStep, ...]
    runs_in_executor: bool = True

    def commands_for(self, target_date: date) -> list[list[str]]:
        commands = [list(command) for command in self.command_factory(target_date)]
        if len(commands) != len(self.program_steps):
            raise RuntimeError(
                f"Nightly step {self.step_key!r} produced {len(commands)} commands "
                f"for {len(self.program_steps)} program step definitions"
            )
        return commands


NIGHTLY_STEP_REGISTRY: tuple[NightlyStepDefinition, ...] = (
    NightlyStepDefinition(
        step_key="memory_consolidation",
        command_factory=lambda _target_date: (
            ("python3", "-m", "brain.jobs.pipelines.consolidate", "--phase", "all"),
        ),
        description="Memory consolidation",
        budget_hint={
            "work_type": "memory_conflict_resolution",
            "estimated_tokens": 18_000,
        },
        program_steps=(NightlyProgramStep("consolidate_all"),),
    ),
    NightlyStepDefinition(
        step_key="nightly_memory_maintenance",
        command_factory=lambda target_date: (
            (
                "python3",
                "-m",
                "brain.jobs.pipelines.nightly_memory_maintenance",
                "--date",
                target_date.isoformat(),
                "--apply",
            ),
        ),
        description="Auditable memory expiry maintenance",
        budget_hint={
            "work_type": "memory_conflict_resolution",
            "estimated_tokens": 1_000,
        },
        program_steps=(NightlyProgramStep("nightly_memory_maintenance"),),
    ),
    NightlyStepDefinition(
        step_key="skill_evolution",
        command_factory=lambda _target_date: (
            ("python3", "-m", "brain.app.cli.skills", "evolve"),
        ),
        description="Skill evolution",
        budget_hint={"work_type": "skill_eval", "estimated_tokens": 12_000},
        program_steps=(NightlyProgramStep("skill_evolution"),),
    ),
    NightlyStepDefinition(
        step_key="meta_learning",
        command_factory=lambda _target_date: (
            ("python3", "-m", "brain.app.cli.meta_learn", "cross-pollinate"),
            ("python3", "-m", "brain.app.cli.meta_learn", "evolve"),
        ),
        description="Meta-learning",
        budget_hint={"work_type": "skill_eval", "estimated_tokens": 10_000},
        program_steps=(
            NightlyProgramStep(
                "meta_learning_cross_pollinate",
                "Meta-learning cross-pollination",
            ),
            NightlyProgramStep("meta_learning_evolve", "Meta-learning evolve"),
        ),
    ),
    NightlyStepDefinition(
        step_key="heuristic_review",
        command_factory=lambda _target_date: (nightly_heuristic_review_command(),),
        description="Heuristic review",
        budget_hint={"work_type": "skill_eval", "estimated_tokens": 4_000},
        program_steps=(NightlyProgramStep("heuristic_review"),),
    ),
    NightlyStepDefinition(
        step_key="meta_evolution",
        command_factory=lambda _target_date: (nightly_meta_evolution_command(),),
        description="Meta-evolution",
        budget_hint={
            "work_type": "context_policy_eval",
            "estimated_tokens": 8_000,
        },
        program_steps=(NightlyProgramStep("meta_evolution"),),
    ),
    NightlyStepDefinition(
        step_key="memory_health",
        command_factory=lambda target_date: (
            (
                "python3",
                "-m",
                "brain.jobs.pipelines.nightly_memory_health",
                "--date",
                target_date.isoformat(),
            ),
        ),
        description="Reconstructive memory health inventory",
        budget_hint={
            "work_type": "memory_conflict_resolution",
            "estimated_tokens": 500,
        },
        program_steps=(NightlyProgramStep("memory_health"),),
    ),
    # Issue #424: this StepSpec existed only in the test-only get_step_specs() path
    # and has never been reachable from the production scheduler executor. Keep it
    # explicit but non-scheduled until maintainers make the product decision about
    # whether context policy evaluation should actually run nightly.
    NightlyStepDefinition(
        step_key="context_policy_eval",
        command_factory=lambda target_date: (
            (
                "python3",
                "-m",
                "brain.jobs.pipelines.nightly_context_eval",
                "--date",
                target_date.isoformat(),
            ),
        ),
        description="Context policy shadow evaluation",
        budget_hint={},
        program_steps=(NightlyProgramStep("context_policy_eval"),),
        runs_in_executor=False,
    ),
    NightlyStepDefinition(
        step_key="reflection",
        command_factory=lambda target_date: (
            (
                "python3",
                "-m",
                "brain.jobs.pipelines.nightly_reflect",
                "--date",
                target_date.isoformat(),
            ),
        ),
        description="LLM reflection",
        budget_hint={
            "work_type": "reflection_dream",
            "estimated_tokens": 10_000,
        },
        program_steps=(NightlyProgramStep("nightly_reflect"),),
    ),
    NightlyStepDefinition(
        step_key="dream",
        command_factory=lambda target_date: (
            (
                "python3",
                "-m",
                "brain.jobs.pipelines.nightly_dream",
                "--date",
                target_date.isoformat(),
            ),
        ),
        description="Dream synthesis",
        budget_hint={
            "work_type": "reflection_dream",
            "estimated_tokens": 10_000,
        },
        program_steps=(NightlyProgramStep("nightly_dream"),),
    ),
    NightlyStepDefinition(
        step_key="wake_up_index",
        command_factory=lambda _target_date: (
            ("python3", "-m", "brain.jobs.pipelines.consolidate", "--phase", "index"),
        ),
        description="Wake-up index",
        budget_hint={
            "work_type": "memory_conflict_resolution",
            "estimated_tokens": 4_000,
        },
        program_steps=(NightlyProgramStep("wake_up_index"),),
    ),
    NightlyStepDefinition(
        step_key="file_sync",
        command_factory=lambda _target_date: (
            ("python3", "-m", "brain.jobs.pipelines.sync_brain_to_files"),
        ),
        description="Brain to files sync",
        budget_hint={
            "work_type": "repo_summary_refresh",
            "estimated_tokens": 3_000,
        },
        program_steps=(NightlyProgramStep("brain_to_files_sync"),),
    ),
    NightlyStepDefinition(
        step_key="project_draft_cleanup",
        command_factory=lambda _target_date: (
            ("python3", "-m", "brain.jobs.pipelines.project_draft_cleanup"),
        ),
        description="Project draft cleanup",
        budget_hint={"work_type": "storage_cleanup", "estimated_tokens": 500},
        program_steps=(NightlyProgramStep("project_draft_cleanup"),),
    ),
    NightlyStepDefinition(
        step_key="experiment_assessment",
        command_factory=lambda target_date: (
            (
                "python3",
                "-m",
                "brain.jobs.pipelines.nightly_assess",
                "--date",
                target_date.isoformat(),
            ),
        ),
        description="Experiment assessment",
        budget_hint={
            "work_type": "context_policy_eval",
            "estimated_tokens": 6_000,
        },
        program_steps=(NightlyProgramStep("experiment_assessment"),),
    ),
    NightlyStepDefinition(
        step_key="self_improvement",
        command_factory=lambda target_date: (
            (
                "python3",
                "-m",
                "brain.jobs.pipelines.nightly_implement",
                "--date",
                target_date.isoformat(),
            ),
        ),
        description="Self-improvement",
        budget_hint={"work_type": "skill_eval", "estimated_tokens": 12_000},
        program_steps=(NightlyProgramStep("self_improvement"),),
    ),
    NightlyStepDefinition(
        step_key="daily_blog",
        command_factory=lambda target_date: (
            (
                "python3",
                str(Path("content") / "blog" / "generate_blog.py"),
                "--date",
                target_date.isoformat(),
            ),
        ),
        description="Daily blog",
        budget_hint={
            "work_type": "reflection_dream",
            "estimated_tokens": 3_000,
        },
        program_steps=(NightlyProgramStep("daily_blog"),),
    ),
)

NIGHTLY_SLEEP_STEP_KEYS: tuple[str, ...] = tuple(
    definition.step_key
    for definition in NIGHTLY_STEP_REGISTRY
    if definition.runs_in_executor
)

NIGHTLY_SLEEP_STEP_BUDGET_HINTS: dict[str, dict[str, object]] = {
    definition.step_key: dict(definition.budget_hint)
    for definition in NIGHTLY_STEP_REGISTRY
    if definition.runs_in_executor
}

_NIGHTLY_SCHEDULER_STEPS_BY_KEY = {
    definition.step_key: definition
    for definition in NIGHTLY_STEP_REGISTRY
    if definition.runs_in_executor
}


def nightly_commands(target_date: date) -> list[list[str]]:
    return [
        command
        for definition in NIGHTLY_STEP_REGISTRY
        if definition.runs_in_executor
        for command in definition.commands_for(target_date)
    ]


def nightly_commands_for_step(step_key: str, target_date: date) -> list[list[str]]:
    definition = _NIGHTLY_SCHEDULER_STEPS_BY_KEY.get(step_key)
    if definition is None:
        return []
    return definition.commands_for(target_date)


def _night_budget_hint(step_key: str) -> dict[str, object]:
    hint = NIGHTLY_SLEEP_STEP_BUDGET_HINTS.get(step_key)
    return dict(hint or {})


def _nightly_planned_step_keys(payload: dict[str, object]) -> tuple[str, ...]:
    allowed_steps = payload.get("night_budget_allowed_steps")
    if not isinstance(allowed_steps, list):
        return NIGHTLY_SLEEP_STEP_KEYS
    allowed = {str(step) for step in allowed_steps}
    return tuple(step_key for step_key in NIGHTLY_SLEEP_STEP_KEYS if step_key in allowed)


def _nightly_step_payload(step_key: str) -> dict[str, object]:
    payload: dict[str, object] = {"phase": step_key}
    hint = _night_budget_hint(step_key)
    if hint:
        payload["night_budget"] = hint
    return payload


def _job_identity(job: SchedulerJob) -> str:
    return " ".join(
        [
            job.job_key.lower(),
            job.family.lower(),
            job.program_key.lower(),
            (job.handler_ref or "").lower(),
            ((job.default_payload or {}).get("name") or "").lower(),
        ]
    )


def _legacy_job_identity_contains(job: SchedulerJob, *fragments: str) -> bool:
    """Quarantine substring dispatch retained for non-canonical legacy programs."""
    identity = _job_identity(job)
    return any(fragment in identity for fragment in fragments)


def _build_nightly_scheduler_step_plan(
    job: SchedulerJob,
    payload: dict[str, object],
) -> list[dict[str, object]]:
    if payload.get("scheduler_split_steps"):
        step_keys = _nightly_planned_step_keys(payload)
        return [
            {
                "step_key": WRAPPER_STEP_KEY,
                "sequence_no": 1,
                "kind": "wrapper",
                "handler_ref": job.handler_ref,
                "payload": {
                    "mode": "wrapper",
                    "night_budget": {
                        "mode": "advisory",
                        "planner": "brain.systems.learning.night_budget:build_night_budget_plan",
                    },
                },
            },
            *[
                {
                    "step_key": step_key,
                    "sequence_no": index + 2,
                    "kind": "phase",
                    "handler_ref": job.handler_ref,
                    "payload": _nightly_step_payload(step_key),
                }
                for index, step_key in enumerate(step_keys)
            ],
        ]
    return [
        {
            "step_key": WRAPPER_STEP_KEY,
            "sequence_no": 1,
            "kind": "wrapper",
            "handler_ref": job.handler_ref,
            "payload": {"mode": "wrapper"},
        }
    ]


def _build_curiosity_scheduler_step_plan(
    job: SchedulerJob,
    payload: dict[str, object],
) -> list[dict[str, object]]:
    return [
        {
            "step_key": "curiosity",
            "sequence_no": 1,
            "kind": "single",
            "handler_ref": job.handler_ref,
            "payload": {"program": "curiosity"},
        }
    ]


def _build_illo_external_heartbeat_scheduler_step_plan(
    job: SchedulerJob,
    payload: dict[str, object],
) -> list[dict[str, object]]:
    return [
        {
            "step_key": "illo_external_heartbeat",
            "sequence_no": 1,
            "kind": "single",
            "handler_ref": job.handler_ref,
            "payload": {"program": "illo_external_heartbeat"},
            "command": ILLO_EXTERNAL_HEARTBEAT_COMMAND,
        }
    ]


def _build_fallback_scheduler_step_plan(
    job: SchedulerJob,
    payload: dict[str, object],
) -> list[dict[str, object]]:
    return [
        {
            "step_key": job.program_key or "scheduler_job",
            "sequence_no": 1,
            "kind": DEFAULT_STEP_KIND,
            "handler_ref": job.handler_ref,
            "payload": {},
        }
    ]


def _build_legacy_scheduler_step_plan(
    job: SchedulerJob,
    payload: dict[str, object],
) -> list[dict[str, object]]:
    """Terminal substring compatibility fallback for non-catalog programs."""
    if _legacy_job_identity_contains(job, "nightly", "sleep"):
        return _build_nightly_scheduler_step_plan(job, payload)
    if _legacy_job_identity_contains(job, "curiosity"):
        return _build_curiosity_scheduler_step_plan(job, payload)
    if _legacy_job_identity_contains(job, "illo_external_heartbeat"):
        return _build_illo_external_heartbeat_scheduler_step_plan(job, payload)
    return _build_fallback_scheduler_step_plan(job, payload)


def build_scheduler_step_plan(job: SchedulerJob) -> list[dict[str, object]]:
    """Return persisted step metadata for a scheduler run."""
    payload = job.default_payload or {}
    custom_plan = payload.get("step_plan")
    if isinstance(custom_plan, list) and custom_plan:
        plan: list[dict[str, object]] = []
        for index, step in enumerate(custom_plan, start=1):
            if not isinstance(step, dict):
                continue
            step_key = str(step.get("step_key") or step.get("key") or f"step_{index}")
            projected_step: dict[str, object] = {
                "step_key": step_key,
                "sequence_no": int(step.get("sequence_no") or index),
                "kind": str(step.get("kind") or DEFAULT_STEP_KIND),
                "handler_ref": step.get("handler_ref") or job.handler_ref,
                "payload": step.get("payload") or {},
                "command": step.get("command"),
                "commands": step.get("commands"),
            }
            if step.get("completion_mode"):
                projected_step["completion_mode"] = str(step["completion_mode"])
            plan.append(projected_step)
        if plan:
            return plan

    definition = SINGLE_COMMAND_PROGRAM_REGISTRY.get(job.program_key)
    if definition is not None:
        return definition.build_step_plan(job, program_key=job.program_key)

    divergent_definition = DIVERGENT_PROGRAM_REGISTRY.get(job.program_key)
    if divergent_definition is not None:
        return divergent_definition.plan_builder(job, payload)

    return _build_legacy_scheduler_step_plan(job, payload)


def _timezone(job: SchedulerJob) -> ZoneInfo:
    try:
        return ZoneInfo(job.timezone)
    except Exception:
        return ZoneInfo("UTC")


def _target_date(job: SchedulerJob, run: SchedulerRun) -> date:
    return run.scheduled_for.astimezone(_timezone(job)).date()


def _nightly_steps(job: SchedulerJob, run: SchedulerRun) -> list[StepSpec]:
    target_date = _target_date(job, run)
    steps: list[StepSpec] = []
    for definition in NIGHTLY_STEP_REGISTRY:
        commands = definition.commands_for(target_date)
        steps.extend(
            StepSpec(
                program_step.step_key,
                command,
                program_step.description or definition.description,
            )
            for program_step, command in zip(
                definition.program_steps,
                commands,
                strict=True,
            )
        )
    return steps


def _curiosity_steps(job: SchedulerJob, run: SchedulerRun) -> list[StepSpec]:
    return [
        StepSpec("curiosity_reading", ["python3", "-m", "brain.jobs.pipelines.curiosity"], "Curiosity reading cycle"),
    ]


def _illo_external_heartbeat_steps(
    job: SchedulerJob,
    run: SchedulerRun,
) -> list[StepSpec]:
    return [
        StepSpec(
            "program",
            ["python3", "-m", "illo_external_heartbeat"],
            "Program fallback",
        )
    ]


def _fallback_steps(job: SchedulerJob, run: SchedulerRun) -> list[StepSpec]:
    payload = job.default_payload or {}
    command = payload.get("command")
    if isinstance(command, str) and command.strip():
        return [StepSpec("run_command", shlex.split(command.strip()), "Scheduler command")]
    if job.handler_kind == "shell_script" and job.handler_ref:
        return [StepSpec("shell_script", ["bash", job.handler_ref], "Shell script")]
    return [StepSpec("program", ["python3", "-m", job.program_key], "Program fallback")]


DIVERGENT_PROGRAM_REGISTRY: dict[str, DivergentSchedulerProgram] = {
    "nightly_sleep": DivergentSchedulerProgram(
        plan_builder=_build_nightly_scheduler_step_plan,
        step_specs_builder=_nightly_steps,
    ),
    "curiosity": DivergentSchedulerProgram(
        plan_builder=_build_curiosity_scheduler_step_plan,
        step_specs_builder=_curiosity_steps,
    ),
    "illo_external_heartbeat": DivergentSchedulerProgram(
        plan_builder=_build_illo_external_heartbeat_scheduler_step_plan,
        step_specs_builder=_illo_external_heartbeat_steps,
    ),
}


def _get_legacy_step_specs(job: SchedulerJob, run: SchedulerRun) -> list[StepSpec]:
    """Terminal substring compatibility fallback for non-catalog programs."""
    if _legacy_job_identity_contains(job, "curiosity"):
        return _curiosity_steps(job, run)
    if _legacy_job_identity_contains(job, "nightly", "sleep"):
        return _nightly_steps(job, run)
    return _fallback_steps(job, run)


def get_step_specs(job: SchedulerJob, run: SchedulerRun) -> list[StepSpec]:
    definition = SINGLE_COMMAND_PROGRAM_REGISTRY.get(job.program_key)
    if definition is not None:
        return definition.build_step_specs()

    divergent_definition = DIVERGENT_PROGRAM_REGISTRY.get(job.program_key)
    if divergent_definition is not None:
        return divergent_definition.step_specs_builder(job, run)

    return _get_legacy_step_specs(job, run)
