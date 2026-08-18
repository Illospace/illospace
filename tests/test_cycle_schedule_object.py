from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from brain.platform.db.models.cycle import (
    Cycle,
    CycleFailureGuardLatch,
    CycleFailureGuardObservation,
    CycleFailureGuardTriggerState,
    CycleRun,
    CycleRunEvaluation,
)
from brain.systems.cycles import commands, health as cycle_health
from brain.systems.cycles import prompts, service, skill_refs
from brain.systems.cycles.access import CycleActor
from brain.systems.cycles.common import MANUAL_CYCLE_ORIGIN
from brain.systems.cycles.queries import due_illo_lane_cycle_clause
from brain.systems.runs.execution_context import bind_agent_context
from brain.systems.runs.tool_catalog.handlers import cycles as cycle_handlers


LEGACY_ENVELOPE_BYTES = (
    '{"version":2,"origin":"scheduled_cycle","cycle_id":5,"cycle_run_id":9,'
    '"cycle_revision_id":null,"cycle_name":"Legacy","scheduled_for":'
    '"2026-08-14T12:00:00+00:00","timezone":"UTC","local_scheduled_for":'
    '"2026-08-14T12:00:00+00:00","launch_context":{"origin":"scheduled_cycle",'
    '"source":"cycle_scheduler","run_kind":"scheduled_digest"},"launch_mode":'
    '"background_cycle_run","active_instruction_source":"cycle.prompt",'
    '"prior_thread_role":"context_only","lifecycle_owner":"cycle_run",'
    '"thread_visibility":"output_target","cycle_memory_role":"source_of_truth",'
    '"scheduled_review_window":{"anchor":"cycle_run.scheduled_for","duration_hours":24,'
    '"start_at":"2026-08-13T12:00:00+00:00","end_at":"2026-08-14T12:00:00+00:00",'
    '"recommendation":"For daily review cycles, inspect [start_at, end_at) instead of a moving '
    'last_24h window based on execution time."},"result_contract":{"kind":'
    '"autonomous_cycle_run_result","run_kind":"scheduled_digest","required_outputs":'
    '["answer_the_cycle_mission","summarize_workspace_evidence_or_explicit_gaps",'
    '"report_evidence_health","record_next_action_or_blocker","short_self_review_summary"],'
    '"degraded_when":["workspace_evidence_sources_fail_or_return_unexpectedly_sparse_results",'
    '"the_run_cannot_access_required_context_or_output_targets",'
    '"the_final_response_does_not_state_evidence_health"],"pagination_health":'
    '"truncated_with_next_page_means_more_available_not_degraded; follow_next_page_to_completion_'
    'and_report_ok_when_no_reader_warnings_or_failures_remain"},"evidence_health":{"status":'
    '"pending","checked_at":null,"scheduled_review_window":{"anchor":'
    '"cycle_run.scheduled_for","duration_hours":24,"start_at":"2026-08-13T12:00:00+00:00",'
    '"end_at":"2026-08-14T12:00:00+00:00","recommendation":"For daily review cycles, inspect '
    '[start_at, end_at) instead of a moving last_24h window based on execution time."},'
    '"expected_checks":["workspace_activity_read","cycle_run_history_read",'
    '"project_context_read_when_relevant","output_target_available"],"repair_instruction":'
    '"Follow next_page tokens until pagination is complete. Routine truncation with a cursor is '
    'more_available, not degraded; report evidence_health=ok after all pages complete. If evidence '
    'readers are empty, warning, failing, or cannot be paged to completion in a way that conflicts '
    'with the mission, report evidence_health=degraded and name the gap before drawing strong '
    'conclusions."},"degradation_tracking":{},"open_ask_stragglers":[]}'
)


class _SharedUnitOfWork:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if exc_type is None:
            await self.session.flush()
        else:
            await self.session.rollback()
        return False


class _Rows:
    def __init__(self, rows):
        self._rows = list(rows)

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None


