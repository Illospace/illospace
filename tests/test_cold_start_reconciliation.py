"""Acceptance coverage for scheduler cold-start gap reconciliation."""

from __future__ import annotations

import importlib
import re
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
import sqlalchemy as sa
from sqlalchemy import func, select, update
from sqlalchemy.dialects.sqlite.base import SQLiteDDLCompiler, SQLiteTypeCompiler

import brain.app.scheduler.cold_start as cold_start
from brain.platform.db.models.agent_run import AgentRunEventRow, AgentRunRow
from brain.platform.db.models.cycle import Cycle, CycleRun
from brain.platform.db.models.domain import Domain, DomainObjectType, DomainRecord
from brain.platform.db.models.external_agent import ExternalAgentConnectionRow
from brain.platform.db.models.inbound import InboundDecisionReceiptRow, InboundEventRow
from brain.platform.db.models.open_ask import OpenAsk
from brain.platform.db.models.org import Org, User
from brain.platform.db.models.scheduler import (
    SchedulerColdStartReconciliation,
    SchedulerLivenessCheckpoint,
)
from brain.systems.cycles.service import async_advance_cycle_schedule_past_gap
from brain.systems.inbound.service import submit_inbound_envelope
from brain.systems.slack.client import SlackApiError
from brain.systems.slack.connector import backfill_monitored_slack_history
from brain.systems.slack.ingress import normalize_slack_socket_event
from tests.scheduler_test_support import make_scheduler_test_session


ORG_ID = "11111111-1111-4111-8111-111111111111"
USER_ID = "22222222-2222-4222-8222-222222222222"
CONNECTION_ID = "33333333-3333-4333-8333-333333333333"


def test_cold_start_receipt_migration_round_trips(monkeypatch):
    migration = importlib.import_module(
        "brain.platform.db.alembic.versions.0049_scheduler_cold_start_reconciliation"
    )
    engine = sa.create_engine("sqlite://")

    with engine.begin() as connection:
        operations = Operations(MigrationContext.configure(connection))
        monkeypatch.setattr(migration, "op", operations)

        migration.upgrade()
        migration.upgrade()

        columns = {
            column["name"]
            for column in sa.inspect(connection).get_columns(
                "scheduler_cold_start_reconciliations"
            )
        }
        assert columns == {
            "id",
            "gap_started_at",
            "reconciled_through",
            "status",
            "lane_results",
            "notice_state",
            "notice_marker",
            "claimed_at",
            "claim_generation",
            "completed_at",
            "notice_posted_at",
            "notice_message_ts",
            "notice_client_msg_id",
            "last_error",
            "created_at",
        }
        checkpoint_columns = {
            column["name"]
            for column in sa.inspect(connection).get_columns(
                "scheduler_liveness_checkpoints"
            )
        }
        assert checkpoint_columns == {
            "checkpoint_key",
            "last_heartbeat_at",
            "last_reconciled_at",
            "created_at",
        }

        migration.downgrade()
        migration.downgrade()
        tables = sa.inspect(connection).get_table_names()
        assert "scheduler_cold_start_reconciliations" not in tables
        assert "scheduler_liveness_checkpoints" not in tables


def _patch_sqlite_for_models() -> None:
    if not hasattr(SQLiteTypeCompiler, "visit_JSONB"):
        SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "TEXT"
    SQLiteTypeCompiler.visit_UUID = lambda self, type_, **kw: "TEXT"
    SQLiteTypeCompiler.visit_BIGINT = lambda self, type_, **kw: "INTEGER"

    original = SQLiteDDLCompiler.get_column_default_string
    if getattr(original, "_cold_start_patch", False):
        return

    def patched(self, column, **kw):
        result = original(self, column, **kw)
        if result:
            result = re.sub(r"::jsonb", "", result)
            result = result.replace("NOW()", "CURRENT_TIMESTAMP")
            result = result.replace("TRUE", "1").replace("FALSE", "0")
        return result

    patched._cold_start_patch = True
    SQLiteDDLCompiler.get_column_default_string = patched


@pytest.fixture
async def receipt_session(async_sqlite_session_factory):
    _patch_sqlite_for_models()
    return await async_sqlite_session_factory(
        [
            SchedulerColdStartReconciliation.__table__,
            SchedulerLivenessCheckpoint.__table__,
        ]
    )


@pytest.fixture
async def workload_session(async_sqlite_session_factory):
    return await make_scheduler_test_session(async_sqlite_session_factory)


