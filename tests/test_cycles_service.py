from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select

from brain.app.api.schemas.cycles import CycleRead, CycleRunRead
from brain.systems.cycles import access as cycle_access
from brain.systems.cycles import execution as cycle_execution
from brain.systems.cycles import prompts as cycle_prompts
from brain.systems.cycles import service
from brain.systems.cycles.common import AGENT_TRIGGERED_CYCLE_ORIGIN, MANUAL_CYCLE_ORIGIN
from brain.systems.cycles.contracts import (
    CYCLE_RESULT_CONTRACT_REQUIRED_OUTPUTS_BY_RUN_KIND,
    cycle_result_contract,
)
from brain.app.api.routers import cycles as cycles_router
from brain.platform.db.models.cycle import Cycle, CycleRun
from brain.platform.db.models.agent_run import AgentRunArtifactRow, AgentRunEventRow
from brain.platform.db.models.run import AgentRun
from brain.platform.db.models.idea import Idea


RAW_PROVIDER_ERROR = (
    "An error occurred while processing your request. You can retry your request, or contact us "
    "through our help center at help.openai.com if the error persists. Please include the request "
    "ID `a7dd7556-test` in your message. | server_error | server_error"
)

RESULT_CONTRACT_REQUIRED_OUTPUTS = [
    "answer_the_cycle_mission",
    "summarize_workspace_evidence_or_explicit_gaps",
    "report_evidence_health",
    "record_next_action_or_blocker",
    "short_self_review_summary",
]
REFLEX_MISSION = (
    "Review GitHub events. If reader failures occur, report them under Evidence health. "
    "End with exactly six lines: Reflex result / Evidence reviewed / Evidence health / "
    "Domain tracking / Next action / Self-review."
)
REFLEX_ANSWER = (
    "Reflex result: the scheduled GitHub review completed with no unresolved work.\n"
    "Evidence reviewed: workspace GitHub events and current cycle state were inspected.\n"
    "Evidence health: ok; the available readers returned complete results.\n"
    "Domain tracking: no durable record update was needed for this run.\n"
    "Next action: inspect new GitHub events at the next scheduled run.\n"
    "Self-review: the mission and advertised result contract are satisfied."
)


def _result_contract(required_outputs=None):
    return {
        "kind": "autonomous_cycle_run_result",
        "required_outputs": list(
            RESULT_CONTRACT_REQUIRED_OUTPUTS
            if required_outputs is None
            else required_outputs
        ),
    }


def _contract_finalization_scenario(
    *,
    cycle_id,
    mission,
    answer,
    events=None,
    launch_contract=None,
    snapshot_contract=None,
):
    launch_contract = launch_contract or _result_contract()
    snapshot_contract = snapshot_contract or launch_contract

    agent_run = AgentRun()
    agent_run.id = 44
    agent_run.root_run_id = 44
    agent_run.user_id = "user-1"
    agent_run.org_id = "org-1"
    agent_run.input_message = mission
    agent_run.model_policy = {}
    agent_run.metadata_ = {
        "source": "cycle",
        "cycle_run_id": 12,
        "launch_envelope": {"result_contract": launch_contract},
    }

    cycle_run = CycleRun()
    cycle_run.id = 12
    cycle_run.cycle_id = cycle_id
    cycle_run.status = "running"
    cycle_run.completed_at = None
    cycle_run.error = None
    cycle_run.skip_reason = None
    cycle_run.prompt_snapshot = mission
    cycle_run.context_snapshot = {
        "result_contract": snapshot_contract,
        "evidence_health": {"status": "pending"},
    }

    cycle = Cycle()
    cycle.id = cycle_id
    cycle.prompt = mission
    cycle.last_status = None
    cycle.last_error = None
    cycle.last_run_at = None

    final_answer = AgentRunArtifactRow(
        id=8,
        run_id=44,
        root_run_id=44,
        artifact_type="final_answer",
        text=answer,
        created_at=datetime(2026, 4, 28, 20, 25, tzinfo=timezone.utc),
    )
    session = _AsyncFakeSession(
        agent_run=agent_run,
        run=cycle_run,
        cycle=cycle,
        artifacts=[final_answer],
        events=events,
    )
    return session, cycle_run, cycle, final_answer


class _FakeSession:
    def __init__(self, agent_run=None, run=None, cycle=None, artifacts=None, events=None):
        self._agent_run = agent_run
        self._run = run
        self._cycle = cycle
        self._artifacts = list(artifacts or [])
        self._events = list(events or [])
        self.added = []

    def get(self, model, value):
        if model is AgentRun:
            return self._agent_run
        if model is CycleRun:
            return self._run
        if model is Cycle:
            return self._cycle
        return None

    def scalars(self, statement):
        text = str(statement)
        if "agent_run_artifacts" in text:
            rows = list(self._artifacts)
            if "agent_run_artifacts.text =" in text:
                params = statement.compile().params
                text_value = next(
                    (value for key, value in params.items() if key.startswith("text_")),
                    None,
                )
                rows = [row for row in rows if row.text == text_value]
            rows.sort(
                key=lambda row: (
                    row.created_at or datetime.min.replace(tzinfo=timezone.utc),
                    row.id or 0,
                ),
                reverse=True,
            )
            return _AllResult(rows)
        if "agent_run_events" in text:
            rows = sorted(
                self._events,
                key=lambda row: (row.sequence_no or 0, row.id or 0),
                reverse=True,
            )
            return _AllResult(rows)
        return _AllResult([])

    def add(self, value):
        self.added.append(value)
        if isinstance(value, AgentRunArtifactRow):
            if value.id is None:
                value.id = 100 + len(self._artifacts) + 1
            if value.created_at is None:
                value.created_at = datetime(2026, 4, 28, 20, 30, tzinfo=timezone.utc)
            self._artifacts.append(value)


class _AsyncFakeSession(_FakeSession):
    async def get(self, model, value):
        return super().get(model, value)

    async def scalars(self, statement):
        return super().scalars(statement)

    async def flush(self):
        return None


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def first(self):
        return self._value


class _FirstResult:
    def __init__(self, value=None):
        self._value = value

    def first(self):
        return self._value

    def scalar_one(self):
        if isinstance(self._value, tuple):
            return 0
        return self._value if self._value is not None else 0


class _AllResult:
    def __init__(self, values):
        self._values = list(values)

    def first(self):
        return self._values[0] if self._values else None

    def all(self):
        return list(self._values)


class _RouterCycleResult:
    def __init__(self, cycle):
        self._cycle = cycle

    def first(self):
        return self._cycle


class _RouterCycleSession:
    def __init__(self, cycle):
        self._cycle = cycle
        self.added = []
        self.flushed = False
        self.refreshed = False
        self.committed = False

    async def scalars(self, statement):
        return _RouterCycleResult(self._cycle)

    async def execute(self, statement):
        return _FirstResult((self._cycle.target_idea_id,))

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        self.flushed = True
        for index, value in enumerate(self.added, start=1):
            if getattr(value, "id", None) is None:
                value.id = index

    async def refresh(self, obj):
        assert obj is self._cycle
        self.refreshed = True

    async def commit(self):
        self.committed = True


class _ExecuteCycleSession:
    def __init__(self, *, run, cycle, idea, owner=None, expected_run_id=None):
        self._scalar_values = [run, cycle, idea]
        self._owner = owner
        self.added = []
        self.statements = []
        self.expected_run_id = expected_run_id

    def scalars(self, statement):
        self.statements.append(statement)
        if self.expected_run_id is not None and len(self.statements) == 1:
            params = statement.compile().params
            if self.expected_run_id not in params.values():
                return _ScalarResult(None)
        if not self._scalar_values:
            return _AllResult([])
        return _ScalarResult(self._scalar_values.pop(0))

    def execute(self, statement):
        return _FirstResult(None)

    def get(self, model, value):
        return self._owner

    def add(self, value):
        self.added.append(value)

    def flush(self):
        for value in self.added:
            if value.__class__.__name__ == "IdeaThread" and getattr(value, "id", None) is None:
                value.id = 123
            if value.__class__.__name__ == "Idea" and getattr(value, "id", None) is None:
                value.id = uuid4()


class _AsyncUnitOfWorkFactory:
    def __init__(self, sessions):
        self._sessions = list(sessions)
        self.uows = []

    def __call__(self):
        if not self._sessions:
            raise AssertionError("unexpected async UnitOfWork")
        uow = _AsyncUoW(self._sessions.pop(0))
        self.uows.append(uow)
        return uow

    def blocking(self):
        raise AssertionError("sync UnitOfWork bridge should not be used")


class _AsyncUoW:
    def __init__(self, session):
        self.session = session
        self.entered = False

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def __enter__(self):
        raise AssertionError("sync UnitOfWork bridge should not be used")

    def __exit__(self, exc_type, exc, tb):
        return False


class _AsyncExecuteCycleSession(_ExecuteCycleSession):
    async def scalars(self, statement):
        return super().scalars(statement)

    async def execute(self, statement):
        return super().execute(statement)

    async def get(self, model, value):
        return super().get(model, value)

    async def flush(self):
        super().flush()


class _AsyncRunNowCreateSession:
    def __init__(self, cycle, created_runs):
        self._cycle = cycle
        self._created_runs = created_runs

    async def get(self, model, value):
        if model is Cycle:
            return self._cycle
        return None

    async def scalars(self, statement):
        return _AllResult([])

    def add(self, value):
        self._created_runs.append(value)

    async def flush(self):
        for value in self._created_runs:
            if isinstance(value, CycleRun) and getattr(value, "id", None) is None:
                value.id = 99
                value.created_at = value.scheduled_for


class _AsyncRunNowLoadSession:
    def __init__(self, run_or_runs):
        self._run_or_runs = run_or_runs

    async def get(self, model, value):
        if model is CycleRun:
            if isinstance(self._run_or_runs, list):
                return self._run_or_runs[0]
            return self._run_or_runs
        return None


class _AsyncCycleListSession:
    def __init__(self, cycles):
        self._cycles = cycles

    async def scalars(self, statement):
        return _AllResult(self._cycles)


class _RecoverStaleSession:
    def __init__(self, *, runs, cycles=None, agent_runs=None):
        self._runs = list(runs)
        self._cycles = {cycle.id: cycle for cycle in (cycles or [])}
        self._agent_runs = {run.id: run for run in (agent_runs or [])}
        self.added = []

    async def scalars(self, statement):
        return _AllResult(self._runs)

    async def get(self, model, value):
        if model is Cycle:
            return self._cycles.get(value)
        if model is AgentRun:
            return self._agent_runs.get(value)
        return None

    def add(self, value):
        self.added.append(value)


class _SingleRunSession:
    def __init__(self, run):
        self._run = run

    async def scalars(self, statement):
        return _ScalarResult(self._run)


def _fail_sync_bridge(*args, **kwargs):
    raise AssertionError("sync DB bridge should not be used")


