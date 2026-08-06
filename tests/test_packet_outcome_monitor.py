"""Briefing packet flatline assessment and alert-latch tests."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import select

from brain.platform.db.models.cycle import Cycle, CycleFailureGuardLatch
from brain.platform.db.models.launch_handoff import LaunchHandoff
from brain.systems.briefing import packet_outcome_monitor
from brain.systems.cycles import memory as cycle_memory
from brain.systems.cycles.common import (
    OFF_SLOT_MATERIAL_ALERT_RUN_KIND,
    SCHEDULED_DIGEST_RUN_KIND,
)
from brain.systems.failure_guard.cycle_latches import CycleAlertLatchStore


_ORG = "9b6f3f7e-0000-0000-0000-000000000001"
_NOW = datetime(2026, 8, 4, 18, 0, tzinfo=timezone.utc)


def _handoff(
    index: int,
    *,
    created_at: datetime,
    launch_count: int = 0,
    last_launched_at: datetime | None = None,
) -> LaunchHandoff:
    return LaunchHandoff(
        id=str(uuid4()),
        org_id=_ORG,
        source_surface="inbound_triage",
        source_ref={},
        target_tool="codex",
        title=f"Packet {index}",
        instructions="Pick up the packet.",
        acceptance_criteria=[],
        context_parts=[],
        status="open",
        launch_count=launch_count,
        last_launched_at=last_launched_at,
        idempotency_key=f"packet-{index}",
        metadata_={"job_ref": f"idea:{index}"},
        created_at=created_at,
        updated_at=created_at,
    )


async def _monitor(session, cycle, *, cycle_run_id):
    return await packet_outcome_monitor.async_monitor_packet_outcomes(
        session,
        cycle,
        cycle_run_id=cycle_run_id,
        now=_NOW,
        latch_store=CycleAlertLatchStore(
            session=session,
            cycle_id=cycle.id,
        ),
    )


async def test_flatline_threshold_latches_resets_and_rearms(
    async_sqlite_session_factory,
    sqlite_postgres_ddl_patch,
    monkeypatch,
):
    session = await async_sqlite_session_factory(
        (
            Cycle.__table__,
            CycleFailureGuardLatch.__table__,
            LaunchHandoff.__table__,
        )
    )
    cycle = Cycle(
        id=2,
        user_id=str(uuid4()),
        org_id=_ORG,
        name="Renamed scheduled digest",
        prompt="Publish the coordinator digest.",
        schedule_expr="0 8 * * 1",
        timezone="UTC",
        enabled=True,
    )
    current_packets = [
        _handoff(index, created_at=_NOW - timedelta(hours=index + 1))
        for index in range(9)
    ]
    session.add(cycle)
    session.add_all(
        current_packets
        + [
            _handoff(
                99,
                created_at=_NOW - timedelta(days=90),
                launch_count=1,
                last_launched_at=_NOW - timedelta(days=57, hours=3),
            )
        ]
    )
    await session.flush()

    posts: list[dict[str, str]] = []

    class FakeSlackClient:
        async def conversations_list(self, **_kwargs):
            return {"channels": [{"id": "C_ALERTS", "name": "alerts"}]}

        async def post_message(self, *, channel, text):
            posts.append({"channel": channel, "text": text})
            return {"ok": True}

    async def fake_client(*, requested_by, reason):
        assert requested_by == "packet_outcome_monitor"
        assert reason == "Deliver a packet launch flatline alert to the team."
        return FakeSlackClient()

    monkeypatch.setenv("ILLO_CYCLE_FAILURE_ALERT_CHANNEL", "#alerts")
    monkeypatch.setenv("ILLO_PUBLIC_URL", "https://illo.example.com")
    monkeypatch.setattr(
        packet_outcome_monitor,
        "slack_web_client_from_runtime",
        fake_client,
    )

    below_threshold = await _monitor(session, cycle, cycle_run_id=100)
    assert below_threshold is not None
    assert below_threshold.edges[0].public_details["minted"] == 9
    assert posts == []

    tenth_packet = _handoff(10, created_at=_NOW - timedelta(hours=12))
    session.add(tenth_packet)
    await session.flush()
    await _monitor(session, cycle, cycle_run_id=101)
    await _monitor(session, cycle, cycle_run_id=102)

    assert len(posts) == 1
    assert posts[0]["channel"] == "C_ALERTS"
    assert "Packets: 10 minted · 0 launched" in posts[0]["text"]
    assert "Days since last launch: 57" in posts[0]["text"]

    current_packets[0].launch_count = 1
    current_packets[0].last_launched_at = _NOW - timedelta(hours=1)
    await session.flush()
    await _monitor(session, cycle, cycle_run_id=103)
    assert list(
        (
            await session.scalars(
                select(CycleFailureGuardLatch).where(
                    CycleFailureGuardLatch.cycle_id == cycle.id
                )
            )
        ).all()
    ) == []

    current_packets[0].launch_count = 0
    current_packets[0].last_launched_at = None
    await session.flush()
    await _monitor(session, cycle, cycle_run_id=104)

    assert len(posts) == 2
    latches = list(
        (
            await session.scalars(
                select(CycleFailureGuardLatch).where(
                    CycleFailureGuardLatch.cycle_id == cycle.id
                )
            )
        ).all()
    )
    assert [row.trigger_kind for row in latches] == [
        "packet_launch_flatline"
    ]


async def test_cycle_finalization_shares_latches_and_passes_persisted_run_kind(
    monkeypatch,
):
    session = object()
    run = SimpleNamespace(
        id=301,
        context_snapshot={
            "launch_context": {
                "run_kind": OFF_SLOT_MATERIAL_ALERT_RUN_KIND,
            }
        },
    )
    cycle = SimpleNamespace(id=2)
    calls = []

    async def fake_monitor(
        seen_session,
        seen_cycle,
        *,
        cycle_run_id,
        now,
        latch_store,
    ):
        calls.append(("monitor", latch_store))

    async def fake_failure_guard(
        seen_session,
        seen_cycle,
        *,
        cycle_run_id,
        status,
        error_text,
        latch_store,
        now,
    ):
        calls.append(("failure_guard", status, latch_store))
        return None

    monkeypatch.setattr(cycle_memory, "async_monitor_packet_outcomes", fake_monitor)
    monkeypatch.setattr(
        cycle_memory,
        "async_apply_cycle_terminal_failure_guard",
        fake_failure_guard,
    )

    await cycle_memory._apply_cycle_terminal_guards(
        session,
        run,
        cycle,
        status="completed",
        error=None,
        now=_NOW,
    )

    assert len(calls) == 1
    assert calls[0][0:2] == ("failure_guard", "completed")
    assert isinstance(calls[0][2], CycleAlertLatchStore)

    run.context_snapshot["launch_context"]["run_kind"] = SCHEDULED_DIGEST_RUN_KIND
    calls.clear()
    await cycle_memory._apply_cycle_terminal_guards(
        session,
        run,
        cycle,
        status="completed",
        error=None,
        now=_NOW,
    )

    assert calls[0][0] == "monitor"
    assert calls[1][0:2] == ("failure_guard", "completed")
    assert isinstance(calls[0][1], CycleAlertLatchStore)
    assert calls[0][1] is calls[1][2]
