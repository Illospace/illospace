from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import zipfile

import pytest


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
        "model_policy": {"model": "openai:gpt-5.6-sol"},
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


def _cycle(**overrides):
    now = datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc)
    values = {
        "id": 7,
        "user_id": "user-1",
        "org_id": "org-1",
        "name": "Weekly sales cycle",
        "prompt": "Run the weekly report",
        "schedule_expr": "0 9 * * 1",
        "timezone": "America/Toronto",
        "enabled": True,
        "model_override": None,
        "thinking_override": "none",
        "execution_mode": "reuse_same_idea",
        "target_idea_id": "idea-1",
        "reopen_archived": True,
        "next_run_at": now,
        "last_run_at": now,
        "last_status": "completed",
        "last_error": None,
        "deleted_at": None,
        "created_at": now,
        "updated_at": now,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _cycle_run(**overrides):
    now = datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc)
    values = {
        "id": 8,
        "cycle_id": 7,
        "scheduled_for": now,
        "started_at": now,
        "completed_at": now,
        "status": "completed",
        "error": None,
        "skip_reason": None,
        "idea_id": "idea-1",
        "run_id": 42,
        "prompt_snapshot": "Run the weekly report",
        "created_at": now,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_agent_trace_snapshot_is_bounded_and_analysis_ready():
    from brain.systems.runs.cortex.recording import build_agent_trace_snapshot_async

    run = _run(
        workspace_ref={
            "workspace_root": "/app/brain/uploads/agent.md",
            "resources": [{"id": "attachment-1", "kind": "file", "path": "/app/brain/uploads/agent.md"}],
        },
        metadata_={
            "source": "cycle",
            "cycle_id": 7,
            "cycle_run_id": 8,
            "launch_envelope": {"cycle_id": 7, "cycle_run_id": 8},
        },
    )
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

    session = AsyncMock()
    session.scalars.side_effect = [
        _result([42]),
        _result([run]),
        _result([message]),
        _result([event]),
        _result([artifact]),
        _result([_cycle()]),
        _result([_cycle_run()]),
    ]

    snapshot = await build_agent_trace_snapshot_async(session, run, saved_by="user-1")

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
    assert snapshot["cycles"]["cycle_runs"][0]["run_id"] == 42
    assert snapshot["diagnostics"]["workspace"][0]["suspicious_file_roots"][0]["path"] == "/app/brain/uploads/agent.md"
    assert snapshot["storage_estimate"]["truncated"] is True
    assert "not copied" not in str(snapshot)


@pytest.mark.asyncio
async def test_agent_trace_snapshot_async_uses_same_bounded_payload_shape():
    from brain.systems.runs.cortex.recording import build_agent_trace_snapshot_async

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

    session = AsyncMock()
    session.scalars.side_effect = [
        _result([42]),
        _result([run]),
        _result([message]),
        _result([event]),
        _result([artifact]),
        _result([]),
        _result([]),
    ]

    snapshot = await build_agent_trace_snapshot_async(session, run, saved_by="user-1")

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


@pytest.mark.asyncio
async def test_thread_trace_snapshot_covers_conversation_and_all_thread_runs():
    from brain.systems.runs.cortex.recording import (
        agent_trace_export_filename,
        build_thread_trace_snapshot_async,
    )

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

    session = AsyncMock()
    session.scalars.side_effect = [
        _result([run, child]),
        _result([first_message, second_message]),
        _result([]),
        _result([event]),
        _result([]),
        _result([artifact]),
        _result([_cycle()]),
        _result([_cycle_run(run_id=43)]),
    ]

    snapshot = await build_thread_trace_snapshot_async(session, "idea-1", saved_by="user-1")

    assert snapshot["export_scope"] == "thread"
    assert snapshot["trace_id"] == "thread:idea-1"
    assert snapshot["storage_policy"]["messages"] == "all_thread_messages"
    assert snapshot["thread"]["message_limit"] is None
    assert snapshot["thread"]["idea_id"] == "idea-1"
    assert [message["id"] for message in snapshot["thread"]["messages"]] == [10, 11]
    assert [run["run_id"] for run in snapshot["runs"]] == [42, 43]
    assert snapshot["related_run_ids"] == [42, 43]
    assert snapshot["tools"][0]["tool_name"] == "read_thread_messages"
    assert snapshot["artifacts"][0]["artifact_type"] == "reply"
    assert snapshot["cycles"]["cycle_count"] == 1
    assert snapshot["cycles"]["cycle_run_count"] == 1
    assert snapshot["diagnostics"]["cycle_summary"]["cycle_run_ids"] == [8]
    assert agent_trace_export_filename(snapshot) == "illo-thread-trace-idea-1.zip"
    assert "not copied" not in str(snapshot)


@pytest.mark.asyncio
async def test_failed_run_trace_snapshot_never_replays_raw_diagnostics():
    from brain.systems.runs.cortex.recording import build_agent_trace_snapshot_async
    from brain.systems.runs.failures import UPSTREAM_FAILED_RUN_MESSAGE

    raw_diagnostic = "provider exploded bearer=trace-secret"
    now = datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc)
    run = _run(
        status="failed",
        completed_at=None,
        failed_at=now,
        context_summary=raw_diagnostic,
        metadata_={"failure": {"category": "upstream", "error": raw_diagnostic}},
    )
    message = SimpleNamespace(
        id=9,
        idea_id="idea-1",
        role="illo",
        content=raw_diagnostic,
        attachments=[],
        metadata_={"run_id": 42, "error": raw_diagnostic},
        message_type="message",
        created_at=now,
    )
    event = SimpleNamespace(
        id=99,
        run_id=42,
        root_run_id=42,
        sequence_no=3,
        event_type="run.failed",
        payload={"failure_category": "upstream", "error": raw_diagnostic},
        producer="agent_runtime",
        visibility="public",
        created_at=now,
    )
    artifact = SimpleNamespace(
        id=100,
        run_id=42,
        root_run_id=42,
        artifact_type="final_answer",
        title=raw_diagnostic,
        payload={"error": raw_diagnostic},
        text=raw_diagnostic,
        uri="debug://raw-diagnostic",
        visibility="public",
        created_at=now,
    )
    session = AsyncMock()
    session.scalars.side_effect = [
        _result([42]),
        _result([run]),
        _result([message]),
        _result([event]),
        _result([artifact]),
        _result([]),
        _result([]),
    ]

    snapshot = await build_agent_trace_snapshot_async(session, run)
    serialized = json.dumps(snapshot)

    assert raw_diagnostic not in serialized
    assert snapshot["run"]["failure"]["message"] == UPSTREAM_FAILED_RUN_MESSAGE
    assert snapshot["thread"]["messages"][0]["content"] == UPSTREAM_FAILED_RUN_MESSAGE
    assert snapshot["events"][0]["payload"]["failure"]["message"] == UPSTREAM_FAILED_RUN_MESSAGE
    assert snapshot["artifacts"][0]["text"] == UPSTREAM_FAILED_RUN_MESSAGE