class _HandlerSession:
    def __init__(self, cycle):
        self.cycle = cycle

    async def scalars(self, _statement):
        return _Rows([self.cycle])

    async def flush(self):
        return None

    def add(self, value):
        self.cycle = value


@pytest.fixture
async def schedule_session(async_sqlite_session_factory, sqlite_postgres_ddl_patch):
    return await async_sqlite_session_factory(
        [
            Cycle.__table__,
            CycleRun.__table__,
            CycleFailureGuardLatch.__table__,
            CycleFailureGuardObservation.__table__,
            CycleFailureGuardTriggerState.__table__,
            CycleRunEvaluation.__table__,
        ]
    )


def _schedule(*, binding: str, next_run_at: datetime) -> Cycle:
    return Cycle(
        user_id=str(uuid4()),
        org_id=None,
        name=f"{binding} schedule",
        prompt="" if binding == "personal-agent" else "Run the legacy mission.",
        schedule_expr="0 9 * * *",
        timezone="UTC",
        enabled=True,
        executor_binding=binding,
        skill_ids=[17] if binding == "personal-agent" else [],
        next_run_at=next_run_at,
    )


@pytest.mark.asyncio
async def test_due_illo_lane_cycle_clause_preserves_cutoff_boundary(schedule_session):
    cutoff = datetime.now(timezone.utc).replace(microsecond=0)
    cycle = _schedule(binding="illo-lane", next_run_at=cutoff)
    schedule_session.add(cycle)
    await schedule_session.flush()

    strict_ids = (
        await schedule_session.scalars(
            select(Cycle.id).where(
                due_illo_lane_cycle_clause(cutoff, inclusive=False)
            )
        )
    ).all()
    inclusive_ids = (
        await schedule_session.scalars(
            select(Cycle.id).where(
                due_illo_lane_cycle_clause(cutoff, inclusive=True)
            )
        )
    ).all()

    assert strict_ids == []
    assert inclusive_ids == [cycle.id]


@pytest.mark.asyncio
async def test_personal_schedule_is_not_fired_and_is_visible_over_mcp(
    monkeypatch,
    schedule_session,
):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    expected_cadence = now - timedelta(minutes=5)
    cycle = _schedule(binding="personal-agent", next_run_at=expected_cadence)
    schedule_session.add(cycle)
    await schedule_session.flush()
    monkeypatch.setattr(service, "UnitOfWork", lambda: _SharedUnitOfWork(schedule_session))

    materialized = await service._async_materialize_due_cycle_runs_once(
        limit=10,
        now=now,
    )

    assert materialized == []
    assert (
        await schedule_session.scalar(
            select(CycleRun.id).where(CycleRun.cycle_id == cycle.id)
        )
        is None
    )
    assert cycle.next_run_at == expected_cadence

    gap = await service.async_advance_cycle_schedule_past_gap(
        schedule_session,
        gap_start=expected_cadence - timedelta(hours=1),
        now=now,
    )
    assert gap["cycles_examined"] == 0
    assert cycle.next_run_at == expected_cadence
    assert await service.async_wake_cycle_now(name=cycle.name) == "not_found"

    backlog = await cycle_health.async_legacy_cycle_backlog_snapshot(
        schedule_session,
        stale_after_minutes=1,
        sample_limit=10,
        now=now,
    )
    assert backlog.status == "ok"
    assert backlog.details["stale_due_cycles_count"] == 0

    monkeypatch.setattr(
        cycle_handlers,
        "UnitOfWork",
        lambda: _SharedUnitOfWork(schedule_session),
    )
    with bind_agent_context({"user_id": cycle.user_id, "org_id": None}):
        full_list = json.loads(
            await cycle_handlers._handle_manage_cycle_async(action="list")
        )
        by_name = json.loads(
            await cycle_handlers._handle_manage_cycle_async(
                action="list",
                name=cycle.name,
            )
        )

    assert full_list["cycles"][0]["executor_binding"] == "personal-agent"
    assert full_list["cycles"][0]["skill_ids"] == [17]
    assert full_list["cycles"][0]["schedule_expr"] == "0 9 * * *"
    assert full_list["cycles"][0]["next_run_at"] == str(expected_cadence)
    assert by_name["cycle"]["id"] == cycle.id
    assert by_name["cycle"]["executor_binding"] == "personal-agent"


