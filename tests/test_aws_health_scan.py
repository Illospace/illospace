"""Tests for the scheduler-launched AWS health scan pipeline."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from brain.jobs.pipelines import aws_health_scan


async def _spawned_payload(monkeypatch, *, now: datetime, last_success: datetime) -> dict:
    session = MagicMock()
    session.scalar = AsyncMock(return_value=last_success)
    uow = MagicMock()
    uow.session = session
    uow.skills.get_by_name = AsyncMock(
        return_value=SimpleNamespace(skill_installation_id=52, thinking_tier="low")
    )
    uow.__aenter__ = AsyncMock(return_value=uow)
    uow.__aexit__ = AsyncMock(return_value=False)

    monkeypatch.setattr(aws_health_scan, "UnitOfWork", lambda: uow)
    monkeypatch.setattr(
        aws_health_scan.runtime_display,
        "async_get_runtime_display_config",
        AsyncMock(
            return_value=aws_health_scan.runtime_display.RuntimeDisplayConfig(
                display_timezone="America/New_York",
            )
        ),
    )
    monkeypatch.setattr(
        aws_health_scan,
        "_skill_actor",
        AsyncMock(return_value=SimpleNamespace(id="user-1", org_id="org-1")),
    )

    admitted_events = []

    async def _admit_work(admission_session, event):
        assert admission_session is session
        admitted_events.append(event)
        return SimpleNamespace(ok=True, run_id=123)

    monkeypatch.setattr(aws_health_scan, "admit_work", _admit_work)

    assert await aws_health_scan.spawn_health_scan_run(now=now) == 123
    session.scalar.assert_awaited_once()
    return admitted_events[0].payload


async def _spawned_message(monkeypatch, *, now: datetime, last_success: datetime) -> str:
    payload = await _spawned_payload(
        monkeypatch,
        now=now,
        last_success=last_success,
    )
    return payload["message"]


@pytest.mark.asyncio
async def test_pipeline_emits_coverage_since_for_stale_last_success(monkeypatch):
    now = datetime(2026, 7, 25, 13, 30, tzinfo=timezone.utc)
    last_success = now - timedelta(minutes=71)

    message = await _spawned_message(monkeypatch, now=now, last_success=last_success)

    rendered = aws_health_scan.runtime_display.format_display_timestamp(
        last_success,
        "America/New_York",
    )
    assert f"coverage-since: {rendered}" in message


@pytest.mark.asyncio
async def test_pipeline_omits_coverage_since_when_last_success_is_fresh(monkeypatch):
    now = datetime(2026, 7, 25, 13, 30, tzinfo=timezone.utc)
    last_success = now - timedelta(minutes=70)

    message = await _spawned_message(monkeypatch, now=now, last_success=last_success)

    assert "coverage-since:" not in message


@pytest.mark.asyncio
async def test_pipeline_caps_coverage_since_at_six_hours(monkeypatch):
    now = datetime(2026, 7, 25, 13, 30, tzinfo=timezone.utc)

    message = await _spawned_message(
        monkeypatch,
        now=now,
        last_success=now - timedelta(hours=8),
    )

    rendered = aws_health_scan.runtime_display.format_display_timestamp(
        now - timedelta(hours=6),
        "America/New_York",
    )
    assert f"coverage-since: {rendered}" in message


@pytest.mark.asyncio
async def test_pipeline_enables_terminal_display_timezone_gate(monkeypatch):
    now = datetime(2026, 7, 25, 13, 30, tzinfo=timezone.utc)
    payload = await _spawned_payload(
        monkeypatch,
        now=now,
        last_success=now - timedelta(minutes=30),
    )

    assert payload["metadata"]["display_timezone"] == "America/New_York"
    assert payload["metadata"]["enforce_display_timezone_on_slack"] is True
    assert (
        "scan-started-at: 07-25 09:30 EDT (13:30 UTC)"
        in payload["message"]
    )


def test_display_timestamp_formatter_is_dst_aware_across_boundary():
    formatter = aws_health_scan.runtime_display.format_display_timestamp

    assert formatter(
        datetime(2026, 7, 25, 13, 3, tzinfo=timezone.utc),
        "America/New_York",
    ) == "07-25 09:03 EDT (13:03 UTC)"
    assert formatter(
        datetime(2026, 1, 25, 13, 3, tzinfo=timezone.utc),
        "America/New_York",
    ) == "01-25 08:03 EST (13:03 UTC)"
    assert formatter(
        datetime(2026, 3, 8, 6, 59, tzinfo=timezone.utc),
        "America/New_York",
    ) == "03-08 01:59 EST (06:59 UTC)"
    assert formatter(
        datetime(2026, 3, 8, 7, 1, tzinfo=timezone.utc),
        "America/New_York",
    ) == "03-08 03:01 EDT (07:01 UTC)"


@pytest.mark.asyncio
async def test_six_actual_alert_payloads_pass_terminal_timezone_gate(monkeypatch):
    from brain.systems.runs.execution_context import bind_agent_context
    from brain.systems.runs.tool_catalog.handlers.slack import _handle_post_slack_reply

    class _SlackClient:
        def __init__(self):
            self.posts = []

        async def post_message(self, **kwargs):
            self.posts.append(kwargs)
            return {"ok": True, "channel": kwargs["channel"], "ts": "1785000000.000001"}

    client = _SlackClient()

    async def _slack_client():
        return client

    monkeypatch.setattr(
        "brain.systems.runs.tool_catalog.handlers.slack._slack_client_from_runtime",
        _slack_client,
    )

    instants = [
        datetime(2026, 7, 25, 13, 3, tzinfo=timezone.utc) + timedelta(hours=index)
        for index in range(6)
    ]
    payloads = [
        (
            f"AWS health alert emission {index + 1}\nObserved at "
            + aws_health_scan.runtime_display.format_display_timestamp(
                instant,
                "America/New_York",
            )
        )
        for index, instant in enumerate(instants)
    ]
    payloads[1] = (
        "AWS health alert emission 2\n"
        f"Observed at {instants[1].isoformat().replace('+00:00', 'Z')}"
    )

    results = []
    for index, body in enumerate(payloads):
        with bind_agent_context(
            {
                "org_id": "org-1",
                "execution_metadata": {
                    "run_id": 900 + index,
                    "org_id": "org-1",
                    "skill_name": aws_health_scan.SKILL_NAME,
                    "display_timezone": "America/New_York",
                    "enforce_display_timezone_on_slack": True,
                },
                "slack_trigger": {
                    "response_target": {
                        "channel_id": "C_ALERTS",
                        "thread_ts": None,
                        "visibility": "public",
                    }
                },
            }
        ):
            results.append(json.loads(await _handle_post_slack_reply(body=body)))

    assert len(results) == 6
    assert results[1] == {
        "ok": False,
        "posted": False,
        "error": "display_timezone_validation_failed",
        "detail": (
            "Every UTC timestamp in this AWS health alert must be paired "
            "with its America/New_York rendering."
        ),
        "invalid_lines": [f"Observed at {instants[1].isoformat().replace('+00:00', 'Z')}"],
        "retryable": True,
    }
    assert all(result["ok"] is True for index, result in enumerate(results) if index != 1)
    assert len(client.posts) == 5


@pytest.mark.asyncio
async def test_pipeline_exits_nonzero_when_headless_run_spawn_fails(monkeypatch, capsys):
    spawn = AsyncMock(side_effect=RuntimeError("admission unavailable"))
    monkeypatch.setattr(aws_health_scan, "spawn_health_scan_run", spawn)

    exit_code = await aws_health_scan.async_main()

    assert exit_code != 0
    spawn.assert_awaited_once_with()
    assert "AWS health scan spawn failed: admission unavailable" in capsys.readouterr().err
