"""Scheduler program step definitions."""
from __future__ import annotations

from dataclasses import dataclass
import shlex
from pathlib import Path
from zoneinfo import ZoneInfo

from brain.platform.db.models.scheduler import SchedulerJob, SchedulerRun

NIGHTLY_SLEEP_STEP_KEYS: tuple[str, ...] = (
    "memory_consolidation",
    "skill_evolution",
    "meta_learning",
    "heuristic_review",
    "meta_evolution",
    "reflection",
    "dream",
    "wake_up_index",
    "file_sync",
    "project_draft_cleanup",
    "experiment_assessment",
    "self_improvement",
    "daily_blog",
)

DEFAULT_STEP_KIND = "single"
WRAPPER_STEP_KEY = "nightly_wrapper"

NIGHTLY_SLEEP_STEP_BUDGET_HINTS: dict[str, dict[str, object]] = {
    "memory_consolidation": {
        "work_type": "memory_conflict_resolution",
        "estimated_tokens": 18_000,
    },
    "skill_evolution": {
        "work_type": "skill_eval",
        "estimated_tokens": 12_000,
    },
    "meta_learning": {
        "work_type": "skill_eval",
        "estimated_tokens": 10_000,
    },
    "heuristic_review": {
        "work_type": "skill_eval",
        "estimated_tokens": 4_000,
    },
    "meta_evolution": {
        "work_type": "context_policy_eval",
        "estimated_tokens": 8_000,
    },
    "reflection": {
        "work_type": "reflection_dream",
        "estimated_tokens": 10_000,
    },
    "dream": {
        "work_type": "reflection_dream",
        "estimated_tokens": 10_000,
    },
    "wake_up_index": {
        "work_type": "memory_conflict_resolution",
        "estimated_tokens": 4_000,
    },
    "file_sync": {
        "work_type": "repo_summary_refresh",
        "estimated_tokens": 3_000,
    },
    "project_draft_cleanup": {
        "work_type": "storage_cleanup",
        "estimated_tokens": 500,
    },
    "experiment_assessment": {
        "work_type": "context_policy_eval",
        "estimated_tokens": 6_000,
    },
    "self_improvement": {
        "work_type": "skill_eval",
        "estimated_tokens": 12_000,
    },
    "daily_blog": {
        "work_type": "reflection_dream",
        "estimated_tokens": 3_000,
    },
}


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
            plan.append(
                {
                    "step_key": step_key,
                    "sequence_no": int(step.get("sequence_no") or index),
                    "kind": str(step.get("kind") or DEFAULT_STEP_KIND),
                    "handler_ref": step.get("handler_ref") or job.handler_ref,
                    "payload": step.get("payload") or {},
                    "command": step.get("command"),
                    "commands": step.get("commands"),
                }
            )
        if plan:
            return plan

    identity = _job_identity(job)
    if job.program_key == "nightly_sleep" or "nightly" in identity or "sleep" in identity:
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

    if job.program_key == "curiosity" or "curiosity" in identity:
        return [
            {
                "step_key": "curiosity",
                "sequence_no": 1,
                "kind": "single",
                "handler_ref": job.handler_ref,
                "payload": {"program": "curiosity"},
            }
        ]

    return [
        {
            "step_key": job.program_key or "scheduler_job",
            "sequence_no": 1,
            "kind": DEFAULT_STEP_KIND,
            "handler_ref": job.handler_ref,
            "payload": {},
        }
    ]


@dataclass(frozen=True)
class StepSpec:
    step_key: str
    command: list[str]
    description: str = ""


def _timezone(job: SchedulerJob) -> ZoneInfo:
    try:
        return ZoneInfo(job.timezone)
    except Exception:
        return ZoneInfo("UTC")


def _target_date(job: SchedulerJob, run: SchedulerRun) -> str:
    return run.scheduled_for.astimezone(_timezone(job)).date().isoformat()


def _python_one_liner(code: str) -> list[str]:
    return ["python3", "-c", code]