def _cycle_execution_objects(*, model_override: str | None) -> tuple[CycleRun, Cycle, Idea]:
    idea_id = uuid4()

    run = CycleRun()
    run.id = 12
    run.cycle_id = 5
    run.status = "queued"
    run.scheduled_for = datetime(2026, 4, 28, 20, 20, tzinfo=timezone.utc)
    run.prompt_snapshot = "Run a scheduled coding-agent check"

    cycle = Cycle()
    cycle.id = 5
    cycle.user_id = "user-1"
    cycle.org_id = "org-1"
    cycle.name = "Scheduled Codex check"
    cycle.prompt = "Run a scheduled coding-agent check"
    cycle.schedule_expr = "20 16 * * *"
    cycle.timezone = "America/Toronto"
    cycle.target_idea_id = idea_id
    cycle.deleted_at = None
    cycle.model_override = model_override
    cycle.thinking_override = None

    idea = Idea()
    idea.id = idea_id
    idea.title = "Scheduled Codex check"
    idea.display_title = None
    idea.description = "Run a scheduled coding-agent check"
    idea.status = "needs_input"
    idea.origin = "cycle"
    idea.origin_ref = "cycle:5"
    idea.salience_score = None
    idea.position_x = None
    idea.position_y = None
    idea.created_at = None
    idea.updated_at = None
    idea.user_id = "user-1"
    idea.org_id = "org-1"
    idea.archived_at = None
    idea.active_agents = []
    idea.attachments = []
    return run, cycle, idea


def test_compute_next_run_at_uses_timezone():
    baseline = datetime(2026, 4, 22, 12, 34, tzinfo=timezone.utc)
    next_run = service.compute_next_run_at(
        "0 9 * * *",
        "America/Toronto",
        from_dt=baseline,
    )
    assert next_run == datetime(2026, 4, 22, 13, 0, tzinfo=timezone.utc)


def test_humanize_schedule_names_single_monday_not_weekdays():
    assert service.humanize_schedule("0 9 * * 1", "America/Toronto") == "Mondays at 9:00 AM (America/Toronto)"


def test_one_time_schedule_uses_timezone_and_expires_after_run():
    expr = service.validate_schedule_expr(
        "at:2026-05-08T09:30:00",
        "America/Toronto",
    )

    next_run = service.compute_next_run_at(expr, "America/Toronto")

    assert expr.startswith("at:")
    assert next_run == datetime(2026, 5, 8, 13, 30, tzinfo=timezone.utc)
    assert (
        service.compute_next_run_at(
            expr,
            "America/Toronto",
            from_dt=next_run,
        )
        is None
    )


def test_humanize_one_time_schedule():
    label = service.humanize_schedule(
        "at:2026-05-08T09:30:00",
        "America/Toronto",
    )

    assert label == "Once at May 8, 2026 9:30 AM (America/Toronto)"


def test_canonical_execution_mode_is_single_autonomous_runtime_policy():
    assert service.canonical_execution_mode(None) == service.REUSABLE_THREAD_EXECUTION_MODE
    assert service.canonical_execution_mode("reuse_same_idea") == service.REUSABLE_THREAD_EXECUTION_MODE
    assert service.canonical_execution_mode("new_idea_per_run") == service.REUSABLE_THREAD_EXECUTION_MODE
    assert service.canonical_execution_mode("ignored") == service.REUSABLE_THREAD_EXECUTION_MODE


def test_validate_schedule_expr_rejects_non_five_field_expr():
    with pytest.raises(ValueError):
        service.validate_schedule_expr("0 0 9 * * *")


def test_validate_nonempty_trimmed_rejects_blank_text():
    with pytest.raises(ValueError):
        service.validate_nonempty_trimmed("   ", "name")


def test_cycle_router_bad_request_returns_400():
    with pytest.raises(cycles_router.HTTPException) as caught:
        raise cycles_router._bad_request(ValueError("Unknown timezone: Mars/Base"))

    assert caught.value.status_code == 400
    assert caught.value.detail == "Unknown timezone: Mars/Base"


def _cycle_for_serialization(*, schedule_expr: str, timezone_name: str) -> Cycle:
    now = datetime(2026, 4, 27, 12, 0, tzinfo=timezone.utc)
    cycle = Cycle()
    cycle.id = 42
    cycle.user_id = "user-1"
    cycle.org_id = "org-1"
    cycle.name = "Cycle smoke test"
    cycle.prompt = "Run the smoke test"
    cycle.schedule_expr = schedule_expr
    cycle.timezone = timezone_name
    cycle.enabled = True
    cycle.model_override = None
    cycle.thinking_override = None
    cycle.execution_mode = "new_idea_per_run"
    cycle.target_idea_id = None
    cycle.reopen_archived = False
    cycle.next_run_at = None
    cycle.last_run_at = None
    cycle.last_status = None
    cycle.last_error = None
    cycle.created_at = now
    cycle.updated_at = now
    return cycle



@pytest.mark.asyncio
async def test_cycle_update_recomputes_schedule_with_new_timezone_before_saving():
    cycle = _cycle_for_serialization(
        schedule_expr="0 9 * * *",
        timezone_name="UTC",
    )
    db = _RouterCycleSession(cycle)

    body = cycles_router.CycleUpdate(
        schedule_expr="0 9 * * 1",
        timezone="America/Toronto",
    )
    response = await cycles_router.update_cycle(
        cycle.id,
        body,
        db=db,
        user={"id": cycle.user_id, "org_id": None},
    )

    assert response["schedule_expr"] == "0 9 * * 1"
    assert response["timezone"] == "America/Toronto"
    assert response["next_run_at"].tzinfo is not None
    assert cycle.schedule_expr == "0 9 * * 1"
    assert cycle.timezone == "America/Toronto"
    assert db.flushed is True
    assert db.refreshed is True


@pytest.mark.asyncio
async def test_cycle_update_refreshes_server_updated_timestamp_before_serializing():
    cycle = _cycle_for_serialization(
        schedule_expr="0 9 * * *",
        timezone_name="America/Toronto",
    )

    class _RaisesOnAccess:
        def __getattribute__(self, name):
            if name in {"__class__", "__repr__", "__str__"}:
                return object.__getattribute__(self, name)
            raise AssertionError("updated_at was serialized before async refresh")

        def __bool__(self):
            raise AssertionError("updated_at was serialized before async refresh")

    class _RefreshRequiredSession(_RouterCycleSession):
        async def flush(self):
            await super().flush()
            cycle.updated_at = _RaisesOnAccess()

        async def refresh(self, obj):
            await super().refresh(obj)
            obj.updated_at = datetime(2026, 4, 27, 13, 0, tzinfo=timezone.utc)

    db = _RefreshRequiredSession(cycle)

    response = await cycles_router.update_cycle(
        cycle.id,
        cycles_router.CycleUpdate(name="Updated cycle"),
        db=db,
        user={"id": cycle.user_id, "org_id": None},
    )

    assert db.flushed is True
    assert db.refreshed is True
    assert response["updated_at"] == datetime(2026, 4, 27, 13, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_cycle_update_validation_failure_does_not_mutate_or_flush():
    cycle = _cycle_for_serialization(
        schedule_expr="0 9 * * *",
        timezone_name="America/Toronto",
    )
    db = _RouterCycleSession(cycle)

    body = cycles_router.CycleUpdate(
        schedule_expr="0 9 * * 1",
        timezone="Mars/Base",
    )
    with pytest.raises(cycles_router.HTTPException) as caught:
        await cycles_router.update_cycle(
            cycle.id,
            body,
            db=db,
            user={"id": cycle.user_id, "org_id": None},
        )

    assert caught.value.status_code == 400
    assert cycle.timezone == "America/Toronto"
    assert cycle.schedule_expr == "0 9 * * *"
    assert db.flushed is False

def test_serialize_cycle_does_not_raise_for_legacy_bad_timezone():
    cycle = _cycle_for_serialization(
        schedule_expr="25 8 * * *",
        timezone_name="Not/A_Timezone",
    )

    serialized = service.serialize_cycle(cycle)

    assert serialized["schedule_human"] == "25 8 * * * (Not/A_Timezone)"
    assert serialized["execution_mode"] == service.REUSABLE_THREAD_EXECUTION_MODE
    assert serialized["reopen_archived"] is True


def test_serialize_cycle_does_not_raise_for_legacy_bad_cron():
    cycle = _cycle_for_serialization(
        schedule_expr="not a cron",
        timezone_name="America/Toronto",
    )

    serialized = service.serialize_cycle(cycle)

    assert serialized["schedule_human"] == "not a cron (America/Toronto)"


def test_serialize_cycle_normalizes_uuid_columns_for_api_schema():
    cycle = _cycle_for_serialization(
        schedule_expr="0 9 * * *",
        timezone_name="America/Toronto",
    )
    cycle.user_id = uuid4()
    cycle.org_id = uuid4()
    cycle.target_idea_id = uuid4()

    serialized = service.serialize_cycle(cycle)

    assert isinstance(serialized["user_id"], str)
    assert isinstance(serialized["org_id"], str)
    assert isinstance(serialized["target_idea_id"], str)
    CycleRead.model_validate(serialized)


def test_serialize_cycle_falls_back_for_legacy_missing_timestamps():
    cycle = _cycle_for_serialization(
        schedule_expr="0 9 * * *",
        timezone_name="America/Toronto",
    )
    cycle.created_at = None
    cycle.updated_at = None

    serialized = service.serialize_cycle(cycle)

    assert serialized["created_at"] is not None
    assert serialized["updated_at"] is not None
    CycleRead.model_validate(serialized)


def test_serialize_cycle_run_normalizes_uuid_columns_for_api_schema():
    run = CycleRun()
    run.id = 7
    run.cycle_id = 42
    run.scheduled_for = datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc)
    run.started_at = None
    run.completed_at = None
    run.status = "completed"
    run.error = None
    run.skip_reason = None
    run.idea_id = uuid4()
    run.run_id = 99
    run.prompt_snapshot = "Run the smoke test"
    run.created_at = None

    serialized = service.serialize_cycle_run(run)

    assert isinstance(serialized["idea_id"], str)
    assert serialized["created_at"] == run.scheduled_for
    CycleRunRead.model_validate(serialized)


def test_on_demand_cycle_launch_exposes_provenance_and_local_anchor():
    cycle = Cycle()
    cycle.id = 2
    cycle.name = "Uwear Ticket Coordinator Check-ins"
    cycle.prompt = "Coordinate Uwear engineering."
    cycle.timezone = "America/New_York"
    cycle.model_override = None
    cycle.thinking_override = None

    run = CycleRun()
    run.id = 1346
    run.cycle_id = cycle.id
    run.revision_id = 41
    run.scheduled_for = datetime(2026, 7, 15, 13, 2, 31, tzinfo=timezone.utc)
    run.context_snapshot = {
        "launch_context": {
            "origin": AGENT_TRIGGERED_CYCLE_ORIGIN,
            "source": "manage_cycle",
            "actor_id": "1438",
            "rationale": "EVENT_TRIGGER: merged PR now requires deploy",
        }
    }

    idea = Idea()
    idea.id = "0772d00e-41ad-4a0a-b26d-1886de587ed8"
    idea.title = "Uwear Ticket Coordinator Runs"

    envelope = cycle_prompts.cycle_launch_envelope(cycle, run)
    message = cycle_prompts.cycle_run_message(idea, cycle, run)

    assert envelope["origin"] == AGENT_TRIGGERED_CYCLE_ORIGIN
    assert envelope["launch_mode"] == "on_demand_cycle_run"
    assert envelope["scheduled_for"] == "2026-07-15T13:02:31+00:00"
    assert envelope["local_scheduled_for"] == "2026-07-15T09:02:31-04:00"
    assert envelope["timezone"] == "America/New_York"
    assert "## On-demand Cycle Launch" in message
    assert "2026-07-15T09:02:31-04:00 (America/New_York)" in message
    assert "EVENT_TRIGGER: merged PR now requires deploy" in message
    assert "## Scheduled Cycle Launch" not in message