class _NoticeClient:
    def __init__(self):
        self.posts: list[dict] = []

    async def conversations_list(self, **_kwargs):
        return {
            "channels": [{"id": "C_SOFTWARE", "name": "4_software"}],
            "response_metadata": {"next_cursor": ""},
        }

    async def conversation_history(self, **_kwargs):
        return {"messages": [], "response_metadata": {"next_cursor": ""}}

    async def post_message(self, **kwargs):
        self.posts.append(kwargs)
        return {"ok": True, "ts": "1785261600.000001"}


class _AmbiguousNoticeClient(_NoticeClient):
    def __init__(self):
        super().__init__()
        self.messages: list[dict] = []
        self.attempts = 0

    async def conversation_history(self, **_kwargs):
        return {
            "messages": list(self.messages),
            "response_metadata": {"next_cursor": ""},
        }

    async def post_message(self, **kwargs):
        self.attempts += 1
        self.messages.append(
            {
                "text": kwargs["text"],
                "ts": "1785261600.000001",
            }
        )
        raise TimeoutError("response lost after Slack accepted the message")


class _LaggingHistoryNoticeClient(_NoticeClient):
    def __init__(self):
        super().__init__()
        self.attempts = 0
        self.deliveries: dict[str, dict] = {}

    async def post_message(self, **kwargs):
        self.attempts += 1
        client_msg_id = kwargs["client_msg_id"]
        delivery = self.deliveries.get(client_msg_id)
        if delivery is None:
            delivery = {
                "text": kwargs["text"],
                "ts": "1785261600.000001",
            }
            self.deliveries[client_msg_id] = delivery
            raise TimeoutError("response lost after Slack accepted the message")
        return {"ok": True, "ts": delivery["ts"]}


@pytest.mark.asyncio
async def test_liveness_checkpoint_long_gap_runs_without_workload_completion(
    receipt_session,
    monkeypatch,
):
    now = datetime(2026, 7, 27, 14, 0, tzinfo=timezone.utc)
    gap_start = now - timedelta(hours=2, minutes=5)
    calls: list[str] = []
    notice = _NoticeClient()

    async def slack_lane(*_args, **_kwargs):
        calls.append("slack")
        return {
            "ingested": 2,
            "deduplicated": 1,
            "acked": 2,
            "errors": [],
        }

    async def cycle_lane(*_args, **_kwargs):
        calls.append("cycles")
        return {
            "missed_slots": [
                {
                    "cycle_id": 7,
                    "cycle_name": "Uwear digest",
                    "scheduled_for": "2026-07-27T12:00:00+00:00",
                    "timezone": "America/Toronto",
                }
            ],
            "missed_slot_count": 1,
            "errors": [],
        }

    async def tracker_lane(*_args, **_kwargs):
        calls.append("tracker")
        return {
            "orgs": 1,
            "summaries": {
                ORG_ID: {
                    "production_gate_reconciliation": {
                        "updated": 1,
                        "flagged": 1,
                        "errors": [],
                    }
                }
            },
        }

    await cold_start.record_scheduler_liveness_checkpoint(
        receipt_session,
        now=gap_start,
    )
    monkeypatch.setattr(cold_start, "backfill_monitored_slack_history", slack_lane)
    monkeypatch.setattr(cold_start, "async_advance_cycle_schedule_past_gap", cycle_lane)
    monkeypatch.setattr(cold_start, "run_cold_start_tracker_maintenance", tracker_lane)

    first = await cold_start.reconcile_cold_start_gap(
        receipt_session,
        now=now,
        notice_client=notice,
    )
    second = await cold_start.reconcile_cold_start_gap(
        receipt_session,
        now=now + timedelta(minutes=1),
        notice_client=notice,
    )

    assert first["status"] == "completed"
    assert first["idempotent_replay"] is False
    assert second["triggered"] is False
    assert second["reason"] == "below_threshold"
    assert calls == ["slack", "cycles", "tracker"]
    assert len(notice.posts) == 1
    posted = notice.posts[0]
    assert posted["channel"] == "C_SOFTWARE"
    assert "2h 5m" in posted["text"]
    assert "Uwear digest" in posted["text"]
    assert "not replayed" in posted["text"]
    assert len(posted["text"]) < 4000
    assert (
        await receipt_session.scalar(
            select(func.count()).select_from(SchedulerColdStartReconciliation)
        )
        == 1
    )


