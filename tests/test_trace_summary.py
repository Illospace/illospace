from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock


def _result(rows):
    result = MagicMock()
    result.all.return_value = rows
    return result


def _mapping_all(rows):
    result = MagicMock()
    result.mappings.return_value.all.return_value = rows
    return result


def _mapping_first(row):
    result = MagicMock()
    result.mappings.return_value.first.return_value = row
    return result


def test_trace_summary_returns_privacy_filtered_skeleton():
    from brain.app.api.routers.system import _build_trace_summary
    from brain.platform.db.models.run import AgentRun

    now = datetime(2026, 4, 23, 12, 0, tzinfo=timezone.utc)
    agent_run = SimpleNamespace(
        id=42,
        trace_id="run:42",
        thread_id="idea-1",
        target_ref={"event": "thread_reply"},
        status="completed",
        profile="fast",
        recipe="fast",
        model_policy={"model": "openai:gpt-5.4"},
        started_at=now,
        completed_at=now,
        created_at=now,
        user_id="user-1",
    )
    scheduler_run = SimpleNamespace(
        id=7,
        trace_id="run:42",
        job_id=3,
        status="settled_success",
        agent_run_id=42,
        parent_run_id=None,
        scheduled_for=now,
        started_at=now,
        finished_at=now,
    )
    step = SimpleNamespace(
        id=8,
        trace_id="run:42",
        run_id=7,
        step_key="handler_execute",
        sequence_no=1,
        status="completed",
        started_at=now,
        finished_at=now,
    )
    db = MagicMock()
    db.get.side_effect = lambda model, key, **_: agent_run if model is AgentRun and key == 42 else None
    db.execute.side_effect = [
        _mapping_all([
            {
                "model": "openai:gpt-5.4",
                "calls": 1,
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_read": 10,
                "cache_write": 0,
                "latency_ms": 250,
            }
        ]),
        _result([SimpleNamespace(tool_name="run_script", source="worker:develop", calls=1)]),
        _result([SimpleNamespace(status="passed", count=1)]),
        _mapping_first({
            "execution_artifact_count": 2,
            "execution_artifact_types": {"branch": 1, "pr": 1},
            "has_output_artifact": True,
            "output_type": "reply",
        }),
    ]
    db.scalars.side_effect = [
        _result([scheduler_run]),
        _result([step]),
        _result(["branch", "pr"]),
    ]
    db.scalar.side_effect = [
        3,
        4,
    ]

    summary = _build_trace_summary(
        db,
        user={"id": "user-1", "role": "member"},
        run_id=42,
    )

    assert summary["trace_id"] == "run:42"
    assert summary["run"]["provider_model"] == "openai/gpt-5.4"
    assert summary["llm_calls"]["count"] == 1
    assert summary["tool_calls"]["tools"] == [
        {"tool_name": "run_script", "source": "worker:develop", "calls": 1}
    ]
    assert summary["verification"]["statuses"] == {"passed": 1}
    assert summary["artifacts"]["execution_artifact_types"] == {"branch": 1, "pr": 1}
    assert summary["recordings"]["flight_recorder_schema_version"] is None
    assert "Sensitive final user-facing reply" not in str(summary)
    assert "private args" not in str(summary)
    assert "private result" not in str(summary)
