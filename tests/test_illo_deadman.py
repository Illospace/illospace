"""Unit tests for the external deadman state machine."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest
import yaml

from ops.external_deadman import watcher as illo_deadman
from ops.external_deadman.watcher import (
    DeadmanState,
    Heartbeat,
    evaluate_deadman,
)


NOW = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)
WORKFLOW_PATH = Path(__file__).parents[1] / ".github" / "workflows" / "illo-deadman.yml"


def _heartbeat(*, age: timedelta) -> Heartbeat:
    return Heartbeat(
        ts=NOW - age,
        last_run_id=2718,
        last_surface="slack",
    )


def test_fresh_heartbeat_does_not_alarm():
    decision = evaluate_deadman(
        _heartbeat(age=timedelta(minutes=5)),
        DeadmanState(),
        now=NOW,
    )

    assert decision.action == "none"
    assert decision.message is None
    assert decision.state == DeadmanState()


def test_stale_heartbeat_starts_outage_with_stable_id():
    heartbeat = _heartbeat(age=timedelta(minutes=13))

    decision = evaluate_deadman(heartbeat, DeadmanState(), now=NOW)

    assert decision.action == "alarm"
    assert decision.state.alarmed is True
    assert decision.state.outage_id == "heartbeat-20260727T114700Z"
    assert "last seen 2026-07-27T11:47:00Z" in decision.message
    assert "run 2718 via slack" in decision.message
    assert "Outage ID: heartbeat-20260727T114700Z" in decision.message


def test_still_stale_heartbeat_does_not_alarm_again():
    heartbeat = _heartbeat(age=timedelta(minutes=13))
    first = evaluate_deadman(heartbeat, DeadmanState(), now=NOW)

    repeated = evaluate_deadman(
        heartbeat,
        first.state,
        now=NOW + timedelta(minutes=5),
    )

    assert repeated.action == "none"
    assert repeated.message is None
    assert repeated.state.alarmed is True


def test_recovery_rearms_and_posts_recovery_line():
    stale = evaluate_deadman(
        _heartbeat(age=timedelta(minutes=13)),
        DeadmanState(),
        now=NOW,
    )

    recovered = evaluate_deadman(
        Heartbeat(
            ts=NOW + timedelta(minutes=5),
            last_run_id=2720,
            last_surface="cortex",
        ),
        stale.state,
        now=NOW + timedelta(minutes=5),
    )

    assert recovered.action == "recovery"
    assert recovered.state.alarmed is False
    assert recovered.state.outage_id is None
    assert "heartbeat resumed at 2026-07-27T12:05:00Z" in recovered.message
    assert "run 2720 via cortex" in recovered.message
    assert "Recovered outage ID: heartbeat-20260727T114700Z" in recovered.message

    rearmed = evaluate_deadman(
        _heartbeat(age=timedelta(minutes=25)),
        recovered.state,
        now=NOW + timedelta(minutes=5),
    )
    assert rearmed.action == "alarm"


def test_missing_heartbeat_waits_for_the_staleness_window():
    first_missing = evaluate_deadman(None, DeadmanState(), now=NOW)

    assert first_missing.action == "none"
    assert first_missing.state.missing_since == NOW

    stale_missing = evaluate_deadman(
        None,
        first_missing.state,
        now=NOW + timedelta(minutes=12),
    )

    assert stale_missing.action == "alarm"
    assert "last seen never" in stale_missing.message


def test_missing_file_does_not_false_recover_an_active_alarm():
    decision = evaluate_deadman(
        None,
        DeadmanState(alarmed=True),
        now=NOW,
    )

    assert decision.action == "none"
    assert decision.state.alarmed is True


def test_alarm_delivery_repeats_when_slack_succeeds_and_state_write_fails(
    monkeypatch,
):
    class FailingStore:
        def read_json(self, path):
            if path == illo_deadman.HEARTBEAT_PATH:
                return (
                    {
                        "ts": "2000-01-01T00:00:00Z",
                        "last_run_id": 2718,
                        "last_surface": "slack",
                    },
                    "heartbeat-sha",
                )
            return None, None

        def write_json(self, _path, _payload):
            raise illo_deadman.WatcherError("state write failed")

    posted = []
    store = FailingStore()
    monkeypatch.setattr(illo_deadman, "GitHubFileStore", lambda **_kwargs: store)
    monkeypatch.setattr(
        illo_deadman,
        "_post_slack",
        lambda _url, message: posted.append(message),
    )
    monkeypatch.setenv("GITHUB_TOKEN", "workflow-token")
    monkeypatch.setenv("SLACK_DEADMAN_WEBHOOK_URL", "https://hooks.slack.test/deadman")
    monkeypatch.delenv("ILLO_DEADMAN_DRY_RUN", raising=False)
    monkeypatch.delenv("ILLO_DEADMAN_FORCE_STALE", raising=False)

    for _ in range(2):
        with pytest.raises(illo_deadman.WatcherError, match="state write failed"):
            illo_deadman.run()

    assert len(posted) == 2
    assert posted[0] == posted[1]
    assert "Outage ID: heartbeat-20000101T000000Z" in posted[0]


def test_recovery_delivery_repeats_for_same_outage_when_state_write_fails(
    monkeypatch,
):
    outage_id = "heartbeat-20000101T000000Z"

    class FailingStore:
        def read_json(self, path):
            if path == illo_deadman.HEARTBEAT_PATH:
                return (
                    {
                        "ts": "2999-01-01T00:00:00Z",
                        "last_run_id": 2720,
                        "last_surface": "cortex",
                    },
                    "heartbeat-sha",
                )
            return (
                {
                    "alarmed": True,
                    "missing_since": None,
                    "outage_id": outage_id,
                },
                "state-sha",
            )

        def write_json(self, _path, _payload):
            raise illo_deadman.WatcherError("state write failed")

    posted = []
    store = FailingStore()
    monkeypatch.setattr(illo_deadman, "GitHubFileStore", lambda **_kwargs: store)
    monkeypatch.setattr(
        illo_deadman,
        "_post_slack",
        lambda _url, message: posted.append(message),
    )
    monkeypatch.setenv("GITHUB_TOKEN", "workflow-token")
    monkeypatch.setenv("SLACK_DEADMAN_WEBHOOK_URL", "https://hooks.slack.test/deadman")
    monkeypatch.delenv("ILLO_DEADMAN_DRY_RUN", raising=False)
    monkeypatch.delenv("ILLO_DEADMAN_FORCE_STALE", raising=False)

    for _ in range(2):
        with pytest.raises(illo_deadman.WatcherError, match="state write failed"):
            illo_deadman.run()

    assert len(posted) == 2
    assert posted[0] == posted[1]
    assert f"Recovered outage ID: {outage_id}" in posted[0]


def test_forced_stale_dry_run_exercises_alarm_without_network(monkeypatch, capsys):
    class FakeStore:
        def __init__(self, **_kwargs):
            pass

        def read_json(self, path):
            if path == illo_deadman.HEARTBEAT_PATH:
                return (
                    {
                        "ts": "2026-07-27T12:00:00Z",
                        "last_run_id": 2718,
                        "last_surface": "slack",
                    },
                    "heartbeat-sha",
                )
            return None, None

    monkeypatch.setattr(illo_deadman, "GitHubFileStore", FakeStore)
    monkeypatch.setenv("ILLO_DEADMAN_DRY_RUN", "true")
    monkeypatch.setenv("ILLO_DEADMAN_FORCE_STALE", "true")

    assert illo_deadman.run() == 0
    output = capsys.readouterr().out.splitlines()
    result = json.loads(output[0])
    assert result["action"] == "alarm"
    assert result["dry_run"] is True
    assert "Deadman dry-run alarm" in output[1]


def test_workflow_schedules_and_exposes_forced_stale_dry_run():
    workflow = yaml.load(WORKFLOW_PATH.read_text(), Loader=yaml.BaseLoader)

    assert workflow["on"]["schedule"] == [{"cron": "*/5 * * * *"}]
    inputs = workflow["on"]["workflow_dispatch"]["inputs"]
    assert inputs["force_stale"]["type"] == "boolean"
    assert inputs["dry_run"]["default"] == "true"
    assert workflow["permissions"] == {"contents": "write"}
    assert workflow["jobs"]["watch"]["steps"][-1]["run"] == (
        "python3 -m ops.external_deadman.watcher"
    )
    env = workflow["jobs"]["watch"]["steps"][-1]["env"]
    assert "secrets.SLACK_DEADMAN_WEBHOOK_URL" in env["SLACK_DEADMAN_WEBHOOK_URL"]
    assert "secrets.ILLO_DEADMAN_HEALTHCHECK_URL" in env[
        "ILLO_DEADMAN_HEALTHCHECK_URL"
    ]
