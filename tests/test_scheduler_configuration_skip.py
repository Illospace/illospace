"""Tests for durable alerts on scheduler configuration skips."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from brain.app.scheduler.configuration_skip_alert import (
    SCHEDULER_CONFIGURATION_SKIP_ALERT_KEY,
    async_alert_first_configuration_skip,
)
from brain.app.scheduler.overdue_alert_state import try_claim_scheduler_alert
from tests.scheduler_test_support import make_scheduler_test_session


NOW = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def session(async_sqlite_session_factory):
    return await make_scheduler_test_session(async_sqlite_session_factory)


async def test_configuration_skip_alert_key_is_durably_claimed_once(session):
    first = await try_claim_scheduler_alert(
        session,
        alert_key=SCHEDULER_CONFIGURATION_SKIP_ALERT_KEY,
        alerted_at=NOW,
    )
    after_restart = await try_claim_scheduler_alert(
        session,
        alert_key=SCHEDULER_CONFIGURATION_SKIP_ALERT_KEY,
        alerted_at=NOW + timedelta(minutes=5),
    )

    assert first is True
    assert after_restart is False


async def test_first_configuration_skip_posts_to_alerts_once():
    claimed = False
    deliveries = []

    async def claim_alert(*, alerted_at):
        nonlocal claimed
        assert alerted_at >= NOW
        if claimed:
            return False
        claimed = True
        return True

    async def release_alert():
        raise AssertionError("successful delivery must keep the durable claim")

    async def deliver_alert(**kwargs):
        deliveries.append(kwargs)

    for minute in range(2):
        await async_alert_first_configuration_skip(
            job_key="illo_external_heartbeat",
            run_id=714 + minute,
            reason="No GitHub App project binding is configured",
            alerted_at=NOW + timedelta(minutes=minute),
            claim_alert=claim_alert,
            release_alert=release_alert,
            deliver_alert=deliver_alert,
        )

    assert len(deliveries) == 1
    alert = deliveries[0]
    assert alert["policy"].channel == "#alerts"
    assert alert["policy"].requested_by == "scheduler_configuration_skip_alert"
    assert alert["subject"].identity == "illo_external_heartbeat"
    assert alert["error_text"] == "No GitHub App project binding is configured"