def test_coordinator_launch_prompt_maps_declared_contract_to_visible_sections():
    cycle = Cycle()
    cycle.id = 2
    cycle.name = "Uwear Ticket Coordinator Check-ins"
    cycle.prompt = "Publish the chantier-primary coordinator digest."
    cycle.timezone = "America/Toronto"
    cycle.model_override = None
    cycle.thinking_override = None

    run = CycleRun()
    run.id = 1364
    run.cycle_id = cycle.id
    run.scheduled_for = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)
    run.guidance_snapshot = []
    run.output_targets_snapshot = []
    run.context_snapshot = {
        "result_contract": cycle_result_contract(run_kind="scheduled_digest")
    }

    idea = Idea()
    idea.id = "coordinator-digest"
    idea.title = "Uwear Ticket Coordinator Runs"

    message = cycle_prompts.cycle_run_message(idea, cycle, run)
    output_section = message.split("## Required Output Sections", 1)[1].split(
        "## Cycle Memory", 1
    )[0]

    for key in cycle_result_contract(
        run_kind="scheduled_digest"
    )["required_outputs"]:
        assert f"`{key}`" in output_section
    assert "`record_next_action_or_blocker` -> `Next action:` or `Blocker:`" in output_section
    assert "`short_self_review_summary` -> `Self-review summary:`" in output_section
    assert "Example required footer:" in output_section


@pytest.mark.parametrize(
    ("run_kind", "expected_outputs"),
    [
        (
            "scheduled_digest",
            RESULT_CONTRACT_REQUIRED_OUTPUTS,
        ),
        (
            "off_slot_material_alert",
            [
                "answer_the_cycle_mission",
                "summarize_workspace_evidence_or_explicit_gaps",
                "report_evidence_health",
            ],
        ),
    ],
)
def test_coordinator_run_kind_contracts_are_explicit(run_kind, expected_outputs):
    contract = cycle_result_contract(run_kind=run_kind)

    assert contract["run_kind"] == run_kind
    assert contract["required_outputs"] == expected_outputs
    assert (
        tuple(expected_outputs)
        == CYCLE_RESULT_CONTRACT_REQUIRED_OUTPUTS_BY_RUN_KIND[run_kind]
    )


def test_material_alert_contract_keeps_evidence_gate_without_digest_footer_fields():
    from brain.systems.cycles.contract_gate import evaluate_cycle_result_contract

    contract = cycle_result_contract(run_kind="off_slot_material_alert")

    assert contract["run_kind"] == "off_slot_material_alert"
    assert contract["required_outputs"] == [
        "answer_the_cycle_mission",
        "summarize_workspace_evidence_or_explicit_gaps",
        "report_evidence_health",
    ]

    alert = (
        "Uwear engineering changed materially: the deploy queue now preserves one active "
        "release per project. Evidence reviewed: workspace run receipts and the merged change. "
        "Evidence health: ok."
    )
    reduced_review = evaluate_cycle_result_contract(
        candidate_answer=alert,
        result_contract=contract,
        mission="Post one concise material engineering change alert.",
    )
    strict_review = evaluate_cycle_result_contract(
        candidate_answer=alert,
        result_contract=cycle_result_contract(run_kind="scheduled_digest"),
        mission="Publish the coordinator digest.",
    )

    assert reduced_review["approved"] is True
    assert strict_review["approved"] is False
    assert strict_review["missing_outputs"] == [
        "record_next_action_or_blocker",
        "short_self_review_summary",
    ]


def test_material_alert_launch_prompt_does_not_request_digest_footer_fields():
    cycle = Cycle()
    cycle.id = 2
    cycle.name = "Uwear Ticket Coordinator Check-ins"
    cycle.prompt = "Post one concise Uwear material engineering change alert."
    cycle.timezone = "America/Toronto"
    cycle.model_override = None
    cycle.thinking_override = None

    run = CycleRun()
    run.id = 1884
    run.cycle_id = cycle.id
    run.scheduled_for = datetime(2026, 7, 18, 19, 47, tzinfo=timezone.utc)
    run.guidance_snapshot = []
    run.output_targets_snapshot = []
    run.context_snapshot = {
        "launch_context": {
            "origin": AGENT_TRIGGERED_CYCLE_ORIGIN,
            "source": "manage_cycle",
            "run_kind": "off_slot_material_alert",
        },
        "result_contract": cycle_result_contract(
            run_kind="off_slot_material_alert"
        ),
    }

    idea = Idea()
    idea.id = "coordinator-material-alert"
    idea.title = "Uwear Ticket Coordinator Runs"

    message = cycle_prompts.cycle_run_message(idea, cycle, run)
    output_section = message.split("## Required Output Sections", 1)[1].split(
        "## Cycle Memory", 1
    )[0]

    assert "`record_next_action_or_blocker`" not in output_section
    assert "`short_self_review_summary`" not in output_section
    assert "Next action:" not in output_section
    assert "Self-review summary:" not in output_section
    assert "End with a short self-review summary" not in message


@pytest.mark.asyncio
async def test_async_run_cycle_now_uses_native_uow_without_sync_bridges(monkeypatch):
    cycle = Cycle()
    cycle.id = 5
    cycle.prompt = "Run the smoke test"
    cycle.deleted_at = None

    created_runs = []
    factory = _AsyncUnitOfWorkFactory([
        _AsyncRunNowCreateSession(cycle, created_runs),
        _AsyncRunNowLoadSession(created_runs),
    ])
    executed_run_ids = []

    async def fake_async_execute_cycle_run(run_id):
        executed_run_ids.append(run_id)
        created_runs[0].status = "completed"

    monkeypatch.setattr(service, "UnitOfWork", factory)
    monkeypatch.setattr(service, "async_execute_cycle_run", fake_async_execute_cycle_run)
    monkeypatch.setattr(service, "open_unit_of_work", _fail_sync_bridge, raising=False)
    monkeypatch.setattr(service, "run_unit_of_work_task", _fail_sync_bridge, raising=False)

    payload = await service.async_run_cycle_now(
        cycle.id,
        run_kind="scheduled_digest",
    )

    assert executed_run_ids == [99]
    assert payload["id"] == 99
    assert payload["cycle_id"] == cycle.id
    assert payload["prompt_snapshot"] == cycle.prompt
    assert payload["status"] == "completed"
    assert payload["context_snapshot"]["launch_context"] == {
        "origin": MANUAL_CYCLE_ORIGIN,
        "source": "cycle.run_now",
        "run_kind": "scheduled_digest",
    }
    assert all(uow.entered for uow in factory.uows)


@pytest.mark.asyncio
async def test_async_run_cycle_now_snapshots_off_slot_material_alert_contract(monkeypatch):
    cycle = Cycle()
    cycle.id = 5
    cycle.prompt = "Post one concise material engineering alert"
    cycle.deleted_at = None
    cycle.org_id = None
    cycle.user_id = None
    cycle.timezone = "America/Toronto"

    created_runs = []
    factory = _AsyncUnitOfWorkFactory([
        _AsyncRunNowCreateSession(cycle, created_runs),
        _AsyncRunNowLoadSession(created_runs),
    ])

    async def fake_async_execute_cycle_run(_run_id):
        created_runs[0].status = "completed"

    monkeypatch.setattr(service, "UnitOfWork", factory)
    monkeypatch.setattr(service, "async_execute_cycle_run", fake_async_execute_cycle_run)

    await service.async_run_cycle_now(
        cycle.id,
        run_kind="off_slot_material_alert",
    )

    snapshot = created_runs[0].context_snapshot
    assert snapshot["launch_context"]["run_kind"] == "off_slot_material_alert"
    assert snapshot["result_contract"]["run_kind"] == "off_slot_material_alert"
    assert "short_self_review_summary" not in snapshot["result_contract"]["required_outputs"]


@pytest.mark.asyncio
async def test_cycle_run_creation_uses_typed_admission(monkeypatch):
    from brain.systems.runs.work_intake import WorkIntakeResult

    calls = []

    async def fake_admit(session, event):
        calls.append((session, event))
        return WorkIntakeResult(ok=True, run_id=77)

    monkeypatch.setattr(service, "admit_work", fake_admit)
    session = object()

    run_id = await service._async_admit_cycle_run(
        session,
        idea_id="idea-1",
        message="cycle prompt",
        priority=1,
        user_id="user-1",
        metadata={"source": "cycle", "cycle_run_id": 12},
        cycle_run_id=12,
    )

    assert run_id == 77
    passed_session, event = calls[0]
    assert passed_session is session
    assert event.source == "cycle"
    assert event.event_type == "cycle.due_run"
    assert event.target == {"kind": "cortex_idea", "idea_id": "idea-1"}
    assert event.payload["metadata"]["source"] == "cycle"
    assert event.payload["metadata"]["cycle_run_id"] == 12
    assert event.policy["producer"] == "cycle"
    assert event.policy["idempotency_key"] == "cycle_run:12"
    assert event.policy["run_event"] == "thread_reply"


