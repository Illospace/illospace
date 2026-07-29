"""Typed detached AgentRun handoff contract tests."""
from __future__ import annotations

import json

import pytest

from brain.contracts.scheduler_handoff import (
    DetachedAgentRunHandoff,
    DetachedAgentRunHandoffError,
    emit_detached_agent_run_handoff,
    parse_detached_agent_run_handoff,
)


def test_detached_agent_run_handoff_round_trips_the_complete_envelope():
    encoded = emit_detached_agent_run_handoff(321)

    assert json.loads(encoded) == {
        "type": "scheduler.detached_agent_run",
        "version": 1,
        "status": "dispatched",
        "scheduler_agent_run_id": 321,
    }
    assert parse_detached_agent_run_handoff(encoded) == DetachedAgentRunHandoff(
        agent_run_id=321
    )


@pytest.mark.parametrize(
    "payload",
    [
        {
            "type": "scheduler.detached_agent_run",
            "version": 1,
            "scheduler_agent_run_id": 321,
        },
        {
            "type": "scheduler.detached_agent_run",
            "version": 1,
            "status": "completed",
            "scheduler_agent_run_id": 321,
        },
        {
            "type": "scheduler.detached_agent_run",
            "version": 2,
            "status": "dispatched",
            "scheduler_agent_run_id": 321,
        },
        {
            "type": "scheduler.detached_agent_run",
            "version": 1,
            "status": "dispatched",
            "scheduler_agent_run_id": "321",
        },
        {
            "type": "scheduler.detached_agent_run",
            "version": 1,
            "status": "dispatched",
            "scheduler_agent_run_id": 321,
            "extra": True,
        },
    ],
)
def test_detached_agent_run_handoff_rejects_any_malformed_envelope(payload):
    with pytest.raises(DetachedAgentRunHandoffError):
        parse_detached_agent_run_handoff(json.dumps(payload))
