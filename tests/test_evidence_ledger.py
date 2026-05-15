from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch


def test_read_only_introspection_tool_calls_normalize_to_records():
    from brain.systems.runs.evidence import (
        compact_evidence_previews,
        evidence_has_observations,
        normalize_tool_call_evidence,
    )

    records = []
    records.extend(normalize_tool_call_evidence(
        "brain_skills",
        {"task": "inspect evidence system"},
        {
            "task": "inspect evidence system",
            "strategy": "investigate_first",
            "recommended_skills": [{"name": "investigate", "match_score": 0.91}],
            "guardrails": ["read before editing"],
        },
    ))
    records.extend(normalize_tool_call_evidence(
        "skill_view",
        {"name": "investigate", "section": "procedure"},
        "{'name': 'investigate', 'section': 'procedure', 'content': 'Read, inspect, report.'}",
    ))
    records.extend(normalize_tool_call_evidence(
        "semantic_search",
        {"query": "evidence ledger", "scope": "code", "limit": 3},
        {"query": "evidence ledger", "count": 1, "results": [{"source": "code", "path": "brain/systems/runs/evidence.py"}]},
    ))
    records.extend(normalize_tool_call_evidence(
        "file_summary",
        {"path": "brain/systems/runs/evidence.py"},
        {"path": "brain/systems/runs/evidence.py", "line_count": 100, "functions": [{"name": "normalize_tool_call_evidence"}]},
    ))
    records.extend(normalize_tool_call_evidence(
        "search_files",
        {"pattern": "EvidenceRecord", "path": "brain"},
        {"matches": "brain/systems/runs/evidence.py:class EvidenceRecord", "count": 1},
    ))
    records.extend(normalize_tool_call_evidence(
        "list_files",
        {"pattern": "*.py", "path": "brain/systems/runs"},
        {"files": ["evidence.py"], "total": 1, "truncated": False},
    ))

    assert {record["source"] for record in records} == {
        "tool:brain_skills",
        "tool:skill_view",
        "tool:semantic_search",
        "tool:file_summary",
        "tool:search_files",
        "tool:list_files",
    }
    assert all(record["type"] == "evidence_record" for record in records)
    assert all(record["kind"] in {"skill", "search", "file"} for record in records)
    assert evidence_has_observations(records)
    assert compact_evidence_previews(records, limit=2) == [
        {
            "kind": "skill",
            "source": "tool:brain_skills",
            "status": "observed",
            "summary": "brain_skills observed: 1 recommendations for inspect evidence system",
        },
        {
            "kind": "skill",
            "source": "tool:skill_view",
            "status": "observed",
            "summary": "skill_view observed: investigate#procedure",
        },
    ]
    json.dumps(records)


def test_command_file_artifacts_normalize_to_json_safe_records():
    from brain.systems.runs.evidence import normalize_execution_artifacts_evidence, normalize_tool_call_evidence

    records = []
    records.extend(normalize_tool_call_evidence(
        "exec_command",
        {"command": "pytest tests/test_evidence_ledger.py", "working_dir": "/repo"},
        {"exit_code": 0, "stdout": "1 passed", "stderr": ""},
    ))
    records.extend(normalize_execution_artifacts_evidence([
        {
            "type": "command_run",
            "command": "python -m pytest tests/test_evidence_ledger.py",
            "status": "passed",
            "exit_code": 0,
            "summary": "1 passed",
        },
        {
            "type": "file_observation",
            "operation": "read",
            "path": "brain/systems/runs/evidence.py",
            "sha256": "abc",
            "size_bytes": 123,
            "observed_at": object(),
        },
    ]))

    assert [record["kind"] for record in records] == ["command", "command", "file"]
    assert records[0]["details"]["stdout_preview"] == "1 passed"
    assert records[2]["details"]["observed_at"].startswith("<object object")
    json.dumps(records)


def test_evidence_records_are_dedupable():
    from brain.systems.runs.evidence import dedupe_evidence_records, normalize_tool_call_records_evidence

    calls = [
        {
            "tool_name": "brain_recall",
            "args": {"query": "run 1277"},
            "result": {"count": 0, "memories": []},
        },
        {
            "tool_name": "brain_recall",
            "args": {"query": "run 1277"},
            "result": {"count": 0, "memories": []},
        },
    ]

    records = normalize_tool_call_records_evidence(calls)
    assert len(records) == 1
    assert records[0]["status"] == "empty"
    assert dedupe_evidence_records(records + records) == records


def test_file_handlers_persist_read_only_evidence_records(tmp_path):
    from brain.systems.runs import tool_handlers

    target = tmp_path / "notes.txt"
    target.write_text("alpha\nbeta\n", encoding="utf-8")
    persisted: list[dict] = []

    with patch.object(
        tool_handlers,
        "_persist_execution_artifacts",
        side_effect=lambda artifacts, run_id=None: persisted.extend(artifacts),
    ):
        tool_handlers._handle_read_file("notes.txt", _workspace=str(tmp_path))
        tool_handlers._handle_list_files("*.txt", _workspace=str(tmp_path))
        tool_handlers._handle_search_files("alpha", path=".", _workspace=str(tmp_path))

    evidence_records = [artifact for artifact in persisted if artifact.get("type") == "evidence_record"]
    assert {record["source"] for record in evidence_records} >= {
        "tool:read_file",
        "tool:list_files",
        "tool:search_files",
    }
    assert any(artifact.get("type") == "file_observation" for artifact in persisted)
    json.dumps(evidence_records)


async def test_brain_recall_wrapper_persists_evidence_record():
    from brain.systems.runs import tool_handlers

    persisted: list[dict] = []

    async def fake_recall(**kwargs):
        return {
            "memories": [{"id": 42, "type": "lesson"}],
            "count": 1,
            "candidate_count": 1,
        }

    with patch.object(
        tool_handlers,
        "_persist_execution_artifacts",
        side_effect=lambda artifacts, run_id=None: persisted.extend(artifacts),
    ):
        wrapped = tool_handlers._wrap_brain_recall(fake_recall)
        await wrapped(query="run evidence", limit=1)

    assert [record["source"] for record in persisted if record.get("type") == "evidence_record"] == [
        "tool:brain_recall"
    ]
    json.dumps(persisted)


async def test_brain_skill_handlers_persist_evidence_records():
    from brain.systems.runs import tool_handlers

    persisted: list[dict] = []

    with (
        patch("brain.app.mcp.server.async_tool_brain_skills", new=AsyncMock(return_value={
            "task": "inspect evidence",
            "strategy": "investigate_first",
            "recommended_skills": [{"name": "investigate"}],
            "guardrails": [],
        })),
        patch("brain.app.mcp.server.async_tool_skill_view", new=AsyncMock(return_value={
            "name": "investigate",
            "section": "procedure",
            "content": "Read carefully.",
        })),
        patch.object(
            tool_handlers,
            "_persist_execution_artifacts",
            side_effect=lambda artifacts, run_id=None: persisted.extend(artifacts),
        ),
    ):
        handlers = tool_handlers._get_tool_handlers()
        await handlers["brain_skills"](task="inspect evidence")
        await handlers["skill_view"](name="investigate", section="procedure")

    assert [record["source"] for record in persisted if record.get("type") == "evidence_record"] == [
        "tool:brain_skills",
        "tool:skill_view",
    ]
    json.dumps(persisted)