@pytest.mark.asyncio
async def test_execute_cycle_run_logs_uuid_idea_id_without_slicing_error(monkeypatch):
    idea_id = uuid4()

    run = CycleRun()
    run.id = 12
    run.cycle_id = 5
    run.status = "queued"
    run.scheduled_for = datetime(2026, 4, 28, 20, 20, tzinfo=timezone.utc)
    run.prompt_snapshot = "Summarize the newest news"

    cycle = Cycle()
    cycle.id = 5
    cycle.user_id = "user-1"
    cycle.org_id = "org-1"
    cycle.name = "Daily Anthropic news summary"
    cycle.prompt = "Summarize the newest news"
    cycle.schedule_expr = "20 16 * * *"
    cycle.timezone = "America/Toronto"
    cycle.target_idea_id = idea_id
    cycle.deleted_at = None
    cycle.model_override = "anthropic/claude-sonnet-4-6"
    cycle.thinking_override = None

    idea = Idea()
    idea.id = idea_id
    idea.title = "Daily Anthropic news summary"
    idea.display_title = None
    idea.description = "Summarize the newest news"
    idea.status = "needs_input"
    idea.origin = "cycle"
    idea.origin_ref = "cycle:5"
    idea.salience_score = None
    idea.position_x = None
    idea.position_y = None
    idea.created_at = None
    idea.updated_at = None
    idea.user_id = "user-1"
    idea.org_id = "org-1"
    idea.archived_at = None
    idea.active_agents = []
    idea.attachments = []

    session = _AsyncExecuteCycleSession(run=run, cycle=cycle, idea=idea, expected_run_id=run.id)
    monkeypatch.setattr(
        service,
        "UnitOfWork",
        _AsyncUnitOfWorkFactory([session]),
    )
    admissions = []

    async def fake_admit(*args, **kwargs):
        admissions.append(kwargs)
        return 77

    monkeypatch.setattr(service, "_async_admit_cycle_run", fake_admit)
    monkeypatch.setattr(service, "publish", lambda *args, **kwargs: None)
    monkeypatch.setattr(service, "_capture_cycle_emotion", lambda *args, **kwargs: None, raising=False)

    await service.async_execute_cycle_run(run.id)

    assert run.idea_id == idea_id
    assert run.run_id == 77
    assert run.status == "running"
    assert cycle.last_status == "running"
    assert admissions[0]["metadata"]["origin"] == "cycle"
    assert admissions[0]["metadata"]["launch_envelope"]["origin"] == "scheduled_cycle"
    assert (
        admissions[0]["metadata"]["launch_envelope"]["launch_context"]["run_kind"]
        == "scheduled_digest"
    )
    assert admissions[0]["metadata"]["launch_envelope"]["launch_mode"] == "background_cycle_run"
    assert admissions[0]["metadata"]["launch_envelope"]["active_instruction_source"] == "cycle.prompt"
    assert admissions[0]["metadata"]["launch_envelope"]["scheduled_review_window"] == {
        "anchor": "cycle_run.scheduled_for",
        "duration_hours": 24,
        "start_at": "2026-04-27T20:20:00+00:00",
        "end_at": "2026-04-28T20:20:00+00:00",
        "recommendation": (
            "For daily review cycles, inspect [start_at, end_at) instead of a moving "
            "last_24h window based on execution time."
        ),
    }
    assert admissions[0]["metadata"]["contract"]["result"]["kind"] == "autonomous_cycle_run_result"
    assert admissions[0]["metadata"]["contract"]["result"]["run_kind"] == "scheduled_digest"
    assert admissions[0]["metadata"]["evidence_health"]["status"] == "pending"
    assert (
        admissions[0]["metadata"]["launch_receipt"]["scheduled_review_window"]["start_at"]
        == "2026-04-27T20:20:00+00:00"
    )
    assert admissions[0]["metadata"]["context_policy"]["prior_thread_role"] == "context_only"
    assert "tool_policy" not in admissions[0]["metadata"]
    assert run.context_snapshot["result_contract"]["kind"] == "autonomous_cycle_run_result"
    assert run.context_snapshot["evidence_health"]["status"] == "pending"
    assert run.context_snapshot["scheduled_review_window"]["end_at"] == "2026-04-28T20:20:00+00:00"
    assert "Scheduled Cycle Launch" in admissions[0]["message"]
    assert "Scheduled evidence window: 2026-04-27T20:20:00+00:00 to 2026-04-28T20:20:00+00:00 UTC." in admissions[0]["message"]
    assert "Result Contract" in admissions[0]["message"]
    assert "Report evidence health explicitly" in admissions[0]["message"]
    assert "Thread messages are output/context surfaces" in admissions[0]["message"]
    assert "Result Contract and Cycle Mission are authoritative" in admissions[0]["message"]
    assert "Historical thread handoff/preview summaries are context only" in admissions[0]["message"]


@pytest.mark.asyncio
async def test_async_execute_cycle_run_uses_native_uow_without_sync_bridges(monkeypatch):
    idea_id = uuid4()

    run = CycleRun()
    run.id = 12
    run.cycle_id = 5
    run.status = "queued"
    run.scheduled_for = datetime(2026, 4, 28, 20, 20, tzinfo=timezone.utc)
    run.prompt_snapshot = "Summarize the newest news"

    cycle = Cycle()
    cycle.id = 5
    cycle.user_id = "user-1"
    cycle.org_id = "org-1"
    cycle.name = "Daily Anthropic news summary"
    cycle.prompt = "Summarize the newest news"
    cycle.schedule_expr = "20 16 * * *"
    cycle.timezone = "America/Toronto"
    cycle.target_idea_id = idea_id
    cycle.deleted_at = None
    cycle.model_override = "anthropic/claude-sonnet-4-6"
    cycle.thinking_override = None

    idea = Idea()
    idea.id = idea_id
    idea.title = "Daily Anthropic news summary"
    idea.display_title = None
    idea.description = "Summarize the newest news"
    idea.status = "needs_input"
    idea.origin = "cycle"
    idea.origin_ref = "cycle:5"
    idea.salience_score = None
    idea.position_x = None
    idea.position_y = None
    idea.created_at = None
    idea.updated_at = None
    idea.user_id = "user-1"
    idea.org_id = "org-1"
    idea.archived_at = None
    idea.active_agents = []
    idea.attachments = []

    session = _AsyncExecuteCycleSession(run=run, cycle=cycle, idea=idea, expected_run_id=run.id)
    factory = _AsyncUnitOfWorkFactory([session])
    admissions = []

    async def fake_async_admit(*args, **kwargs):
        admissions.append(kwargs)
        return 77

    monkeypatch.setattr(service, "UnitOfWork", factory)
    monkeypatch.setattr(service, "_async_admit_cycle_run", fake_async_admit)
    monkeypatch.setattr(service, "publish", lambda *args, **kwargs: None)
    monkeypatch.setattr(service, "open_unit_of_work", _fail_sync_bridge, raising=False)
    monkeypatch.setattr(service, "run_unit_of_work_task", _fail_sync_bridge, raising=False)

    await service.async_execute_cycle_run(run.id)

    assert factory.uows[0].entered is True
    assert run.idea_id == idea_id
    assert run.run_id == 77
    assert run.status == "running"
    assert cycle.last_status == "running"
    assert admissions[0]["metadata"]["origin"] == "cycle"
    assert "tool_policy" not in admissions[0]["metadata"]


@pytest.mark.asyncio
async def test_execute_cycle_run_auth_blocks_expired_codex_before_agent_admission(monkeypatch):
    from brain.platform.integrations.openai_codex_auth import (
        OpenAICodexCredential,
        encode_codex_auth_payload,
    )

    run, cycle, idea = _cycle_execution_objects(model_override="openai/gpt-5.3-codex")
    expired_payload = json.dumps(encode_codex_auth_payload(OpenAICodexCredential(
        access_token="expired-access",
        refresh_token="refresh-token-123",
        account_id="acct_123",
        expires_at=time.time() - 30,
        auth_mode="chatgpt",
    )))
    session = _AsyncExecuteCycleSession(run=run, cycle=cycle, idea=idea, expected_run_id=run.id)

    async def fake_resolve_key(active_session, **kwargs):
        assert active_session is session
        assert kwargs["user_id"] == "user-1"
        assert kwargs["org_id"] == "org-1"
        assert kwargs["provider"] == "openai"
        return expired_payload, "codex_subscription"

    refresh_calls = []

    def fake_refresh(refresh_token):
        refresh_calls.append(refresh_token)
        raise RuntimeError("invalid_grant")

    admissions = []

    async def fail_admit(*args, **kwargs):
        admissions.append(kwargs)
        raise AssertionError("agent admission should not run after auth preflight blocks")

    published = []
    monkeypatch.setattr(service, "UnitOfWork", _AsyncUnitOfWorkFactory([session]))
    monkeypatch.setattr("brain.platform.integrations.llm._async_resolve_key_from_db", fake_resolve_key)
    monkeypatch.setattr("brain.platform.integrations.llm.refresh_codex_access_token", fake_refresh)
    monkeypatch.setattr(service, "_async_admit_cycle_run", fail_admit)
    monkeypatch.setattr(service, "publish", lambda event, payload: published.append((event, payload)))

    await service.async_execute_cycle_run(run.id)

    assert admissions == []
    assert refresh_calls == ["refresh-token-123"]
    assert run.status == "auth_blocked"
    assert run.run_id is None
    assert run.started_at is None
    assert cycle.last_status == "auth_blocked"
    assert "OpenAI Codex / ChatGPT" in run.error
    assert "Settings > Access" in run.error
    assert "token expired and refresh failed" not in run.error
    assert run.context_snapshot["auth_preflight"]["status"] == "auth_blocked"
    assert run.context_snapshot["auth_preflight"]["credential"] == "OpenAI Codex / ChatGPT"

    thread_msg = next(item for item in session.added if item.__class__.__name__ == "IdeaThread")
    assert thread_msg.role == "illo"
    assert "OpenAI Codex / ChatGPT" in thread_msg.content
    assert "signing in to Codex / ChatGPT again" in thread_msg.content
    evaluation = next(item for item in session.added if item.__class__.__name__ == "CycleRunEvaluation")
    assert evaluation.details["status"] == "auth_blocked"
    assert any(event == "thread_message" for event, _payload in published)


@pytest.mark.asyncio
async def test_execute_cycle_run_valid_codex_preflight_proceeds_to_agent_admission(monkeypatch):
    from brain.platform.integrations.openai_codex_auth import (
        OpenAICodexCredential,
        encode_codex_auth_payload,
    )

    run, cycle, idea = _cycle_execution_objects(model_override="openai/gpt-5.3-codex")
    valid_payload = json.dumps(encode_codex_auth_payload(OpenAICodexCredential(
        access_token="fresh-access",
        refresh_token="refresh-token-123",
        account_id="acct_123",
        expires_at=time.time() + 1800,
        auth_mode="chatgpt",
    )))
    session = _AsyncExecuteCycleSession(run=run, cycle=cycle, idea=idea, expected_run_id=run.id)

    async def fake_resolve_key(active_session, **kwargs):
        assert active_session is session
        assert kwargs["provider"] == "openai"
        return valid_payload, "codex_subscription"

    def fail_refresh(_refresh_token):
        raise AssertionError("valid Codex token should not refresh during preflight")

    codex_client_calls = []

    def fake_codex_client(*args, **kwargs):
        codex_client_calls.append((args, kwargs))
        return object()

    admissions = []

    async def fake_admit(*args, **kwargs):
        admissions.append(kwargs)
        return 77

    monkeypatch.setattr(service, "UnitOfWork", _AsyncUnitOfWorkFactory([session]))
    monkeypatch.setattr("brain.platform.integrations.llm._async_resolve_key_from_db", fake_resolve_key)
    monkeypatch.setattr("brain.platform.integrations.llm.refresh_codex_access_token", fail_refresh)
    monkeypatch.setattr("brain.platform.integrations.llm.OpenAICodexClient", fake_codex_client)
    monkeypatch.setattr(service, "_async_admit_cycle_run", fake_admit)
    monkeypatch.setattr(service, "publish", lambda *args, **kwargs: None)

    await service.async_execute_cycle_run(run.id)

    assert len(admissions) == 1
    assert run.status == "running"
    assert run.run_id == 77
    assert run.started_at is not None
    assert cycle.last_status == "running"
    assert run.context_snapshot["auth_preflight"]["status"] == "passed"
    assert codex_client_calls[0][0][0] == "fresh-access"
    assert admissions[0]["metadata"]["launch_envelope"]["origin"] == "scheduled_cycle"


