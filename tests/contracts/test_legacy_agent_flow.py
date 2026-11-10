"""These caller fields come from the legacy schemas, before the new handlers."""

import json
from pathlib import Path

import pytest

from illo.db import one
from tests.mcp.client import AgentClient

CONTRACT = json.loads(Path(__file__).with_name("legacy_agent_calls.json").read_text())


@pytest.mark.parametrize(
    "source_tool,origin",
    [
        ("codex", "codex.memory"),
        ("claude-code", "claude-code.session-preserve"),
    ],
)
async def test_legacy_preserve_receipt_and_recall(app, http, source_tool, origin):
    agent = await AgentClient.create(app, http)
    args = {**CONTRACT["submit"], "source_tool": source_tool, "origin": origin}
    saved = await agent.call("illo_submit", args)
    assert set(CONTRACT["submit_fields"]) <= saved.keys()
    assert saved["status"] == saved["handling_status"] == "completed"
    assert saved["run_id"] is None and saved["error"] is None
    assert saved["event_id"] == saved["submission_id"] == saved["result_id"]
    outcome = saved["ilo_outcome"]
    assert outcome["evidence_status"] == saved["evidence_status"] == "satisfied"
    refs = outcome["mutated_target_refs"]
    assert refs == saved["mutated_target_refs"] and len(refs) == 1
    ref = refs[0]
    assert ref["kind"] == "item" and ref["role"] == "preserved" and ref["visibility"] == "team"
    assert ref["source_ref"] == f"saved:{ref['id']}" == outcome["handle"]

    for alias in ("event_id", "submission_id", "result_id"):
        result = await agent.call("illo_get_result", {alias: saved["event_id"], "compact": True})
        assert set(CONTRACT["result_fields"]) <= result.keys()
        assert result["terminal"] and result["run_status"] == "completed"
        assert result["requires_durable_evidence"] and result["completed_at"]
        assert result["reconciled_at"] and result["final_answer"]
        assert result["evidence_status"] == "satisfied"
        assert result["mutated_target_refs"] == refs and result["failure"] is None

    found = await agent.call(
        "illo_read",
        {
            "capability": "knowledge.search",
            "arguments": {"query": "cobalt", "limit": 500},
        },
    )
    assert set(CONTRACT["search_fields"]) <= found.keys()
    assert found["requested_limit"] == 500 and found["effective_limit"] == 50
    assert found["semantic_available"] is False
    assert found["semantic_degraded_reason"] == "not_configured"
    hit = found["results"][0]
    assert set(CONTRACT["hit_fields"]) <= hit.keys()
    assert {"lexical", "recency", "rrf"} <= hit["scores"].keys()
    assert hit["source_ref"] == ref["source_ref"] and hit["source_created_at"]
    full = await agent.call(
        "illo_read",
        {
            "capability": "knowledge.get",
            "arguments": {"source_ref": hit["source_ref"]},
        },
    )
    assert full["body"] == args["message"] + "\n\n" + args["parts"][0]["text"]
    assert full["meta"]["session_id"] == args["session_id"]
    assert full["meta"]["origin"] == origin
    assert full["meta"]["source_tool"] == source_tool
    assert full["owner"] == agent.person_id
    workspace = await agent.call(
        "illo_read",
        {
            "capability": "workspace.search",
            "arguments": {"query": "cobalt"},
        },
    )
    assert workspace == {"query": "cobalt", "results": found["results"]}
    async with app.state.ctx.pool.connection() as conn:
        assert (await one(conn, "SELECT count(*) AS n FROM runs"))["n"] == 0


async def test_replay_and_mismatch_have_true_evidence(app, http):
    agent = await AgentClient.create(app, http)
    first = await agent.call("illo_submit", CONTRACT["submit"])
    # Aliases normalize to source, and surrounding whitespace is not material.
    args = {
        **CONTRACT["submit"],
        "message": "  " + CONTRACT["submit"]["message"] + "  ",
        "source": {"repo": CONTRACT["submit"]["repo"]},
    }
    replay = await agent.call("illo_submit", args)
    assert replay["idempotent_replay"] and replay["replay_body_matches"]
    assert replay["event_id"] == first["event_id"]
    mismatch = await agent.call("illo_submit", {**args, "message": "A different decision."})
    assert mismatch["idempotent_replay"] and not mismatch["replay_body_matches"]
    assert (
        mismatch["evidence_status"]
        == mismatch["ilo_outcome"]["evidence_status"]
        == "replay_mismatch"
    )
    assert mismatch["mutated_target_refs"] == mismatch["ilo_outcome"]["mutated_target_refs"] == []
    assert mismatch["ilo_outcome"]["reason"] == "idempotency_key_reused_with_different_body"
    stored = await agent.call("illo_get_result", {"event_id": first["event_id"]})
    assert stored["evidence_status"] == "satisfied"
    async with app.state.ctx.pool.connection() as conn:
        assert (await one(conn, "SELECT count(*) AS n FROM items"))["n"] == 1
        assert (await one(conn, "SELECT count(*) AS n FROM submissions"))["n"] == 1


@pytest.mark.parametrize("field", CONTRACT["forbidden_targets"])
async def test_direct_targets_stay_forbidden(app, http, field):
    agent = await AgentClient.create(app, http)
    await agent.call("illo_submit", {**CONTRACT["submit"], field: "forbidden"}, error=True)


async def test_source_wins_and_get_aliases(app, http):
    agent = await AgentClient.create(app, http)
    saved = await agent.call(
        "illo_submit",
        {
            **CONTRACT["submit"],
            "source": {
                "task_title": "Source title",
                "repo": "Acme/source",
                "source_tool": "claude-code",
                "model": "test-model",
            },
        },
    )
    ref = saved["ilo_outcome"]["handle"]
    for arguments in ({"ref": ref}, {"id": ref}, {"id": saved["mutated_target_refs"][0]["id"]}):
        full = await agent.call(
            "illo_read", {"capability": "knowledge.get", "arguments": arguments}
        )
        assert full["title"] == "Source title"
        assert full["meta"]["repo"] == "Acme/source" and full["meta"]["model"] == "test-model"
        assert full["meta"]["source_tool"] == "claude-code"
