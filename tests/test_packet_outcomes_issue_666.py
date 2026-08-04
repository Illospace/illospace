"""Issue #666: packet outcomes must reach digests and page on a flatline."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import select

from brain.platform.db.models.cycle import Cycle, CycleFailureGuardLatch
from brain.platform.db.models.idea import Idea
from brain.platform.db.models.launch_handoff import LaunchHandoff
from brain.platform.db.models.cycle import CycleRun
from brain.systems.briefing.outcomes import (
    format_outcomes_line,
    load_packet_outcome_report,
    packet_outcomes,
)
from brain.systems.cycles import cycle_failure_guard, prompts
from brain.systems.cycles.contracts import cycle_result_contract


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


def _coordinator_message() -> str:
    cycle = Cycle(
        id=2,
        user_id=str(uuid4()),
        org_id=_ORG,
        name="Uwear Ticket Coordinator Check-ins",
        prompt="Publish the chantier-primary coordinator digest.",
        schedule_expr="0 8 * * 1",
        timezone="America/Toronto",
        enabled=True,
    )
    run = CycleRun(
        id=44,
        cycle_id=cycle.id,
        scheduled_for=_NOW,
        prompt_snapshot=cycle.prompt,
        guidance_snapshot=[],
        output_targets_snapshot=[],
        context_snapshot={
            "result_contract": cycle_result_contract(run_kind="scheduled_digest")
        },
    )
    idea = Idea(id=str(uuid4()), title="Coordinator digest")
    return prompts.cycle_run_message(idea, cycle, run)


async def test_digest_instruction_consumes_the_same_direct_outcomes_line(
    async_sqlite_session_factory,
):
    session = await async_sqlite_session_factory((LaunchHandoff.__table__,))
    rows = [
        _handoff(1, created_at=_NOW - timedelta(hours=72)),
        _handoff(
            2,
            created_at=_NOW - timedelta(hours=4),
            launch_count=1,
            last_launched_at=_NOW - timedelta(hours=3),
        ),
    ]
    session.add_all(rows)
    await session.flush()

    report = await load_packet_outcome_report(session, org_id=_ORG, now=_NOW)
    loaded_rows = list(
        (
            await session.scalars(
                select(LaunchHandoff).where(LaunchHandoff.org_id == _ORG)
            )
        ).all()
    )
    direct_line = format_outcomes_line(packet_outcomes(loaded_rows, now=_NOW))

    assert report.digest_line == direct_line
    assert report.digest_line == "Packets: 2 minted · 1 launched · 1 ignored >48h · median 60m to launch"
    message = _coordinator_message()
    assert "`packets.outcomes` with `since_hours: 168`" in message
    assert "append that value verbatim" in message
    assert "Do not recalculate or paraphrase its packet counts" in message


async def test_flatline_posts_once_with_mint_count_and_days_since_launch(
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
        name="Uwear Ticket Coordinator Check-ins",
        prompt="Publish the coordinator digest.",
        schedule_expr="0 8 * * 1",
        timezone="UTC",
        enabled=True,
    )
    session.add(cycle)
    session.add_all(
        [
            _handoff(index, created_at=_NOW - timedelta(hours=index + 1))
            for index in range(10)
        ]
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
        assert requested_by == "cycle_failure_alert"
        assert reason == "Deliver a packet launch flatline alert to the team."
        return FakeSlackClient()

    monkeypatch.setenv("ILLO_CYCLE_FAILURE_ALERT_CHANNEL", "#alerts")
    monkeypatch.setenv("ILLO_PUBLIC_URL", "https://illo.example.com")
    monkeypatch.setattr(cycle_failure_guard, "slack_web_client_from_runtime", fake_client)

    for run_id in (101, 102):
        await cycle_failure_guard.async_apply_packet_launch_flatline_guard(
            session,
            cycle,
            cycle_run_id=run_id,
            now=_NOW,
        )

    assert len(posts) == 1
    assert posts[0]["channel"] == "C_ALERTS"
    assert "Packets: 10 minted · 0 launched" in posts[0]["text"]
    assert "Days since last launch: 57" in posts[0]["text"]
    latches = list(
        (
            await session.scalars(
                select(CycleFailureGuardLatch).where(
                    CycleFailureGuardLatch.cycle_id == cycle.id
                )
            )
        ).all()
    )
    assert [row.trigger_kind for row in latches] == ["packet_launch_flatline"]