@pytest.mark.asyncio
async def test_later_checkpoint_window_gets_its_own_receipt(
    receipt_session,
    monkeypatch,
):
    first_start = datetime(2026, 7, 27, 10, 0, tzinfo=timezone.utc)
    first_end = first_start + timedelta(hours=2)
    second_end = first_end + timedelta(hours=3)
    windows: list[tuple[datetime, datetime]] = []
    notice = _NoticeClient()

    async def slack_lane(*_args, gap_start, now, **_kwargs):
        windows.append((gap_start, now))
        return {"errors": []}

    await cold_start.record_scheduler_liveness_checkpoint(
        receipt_session,
        now=first_start,
    )
    monkeypatch.setattr(cold_start, "backfill_monitored_slack_history", slack_lane)
    monkeypatch.setattr(
        cold_start,
        "async_advance_cycle_schedule_past_gap",
        AsyncMock(return_value={"missed_slots": [], "errors": []}),
    )
    monkeypatch.setattr(
        cold_start,
        "run_cold_start_tracker_maintenance",
        AsyncMock(return_value={"orgs": 0, "summaries": {}}),
    )

    first = await cold_start.reconcile_cold_start_gap(
        receipt_session,
        now=first_end,
        notice_client=notice,
    )
    second = await cold_start.reconcile_cold_start_gap(
        receipt_session,
        now=second_end,
        notice_client=notice,
    )

    assert windows == [(first_start, first_end), (first_end, second_end)]
    assert first["receipt_id"] != second["receipt_id"]
    assert len(notice.posts) == 2
    assert (
        await receipt_session.scalar(
            select(func.count()).select_from(SchedulerColdStartReconciliation)
        )
        == 2
    )


@pytest.mark.asyncio
async def test_stale_running_receipt_reclaims_its_exact_window(
    receipt_session,
    monkeypatch,
):
    gap_start = datetime(2026, 7, 27, 10, 0, tzinfo=timezone.utc)
    gap_end = gap_start + timedelta(hours=2)
    retry_at = gap_end + timedelta(minutes=31)
    receipt, generation = await cold_start._claim_receipt(
        receipt_session,
        gap_start=gap_start,
        gap_end=gap_end,
        claimed_at=gap_end,
    )
    assert generation == 1
    await cold_start.record_scheduler_liveness_checkpoint(
        receipt_session,
        now=gap_start,
    )
    slack = AsyncMock(return_value={"errors": []})
    monkeypatch.setattr(cold_start, "backfill_monitored_slack_history", slack)
    monkeypatch.setattr(
        cold_start,
        "async_advance_cycle_schedule_past_gap",
        AsyncMock(return_value={"missed_slots": [], "errors": []}),
    )
    monkeypatch.setattr(
        cold_start,
        "run_cold_start_tracker_maintenance",
        AsyncMock(return_value={"orgs": 0, "summaries": {}}),
    )

    result = await cold_start.reconcile_cold_start_gap(
        receipt_session,
        now=retry_at,
        notice_client=_NoticeClient(),
    )

    assert result["receipt_id"] == receipt.id
    assert result["gap_end"] == gap_end.isoformat()
    assert result["claim_generation"] == 2
    slack.assert_awaited_once_with(
        receipt_session,
        gap_start=gap_start,
        now=gap_end,
    )
    assert (
        await receipt_session.scalar(
            select(func.count()).select_from(SchedulerColdStartReconciliation)
        )
        == 1
    )


@pytest.mark.asyncio
async def test_short_gap_keeps_startup_behavior_unchanged(
    receipt_session,
    monkeypatch,
):
    now = datetime(2026, 7, 27, 14, 0, tzinfo=timezone.utc)
    notice = _NoticeClient()
    lane = AsyncMock(side_effect=AssertionError("cold-start lane must not run"))
    await cold_start.record_scheduler_liveness_checkpoint(
        receipt_session,
        now=now - timedelta(minutes=5),
    )
    monkeypatch.setattr(cold_start, "backfill_monitored_slack_history", lane)
    monkeypatch.setattr(cold_start, "async_advance_cycle_schedule_past_gap", lane)
    monkeypatch.setattr(cold_start, "run_cold_start_tracker_maintenance", lane)

    result = await cold_start.reconcile_cold_start_gap(
        receipt_session,
        now=now,
        notice_client=notice,
    )

    assert result["triggered"] is False
    assert result["reason"] == "below_threshold"
    assert lane.await_count == 0
    assert notice.posts == []
    assert (
        await receipt_session.scalar(
            select(func.count()).select_from(SchedulerColdStartReconciliation)
        )
        == 0
    )


