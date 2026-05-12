from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO
import json
from types import SimpleNamespace
from unittest.mock import MagicMock
import zipfile


def _result(rows):
    result = MagicMock()
    result.all.return_value = rows
    return result


def _run(**overrides):
    now = datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc)
    values = {
        "id": 42,
        "trace_id": "run:42",
        "thread_id": "idea-1",
        "org_id": "org-1",
        "user_id": "user-1",
        "parent_run_id": None,
        "root_run_id": None,
        "profile": "fast",
        "recipe": "direct",
        "status": "completed",
        "input_message": "Explain the activity panel",
        "target_ref": {"event": "thread_reply"},
        "workspace_ref": {"cwd": "/repo"},
        "model_policy": {"model": "openai:gpt-5.4"},
        "context_summary": "Used recent thread context.",
        "metadata_": {"source": "test"},
        "created_at": now,
        "updated_at": now,
        "started_at": now,
        "paused_at": None,
        "completed_at": now,
        "failed_at": None,
        "canceled_at": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_agent_trace_snapshot_is_bounded_and_analysis_ready():
    from brain.systems.runs.cortex.recording import build_agent_trace_snapshot
    from brain.platform.db.models.agent_run import AgentRunRow

    run = _run()
    now = datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc)
    message = SimpleNamespace(
        id=9,
        idea_id="idea-1",
        role="illo",
        content="Final answer " + ("x" * 5000),
        attachments=[],
        metadata_={"run_id": 42, "trace_id": "run:42", "private": "not copied"},
        message_type="message",
        created_at=now,
    )
    event = SimpleNamespace(
        id=99,
        run_id=42,
        root_run_id=42,
        sequence_no=3,
        event_type="run.tool_completed",
        payload={"tool_name": "read_file", "args": {"path": "README.md"}, "result": "y" * 5000},
        producer="agent_runtime",
        visibility="public",
        created_at=now,
    )
    artifact = SimpleNamespace(
        id=100,
        run_id=42,
        root_run_id=42,
        artifact_type="reply",
        title="Final reply",
        payload={"large": "z" * 5000},
        text="artifact text",
        uri=None,
        visibility="public",
        created_at=now,
    )

    session = MagicMock()
    session.get.side_effect = lambda model, key: run if model is AgentRunRow and key == 42 else None
    session.scalars.side_effect = [
        _result([42]),
        _result([message]),
        _result([event]),
        _result([artifact]),
    ]

    snapshot = build_agent_trace_snapshot(session, run, saved_by="user-1")

    assert snapshot["schema_version"] == 1
    assert snapshot["trace_id"] == "run:42"
    assert snapshot["thread"]["messages"][0]["metadata"] == {
        "run_id": 42,
        "trace_id": "run:42",
        "execution_profile": None,
        "live_agent_text": None,
    }
    assert snapshot["tools"][0]["tool_name"] == "read_file"
    assert snapshot["artifacts"][0]["artifact_type"] == "reply"
    assert snapshot["storage_estimate"]["truncated"] is True
    assert "not copied" not in str(snapshot)