@pytest.mark.asyncio
async def test_execute_cycle_run_creates_execution_thread_when_target_thread_is_busy(monkeypatch):
    target_idea_id = uuid4()

    run = CycleRun()
    run.id = 12
    run.cycle_id = 5
    run.status = "queued"
    run.scheduled_for = datetime(2026, 4, 28, 20, 20, tzinfo=timezone.utc)
    run.prompt_snapshot = "Summarize the newest news"

    cycle = Cycle()
    cycle.id = 5
    cycle.user_id = "user-1"
    cycle.org_id = "org-1"
    cycle.name = "Daily Anthropic news summary"
    cycle.prompt = "Summarize the newest news"
    cycle.schedule_expr = "20 16 * * *"
    cycle.timezone = "America/Toronto"
    cycle.target_idea_id = target_idea_id
    cycle.deleted_at = None
    cycle.model_override = "anthropic/claude-sonnet-4-6"
    cycle.thinking_override = None

    target_idea = Idea()
    target_idea.id = target_idea_id
    target_idea.title = "Daily Anthropic news summary"
    target_idea.display_title = None
    target_idea.description = "Summarize the newest news"
    target_idea.status = "working"
    target_idea.origin = "cycle"
    target_idea.origin_ref = "cycle:5"
    target_idea.salience_score = None
    target_idea.position_x = None
    target_idea.position_y = None
    target_idea.created_at = None
    target_idea.updated_at = None
    target_idea.user_id = "user-1"
    target_idea.org_id = "org-1"
    target_idea.archived_at = None
    target_idea.active_agents = []
    target_idea.attachments = []

    session = _AsyncExecuteCycleSession(
        run=run,
        cycle=cycle,
        idea=target_idea,
        expected_run_id=run.id,
    )
    admissions = []

    async def fake_async_admit(*args, **kwargs):
        admissions.append(kwargs)
        return 77

    async def fake_idea_has_active_run(*_args, **_kwargs):
        return True

    monkeypatch.setattr(service, "UnitOfWork", _AsyncUnitOfWorkFactory([session]))
    monkeypatch.setattr(cycle_execution, "_async_idea_has_active_run", fake_idea_has_active_run)
    monkeypatch.setattr(service, "_async_admit_cycle_run", fake_async_admit)
    monkeypatch.setattr(service, "publish", lambda *args, **kwargs: None)

    await service.async_execute_cycle_run(run.id)

    created_ideas = [item for item in session.added if isinstance(item, Idea)]
    assert len(created_ideas) == 1
    assert created_ideas[0].origin == "cycle_run"
    assert str(run.idea_id) == str(created_ideas[0].id)
    assert str(cycle.target_idea_id) == str(target_idea_id)
    assert run.status == "running"
    assert run.skip_reason is None
    assert admissions[0]["idea_id"] == created_ideas[0].id
    target_ids = {
        target.get("target_id")
        for target in admissions[0]["metadata"]["cycle_memory"]["output_targets"]
        if isinstance(target, dict) and target.get("target_type") == "thread"
    }
    assert str(target_idea_id) in target_ids
    assert str(created_ideas[0].id) in target_ids


@pytest.mark.asyncio
async def test_manage_cycle_list_uses_native_uow_without_sync_bridges(monkeypatch):
    from brain.systems.runs.tool_catalog.handlers import cycles as cycle_handlers
    from brain.systems.runs.execution_context import bind_agent_context

    cycle = _cycle_for_serialization(
        schedule_expr="0 9 * * *",
        timezone_name="America/Toronto",
    )
    factory = _AsyncUnitOfWorkFactory([_AsyncCycleListSession([cycle])])

    monkeypatch.setattr(cycle_handlers, "UnitOfWork", factory)
    monkeypatch.setattr(cycle_handlers, "open_unit_of_work", _fail_sync_bridge, raising=False)
    monkeypatch.setattr(cycle_handlers, "run_unit_of_work_task", _fail_sync_bridge, raising=False)

    with bind_agent_context({"user_id": "user-1", "org_id": None}):
        payload = json.loads(await cycle_handlers._handle_manage_cycle_async(action="list"))

    assert factory.uows[0].entered is True
    assert payload["cycles"][0]["id"] == cycle.id
    assert payload["cycles"][0]["name"] == cycle.name


@pytest.mark.asyncio
async def test_manage_cycle_run_propagates_agent_trigger_provenance(monkeypatch):
    from brain.systems.runs.tool_catalog.handlers import cycles as cycle_handlers
    from brain.systems.runs.execution_context import bind_agent_context

    cycle = _cycle_for_serialization(
        schedule_expr="0 8,13,18 * * *",
        timezone_name="America/New_York",
    )
    factory = _AsyncUnitOfWorkFactory([_AsyncCycleListSession([cycle])])
    captured = {}

    async def fake_run_cycle_now(cycle_id, *, run_kind, launch_context=None):
        captured["cycle_id"] = cycle_id
        captured["run_kind"] = run_kind
        captured["launch_context"] = launch_context
        return {"id": 99, "cycle_id": cycle_id, "status": "queued"}

    monkeypatch.setattr(cycle_handlers, "UnitOfWork", factory)
    monkeypatch.setattr(cycle_handlers, "async_run_cycle_now", fake_run_cycle_now)
    monkeypatch.setattr(cycle_handlers, "publish_cycle_change", lambda **_kwargs: None)

    with bind_agent_context(
        {
            "user_id": "user-1",
            "org_id": None,
            "run_id": 1438,
            "idea_id": "reflex-thread",
        }
    ):
        payload = json.loads(
            await cycle_handlers._handle_manage_cycle_async(
                action="run",
                id=cycle.id,
                rationale="EVENT_TRIGGER: PR #990 merged and needs deploy",
            )
        )

    assert payload["run"]["id"] == 99
    assert captured == {
        "cycle_id": cycle.id,
        "run_kind": "off_slot_material_alert",
        "launch_context": {
            "origin": AGENT_TRIGGERED_CYCLE_ORIGIN,
            "source": "manage_cycle",
            "actor_type": "agent",
            "actor_id": "1438",
            "thread_id": "reflex-thread",
            "rationale": "EVENT_TRIGGER: PR #990 merged and needs deploy",
        },
    }


@pytest.mark.asyncio
async def test_manage_cycle_run_can_select_explicit_scheduled_digest_contract(monkeypatch):
    from brain.systems.runs.tool_catalog.handlers import cycles as cycle_handlers
    from brain.systems.runs.execution_context import bind_agent_context

    cycle = _cycle_for_serialization(
        schedule_expr="0 8,13,18 * * *",
        timezone_name="America/New_York",
    )
    factory = _AsyncUnitOfWorkFactory([_AsyncCycleListSession([cycle])])
    captured = {}

    async def fake_run_cycle_now(cycle_id, *, run_kind, launch_context=None):
        captured["cycle_id"] = cycle_id
        captured["run_kind"] = run_kind
        captured["launch_context"] = launch_context
        return {"id": 99, "cycle_id": cycle_id, "status": "queued"}

    monkeypatch.setattr(cycle_handlers, "UnitOfWork", factory)
    monkeypatch.setattr(cycle_handlers, "async_run_cycle_now", fake_run_cycle_now)
    monkeypatch.setattr(cycle_handlers, "publish_cycle_change", lambda **_kwargs: None)

    with bind_agent_context(
        {
            "user_id": "user-1",
            "org_id": None,
            "run_id": 1438,
            "idea_id": "reflex-thread",
        }
    ):
        payload = json.loads(
            await cycle_handlers._handle_manage_cycle_async(
                action="run",
                id=cycle.id,
                rationale="Re-run the scheduled digest",
                run_kind="scheduled_digest",
            )
        )

    assert payload["run"]["id"] == 99
    assert captured["run_kind"] == "scheduled_digest"


@pytest.mark.asyncio
async def test_manage_cycle_run_rejects_unknown_run_kind_before_loading_cycle(
    monkeypatch,
):
    from brain.systems.runs.tool_catalog.handlers import cycles as cycle_handlers
    from brain.systems.runs.execution_context import bind_agent_context

    def forbidden_unit_of_work():
        raise AssertionError("invalid run kinds must fail before database access")

    monkeypatch.setattr(cycle_handlers, "UnitOfWork", forbidden_unit_of_work)

    with bind_agent_context({"user_id": "user-1", "idea_id": "reflex-thread"}):
        payload = json.loads(
            await cycle_handlers._handle_manage_cycle_async(
                action="run",
                id=2,
                run_kind="inferred_off_slot",
            )
        )

    assert payload == {
        "error": (
            "cycle run_kind must be one of: "
            "off_slot_material_alert, scheduled_digest"
        )
    }


@pytest.mark.asyncio
async def test_recover_stale_cycle_runs_executes_recent_queued_and_settles_old_rows(monkeypatch):
    now = datetime.now(timezone.utc)

    recent_queued = CycleRun()
    recent_queued.id = 21
    recent_queued.cycle_id = 5
    recent_queued.status = "queued"
    recent_queued.scheduled_for = now.replace(microsecond=0) - timedelta(hours=2)
    recent_queued.prompt_snapshot = "Run recent missed work"

    old_queued = CycleRun()
    old_queued.id = 22
    old_queued.cycle_id = 5
    old_queued.status = "queued"
    old_queued.scheduled_for = now.replace(microsecond=0) - timedelta(days=3)
    old_queued.prompt_snapshot = "Run ancient missed work"

    stale_running = CycleRun()
    stale_running.id = 23
    stale_running.cycle_id = 6
    stale_running.status = "running"
    stale_running.scheduled_for = now.replace(microsecond=0) - timedelta(hours=3)
    stale_running.started_at = stale_running.scheduled_for
    stale_running.run_id = 99
    stale_running.prompt_snapshot = "Finish stale running work"

    active_cycle = Cycle()
    active_cycle.id = 5
    active_cycle.deleted_at = None
    active_cycle.last_run_at = now
    active_cycle.last_status = "completed"
    active_cycle.last_error = None

    running_cycle = Cycle()
    running_cycle.id = 6
    running_cycle.deleted_at = None
    running_cycle.last_run_at = stale_running.scheduled_for
    running_cycle.last_status = "running"
    running_cycle.last_error = None

    agent_run = AgentRun()
    agent_run.id = 99
    agent_run.status = "completed"

    factory = _AsyncUnitOfWorkFactory([
        _RecoverStaleSession(
            runs=[old_queued, recent_queued, stale_running],
            cycles=[active_cycle, running_cycle],
            agent_runs=[agent_run],
        )
    ])
    executed_run_ids = []

    async def fake_async_execute_cycle_run(run_id):
        executed_run_ids.append(run_id)
        recent_queued.status = "running"

    monkeypatch.setattr(service, "UnitOfWork", factory)
    monkeypatch.setattr(service, "async_execute_cycle_run", fake_async_execute_cycle_run)

    recovered = await service.async_recover_stale_cycle_runs_once(
        stale_after_seconds=60,
        catchup_window_seconds=24 * 60 * 60,
    )

    assert recovered == [recent_queued.id]
    assert executed_run_ids == [recent_queued.id]
    assert old_queued.status == "skipped"
    assert old_queued.skip_reason == "missed_catchup_window"
    assert active_cycle.last_status == "completed"
    assert stale_running.status == "completed"
    assert stale_running.completed_at is not None
    assert running_cycle.last_status == "completed"


@pytest.mark.asyncio
async def test_execute_cycle_run_ignores_non_queued_rows(monkeypatch):
    run = CycleRun()
    run.id = 12
    run.status = "running"

    factory = _AsyncUnitOfWorkFactory([
        _SingleRunSession(run),
    ])

    monkeypatch.setattr(service, "UnitOfWork", factory)

    await service.async_execute_cycle_run(run.id)

    assert run.status == "running"