@pytest.mark.asyncio
async def test_failed_slack_lane_does_not_block_tracker_cycles_or_notice(
    receipt_session,
    monkeypatch,
):
    now = datetime(2026, 7, 27, 14, 0, tzinfo=timezone.utc)
    gap_start = now - timedelta(hours=3)
    calls: list[str] = []
    notice = _NoticeClient()

    await cold_start.record_scheduler_liveness_checkpoint(
        receipt_session,
        now=gap_start,
    )

    async def failed_slack(*_args, **_kwargs):
        calls.append("slack")
        raise RuntimeError("Slack history unavailable")

    async def healthy_cycles(*_args, **_kwargs):
        calls.append("cycles")
        return {"missed_slots": [], "errors": []}

    async def healthy_tracker(*_args, **_kwargs):
        calls.append("tracker")
        return {"orgs": 0, "summaries": {}}

    monkeypatch.setattr(cold_start, "backfill_monitored_slack_history", failed_slack)
    monkeypatch.setattr(
        cold_start,
        "async_advance_cycle_schedule_past_gap",
        healthy_cycles,
    )
    monkeypatch.setattr(cold_start, "run_cold_start_tracker_maintenance", healthy_tracker)

    result = await cold_start.reconcile_cold_start_gap(
        receipt_session,
        now=now,
        notice_client=notice,
    )

    assert result["status"] == "degraded"
    assert calls == ["slack", "cycles", "tracker"]
    assert len(notice.posts) == 1
    assert "Slack backfill: FAILED/DEGRADED" in notice.posts[0]["text"]

    async def recovered_slack(*_args, **_kwargs):
        calls.append("slack")
        return {"errors": []}

    monkeypatch.setattr(cold_start, "backfill_monitored_slack_history", recovered_slack)
    retry = await cold_start.reconcile_cold_start_gap(
        receipt_session,
        now=now + timedelta(minutes=1),
        notice_client=notice,
    )

    assert retry["status"] == "completed"
    assert retry["claim_generation"] == 2
    assert calls == ["slack", "cycles", "tracker", "slack"]
    assert len(notice.posts) == 1


@pytest.mark.asyncio
async def test_permanent_lane_failure_exhausts_retry_budget_and_stays_terminal(
    receipt_session,
    monkeypatch,
):
    gap_start = datetime(2026, 7, 27, 10, 0, tzinfo=timezone.utc)
    gap_end = gap_start + timedelta(hours=2)
    notice = _NoticeClient()
    slack = AsyncMock(side_effect=RuntimeError("Slack history unavailable"))
    await cold_start.record_scheduler_liveness_checkpoint(
        receipt_session,
        now=gap_start,
    )
    monkeypatch.setattr(cold_start, "backfill_monitored_slack_history", slack)
    monkeypatch.setattr(
        cold_start,
        "async_advance_cycle_schedule_past_gap",
        AsyncMock(return_value={"missed_slots": [], "errors": []}),
    )
    monkeypatch.setattr(
        cold_start,
        "run_cold_start_tracker_maintenance",
        AsyncMock(return_value={"orgs": 0, "summaries": {}}),
    )

    attempts = [
        await cold_start.reconcile_cold_start_gap(
            receipt_session,
            now=gap_end + timedelta(minutes=offset),
            notice_client=notice,
        )
        for offset in range(4)
    ]
    receipt = await receipt_session.scalar(
        select(SchedulerColdStartReconciliation)
    )
    checkpoint = await receipt_session.get(
        SchedulerLivenessCheckpoint,
        cold_start.LIVENESS_CHECKPOINT_KEY,
        populate_existing=True,
    )

    assert [result["status"] for result in attempts[:3]] == [
        "degraded",
        "degraded",
        "failed",
    ]
    assert attempts[2]["retry"] == {
        "attempts": 3,
        "max_attempts": 3,
        "exhausted": True,
    }
    assert attempts[3]["triggered"] is False
    assert slack.await_count == cold_start.MAX_RECONCILIATION_ATTEMPTS
    assert receipt is not None
    assert receipt.status == "failed"
    assert "retry budget exhausted" in (receipt.last_error or "")
    assert checkpoint is not None
    assert checkpoint.last_reconciled_at.replace(tzinfo=timezone.utc) == gap_start