@pytest.mark.asyncio
async def test_cycle_trace_state_redacts_stored_and_query_diagnostics():
    from brain.systems.runs.cortex.recording import (
        _cycle_payload,
        _cycle_run_payload,
        _trace_cycle_state_async,
    )

    raw_diagnostic = "scheduler database error token=cycle-secret"
    cycle_payload = _cycle_payload(_cycle(last_status="failed", last_error=raw_diagnostic))
    cycle_run_payload = _cycle_run_payload(_cycle_run(status="failed", error=raw_diagnostic))

    class FailingSession:
        async def scalars(self, _stmt):
            raise RuntimeError(raw_diagnostic)

    failed_state = await _trace_cycle_state_async(
        FailingSession(),
        idea_id="idea-1",
        runs=[_run(metadata_={"cycle_id": 7})],
    )
    serialized = json.dumps([cycle_payload, cycle_run_payload, failed_state])

    assert raw_diagnostic not in serialized
    assert cycle_payload["last_error"] == "The last scheduled run did not complete."
    assert cycle_run_payload["error"] == "The scheduled run did not complete."
    assert failed_state["error"] == "Cycle diagnostics were unavailable."


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
        "cycles": {
            "cycles": [{"id": 7, "name": "Weekly sales cycle", "last_status": "completed"}],
            "cycle_runs": [{"id": 8, "cycle_id": 7, "run_id": 42, "status": "completed"}],
            "cycle_count": 1,
            "cycle_run_count": 1,
        },
        "diagnostics": {
            "delivery_signals": [{"source": "artifact", "run_id": 42, "title": "webhook failed"}],
        },
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
    assert manifest["diagnostics"]["cycle_run_count"] == 1
    assert trace["run"]["input_message"] == "Why did Illo answer that?"
    assert activity["item_count"] == 6
    assert any(item["kind"] == "cycle_run" for item in activity["items"])
    assert "trace.json" in readme
    assert "CycleRun" in readme
    assert "not saved as a database artifact" in readme


def test_agent_trace_export_zip_redacts_secret_like_values():
    from brain.systems.runs.cortex.recording import build_agent_trace_export_zip

    snapshot = {
        "schema_version": 1,
        "trace_id": "run:42",
        "run": {
            "run_id": 42,
            "status": "completed",
            "metadata": {"github_token": "ghp_" + "a" * 36},
        },
        "thread": {"messages": []},
        "events": [
            {
                "id": 2,
                "run_id": 42,
                "sequence_no": 1,
                "event_type": "run.tool_started",
                "created_at": "2026-05-12T12:00:02+00:00",
                "payload": {
                    "tool_name": "exec_command",
                    "args": {
                        "cmd": "printf '%s' ghp_" + "b" * 36,
                        "authorization": "Bearer " + "c" * 36,
                    },
                },
            }
        ],
        "tools": [
            {
                "tool_name": "exec_command",
                "args": {"cmd": "gh auth login --with-token <<< ghp_" + "d" * 36},
            }
        ],
        "artifacts": [],
        "cycles": {},
        "diagnostics": {
            "delivery_signals": [
                {
                    "payload": {
                        "api_key": "sk-" + "e" * 36,
                        "message": "token was ghp_" + "f" * 36,
                    }
                }
            ]
        },
    }

    archive_bytes = build_agent_trace_export_zip(snapshot)

    with zipfile.ZipFile(BytesIO(archive_bytes)) as archive:
        trace_text = archive.read("trace.json").decode("utf-8")
        activity_text = archive.read("activity.json").decode("utf-8")

    assert "ghp_" not in trace_text
    assert "ghp_" not in activity_text
    assert "sk-" not in trace_text
    assert "Bearer c" not in trace_text
    assert "[secret redacted]" in trace_text
    assert "[redacted]" in trace_text