async def test_cycle_busy_thread_detection_uses_agent_run_open_statuses():
    class _CaptureExecuteSession:
        def __init__(self):
            self.statement = None

        async def execute(self, statement):
            self.statement = statement
            return _FirstResult(None)

    session = _CaptureExecuteSession()

    assert await cycle_execution._async_idea_has_active_run(session, "idea-1") is False

    compiled_params = session.statement.compile().params
    status_values = next(
        value
        for value in compiled_params.values()
        if isinstance(value, (list, tuple, set)) and "queued" in value
    )
    assert set(status_values) == {"queued", "starting", "running", "paused", "verifying"}
    assert "pending_approval" not in status_values


def test_finalize_cycle_run_from_run_updates_cycle_and_run(monkeypatch):
    agent_run = AgentRun()
    agent_run.id = 44
    agent_run.root_run_id = 44
    agent_run.user_id = "user-1"
    agent_run.org_id = "org-1"
    agent_run.input_message = "Run the Cycle mission"
    agent_run.model_policy = {}
    agent_run.metadata_ = {"source": "cycle", "cycle_run_id": 12}

    cycle_run = CycleRun()
    cycle_run.id = 12
    cycle_run.cycle_id = 5
    cycle_run.status = "running"
    cycle_run.completed_at = None
    cycle_run.error = None
    cycle_run.skip_reason = None
    cycle_run.context_snapshot = {
        "scheduled_review_window": {"start_at": "2026-04-27T20:20:00+00:00"},
        "result_contract": {"kind": "autonomous_cycle_run_result"},
        "evidence_health": {"status": "pending"},
        "launch_receipts": [{"kind": "cycle_launch_receipt", "cycle_run_id": 12}],
    }

    cycle = Cycle()
    cycle.id = 5
    cycle.prompt = "Run the smoke test. Report evidence health, next action, and self-review."
    cycle.last_status = None
    cycle.last_error = "old"
    cycle.last_run_at = None

    final_answer = AgentRunArtifactRow(
        id=8,
        run_id=44,
        root_run_id=44,
        artifact_type="final_answer",
        text=(
            "I completed the Cycle mission. Evidence health: ok. Workspace evidence "
            "showed the smoke test was clean. Next action: continue monitoring. "
            "Self-review summary: contract satisfied."
        ),
        created_at=datetime(2026, 4, 28, 20, 25, tzinfo=timezone.utc),
    )
    completed_side_effect = AgentRunEventRow(
        id=4,
        run_id=44,
        root_run_id=44,
        sequence_no=4,
        event_type="run.tool_completed",
        payload={
            "tool_name": "update_cycle_tracker",
            "result": {"status": "ok", "record_id": 26},
        },
    )
    fake_session = _AsyncFakeSession(
        agent_run=agent_run,
        run=cycle_run,
        cycle=cycle,
        artifacts=[final_answer],
        events=[completed_side_effect],
    )
    monkeypatch.setattr(
        service,
        "UnitOfWork",
        _AsyncUnitOfWorkFactory([fake_session]),
    )

    service.finalize_cycle_run_from_run(44, status="completed")

    assert cycle_run.status == "completed"
    assert cycle_run.completed_at is not None
    assert cycle_run.error is None
    assert cycle.last_status == "completed"
    assert cycle.last_error is None
    assert cycle.last_run_at is not None
    evaluation = fake_session.added[0]
    assert evaluation.details["scheduled_review_window"]["start_at"] == "2026-04-27T20:20:00+00:00"
    assert evaluation.details["result_contract"]["kind"] == "autonomous_cycle_run_result"
    assert evaluation.details["evidence_health"]["status"] == "pending"
    assert evaluation.details["launch_receipts"] == [
        {"kind": "cycle_launch_receipt", "cycle_run_id": 12}
    ]
    verdict = evaluation.details["mission_result_contract_verdict"]
    assert verdict["settlement_status"] == "mission_success"
    assert verdict["candidate_artifact_id"] == 8
    assert verdict["side_effects_succeeded"] is True
    assert fake_session._artifacts[-1].text == final_answer.text


def test_cycle_contract_rejects_raw_provider_error_as_visible_answer():
    from brain.systems.cycles.contract_gate import evaluate_cycle_result_contract

    review = evaluate_cycle_result_contract(
        candidate_answer=RAW_PROVIDER_ERROR,
        result_contract={"kind": "autonomous_cycle_run_result", "required_outputs": []},
        mission="",
        evidence_packet={},
    )

    assert review["approved"] is False
    assert review["missing_outputs"] == ["visible_final_answer"]
    assert review["provider_error"] == "server_error"
    assert "help.openai.com" not in review["candidate_summary"]


def test_cycle_contract_rejects_empty_answer_with_captured_provider_exception():
    from brain.systems.cycles.contract_gate import evaluate_cycle_result_contract

    review = evaluate_cycle_result_contract(
        candidate_answer=None,
        result_contract={"kind": "autonomous_cycle_run_result", "required_outputs": []},
        mission="",
        evidence_packet={},
        provider_exception=RuntimeError("upstream request failed | overloaded_error"),
    )

    assert review["approved"] is False
    assert review["missing_outputs"] == ["visible_final_answer"]
    assert review["provider_error"] == "overloaded_error"


@pytest.mark.parametrize("completed_event_count", [0, 2], ids=["no-op", "acted-on-events"])
def test_reflex_cycle_satisfied_launch_contract_completes(
    monkeypatch,
    completed_event_count,
):
    from brain.systems.cycles import contract_gate

    events = [
        AgentRunEventRow(
            id=index + 1,
            run_id=44,
            root_run_id=44,
            sequence_no=index + 1,
            event_type="run.tool_completed",
            payload={
                "tool_name": "update_github_event",
                "result": {"status": "ok", "event_id": index + 100},
            },
        )
        for index in range(completed_event_count)
    ]
    session, cycle_run, cycle, final_answer = _contract_finalization_scenario(
        cycle_id=8,
        mission=REFLEX_MISSION,
        answer=REFLEX_ANSWER,
        events=events,
    )
    repair_calls = []

    async def fake_repair(**kwargs):
        repair_calls.append(kwargs)
        return None

    monkeypatch.setattr(contract_gate, "_async_repair_cycle_contract_answer", fake_repair)
    monkeypatch.setattr(service, "UnitOfWork", _AsyncUnitOfWorkFactory([session]))

    service.finalize_cycle_run_from_run(44, status="completed")

    assert repair_calls == []
    assert cycle_run.status == "completed"
    assert cycle_run.error is None
    assert cycle.last_status == "completed"
    assert cycle.last_error is None
    verdict = cycle_run.context_snapshot["mission_result_contract_verdict"]
    assert verdict["settlement_status"] == "mission_success"
    assert verdict["missing_outputs"] == []
    assert verdict["side_effects_succeeded"] is bool(completed_event_count)
    assert session._artifacts[-1] is final_answer


def test_coordinator_posted_digest_scores_one_without_repair_or_repost(monkeypatch):
    from brain.systems.cycles import contract_gate

    mission = (
        "Publish the chantier-primary coordinator digest with exact tracker counts, "
        "moving chantiers, loose items, and the per-person recap."
    )
    answer = (
        "Chantier-primary digest: 3 active chantiers, 8 open issues, and 4 open PRs.\n"
        "Moving chantier — coordinator reliability: the contract fix is ready for review; "
        "Reda owns the merge and there are no blockers.\n"
        "Loose items: none. Per-person recap: Reda reviews; Axel and JB have no queued work.\n"
        "Evidence reviewed: workspace trackers, GitHub issues, PRs, and chantier records.\n"
        "Evidence health: ok — every required reader completed and the Slack post succeeded.\n"
        "Next action: Reda reviews the coordinator contract fix.\n"
        "Self-review summary: full sweep, reconciled counts, and one complete digest posted."
    )
    slack_post = AgentRunEventRow(
        id=1,
        run_id=44,
        root_run_id=44,
        sequence_no=1,
        event_type="run.tool_completed",
        payload={
            "tool_name": "post_slack_reply",
            "result": {"status": "ok", "channel": "uwear-engineering"},
        },
    )
    session, cycle_run, cycle, _ = _contract_finalization_scenario(
        cycle_id=2,
        mission=mission,
        answer=answer,
        events=[slack_post],
        launch_contract=cycle_result_contract(run_kind="scheduled_digest"),
    )
    repair_calls = []

    async def fake_repair(**kwargs):
        repair_calls.append(kwargs)
        return None

    monkeypatch.setattr(contract_gate, "_async_repair_cycle_contract_answer", fake_repair)
    monkeypatch.setattr(service, "UnitOfWork", _AsyncUnitOfWorkFactory([session]))

    service.finalize_cycle_run_from_run(44, status="completed")

    assert repair_calls == []
    assert cycle_run.status == "completed"
    assert cycle_run.error is None
    assert cycle.last_status == "completed"
    assert cycle.last_error is None
    evaluation = next(
        item for item in session.added if item.__class__.__name__ == "CycleRunEvaluation"
    )
    assert evaluation.score == 1
    verdict = evaluation.details["mission_result_contract_verdict"]
    assert verdict["settlement_status"] == "mission_success"
    assert verdict["missing_outputs"] == []
    assert verdict["final_missing_outputs"] == []
    assert verdict["enforced_required_outputs"] == RESULT_CONTRACT_REQUIRED_OUTPUTS
    assert verdict["side_effects_succeeded"] is True
    assert len(session._events) == 1
    assert len(session._artifacts) == 1


@pytest.mark.parametrize(
    ("run_id", "answer"),
    [
        (
            1857,
            "Material engineering alert: deploy admission now preserves the active release "
            "when a duplicate trigger arrives. Evidence reviewed: merged change, deploy "
            "receipt, Slack post, and tracker update. Evidence health: ok. Next action: "
            "monitor the next trigger.",
        ),
        (
            1884,
            "Material engineering alert: chantier movement now records the newly blocked "
            "member and owner in the tracker. Evidence reviewed: current chantier, issue, "
            "Slack post, and tracker update. Evidence health: ok. Self-review summary: "
            "single material change verified and posted once.",
        ),
    ],
    ids=["run-1857-no-self-review", "run-1884-no-next-action"],
)
def test_off_slot_material_alert_settles_completed_without_digest_footer_fields(
    monkeypatch,
    run_id,
    answer,
):
    from brain.systems.cycles import contract_gate

    side_effects = [
        AgentRunEventRow(
            id=1,
            run_id=44,
            root_run_id=44,
            sequence_no=1,
            event_type="run.tool_completed",
            payload={
                "tool_name": "post_slack_reply",
                "result": {"status": "ok", "channel": "4_software"},
            },
        ),
        AgentRunEventRow(
            id=2,
            run_id=44,
            root_run_id=44,
            sequence_no=2,
            event_type="run.tool_completed",
            payload={
                "tool_name": "update_domain_record",
                "result": {"status": "ok", "record_id": run_id},
            },
        ),
    ]
    contract = cycle_result_contract(run_kind="off_slot_material_alert")
    session, cycle_run, cycle, _ = _contract_finalization_scenario(
        cycle_id=2,
        mission="Post one concise material engineering change alert.",
        answer=answer,
        events=side_effects,
        launch_contract=contract,
    )
    repair_calls = []

    async def fake_repair(**kwargs):
        repair_calls.append(kwargs)
        return None

    monkeypatch.setattr(contract_gate, "_async_repair_cycle_contract_answer", fake_repair)
    monkeypatch.setattr(service, "UnitOfWork", _AsyncUnitOfWorkFactory([session]))

    service.finalize_cycle_run_from_run(44, status="completed")

    assert repair_calls == []
    assert cycle_run.status == "completed"
    assert cycle_run.error is None
    assert cycle.last_status == "completed"
    verdict = cycle_run.context_snapshot["mission_result_contract_verdict"]
    assert verdict["settlement_status"] == "mission_success"
    assert verdict["missing_outputs"] == []
    assert verdict["final_missing_outputs"] == []
    assert verdict["enforced_required_outputs"] == contract["required_outputs"]