@pytest.mark.asyncio
async def test_old_degraded_receipt_captures_and_preserves_new_outage_window(
    receipt_session,
    monkeypatch,
):
    first_start = datetime(2026, 7, 27, 10, 0, tzinfo=timezone.utc)
    first_end = first_start + timedelta(hours=2)
    second_start = first_end + timedelta(hours=2)
    second_end = second_start + timedelta(hours=2)
    windows: list[tuple[datetime, datetime]] = []
    fail_first = True

    async def slack_lane(*_args, gap_start, now, **_kwargs):
        nonlocal fail_first
        windows.append((gap_start, now))
        if fail_first:
            fail_first = False
            raise RuntimeError("temporary Slack outage")
        return {"errors": []}

    await cold_start.record_scheduler_liveness_checkpoint(
        receipt_session,
        now=first_start,
    )
    monkeypatch.setattr(cold_start, "backfill_monitored_slack_history", slack_lane)
    monkeypatch.setattr(
        cold_start,
        "async_advance_cycle_schedule_past_gap",
        AsyncMock(return_value={"missed_slots": [], "errors": []}),
    )
    monkeypatch.setattr(
        cold_start,
        "run_cold_start_tracker_maintenance",
        AsyncMock(return_value={"orgs": 0, "summaries": {}}),
    )
    notice = _NoticeClient()

    first = await cold_start.reconcile_cold_start_gap(
        receipt_session,
        now=first_end,
        notice_client=notice,
    )
    assert first["status"] == "degraded"
    await cold_start.record_scheduler_liveness_checkpoint(
        receipt_session,
        now=second_start,
    )

    retried = await cold_start.reconcile_cold_start_gap(
        receipt_session,
        now=second_end,
        notice_client=notice,
    )
    pending = await receipt_session.get(
        SchedulerColdStartReconciliation,
        retried["captured_receipt_id"],
        populate_existing=True,
    )

    assert retried["status"] == "completed"
    assert pending is not None
    assert pending.status == "pending"
    assert pending.gap_started_at.replace(tzinfo=timezone.utc) == second_start
    assert pending.reconciled_through.replace(tzinfo=timezone.utc) == second_end
    assert retried["reconciliation_checkpoint_at"] == first_end.isoformat()

    reconciled = await cold_start.reconcile_cold_start_gap(
        receipt_session,
        now=second_end + timedelta(minutes=1),
        notice_client=notice,
    )

    assert reconciled["receipt_id"] == pending.id
    assert reconciled["status"] == "completed"
    assert reconciled["gap_start"] == second_start.isoformat()
    assert reconciled["gap_end"] == second_end.isoformat()
    assert windows == [
        (first_start, first_end),
        (first_start, first_end),
        (second_start, second_end),
    ]


@pytest.mark.asyncio
async def test_missing_checkpoint_falls_back_to_successful_workload(
    workload_session,
    monkeypatch,
):
    now = datetime(2026, 7, 27, 14, 0, tzinfo=timezone.utc)
    gap_start = now - timedelta(hours=2)
    workload_session.add(
        AgentRunRow(
            thread_id="cold-start-fallback",
            profile="default",
            recipe="test",
            status="completed",
            input_message="Historical completed workload",
            target_ref={},
            workspace_ref={},
            model_policy={},
            metadata_={},
            completed_at=gap_start,
        )
    )
    await workload_session.commit()
    slack = AsyncMock(return_value={"errors": []})
    monkeypatch.setattr(cold_start, "backfill_monitored_slack_history", slack)
    monkeypatch.setattr(
        cold_start,
        "async_advance_cycle_schedule_past_gap",
        AsyncMock(return_value={"missed_slots": [], "errors": []}),
    )
    monkeypatch.setattr(
        cold_start,
        "run_cold_start_tracker_maintenance",
        AsyncMock(return_value={"orgs": 0, "summaries": {}}),
    )

    result = await cold_start.reconcile_cold_start_gap(
        workload_session,
        now=now,
        notice_client=_NoticeClient(),
    )

    assert result["triggered"] is True
    assert result["fallback_to_workload"] is True
    assert result["gap_start"] == gap_start.isoformat()
    assert result["status"] == "completed"
    slack.assert_awaited_once_with(
        workload_session,
        gap_start=gap_start,
        now=now,
    )


@pytest.mark.asyncio
async def test_ambiguous_notice_send_is_found_in_history_before_retry(
    receipt_session,
    monkeypatch,
):
    now = datetime(2026, 7, 27, 14, 0, tzinfo=timezone.utc)
    gap_start = now - timedelta(hours=2)
    notice = _AmbiguousNoticeClient()
    await cold_start.record_scheduler_liveness_checkpoint(
        receipt_session,
        now=gap_start,
    )
    monkeypatch.setattr(
        cold_start,
        "backfill_monitored_slack_history",
        AsyncMock(return_value={"errors": []}),
    )
    monkeypatch.setattr(
        cold_start,
        "async_advance_cycle_schedule_past_gap",
        AsyncMock(return_value={"missed_slots": [], "errors": []}),
    )
    monkeypatch.setattr(
        cold_start,
        "run_cold_start_tracker_maintenance",
        AsyncMock(return_value={"orgs": 0, "summaries": {}}),
    )

    first = await cold_start.reconcile_cold_start_gap(
        receipt_session,
        now=now,
        notice_client=notice,
    )
    second = await cold_start.reconcile_cold_start_gap(
        receipt_session,
        now=now + timedelta(minutes=1),
        notice_client=notice,
    )

    assert first["notice"]["state"] == "posting"
    assert first["notice"]["last_error"] == (
        "response lost after Slack accepted the message"
    )
    assert second["notice"]["state"] == "posted"
    assert notice.attempts == 1
    assert len(notice.messages) == 1