@pytest.mark.asyncio
async def test_personal_schedule_manual_run_can_be_claimed(schedule_session):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    cycle = _schedule(binding="personal-agent", next_run_at=now + timedelta(hours=1))
    schedule_session.add(cycle)
    await schedule_session.flush()
    run = CycleRun(
        cycle_id=cycle.id,
        scheduled_for=now,
        prompt_snapshot="",
        status="queued",
        context_snapshot={
            "launch_context": {
                "origin": MANUAL_CYCLE_ORIGIN,
                "source": "cycle.run_now",
                "run_kind": "off_slot_material_alert",
            }
        },
    )
    schedule_session.add(run)
    await schedule_session.flush()

    claimed = await service.async_claim_cycle_run(schedule_session, run.id)

    assert claimed == (run, cycle)
    assert run.status == "running"


@pytest.mark.asyncio
async def test_personal_schedule_scheduled_run_cannot_be_claimed(schedule_session):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    cycle = _schedule(binding="personal-agent", next_run_at=now)
    schedule_session.add(cycle)
    await schedule_session.flush()
    run = CycleRun(
        cycle_id=cycle.id,
        scheduled_for=now,
        prompt_snapshot="",
        status="queued",
        context_snapshot={},
    )
    schedule_session.add(run)
    await schedule_session.flush()

    claimed = await service.async_claim_cycle_run(schedule_session, run.id)

    assert claimed is None
    assert run.status == "skipped"
    assert run.skip_reason == "personal_agent_executor"


@pytest.mark.asyncio
async def test_stale_recovery_does_not_launch_personal_scheduled_run(
    monkeypatch,
    schedule_session,
):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    cycle = _schedule(binding="personal-agent", next_run_at=now)
    schedule_session.add(cycle)
    await schedule_session.flush()
    run = CycleRun(
        cycle_id=cycle.id,
        scheduled_for=now - timedelta(minutes=2),
        prompt_snapshot="",
        status="queued",
        context_snapshot={},
    )
    schedule_session.add(run)
    await schedule_session.flush()
    monkeypatch.setattr(service, "UnitOfWork", lambda: _SharedUnitOfWork(schedule_session))

    recovered = await service.async_recover_stale_cycle_runs_once(
        stale_after_seconds=0,
        catchup_window_seconds=3600,
    )

    assert recovered == []
    assert run.status == "skipped"
    assert run.skip_reason == "personal_agent_executor"


def test_cycle_without_skills_keeps_the_legacy_envelope_byte_for_byte():
    cycle = Cycle()
    cycle.id = 5
    cycle.name = "Legacy"
    cycle.prompt = "Do the work."
    cycle.timezone = "UTC"
    cycle.skill_ids = []
    run = CycleRun()
    run.id = 9
    run.revision_id = None
    run.scheduled_for = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
    run.context_snapshot = {}

    encoded = json.dumps(prompts.cycle_launch_envelope(cycle, run), separators=(",", ":"))

    assert encoded == LEGACY_ENVELOPE_BYTES


