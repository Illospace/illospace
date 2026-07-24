"""Acceptance coverage for the shared Cycle exception-ping gate (#454)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib
import json
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateTable

from brain.platform.db.models.cycle import Cycle, CycleRun
from brain.platform.db.models.idea import Idea
from brain.systems.cycles.common import (
    OFF_SLOT_MATERIAL_ALERT_RUN_KIND,
    SCHEDULED_DIGEST_RUN_KIND,
)


_USER_ID = "0d346c77-1eb4-4291-a6cd-b4a9df0070e6"
_ORG_ID = "f253cace-f4d1-4366-af3f-18b6c24dbead"
_CHANNEL_ID = "C4SOFTWARE"


class _SlackClient:
    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []

    async def post_message(self, **kwargs):
        self.posts.append(kwargs)
        return {
            "ok": True,
            "channel": kwargs["channel"],
            "ts": f"1784900000.{len(self.posts):06d}",
        }


def test_exception_ping_migration_adds_shared_cycle_state_once(monkeypatch):
    migration = importlib.import_module(
        "brain.platform.db.alembic.versions.0037_exception_ping_state"
    )
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(
            sa.text("CREATE TABLE cycles (id INTEGER PRIMARY KEY)")
        )
        operations = Operations(MigrationContext.configure(connection))
        monkeypatch.setattr(migration, "op", operations)

        migration.upgrade()
        migration.upgrade()

        columns = {
            column["name"]: column
            for column in sa.inspect(connection).get_columns("cycles")
        }
        assert "exception_ping_state" in columns
        connection.execute(sa.text("INSERT INTO cycles (id) VALUES (1)"))
        stored = connection.execute(
            sa.text("SELECT exception_ping_state FROM cycles")
        ).scalar_one()
        assert json.loads(stored) == {}


@pytest.fixture
async def exception_ping_store(tmp_path, monkeypatch, sqlite_postgres_ddl_patch):
    database_path = Path(tmp_path) / "exception-pings.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.execute(CreateTable(Cycle.__table__))
        await connection.execute(CreateTable(CycleRun.__table__))

    class TestUnitOfWork:
        async def __aenter__(self):
            self.session = sessions()
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            if exc_type:
                await self.session.rollback()
            else:
                await self.session.commit()
            await self.session.close()

    monkeypatch.setattr(
        "brain.systems.cycles.exception_ping.UnitOfWork",
        TestUnitOfWork,
    )
    try:
        yield sessions
    finally:
        await engine.dispose()


async def _seed_cycle_runs(
    sessions,
    *run_kinds: str,
    cycle_id: int = 2,
) -> list[int]:
    now = datetime.now(timezone.utc)
    async with sessions() as session:
        session.add(
            Cycle(
                id=cycle_id,
                user_id=_USER_ID,
                org_id=_ORG_ID,
                name="Uwear Ticket Coordinator Check-ins",
                prompt="Coordinate engineering work.",
                schedule_expr="0 8,13,18 * * *",
                timezone="America/Toronto",
                enabled=True,
                exception_ping_state={},
            )
        )
        runs = []
        for index, run_kind in enumerate(run_kinds, start=1):
            run = CycleRun(
                id=cycle_id * 100 + index,
                cycle_id=cycle_id,
                scheduled_for=now + timedelta(seconds=index),
                status="running",
                prompt_snapshot="Coordinate engineering work.",
                context_snapshot={
                    "launch_context": {
                        "source": "test",
                        "run_kind": run_kind,
                    }
                },
            )
            session.add(run)
            runs.append(run)
        await session.commit()
        return [int(run.id) for run in runs]


async def _post_exception_ping(
    monkeypatch,
    client: _SlackClient,
    *,
    run_id: int,
    run_kind: str,
    exception_ping: dict[str, Any],
    body: str | None = None,
) -> dict[str, Any]:
    from brain.systems.runs.execution_context import bind_agent_context
    from brain.systems.runs.tool_catalog.handlers.slack import _handle_post_slack_reply

    async def slack_client() -> _SlackClient:
        return client

    monkeypatch.setattr(
        "brain.systems.runs.tool_catalog.handlers.slack._slack_client_from_runtime",
        slack_client,
    )
    target = str(exception_ping["target_teammate_id"])
    with bind_agent_context(
        {
            "org_id": _ORG_ID,
            "run_id": run_id + 10_000,
            "execution_metadata": {
                "source": "cycle",
                "cycle_id": 2,
                "cycle_run_id": run_id,
                "launch_envelope": {
                    "launch_context": {"run_kind": run_kind},
                },
            },
            "slack_trigger": {
                "response_target": {
                    "channel_id": _CHANNEL_ID,
                    "thread_ts": None,
                    "visibility": "public",
                },
            },
        }
    ):
        result = await _handle_post_slack_reply(
            body=body or f"<@{target}> material engineering update.",
            exception_ping=exception_ping,
        )
    return json.loads(result)


@pytest.mark.asyncio
async def test_scheduled_and_off_slot_paths_share_one_person_throttle(
    monkeypatch,
    exception_ping_store,
):
    from brain.systems.cycles.exception_ping import EXCEPTION_PING_LEDGER_KEY

    run_ids = await _seed_cycle_runs(
        exception_ping_store,
        SCHEDULED_DIGEST_RUN_KIND,
        OFF_SLOT_MATERIAL_ALERT_RUN_KIND,
    )
    client = _SlackClient()
    first = await _post_exception_ping(
        monkeypatch,
        client,
        run_id=run_ids[0],
        run_kind=SCHEDULED_DIGEST_RUN_KIND,
        exception_ping={
            "target_teammate_id": "UAXEL",
            "item_ref": "github:Illospace/illospace:pr:1254",
            "change_types": ["ownership_change"],
            "facts": {
                "previous_owner_id": "UREDA",
                "current_owner_id": "UAXEL",
            },
        },
    )
    second = await _post_exception_ping(
        monkeypatch,
        client,
        run_id=run_ids[1],
        run_kind=OFF_SLOT_MATERIAL_ALERT_RUN_KIND,
        exception_ping={
            "target_teammate_id": "UAXEL",
            "item_ref": "github:Illospace/illospace:pr:1255",
            "change_types": ["active_set_enter"],
            "facts": {"active_before": False, "active_after": True},
        },
    )

    assert first["posted"] is True
    assert second["posted"] is False
    assert second["suppressed"] is True
    assert second["reason"] == "person_throttled_within_60_minutes"
    assert len(client.posts) == 1

    async with exception_ping_store() as session:
        cycle = await session.get(Cycle, 2)
        later_run = await session.get(CycleRun, run_ids[1])
    person_state = cycle.exception_ping_state["last_ping_by_teammate"]["UAXEL"]
    assert person_state["cycle_run_id"] == run_ids[0]
    assert person_state["run_kind"] == SCHEDULED_DIGEST_RUN_KIND
    decisions = later_run.context_snapshot[EXCEPTION_PING_LEDGER_KEY]["decisions"]
    assert decisions[-1]["decision"] == "suppressed"
    assert decisions[-1]["reason"] == "person_throttled_within_60_minutes"
    assert decisions[-1]["previous_cycle_run_id"] == run_ids[0]


@pytest.mark.asyncio
async def test_recent_owner_pr_ci_churn_stays_in_ledger_without_slack(
    monkeypatch,
    exception_ping_store,
):
    from brain.systems.cycles.exception_ping import EXCEPTION_PING_LEDGER_KEY

    [run_id] = await _seed_cycle_runs(
        exception_ping_store,
        OFF_SLOT_MATERIAL_ALERT_RUN_KIND,
    )
    client = _SlackClient()
    result = await _post_exception_ping(
        monkeypatch,
        client,
        run_id=run_id,
        run_kind=OFF_SLOT_MATERIAL_ALERT_RUN_KIND,
        exception_ping={
            "target_teammate_id": "UAXEL",
            "item_ref": "github:Illospace/illospace:pr:1255",
            "change_types": ["ci_status_transition"],
            "facts": {
                "pr_opened_at": (
                    datetime.now(timezone.utc) - timedelta(minutes=30)
                ).isoformat(),
                "pr_opened_by_teammate_id": "UAXEL",
                "ci_status_before": "pending",
                "ci_status_after": "failure",
            },
        },
    )

    assert result["posted"] is False
    assert result["ledger_line"] == "Slack skipped: no material todo-list change"
    assert client.posts == []
    async with exception_ping_store() as session:
        run = await session.get(CycleRun, run_id)
    decision = run.context_snapshot[EXCEPTION_PING_LEDGER_KEY]["decisions"][-1]
    assert decision["item_ref"] == "github:Illospace/illospace:pr:1255"
    assert decision["decision"] == "suppressed"
    assert decision["reason"] == "recent_owner_pr_ci_churn"
    assert decision["ledger_line"] == "Slack skipped: no material todo-list change"


@pytest.mark.asyncio
async def test_auto_filed_alert_issue_already_in_alerts_skips_software_ping(
    monkeypatch,
    exception_ping_store,
):
    from brain.systems.cycles.exception_ping import EXCEPTION_PING_LEDGER_KEY

    [run_id] = await _seed_cycle_runs(
        exception_ping_store,
        OFF_SLOT_MATERIAL_ALERT_RUN_KIND,
    )
    client = _SlackClient()
    result = await _post_exception_ping(
        monkeypatch,
        client,
        run_id=run_id,
        run_kind=OFF_SLOT_MATERIAL_ALERT_RUN_KIND,
        exception_ping={
            "target_teammate_id": "UREDA",
            "item_ref": "github:Illospace/illospace:issue:1260",
            "change_types": ["new_unassigned_high_severity"],
            "facts": {
                "severity": "high",
                "is_unassigned": True,
                "auto_filed_alert_issue": True,
                "posted_to_alerts": True,
            },
        },
    )

    assert result["posted"] is False
    assert result["reason"] == "auto_filed_alert_already_posted"
    assert result["ledger_line"] == "Slack skipped: no material todo-list change"
    assert client.posts == []
    async with exception_ping_store() as session:
        run = await session.get(CycleRun, run_id)
    decision = run.context_snapshot[EXCEPTION_PING_LEDGER_KEY]["decisions"][-1]
    assert decision["item_ref"] == "github:Illospace/illospace:issue:1260"
    assert decision["reason"] == "auto_filed_alert_already_posted"


@pytest.mark.asyncio
async def test_off_slot_cycle_post_without_gate_metadata_fails_closed(
    monkeypatch,
    exception_ping_store,
):
    from brain.systems.cycles.exception_ping import EXCEPTION_PING_LEDGER_KEY
    from brain.systems.runs.execution_context import bind_agent_context
    from brain.systems.runs.tool_catalog.handlers.slack import _handle_post_slack_reply

    [run_id] = await _seed_cycle_runs(
        exception_ping_store,
        OFF_SLOT_MATERIAL_ALERT_RUN_KIND,
    )
    client = _SlackClient()
    monkeypatch.setattr(
        "brain.systems.runs.tool_catalog.handlers.slack._slack_client_from_runtime",
        lambda: client,
    )
    with bind_agent_context(
        {
            "execution_metadata": {
                "source": "cycle",
                "cycle_run_id": run_id,
                "launch_envelope": {
                    "launch_context": {
                        "run_kind": OFF_SLOT_MATERIAL_ALERT_RUN_KIND,
                    }
                },
            },
            "slack_trigger": {
                "response_target": {
                    "channel_id": _CHANNEL_ID,
                    "thread_ts": None,
                    "visibility": "public",
                }
            },
        }
    ):
        result = json.loads(
            await _handle_post_slack_reply(body="<@UAXEL> routine update.")
        )

    assert result["posted"] is False
    assert result["error"] == "exception_ping_metadata_required"
    assert client.posts == []
    async with exception_ping_store() as session:
        run = await session.get(CycleRun, run_id)
    decision = run.context_snapshot[EXCEPTION_PING_LEDGER_KEY]["decisions"][-1]
    assert decision["reason"] == "exception_ping_metadata_required"


_MATERIAL_CASES = (
    (
        "ownership_change",
        {"previous_owner_id": "UREDA", "current_owner_id": "UAXEL"},
    ),
    ("blocker_hit", {"blocker_before": False, "blocker_after": True}),
    ("blocker_clear", {"blocker_before": True, "blocker_after": False}),
    ("active_set_enter", {"active_before": False, "active_after": True}),
    ("active_set_leave", {"active_before": True, "active_after": False}),
    (
        "new_unassigned_high_severity",
        {"severity": "high", "is_unassigned": True},
    ),
    ("chantier_must_surface", {"must_surface": True}),
)


@pytest.mark.asyncio
@pytest.mark.parametrize(("change_type", "facts"), _MATERIAL_CASES)
async def test_genuine_material_changes_still_post(
    monkeypatch,
    exception_ping_store,
    change_type,
    facts,
):
    [run_id] = await _seed_cycle_runs(
        exception_ping_store,
        OFF_SLOT_MATERIAL_ALERT_RUN_KIND,
    )
    client = _SlackClient()
    result = await _post_exception_ping(
        monkeypatch,
        client,
        run_id=run_id,
        run_kind=OFF_SLOT_MATERIAL_ALERT_RUN_KIND,
        exception_ping={
            "target_teammate_id": f"U{change_type.upper()}",
            "item_ref": f"test:{change_type}",
            "change_types": [change_type],
            "facts": facts,
        },
    )

    assert result["posted"] is True
    assert result["matched_change_types"] == [change_type]
    assert len(client.posts) == 1


def test_coordinator_launch_prompt_explains_code_authoritative_gate():
    from brain.systems.cycles.prompts import cycle_run_message

    cycle = Cycle()
    cycle.id = 2
    cycle.name = "Uwear Ticket Coordinator Check-ins"
    cycle.prompt = "Coordinate engineering work."
    cycle.timezone = "America/Toronto"
    cycle.degradation_state = {}
    run = CycleRun()
    run.id = 201
    run.scheduled_for = datetime(2026, 7, 24, 17, 0, tzinfo=timezone.utc)
    run.guidance_snapshot = []
    run.output_targets_snapshot = []
    run.context_snapshot = {
        "launch_context": {
            "source": "test",
            "run_kind": OFF_SLOT_MATERIAL_ALERT_RUN_KIND,
        },
        "degradation_tracking": {},
        "open_ask_stragglers": [],
    }
    idea = Idea()
    idea.id = "33cf042f-3059-411d-bf4b-04c12b114fc9"
    idea.title = "Coordinator"

    prompt = cycle_run_message(idea, cycle, run)

    assert "AUTHORITATIVE EXCEPTION-PING GATE" in prompt
    assert "shared 60-minute throttle per teammate across both run kinds" in prompt
    assert "same-owner PR's CI transition within one hour" in prompt
    assert "auto-filed alert issue" in prompt
    assert "Slack skipped: no material todo-list change" in prompt