@pytest.mark.asyncio
async def test_notice_reuses_stable_client_message_id_when_history_lags(
    receipt_session,
    monkeypatch,
):
    now = datetime(2026, 7, 27, 14, 0, tzinfo=timezone.utc)
    notice = _LaggingHistoryNoticeClient()
    await cold_start.record_scheduler_liveness_checkpoint(
        receipt_session,
        now=now - timedelta(hours=2),
    )
    monkeypatch.setattr(
        cold_start,
        "backfill_monitored_slack_history",
        AsyncMock(return_value={"errors": []}),
    )
    monkeypatch.setattr(
        cold_start,
        "async_advance_cycle_schedule_past_gap",
        AsyncMock(return_value={"missed_slots": [], "errors": []}),
    )
    monkeypatch.setattr(
        cold_start,
        "run_cold_start_tracker_maintenance",
        AsyncMock(return_value={"orgs": 0, "summaries": {}}),
    )

    first = await cold_start.reconcile_cold_start_gap(
        receipt_session,
        now=now,
        notice_client=notice,
    )
    second = await cold_start.reconcile_cold_start_gap(
        receipt_session,
        now=now + timedelta(minutes=1),
        notice_client=notice,
    )

    assert first["notice"]["state"] == "posting"
    assert second["notice"]["state"] == "posted"
    assert notice.attempts == 2
    assert len(notice.deliveries) == 1


@pytest.mark.asyncio
async def test_claim_heartbeat_blocks_reclaim_and_fence_blocks_stale_sender(
    receipt_session,
):
    gap_start = datetime(2026, 7, 27, 10, 0, tzinfo=timezone.utc)
    gap_end = gap_start + timedelta(hours=2)
    receipt, first_generation = await cold_start._claim_receipt(
        receipt_session,
        gap_start=gap_start,
        gap_end=gap_end,
        claimed_at=gap_end,
    )
    assert first_generation == 1
    assert await cold_start._heartbeat_claim(
        receipt_session,
        receipt_id=receipt.id,
        claim_generation=first_generation,
        now=gap_end + timedelta(minutes=20),
    )

    active, active_generation = await cold_start._claim_receipt(
        receipt_session,
        gap_start=gap_start,
        gap_end=gap_end,
        claimed_at=gap_end + timedelta(minutes=31),
    )
    assert active_generation is None
    assert active.claim_generation == first_generation

    superseding, second_generation = await cold_start._claim_receipt(
        receipt_session,
        gap_start=gap_start,
        gap_end=gap_end,
        claimed_at=gap_end + timedelta(minutes=51),
    )
    assert second_generation == 2
    assert superseding.claim_generation == 2

    notice = _NoticeClient()
    with pytest.raises(cold_start.ClaimSuperseded):
        await cold_start._deliver_notice(
            receipt_session,
            receipt,
            claim_generation=first_generation,
            now=gap_end + timedelta(minutes=51),
            client=notice,
        )
    assert notice.posts == []


@pytest.mark.asyncio
async def test_notice_claim_is_revalidated_after_posting_cas_before_slack_send(
    receipt_session,
    monkeypatch,
):
    gap_start = datetime(2026, 7, 27, 10, 0, tzinfo=timezone.utc)
    gap_end = gap_start + timedelta(hours=2)
    receipt, generation = await cold_start._claim_receipt(
        receipt_session,
        gap_start=gap_start,
        gap_end=gap_end,
        claimed_at=gap_end,
    )
    assert generation == 1
    original_heartbeat = cold_start._heartbeat_claim

    async def supersede_before_revalidation(
        heartbeat_session,
        *,
        receipt_id,
        claim_generation,
        now=None,
    ):
        del now
        await heartbeat_session.execute(
            update(SchedulerColdStartReconciliation)
            .where(
                SchedulerColdStartReconciliation.id == receipt_id,
                SchedulerColdStartReconciliation.claim_generation
                == claim_generation,
            )
            .values(
                claim_generation=claim_generation + 1,
                claimed_at=gap_end,
            )
            .execution_options(synchronize_session=False)
        )
        await heartbeat_session.commit()
        return await original_heartbeat(
            heartbeat_session,
            receipt_id=receipt_id,
            claim_generation=claim_generation,
        )

    monkeypatch.setattr(
        cold_start,
        "_heartbeat_claim",
        supersede_before_revalidation,
    )
    notice = _NoticeClient()

    with pytest.raises(cold_start.ClaimSuperseded):
        await cold_start._deliver_notice(
            receipt_session,
            receipt,
            claim_generation=generation,
            now=gap_end,
            client=notice,
        )

    assert notice.posts == []