def test_coordinator_unswept_unposted_digest_still_degrades(monkeypatch):
    from brain.systems.cycles import contract_gate

    session, cycle_run, cycle, _ = _contract_finalization_scenario(
        cycle_id=2,
        mission="Sweep the workspace and publish the chantier-primary coordinator digest.",
        answer="The coordinator could not sweep the workspace or post the digest to Slack.",
        events=[
            AgentRunEventRow(
                id=1,
                run_id=44,
                root_run_id=44,
                sequence_no=1,
                event_type="run.tool_failed",
                payload={
                    "tool_name": "post_slack_reply",
                    "is_error": True,
                    "error": "Slack output target unavailable",
                },
            )
        ],
        launch_contract=cycle_result_contract(run_kind="scheduled_digest"),
    )

    async def fake_repair(**kwargs):
        return None

    monkeypatch.setattr(contract_gate, "_async_repair_cycle_contract_answer", fake_repair)
    monkeypatch.setattr(service, "UnitOfWork", _AsyncUnitOfWorkFactory([session]))

    service.finalize_cycle_run_from_run(44, status="completed")

    assert cycle_run.status == "degraded"
    assert cycle_run.error.startswith("mission_contract_failed: missing")
    assert cycle.last_status == "degraded"
    evaluation = next(
        item for item in session.added if item.__class__.__name__ == "CycleRunEvaluation"
    )
    assert evaluation.score == 0
    verdict = evaluation.details["mission_result_contract_verdict"]
    assert verdict["settlement_status"] == "mission_contract_failed"
    assert verdict["side_effects_succeeded"] is False
    assert "report_evidence_health" in verdict["final_missing_outputs"]
    assert "short_self_review_summary" in verdict["final_missing_outputs"]


@pytest.mark.parametrize(
    ("mission", "evidence_packet"),
    [
        (REFLEX_MISSION, {}),
        (
            "Publish a coordinator digest with domain tracking.",
            {"domain_side_effects_succeeded": True},
        ),
    ],
    ids=["reflex", "coordinator"],
)
def test_cycle_contract_enforces_only_advertised_required_outputs(mission, evidence_packet):
    from brain.systems.cycles.contract_gate import evaluate_cycle_result_contract

    result_contract = _result_contract(["report_evidence_health"])
    review = evaluate_cycle_result_contract(
        candidate_answer="Evidence health: ok.",
        result_contract=result_contract,
        mission=mission,
        evidence_packet=evidence_packet,
    )

    assert set(review["missing_outputs"]) <= set(result_contract["required_outputs"])
    assert set(review["enforced_required_outputs"]) <= set(
        result_contract["required_outputs"]
    )
    assert review["approved"] is True


def test_cycle_contract_for_run_prefers_advertised_launch_envelope():
    from brain.systems.cycles.contract_gate import cycle_result_contract_for_run

    launch_contract = _result_contract(["report_evidence_health"])
    stale_snapshot_contract = _result_contract(["failure_map"])
    session, cycle_run, _, _ = _contract_finalization_scenario(
        cycle_id=8,
        mission=REFLEX_MISSION,
        answer="Evidence health: ok.",
        launch_contract=launch_contract,
        snapshot_contract=stale_snapshot_contract,
    )

    assert cycle_result_contract_for_run(session._agent_run, cycle_run) == launch_contract


def test_reflex_cycle_missing_declared_evidence_health_still_degrades(monkeypatch):
    from brain.systems.cycles import contract_gate

    answer_without_evidence_health = "\n".join(
        line for line in REFLEX_ANSWER.splitlines() if not line.startswith("Evidence health:")
    )
    session, cycle_run, cycle, _ = _contract_finalization_scenario(
        cycle_id=8,
        mission=REFLEX_MISSION,
        answer=answer_without_evidence_health,
    )

    async def fake_repair(**kwargs):
        return None

    monkeypatch.setattr(contract_gate, "_async_repair_cycle_contract_answer", fake_repair)
    monkeypatch.setattr(service, "UnitOfWork", _AsyncUnitOfWorkFactory([session]))

    service.finalize_cycle_run_from_run(44, status="completed")

    assert cycle_run.status == "degraded"
    assert cycle_run.error == "mission_contract_failed: missing report_evidence_health"
    assert cycle.last_status == "degraded"
    assert cycle.last_error == cycle_run.error
    verdict = cycle_run.context_snapshot["mission_result_contract_verdict"]
    assert verdict["final_missing_outputs"] == ["report_evidence_health"]


@pytest.mark.asyncio
async def test_cycle_contract_gate_repairs_bad_visible_answer_once(monkeypatch):
    from brain.systems.cycles import contract_gate

    agent_run = AgentRun()
    agent_run.id = 44
    agent_run.root_run_id = 44
    agent_run.user_id = "user-1"
    agent_run.org_id = "org-1"
    agent_run.input_message = "Daily Illo Conversation Improvements"
    agent_run.model_policy = {}
    agent_run.metadata_ = {"source": "cycle", "cycle_run_id": 12}

    cycle_run = CycleRun()
    cycle_run.id = 12
    cycle_run.cycle_id = 5
    cycle_run.status = "running"
    cycle_run.prompt_snapshot = (
        "Daily Illo Conversation Improvements. Produce: 24h readout, failure map, "
        "codebase implications, proposals, tracking summary, impact loop, next action. "
        "Include Domain tracking, evidence health, and a short self-review summary."
    )
    cycle_run.context_snapshot = {
        "result_contract": {
            "kind": "autonomous_cycle_run_result",
            "required_outputs": [
                "answer_the_cycle_mission",
                "summarize_workspace_evidence_or_explicit_gaps",
                "report_evidence_health",
                "record_next_action_or_blocker",
                "short_self_review_summary",
            ],
        },
        "evidence_health": {"status": "pending"},
    }

    cycle = Cycle()
    cycle.id = 5
    cycle.prompt = cycle_run.prompt_snapshot

    bad_answer = AgentRunArtifactRow(
        id=8,
        run_id=44,
        root_run_id=44,
        artifact_type="final_answer",
        text=(
            "Verified current runtime facts:\n"
            "- I'm Illo, the agent inside an Illospace workspace.\n"
            "- Runtime scope: workspace-bound, user-bound, and thread-bound."
        ),
        created_at=datetime(2026, 4, 28, 20, 25, tzinfo=timezone.utc),
    )
    domain_event = AgentRunEventRow(
        id=4,
        run_id=44,
        root_run_id=44,
        sequence_no=4,
        event_type="run.tool_completed",
        payload={
            "tool_name": "create_domain_record",
            "result": {"status": "ok", "record_id": 26},
        },
    )
    session = _AsyncFakeSession(
        agent_run=agent_run,
        run=cycle_run,
        cycle=cycle,
        artifacts=[bad_answer],
        events=[domain_event],
    )
    repair_calls = []

    async def fake_repair(**kwargs):
        repair_calls.append(kwargs)
        return (
            "24h readout: reviewed workspace evidence and Domain records.\n"
            "Failure map: the visible answer was runtime introspection.\n"
            "Codebase implications: enforce the Cycle contract before finalization.\n"
            "Proposals: keep the contract gate and one repair pass.\n"
            "Tracking summary: Domain record 26 captured the work.\n"
            "Impact loop: future runs can compare repaired answers to outcomes.\n"
            "Next action: monitor the next scheduled run.\n"
            "evidence_health=ok.\n"
            "Self-review summary: mission result contract satisfied after repair."
        )

    monkeypatch.setattr(contract_gate, "_async_repair_cycle_contract_answer", fake_repair)

    verdict = await contract_gate.async_prepare_cycle_run_visible_finalization(session, 44)

    assert len(repair_calls) == 1
    assert repair_calls[0]["candidate_answer"] == bad_answer.text
    assert "answer_the_cycle_mission" in repair_calls[0]["missing_outputs"]
    assert "failure map" not in repair_calls[0]["missing_outputs"]
    assert verdict["repair_attempted"] is True
    assert verdict["repair_succeeded"] is True
    assert verdict["settlement_status"] == "mission_success_after_repair"
    assert verdict["side_effects_succeeded"] is True
    assert verdict["domain_side_effects_succeeded"] is True
    latest_answer = session._artifacts[-1]
    assert latest_answer.artifact_type == "final_answer"
    assert latest_answer.text.startswith("24h readout")
    assert "Verified current runtime facts" not in latest_answer.text
    assert cycle_run.context_snapshot["mission_result_contract_verdict"] == verdict


@pytest.mark.asyncio
async def test_cycle_contract_gate_appends_one_missing_required_output_to_draft(monkeypatch):
    from brain.systems.cycles import contract_gate

    agent_run = AgentRun()
    agent_run.id = 44
    agent_run.root_run_id = 44
    agent_run.user_id = "user-1"
    agent_run.org_id = "org-1"
    agent_run.input_message = "Review the scheduled Cycle smoke test."
    agent_run.model_policy = {}
    agent_run.metadata_ = {"source": "cycle", "cycle_run_id": 12}

    cycle_run = CycleRun()
    cycle_run.id = 12
    cycle_run.cycle_id = 5
    cycle_run.status = "running"
    cycle_run.prompt_snapshot = agent_run.input_message
    cycle_run.context_snapshot = {
        "result_contract": {
            "kind": "autonomous_cycle_run_result",
            "required_outputs": [
                "answer_the_cycle_mission",
                "summarize_workspace_evidence_or_explicit_gaps",
                "report_evidence_health",
                "record_next_action_or_blocker",
                "short_self_review_summary",
            ],
        }
    }

    cycle = Cycle()
    cycle.id = 5
    cycle.prompt = cycle_run.prompt_snapshot

    substantive_draft = AgentRunArtifactRow(
        id=8,
        run_id=44,
        root_run_id=44,
        artifact_type="final_answer",
        text=(
            "The scheduled Cycle smoke test completed successfully and its tracker was updated. "
            "Workspace evidence showed the expected output with no explicit gaps. "
            "Evidence health: ok. Next action: monitor the next scheduled run."
        ),
        created_at=datetime(2026, 4, 28, 20, 25, tzinfo=timezone.utc),
    )
    completed_side_effect = AgentRunEventRow(
        id=4,
        run_id=44,
        root_run_id=44,
        sequence_no=4,
        event_type="run.tool_completed",
        payload={"tool_name": "update_cycle_tracker", "result": {"status": "ok"}},
    )
    session = _AsyncFakeSession(
        agent_run=agent_run,
        run=cycle_run,
        cycle=cycle,
        artifacts=[substantive_draft],
        events=[completed_side_effect],
    )
    repair_calls = []

    async def fake_repair(**kwargs):
        repair_calls.append(kwargs)
        return "Self-review summary: the mission result is supported by the recorded evidence."

    monkeypatch.setattr(contract_gate, "_async_repair_cycle_contract_answer", fake_repair)

    verdict = await contract_gate.async_prepare_cycle_run_visible_finalization(session, 44)

    assert len(repair_calls) == 1
    assert repair_calls[0]["candidate_answer"] == substantive_draft.text
    assert repair_calls[0]["missing_outputs"] == ["short_self_review_summary"]
    assert verdict["settlement_status"] == "mission_success_after_repair"
    assert verdict["repair_succeeded"] is True
    assert verdict["final_missing_outputs"] == []
    repaired_answer = session._artifacts[-1].text
    assert substantive_draft.text in repaired_answer
    assert "Self-review summary:" in repaired_answer
    assert repaired_answer.count("Workspace evidence showed") == 1