def test_referenced_skills_replace_a_remaining_legacy_prompt():
    cycle = Cycle()
    cycle.id = 8
    cycle.name = "Migrated schedule"
    cycle.prompt = "This legacy mission must not remain active."
    cycle.timezone = "UTC"
    cycle.skill_ids = [2]
    run = CycleRun()
    run.id = 12
    run.scheduled_for = datetime(2026, 8, 14, 13, 0, tzinfo=timezone.utc)
    run.context_snapshot = {
        "schedule_skills": {
            "skill_ids": [2],
            "skills": [
                {
                    "id": 2,
                    "name": "release-radar",
                    "version": 4,
                    "content": "Follow the current skill mission.",
                    "truncated": False,
                }
            ],
            "content_max_chars": 12_000,
            "content_chars": 33,
            "truncated": False,
        }
    }
    idea = SimpleNamespace(id="idea-1", title="Migrated schedule")

    message = prompts.cycle_run_message(idea, cycle, run)

    assert "## Schedule Skills" in message
    assert "Follow the current skill mission." in message
    assert "## Cycle Mission" not in message
    assert "This legacy mission must not remain active." not in message


@pytest.mark.asyncio
async def test_referenced_skill_content_is_ordered_bounded_and_delivered(
    monkeypatch,
):
    skills = {
        2: SimpleNamespace(
            id=2,
            name="release-radar",
            version=4,
            description="Find material releases.",
            procedure="1. Read all release sources.\n2. Report material changes.",
            guardrails=["Never publish without evidence."],
            archived=False,
        ),
        1: SimpleNamespace(
            id=1,
            name="large-sweep",
            version=1,
            description="Sweep the full workspace.",
            procedure="x" * 20_000,
            guardrails=[],
            archived=False,
        ),
    }
    calls = []

    class FakeSkillRepository:
        def __init__(self, session):
            assert session == "session"

        async def a_get_visible(self, *, org_id, user_id, skill_id):
            calls.append((org_id, user_id, skill_id))
            return skills.get(skill_id)

    monkeypatch.setattr(skill_refs, "SkillRepository", FakeSkillRepository)
    cycle = Cycle()
    cycle.id = 8
    cycle.user_id = "user-1"
    cycle.org_id = "org-1"
    cycle.name = "Skill schedule"
    cycle.prompt = ""
    cycle.timezone = "UTC"
    cycle.skill_ids = [2, 999, 1]

    snapshot = await skill_refs.async_resolve_cycle_skill_snapshot("session", cycle)

    assert calls == [
        ("org-1", "user-1", 2),
        ("org-1", "user-1", 999),
        ("org-1", "user-1", 1),
    ]
    assert [skill["id"] for skill in snapshot["skills"]] == [2, 1]
    assert sum(len(skill["content"]) for skill in snapshot["skills"]) == 12_000
    assert snapshot["truncated"] is True
    assert "Find material releases." in snapshot["skills"][0]["content"]
    assert "Never publish without evidence." in snapshot["skills"][0]["content"]

    run = CycleRun()
    run.id = 12
    run.scheduled_for = datetime(2026, 8, 14, 13, 0, tzinfo=timezone.utc)
    run.context_snapshot = {"schedule_skills": snapshot}
    idea = SimpleNamespace(id="idea-1", title="Skill schedule")
    envelope = prompts.cycle_launch_envelope(cycle, run)
    message = prompts.cycle_run_message(idea, cycle, run)

    assert envelope["version"] == 3
    assert envelope["active_instruction_source"] == "cycle.skills"
    assert envelope["skill_ids"] == [2, 999, 1]
    assert envelope["skills_truncated"] is True
    assert "## Schedule Skills" in message
    assert "release-radar" in message
    assert "Never publish without evidence." in message
    assert "## Cycle Mission" not in message


@pytest.mark.asyncio
async def test_manage_cycle_rejects_invalid_executor_before_database_access(monkeypatch):
    def forbidden_unit_of_work():
        raise AssertionError("invalid executor_binding must fail before database access")

    monkeypatch.setattr(cycle_handlers, "UnitOfWork", forbidden_unit_of_work)
    with bind_agent_context({"user_id": "user-1", "org_id": "org-1"}):
        payload = json.loads(
            await cycle_handlers._handle_manage_cycle_async(
                action="create",
                name="Invalid binding",
                skill_ids=[1],
                schedule_expr="0 9 * * *",
                timezone="UTC",
                executor_binding="packet-lane",
            )
        )

    assert payload == {
        "error": "executor_binding must be one of: illo-lane, personal-agent"
    }