@pytest.mark.asyncio
async def test_notice_post_commit_supersession_reports_possible_delivery(
    receipt_session,
):
    gap_start = datetime(2026, 7, 27, 10, 0, tzinfo=timezone.utc)
    gap_end = gap_start + timedelta(hours=2)
    receipt, generation = await cold_start._claim_receipt(
        receipt_session,
        gap_start=gap_start,
        gap_end=gap_end,
        claimed_at=gap_end,
    )
    assert generation == 1

    class SupersedingNoticeClient(_NoticeClient):
        async def post_message(self, **kwargs):
            self.posts.append(kwargs)
            await receipt_session.execute(
                update(SchedulerColdStartReconciliation)
                .where(
                    SchedulerColdStartReconciliation.id == receipt.id,
                    SchedulerColdStartReconciliation.claim_generation == generation,
                )
                .values(
                    claim_generation=generation + 1,
                    claimed_at=gap_end,
                )
                .execution_options(synchronize_session=False)
            )
            await receipt_session.commit()
            return {"ok": True, "ts": "1785261600.000001"}

    notice = SupersedingNoticeClient()

    with pytest.raises(
        cold_start.NoticeDeliveryCommitFailed,
        match="may already have delivered",
    ):
        await cold_start._deliver_notice(
            receipt_session,
            receipt,
            claim_generation=generation,
            now=gap_end,
            client=notice,
        )

    assert len(notice.posts) == 1


@pytest.fixture
async def cycle_session(async_sqlite_session_factory):
    _patch_sqlite_for_models()
    return await async_sqlite_session_factory(
        [Cycle.__table__, CycleRun.__table__]
    )


@pytest.mark.asyncio
async def test_cycle_gap_advance_names_slots_without_replaying_missed_digests(
    cycle_session,
):
    cycle = Cycle(
        user_id=USER_ID,
        name="Uwear digest",
        prompt="Publish the digest.",
        schedule_expr="0 8,13,18 * * *",
        timezone="America/Toronto",
        next_run_at=datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc),
        enabled=True,
        retry_policy={},
        degradation_state={},
        exception_ping_state={},
    )
    cycle_session.add(cycle)
    await cycle_session.flush()

    result = await async_advance_cycle_schedule_past_gap(
        cycle_session,
        gap_start=datetime(2026, 7, 25, 19, 44, tzinfo=timezone.utc),
        now=datetime(2026, 7, 27, 14, 0, tzinfo=timezone.utc),
    )
    replay = await async_advance_cycle_schedule_past_gap(
        cycle_session,
        gap_start=datetime(2026, 7, 25, 19, 44, tzinfo=timezone.utc),
        now=datetime(2026, 7, 27, 14, 0, tzinfo=timezone.utc),
    )

    assert result["missed_slot_count"] == 4
    assert [slot["scheduled_for"] for slot in result["missed_slots"]] == [
        "2026-07-26T12:00:00+00:00",
        "2026-07-26T17:00:00+00:00",
        "2026-07-26T22:00:00+00:00",
        "2026-07-27T12:00:00+00:00",
    ]
    assert cycle.next_run_at == datetime(2026, 7, 27, 17, 0, tzinfo=timezone.utc)
    assert replay["missed_slot_count"] == 0
    assert replay["cycles_advanced"] == 0
    assert (
        await cycle_session.scalar(select(func.count()).select_from(CycleRun))
        == 0
    )


@pytest.mark.asyncio
async def test_cycle_gap_advance_leaves_current_minute_for_normal_cadence(
    cycle_session,
):
    current_slot = datetime(2026, 7, 27, 17, 0, tzinfo=timezone.utc)
    cycle = Cycle(
        user_id=USER_ID,
        name="Current digest",
        prompt="Publish the current digest.",
        schedule_expr="0 8,13,18 * * *",
        timezone="America/Toronto",
        next_run_at=current_slot,
        enabled=True,
        retry_policy={},
        degradation_state={},
        exception_ping_state={},
    )
    cycle_session.add(cycle)
    await cycle_session.flush()

    result = await async_advance_cycle_schedule_past_gap(
        cycle_session,
        gap_start=current_slot - timedelta(hours=2),
        now=current_slot + timedelta(seconds=30),
    )

    assert result["missed_slot_count"] == 0
    assert cycle.next_run_at == current_slot