def _nightly_steps(job: SchedulerJob, run: SchedulerRun) -> list[StepSpec]:
    target_date = _target_date(job, run)
    return [
        StepSpec("consolidate_all", ["python3", "-m", "brain.jobs.pipelines.consolidate", "--phase", "all"], "Memory consolidation"),
        StepSpec("skill_evolution", ["python3", "-m", "brain.app.cli.skills", "evolve"], "Skill evolution"),
        StepSpec("meta_learning_cross_pollinate", ["python3", "-m", "brain.app.cli.meta_learn", "cross-pollinate"], "Meta-learning cross-pollination"),
        StepSpec("meta_learning_evolve", ["python3", "-m", "brain.app.cli.meta_learn", "evolve"], "Meta-learning evolve"),
        StepSpec(
            "heuristic_review",
            _python_one_liner(
                "from brain.systems.feedback.heuristics import nightly_heuristic_review; "
                "r = nightly_heuristic_review(); "
                "print(f'Pruned: {r[\"pruned\"]}, Fitness updated: {r[\"skills_updated\"]}')"
            ),
            "Heuristic review",
        ),
        StepSpec(
            "meta_evolution",
            _python_one_liner(
                "from brain.systems.feedback.meta_evolution import run_meta_evolution; "
                "stats = run_meta_evolution(); "
                "print(f'Insights: {stats[\"insights_total\"]}, Regressions: {stats[\"regressions\"]}, Adjustments: {len(stats[\"adjustments\"])}')"
            ),
            "Meta-evolution",
        ),
        StepSpec("context_policy_eval", ["python3", "-m", "brain.jobs.pipelines.nightly_context_eval", "--date", target_date], "Context policy shadow evaluation"),
        StepSpec("nightly_reflect", ["python3", "-m", "brain.jobs.pipelines.nightly_reflect", "--date", target_date], "LLM reflection"),
        StepSpec("nightly_dream", ["python3", "-m", "brain.jobs.pipelines.nightly_dream", "--date", target_date], "Dream synthesis"),
        StepSpec("wake_up_index", ["python3", "-m", "brain.jobs.pipelines.consolidate", "--phase", "index"], "Wake-up index"),
        StepSpec("brain_to_files_sync", ["python3", "-m", "brain.jobs.pipelines.sync_brain_to_files"], "Brain to files sync"),
        StepSpec("project_draft_cleanup", ["python3", "-m", "brain.jobs.pipelines.project_draft_cleanup"], "Project draft cleanup"),
        StepSpec("experiment_assessment", ["python3", "-m", "brain.jobs.pipelines.nightly_assess", "--date", target_date], "Experiment assessment"),
        StepSpec("self_improvement", ["python3", "-m", "brain.jobs.pipelines.nightly_implement", "--date", target_date], "Self-improvement"),
        StepSpec("daily_blog", ["python3", str(Path("content") / "blog" / "generate_blog.py"), "--date", target_date], "Daily blog"),
    ]


def _curiosity_steps(job: SchedulerJob, run: SchedulerRun) -> list[StepSpec]:
    return [
        StepSpec("curiosity_reading", ["python3", "-m", "brain.jobs.pipelines.curiosity"], "Curiosity reading cycle"),
    ]


def _fallback_steps(job: SchedulerJob, run: SchedulerRun) -> list[StepSpec]:
    payload = job.default_payload or {}
    command = payload.get("command")
    if isinstance(command, str) and command.strip():
        return [StepSpec("run_command", shlex.split(command.strip()), "Scheduler command")]
    if job.handler_kind == "shell_script" and job.handler_ref:
        return [StepSpec("shell_script", ["bash", job.handler_ref], "Shell script")]
    return [StepSpec("program", ["python3", "-m", job.program_key], "Program fallback")]


def get_step_specs(job: SchedulerJob, run: SchedulerRun) -> list[StepSpec]:
    key = _job_identity(job)
    if "curiosity" in key:
        return _curiosity_steps(job, run)
    if "nightly" in key or "sleep" in key:
        return _nightly_steps(job, run)
    return _fallback_steps(job, run)