@pytest.mark.asyncio
async def test_manage_cycle_create_and_update_forward_schedule_bindings(monkeypatch):
    now = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
    cycle = Cycle()
    cycle.id = 71
    cycle.user_id = "user-1"
    cycle.org_id = "org-1"
    cycle.name = "Personal release radar"
    cycle.prompt = ""
    cycle.schedule_expr = "0 9 * * *"
    cycle.timezone = "UTC"
    cycle.enabled = True
    cycle.max_concurrency = 1
    cycle.timeout_seconds = None
    cycle.model_override = None
    cycle.thinking_override = None
    cycle.execution_policy_key = None
    cycle.executor_binding = "personal-agent"
    cycle.skill_ids = [2]
    cycle.target_idea_id = None
    cycle.next_run_at = now
    cycle.last_run_at = None
    cycle.last_status = None
    cycle.last_error = None
    cycle.created_at = now
    cycle.updated_at = now
    session = _HandlerSession(cycle)
    captured = {}

    async def fake_create(_session, **kwargs):
        captured["create"] = kwargs
        return cycle

    async def fake_update(_session, _cycle, **kwargs):
        captured["update"] = kwargs
        cycle.executor_binding = kwargs["executor_binding"]
        cycle.skill_ids = kwargs["skill_ids"]
        return cycle

    monkeypatch.setattr(cycle_handlers, "UnitOfWork", lambda: _SharedUnitOfWork(session))
    monkeypatch.setattr(cycle_handlers, "async_create_cycle", fake_create)
    monkeypatch.setattr(cycle_handlers, "async_update_cycle", fake_update)
    monkeypatch.setattr(cycle_handlers, "publish_cycle_change_safe", lambda **_kwargs: None)

    with bind_agent_context({"user_id": "user-1", "org_id": "org-1"}):
        created = json.loads(
            await cycle_handlers._handle_manage_cycle_async(
                action="create",
                name=cycle.name,
                schedule_expr=cycle.schedule_expr,
                timezone=cycle.timezone,
                executor_binding="personal-agent",
                skill_ids=[2],
            )
        )
        updated = json.loads(
            await cycle_handlers._handle_manage_cycle_async(
                action="update",
                id=cycle.id,
                executor_binding="illo-lane",
                skill_ids=[],
            )
        )

    assert captured["create"]["executor_binding"] == "personal-agent"
    assert captured["create"]["skill_ids"] == [2]
    assert created["created"]["executor_binding"] == "personal-agent"
    assert created["created"]["skill_ids"] == [2]
    assert captured["update"]["executor_binding"] == "illo-lane"
    assert captured["update"]["skill_ids"] == []
    assert updated["updated"]["executor_binding"] == "illo-lane"
    assert updated["updated"]["skill_ids"] == []


@pytest.mark.asyncio
async def test_create_skill_schedule_does_not_require_embedded_prompt(monkeypatch):
    session = _HandlerSession(None)

    async def fake_revision(*_args, **_kwargs):
        return SimpleNamespace(id=1)

    async def fake_targets(*_args, **_kwargs):
        return None

    monkeypatch.setattr(commands, "async_record_cycle_revision", fake_revision)
    monkeypatch.setattr(commands, "_seed_default_output_targets", fake_targets)

    cycle = await commands.async_create_cycle(
        session,
        actor=CycleActor(
            user_id="user-1",
            org_id="org-1",
            principal_type="agent",
            source_id="run-1",
        ),
        name="Personal release radar",
        prompt=None,
        timezone_name="UTC",
        schedule_expr="0 9 * * *",
        executor_binding="personal-agent",
        skill_ids=[2],
    )

    assert cycle.prompt == ""
    assert cycle.executor_binding == "personal-agent"
    assert cycle.skill_ids == [2]
    assert cycle.next_run_at is not None