def test_cycle_contract_repair_prompt_requests_only_missing_sections():
    from brain.systems.cycles.contract_gate import _repair_prompt

    candidate = (
        "Mission complete. Workspace evidence showed success. Evidence health: ok. "
        "Next action: monitor."
    )

    messages = _repair_prompt(
        mission="Review the scheduled Cycle smoke test.",
        result_contract={"required_outputs": ["short_self_review_summary"]},
        evidence_packet={"side_effects_succeeded": True},
        missing_outputs=["short_self_review_summary"],
        candidate_answer=candidate,
        append_only=True,
    )

    prompt = "\n".join(message["content"] for message in messages)
    assert "append only the missing section" in prompt.lower()
    assert "do not repeat or rewrite" in prompt.lower()
    assert candidate in prompt


def test_finalize_cycle_run_degrades_when_contract_repair_fails(monkeypatch):
    from brain.systems.cycles import contract_gate

    agent_run = AgentRun()
    agent_run.id = 44
    agent_run.root_run_id = 44
    agent_run.user_id = "user-1"
    agent_run.org_id = "org-1"
    agent_run.input_message = "Daily Illo Conversation Improvements"
    agent_run.model_policy = {}
    agent_run.metadata_ = {"source": "cycle", "cycle_run_id": 12}

    cycle_run = CycleRun()
    cycle_run.id = 12
    cycle_run.cycle_id = 5
    cycle_run.status = "running"
    cycle_run.error = None
    cycle_run.skip_reason = None
    cycle_run.prompt_snapshot = (
        "Daily Illo Conversation Improvements. Produce: 24h readout, failure map, "
        "codebase implications, proposals, tracking summary, impact loop, next action. "
        "Include Domain tracking, evidence health, and a short self-review summary."
    )
    cycle_run.context_snapshot = {
        "result_contract": {
            "kind": "autonomous_cycle_run_result",
            "required_outputs": [
                "answer_the_cycle_mission",
                "summarize_workspace_evidence_or_explicit_gaps",
                "report_evidence_health",
                "record_next_action_or_blocker",
                "short_self_review_summary",
            ],
        },
        "evidence_health": {"status": "pending"},
    }

    cycle = Cycle()
    cycle.id = 5
    cycle.prompt = cycle_run.prompt_snapshot
    cycle.last_status = None
    cycle.last_error = None
    cycle.last_run_at = None

    substantive_draft = AgentRunArtifactRow(
        id=8,
        run_id=44,
        root_run_id=44,
        artifact_type="final_answer",
        text=(
            "24h readout: workspace evidence showed the scheduled review completed. "
            "Failure map: no execution failures were observed. Codebase implications: none. "
            "Proposals: continue monitoring. Tracking summary: the tracker update succeeded. "
            "Impact loop: compare the next run. Next action: monitor the next scheduled run. "
            "Evidence health: ok. Domain record 26 contains the tracking result."
        ),
        created_at=datetime(2026, 4, 28, 20, 25, tzinfo=timezone.utc),
    )
    domain_event = AgentRunEventRow(
        id=4,
        run_id=44,
        root_run_id=44,
        sequence_no=4,
        event_type="run.tool_completed",
        payload={
            "tool_name": "create_domain_record",
            "result": {"status": "ok", "record_id": 26},
        },
    )
    fake_session = _AsyncFakeSession(
        agent_run=agent_run,
        run=cycle_run,
        cycle=cycle,
        artifacts=[substantive_draft],
        events=[domain_event],
    )

    repair_calls = []

    async def fake_repair(**kwargs):
        repair_calls.append(kwargs)
        return "The repair attempt did not include the requested section."

    monkeypatch.setattr(contract_gate, "_async_repair_cycle_contract_answer", fake_repair)
    monkeypatch.setattr(service, "UnitOfWork", _AsyncUnitOfWorkFactory([fake_session]))

    service.finalize_cycle_run_from_run(44, status="completed")

    assert len(repair_calls) == 1
    assert cycle_run.status == "degraded"
    assert cycle.last_status == "degraded"
    assert cycle_run.error.startswith("mission_contract_failed: missing")
    latest_answer = fake_session._artifacts[-1].text
    assert substantive_draft.text in latest_answer
    assert "Cycle run degraded: mission_contract_failed" in latest_answer
    assert latest_answer != "Cycle run degraded: mission_contract_failed"
    evaluation = next(
        item for item in fake_session.added if item.__class__.__name__ == "CycleRunEvaluation"
    )
    verdict = evaluation.details["mission_result_contract_verdict"]
    assert verdict["settlement_status"] == "mission_contract_failed"
    assert verdict["repair_attempted"] is True
    assert verdict["side_effects_succeeded"] is True
    assert verdict["missing_outputs"] == ["short_self_review_summary"]


@pytest.mark.asyncio
async def test_cycle_contract_provider_error_degrades_without_leaking_and_preserves_side_effects(
    monkeypatch,
):
    from brain.systems.cycles import contract_gate

    agent_run = AgentRun()
    agent_run.id = 44
    agent_run.root_run_id = 44
    agent_run.user_id = "user-1"
    agent_run.org_id = "org-1"
    agent_run.input_message = "Record the daily tracker update."
    agent_run.model_policy = {}
    agent_run.metadata_ = {"source": "cycle", "cycle_run_id": 12}

    cycle_run = CycleRun()
    cycle_run.id = 12
    cycle_run.cycle_id = 5
    cycle_run.status = "running"
    cycle_run.prompt_snapshot = "Record the daily tracker update."
    cycle_run.context_snapshot = {
        "result_contract": {
            "kind": "autonomous_cycle_run_result",
            "required_outputs": [],
        }
    }

    cycle = Cycle()
    cycle.id = 5
    cycle.prompt = cycle_run.prompt_snapshot

    provider_error_artifact = AgentRunArtifactRow(
        id=8,
        run_id=44,
        root_run_id=44,
        artifact_type="final_answer",
        text=RAW_PROVIDER_ERROR,
        created_at=datetime(2026, 4, 28, 20, 25, tzinfo=timezone.utc),
    )
    tracker_event = AgentRunEventRow(
        id=4,
        run_id=44,
        root_run_id=44,
        sequence_no=4,
        event_type="run.tool_completed",
        payload={
            "tool_name": "create_domain_record",
            "result": {"status": "ok", "record_id": 26},
        },
    )
    session = _AsyncFakeSession(
        agent_run=agent_run,
        run=cycle_run,
        cycle=cycle,
        artifacts=[provider_error_artifact],
        events=[tracker_event],
    )
    repair_calls = []

    async def fake_repair(**kwargs):
        repair_calls.append(kwargs)
        return RAW_PROVIDER_ERROR

    monkeypatch.setattr(contract_gate, "_async_repair_cycle_contract_answer", fake_repair)

    verdict = await contract_gate.async_prepare_cycle_run_visible_finalization(session, 44)

    assert len(repair_calls) == 1
    assert verdict["settlement_status"] == "mission_contract_failed"
    assert verdict["provider_error"] == "server_error"
    assert verdict["side_effects_succeeded"] is True
    assert verdict["domain_side_effects_succeeded"] is True
    degraded_answer = session._artifacts[-1].text
    assert "upstream model provider failed with server_error" in degraded_answer
    assert "create_domain_record" in degraded_answer
    assert "remain applied" in degraded_answer
    assert all("help.openai.com" not in str(artifact.text or "") for artifact in session._artifacts)


def test_finalize_cycle_run_from_run_ignores_non_cycle_run(monkeypatch):
    agent_run = AgentRun()
    agent_run.metadata_ = {"source": "user"}

    cycle_run = CycleRun()
    cycle_run.status = "running"
    cycle = Cycle()

    monkeypatch.setattr(
        service,
        "UnitOfWork",
        _AsyncUnitOfWorkFactory([
            _AsyncFakeSession(agent_run=agent_run, run=cycle_run, cycle=cycle)
        ]),
    )

    service.finalize_cycle_run_from_run(44, status="failed", error="boom")

    assert cycle_run.status == "running"
    assert cycle.last_status is None


def test_cycle_route_scope_uses_workspace_when_available():
    conditions = cycle_access.cycle_scope_conditions(
        cycle_access.CycleActor.from_user_payload(
            {"id": "user-2", "org_id": "org-1", "principal_type": "human"}
        )
    )

    compiled = str(
        select(Cycle.id).where(*conditions).compile(compile_kwargs={"literal_binds": True})
    )

    assert "cycles.org_id =" in compiled
    assert "cycles.org_id IS NULL" in compiled
    assert "users.org_id =" in compiled


def test_cycle_target_idea_scope_uses_workspace_when_available():
    conditions = cycle_access.target_idea_scope_conditions(
        "idea-1",
        cycle_access.CycleActor.from_user_payload(
            {"id": "user-2", "org_id": "org-1", "principal_type": "human"}
        ),
    )

    compiled = str(
        select(Idea.id).where(*conditions).compile(compile_kwargs={"literal_binds": True})
    )

    assert "ideas.id =" in compiled
    assert "ideas.org_id =" in compiled
    assert "ideas.org_id IS NULL" in compiled
    assert "users.org_id =" in compiled


def test_cycle_executor_target_idea_scope_uses_workspace_with_legacy_fallback():
    cycle = Cycle()
    cycle.user_id = "user-1"
    cycle.org_id = "org-1"

    compiled = str(
        select(Idea.id)
        .where(cycle_access.cycle_target_idea_scope_condition(cycle))
        .compile(compile_kwargs={"literal_binds": True})
    )

    assert "ideas.org_id =" in compiled
    assert "ideas.org_id IS NULL" in compiled
    assert "users.org_id =" in compiled


def test_finalize_cycle_run_from_run_skips_terminal_runs(monkeypatch):
    agent_run = AgentRun()
    agent_run.metadata_ = {"source": "cycle", "cycle_run_id": 12}

    cycle_run = CycleRun()
    cycle_run.id = 12
    cycle_run.cycle_id = 5
    cycle_run.status = "completed"

    cycle = Cycle()
    cycle.id = 5
    cycle.last_status = None

    monkeypatch.setattr(
        service,
        "UnitOfWork",
        _AsyncUnitOfWorkFactory([
            _AsyncFakeSession(agent_run=agent_run, run=cycle_run, cycle=cycle)
        ]),
    )

    service.finalize_cycle_run_from_run(44, status="failed", error="boom")

    assert cycle_run.status == "completed"
    assert cycle.last_status is None
