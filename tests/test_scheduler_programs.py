"""Focused contract tests for scheduler program dispatch."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
import json
from types import SimpleNamespace

from brain.app.scheduler.programs import (
    SINGLE_COMMAND_PROGRAM_REGISTRY,
    SingleCommandProgram,
    StepSpec,
    build_scheduler_step_plan,
    get_step_specs,
)


_SCHEDULED_FOR = datetime(2026, 7, 27, 3, 0, tzinfo=timezone.utc)
_PINNED_PROJECTION_HASHES = {
    "nightly_sleep": "0cb9173b9e0b71a094063bb867d644304f4c8b996fec6c9d1656b31dde060ee0",
    "curiosity_cron": "326e1c241800e49721978238c967c423e1ce1133030ca1223c77bb3f44a8519d",
    "uwear_aws_health_scan": (
        "10d9a7cb36fa4e7b93d7bb2a76b73228d893420220f13e93a17a85f2f23d87ee"
    ),
    "uwear_staging_promotion_pr": (
        "073a6f20f32cf4d6e4db66f7f3e4a151c095728ee199ce29a0e2dcfe12d71e6b"
    ),
    "illo_external_heartbeat": (
        "5c2f2eede89b1fd4cc5cd067116eb62a02444e0152106325ee133fad9f2bee3b"
    ),
}


def _job(program_name: str) -> SimpleNamespace:
    definitions = {
        "nightly_sleep": {
            "job_key": "nightly_sleep",
            "family": "nightly_sleep",
            "program_key": "nightly_sleep",
            "handler_ref": "brain.app.scheduler.programs:nightly_sleep",
            "default_payload": {
                "name": "Nightly Sleep",
                "scheduler_split_steps": True,
            },
        },
        "curiosity_cron": {
            "job_key": "curiosity_cron",
            "family": "curiosity_cron",
            "program_key": "curiosity",
            "handler_ref": "brain.app.scheduler.programs:curiosity",
            "default_payload": {"name": "Curiosity Engine"},
        },
        "uwear_aws_health_scan": {
            "job_key": "uwear_aws_health_scan",
            "family": "uwear_aws_health_scan",
            "program_key": "uwear_aws_health_scan",
            "handler_ref": "brain.app.scheduler.programs:uwear_aws_health_scan",
            "default_payload": {"name": "Uwear AWS Health Scan"},
        },
        "uwear_staging_promotion_pr": {
            "job_key": "uwear_staging_promotion_pr",
            "family": "uwear_staging_promotion_pr",
            "program_key": "uwear_staging_promotion_pr",
            "handler_ref": "brain.app.scheduler.programs:uwear_staging_promotion_pr",
            "default_payload": {"name": "Uwear Staging Promotion PR"},
        },
        "illo_external_heartbeat": {
            "job_key": "illo_external_heartbeat",
            "family": "illo_external_heartbeat",
            "program_key": "illo_external_heartbeat",
            "handler_ref": "brain.app.scheduler.programs:illo_external_heartbeat",
            "default_payload": {"name": "Illo External Heartbeat"},
        },
    }
    return SimpleNamespace(
        **definitions[program_name],
        handler_kind="scheduler_builtin",
        timezone="UTC",
    )


def _projection_bytes(program_name: str) -> bytes:
    job = _job(program_name)
    run = SimpleNamespace(scheduled_for=_SCHEDULED_FOR)
    projection = {
        "plan": build_scheduler_step_plan(job),
        "specs": [asdict(step) for step in get_step_specs(job, run)],
    }
    return json.dumps(
        projection,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def test_existing_scheduler_program_plan_and_specs_match_pinned_projections():
    actual = {
        program_name: sha256(_projection_bytes(program_name)).hexdigest()
        for program_name in _PINNED_PROJECTION_HASHES
    }

    assert actual == _PINNED_PROJECTION_HASHES


def test_registry_only_registration_drives_plan_and_specs(monkeypatch):
    monkeypatch.setitem(
        SINGLE_COMMAND_PROGRAM_REGISTRY,
        "registry_only_program",
        SingleCommandProgram(
            command=("python3", "-m", "example.registry_only"),
            step_key="registry_only_step",
            description="Registry-only scheduler program",
        ),
    )
    job = SimpleNamespace(
        job_key="unrelated_job_key",
        family="unrelated_family",
        program_key="registry_only_program",
        handler_kind="scheduler_builtin",
        handler_ref="example:handler",
        default_payload={},
        timezone="UTC",
    )
    run = SimpleNamespace(scheduled_for=_SCHEDULED_FOR)

    assert build_scheduler_step_plan(job) == [
        {
            "step_key": "registry_only_step",
            "sequence_no": 1,
            "kind": "single",
            "handler_ref": "example:handler",
            "payload": {"program": "registry_only_program"},
            "command": ["python3", "-m", "example.registry_only"],
        }
    ]
    assert get_step_specs(job, run) == [
        StepSpec(
            "registry_only_step",
            ["python3", "-m", "example.registry_only"],
            "Registry-only scheduler program",
        )
    ]


def test_single_command_registry_dispatches_only_by_exact_program_key():
    job = SimpleNamespace(
        job_key="uwear_aws_health_scan_shadow",
        family="uwear_aws_health_scan_shadow",
        program_key="unregistered_program",
        handler_kind="scheduler_builtin",
        handler_ref="example:uwear_aws_health_scan_shadow",
        default_payload={"name": "Uwear AWS Health Scan Shadow"},
        timezone="UTC",
    )
    run = SimpleNamespace(scheduled_for=_SCHEDULED_FOR)

    assert build_scheduler_step_plan(job) == [
        {
            "step_key": "unregistered_program",
            "sequence_no": 1,
            "kind": "single",
            "handler_ref": "example:uwear_aws_health_scan_shadow",
            "payload": {},
        }
    ]
    assert get_step_specs(job, run) == [
        StepSpec(
            "program",
            ["python3", "-m", "unregistered_program"],
            "Program fallback",
        )
    ]
