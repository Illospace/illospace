from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace


async def test_interruption_service_returns_typed_results_and_notifies_after_commit(
    monkeypatch,
):
    from brain.systems.runs import interruption

    events = []
    occurred_at = datetime(2026, 7, 22, 17, 55, tzinfo=timezone.utc)

    class FakeUnitOfWork:
        session = object()

        async def __aenter__(self):
            events.append("enter")
            return self

        async def __aexit__(self, *_exc_info):
            events.append("commit")

    class FakeStore:
        def __init__(self, session):
            assert session is FakeUnitOfWork.session

        async def interrupt_and_requeue(self, run_id, *, reason, interrupted_at):
            events.append(("persist", run_id))
            return (
                SimpleNamespace(
                    id=run_id,
                    metadata={
                        "interruption": {
                            "reason": reason,
                            "interrupted_at": interrupted_at.isoformat(),
                            "requeued": True,
                        }
                    },
                ),
                True,
            )

    async def notify(result):
        events.append(("notify", result.run_id))

    monkeypatch.setattr(interruption, "UnitOfWork", FakeUnitOfWork)
    monkeypatch.setattr(interruption, "AsyncAgentRunStore", FakeStore)
    monkeypatch.setattr(interruption, "notify_run_interruption", notify)

    results = await interruption.interrupt_and_requeue_run_ids(
        [2330, 2327, 2330],
        reason="worker_shutdown_drain_timeout",
        interrupted_at=occurred_at,
    )

    assert results == (
        interruption.RunInterruption(
            run_id=2327,
            reason="worker_shutdown_drain_timeout",
            interrupted_at=occurred_at,
            requeued=True,
        ),
        interruption.RunInterruption(
            run_id=2330,
            reason="worker_shutdown_drain_timeout",
            interrupted_at=occurred_at,
            requeued=True,
        ),
    )
    assert events == [
        "enter",
        ("persist", 2327),
        ("persist", 2330),
        "commit",
        ("notify", 2327),
        ("notify", 2330),
    ]


def test_interruption_presentation_is_not_a_terminal_failure():
    from brain.systems.runs import failures, interruption

    result = interruption.RunInterruption(
        run_id=2330,
        reason="worker_shutdown_drain_timeout",
        interrupted_at=datetime(2026, 7, 22, 17, 55, tzinfo=timezone.utc),
        requeued=True,
    )

    assert interruption.interrupted_run_message(result) == (
        "I was interrupted by a system restart at 17:55 UTC (run 2330); "
        "I've re-queued it and will reply here when it finishes."
    )
    assert interruption.interruption_notice_condition(result) == "interruption:requeued"
    assert not hasattr(failures, "interrupted_run_message")