def test_thread_trace_snapshot_covers_conversation_and_all_thread_runs():
    from brain.systems.runs.cortex.recording import (
        agent_trace_export_filename,
        build_thread_trace_snapshot,
    )
    from brain.platform.db.models.agent_run import AgentRunRow

    now = datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc)
    run = _run(id=42, trace_id="run:42", input_message="Start the thread")
    child = _run(
        id=43,
        trace_id="run:43",
        parent_run_id=42,
        root_run_id=42,
        input_message="Worker slice",
    )
    first_message = SimpleNamespace(
        id=10,
        idea_id="idea-1",
        role="user",
        content="Can you analyze this whole thread?",
        attachments=[],
        metadata_={"run_id": 42, "trace_id": "run:42", "private": "not copied"},
        message_type="message",
        created_at=now,
    )
    second_message = SimpleNamespace(
        id=11,
        idea_id="idea-1",
        role="illo",
        content="Here is the full-thread answer.",
        attachments=[],
        metadata_={"run_id": 43, "trace_id": "run:43"},
        message_type="message",
        created_at=now,
    )
    event = SimpleNamespace(
        id=99,
        run_id=43,
        root_run_id=42,
        sequence_no=3,
        event_type="run.tool_completed",
        payload={"tool_name": "read_thread_messages", "result": "ok"},
        producer="agent_runtime",
        visibility="public",
        created_at=now,
    )
    artifact = SimpleNamespace(
        id=100,
        run_id=43,
        root_run_id=42,
        artifact_type="reply",
        title="Thread answer",
        payload={"summary": "ok"},
        text="artifact text",
        uri=None,
        visibility="public",
        created_at=now,
    )

    session = MagicMock()
    session.get.side_effect = lambda model, key: {42: run, 43: child}.get(key) if model is AgentRunRow else None
    session.scalars.side_effect = [
        _result([run, child]),
        _result([second_message, first_message]),
        _result([event]),
        _result([artifact]),
    ]

    snapshot = build_thread_trace_snapshot(session, "idea-1", saved_by="user-1")

    assert snapshot["export_scope"] == "thread"
    assert snapshot["trace_id"] == "thread:idea-1"
    assert snapshot["thread"]["idea_id"] == "idea-1"
    assert [message["id"] for message in snapshot["thread"]["messages"]] == [10, 11]
    assert [run["run_id"] for run in snapshot["runs"]] == [42, 43]
    assert snapshot["related_run_ids"] == [42, 43]
    assert snapshot["tools"][0]["tool_name"] == "read_thread_messages"
    assert snapshot["artifacts"][0]["artifact_type"] == "reply"
    assert agent_trace_export_filename(snapshot) == "illo-thread-trace-idea-1.zip"
    assert "not copied" not in str(snapshot)


def test_agent_trace_export_zip_contains_shareable_trace_files():
    from brain.systems.runs.cortex.recording import (
        agent_trace_export_filename,
        build_agent_trace_export_zip,
    )

    snapshot = {
        "schema_version": 1,
        "trace_id": "run:42",
        "run": {
            "run_id": 42,
            "status": "completed",
            "started_at": "2026-05-12T12:00:00+00:00",
            "input_message": "Why did Illo answer that?",
        },
        "thread": {
            "messages": [
                {
                    "id": 1,
                    "role": "user",
                    "created_at": "2026-05-12T12:00:01+00:00",
                    "message_type": "message",
                    "content": "Original prompt",
                    "metadata": {"run_id": 42, "trace_id": "run:42"},
                }
            ]
        },
        "events": [
            {
                "id": 2,
                "run_id": 42,
                "sequence_no": 1,
                "event_type": "run.tool_completed",
                "created_at": "2026-05-12T12:00:02+00:00",
                "payload": {"tool_name": "read_file", "result": "ok"},
            }
        ],
        "artifacts": [],
        "storage_estimate": {"json_bytes": 512, "truncated": False},
    }

    archive_bytes = build_agent_trace_export_zip(snapshot)

    assert agent_trace_export_filename(snapshot) == "illo-trace-run-42.zip"
    with zipfile.ZipFile(BytesIO(archive_bytes)) as archive:
        assert set(archive.namelist()) == {"README.md", "activity.json", "manifest.json", "trace.json"}
        manifest = json.loads(archive.read("manifest.json"))
        trace = json.loads(archive.read("trace.json"))
        activity = json.loads(archive.read("activity.json"))
        readme = archive.read("README.md").decode("utf-8")

    assert "artifact_id" not in manifest
    assert manifest["trace_id"] == "run:42"
    assert trace["run"]["input_message"] == "Why did Illo answer that?"
    assert activity["item_count"] == 3
    assert "trace.json" in readme
    assert "not saved as a database artifact" in readme