@pytest.fixture
async def slack_session(async_sqlite_session_factory):
    _patch_sqlite_for_models()
    return await async_sqlite_session_factory(
        [
            Org.__table__,
            User.__table__,
            Domain.__table__,
            DomainObjectType.__table__,
            DomainRecord.__table__,
            ExternalAgentConnectionRow.__table__,
            AgentRunRow.__table__,
            AgentRunEventRow.__table__,
            OpenAsk.__table__,
            InboundEventRow.__table__,
            InboundDecisionReceiptRow.__table__,
        ]
    )


async def _seed_slack_connection(session) -> ExternalAgentConnectionRow:
    session.add(Org(id=ORG_ID, name="Test Org", slug="test-org"))
    session.add(
        User(
            id=USER_ID,
            org_id=ORG_ID,
            name="Reda",
            email="reda@example.com",
        )
    )
    connection = ExternalAgentConnectionRow(
        id=CONNECTION_ID,
        org_id=ORG_ID,
        owner_user_id=USER_ID,
        display_name="Slack",
        agent_kind="slack",
        transport="slack_socket_mode",
        status="online",
        remote_agent_id="T789",
        remote_agent_card={},
        capabilities={"slack": {"socket_mode": True}},
        auth_metadata={},
        metadata_={
            "slack": {
                "team_id": "T789",
                "bot_user_id": "BILLO",
                "monitored_channels": ["C_ALERTS"],
            }
        },
    )
    session.add(connection)
    await session.flush()
    return connection


def _history_mention() -> dict:
    return {
        "type": "message",
        "user": "U123",
        "text": "<@BILLO> please triage this",
        "ts": "1785150300.000200",
    }


class _HistoryClient:
    bot_token = "xoxb-test"

    def __init__(self):
        self.reactions: set[tuple[str, str, str]] = set()

    async def conversation_history(self, **_kwargs):
        return {
            "messages": [
                _history_mention(),
                {
                    "type": "message",
                    "subtype": "bot_message",
                    "bot_id": "B_ROLLBAR",
                    "app_id": "A_ROLLBAR",
                    "text": "Rollbar: PROD #2323 100th error: DeadlockDetected",
                    "ts": "1785149400.000100",
                },
            ],
            "response_metadata": {"next_cursor": ""},
        }

    async def add_reaction(self, *, channel, timestamp, name):
        key = (channel, timestamp, name)
        if key in self.reactions:
            raise SlackApiError("already_reacted")
        self.reactions.add(key)
        return {"ok": True}


@pytest.mark.asyncio
async def test_slack_history_backfill_ingests_alert_and_mention_with_partial_dedup(
    slack_session,
    monkeypatch,
):
    import brain.systems.slack.connector as connector

    connection = await _seed_slack_connection(slack_session)
    mention_envelope = normalize_slack_socket_event(
        {
            "payload": {
                "team_id": "T789",
                "event": {
                    **_history_mention(),
                    "channel": "C_ALERTS",
                    "channel_type": "channel",
                },
            }
        },
        bot_user_id="BILLO",
        monitored_channels={"C_ALERTS"},
    )
    assert mention_envelope is not None
    await submit_inbound_envelope(
        slack_session,
        connection=connection,
        envelope=mention_envelope,
        ingress_context={"transport": "slack_socket_mode"},
    )

    monkeypatch.setattr(
        connector,
        "enrich_monitored_intake",
        AsyncMock(return_value=None),
    )
    obligation_replies = AsyncMock(return_value=None)
    monkeypatch.setattr(
        connector,
        "_record_inbound_obligation_reply",
        obligation_replies,
    )
    client = _HistoryClient()

    async def client_factory(_connection):
        return client

    first = await backfill_monitored_slack_history(
        slack_session,
        gap_start=datetime(2026, 7, 27, 0, 0, tzinfo=timezone.utc),
        now=datetime(2026, 7, 28, 0, 0, tzinfo=timezone.utc),
        client_factory=client_factory,
    )
    second = await backfill_monitored_slack_history(
        slack_session,
        gap_start=datetime(2026, 7, 27, 0, 0, tzinfo=timezone.utc),
        now=datetime(2026, 7, 28, 0, 0, tzinfo=timezone.utc),
        client_factory=client_factory,
    )

    assert first["ingested"] == 1
    assert first["deduplicated"] == 1
    assert first["acked"] == 2
    assert second["ingested"] == 0
    assert second["deduplicated"] == 2
    assert obligation_replies.await_count == 4
    assert len(client.reactions) == 2
    assert (
        await slack_session.scalar(select(func.count()).select_from(InboundEventRow))
        == 2
    )
    assert (
        await slack_session.scalar(select(func.count()).select_from(AgentRunRow))
        == 2
    )
