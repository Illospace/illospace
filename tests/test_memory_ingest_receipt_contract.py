"""Memory ingestion visibility and receipt eligibility contract."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from brain.platform.db.models.reconstructive_memory import MemoryNode
from tests.test_reconstructive_memory import _TEST_ORG_ID, _TEST_USER_ID, _session


@pytest.fixture
async def memory_ingest_tool(async_sqlite_session_factory, monkeypatch):
    session = await _session(async_sqlite_session_factory)

    class _PatchedUnitOfWork:
        async def __aenter__(self):
            self.session = session
            return self

        async def __aexit__(self, exc_type, exc, tb):
            if exc_type is None:
                await session.flush()
            return False

    from brain.app.mcp import server as mcp_server

    monkeypatch.setattr(mcp_server, "UnitOfWork", _PatchedUnitOfWork)
    return session, mcp_server


async def test_memory_ingest_source_reports_private_visibility(memory_ingest_tool):
    session, mcp_server = memory_ingest_tool
    payload = await mcp_server.async_tool_memory_ingest_source(
        content="Preserve the source spans for this private memory.",
        user_id=_TEST_USER_ID,
        org_id=_TEST_ORG_ID,
        visibility="private",
    )

    node = await session.get(MemoryNode, payload["content_node_id"])
    assert payload["visibility"] == node.visibility == "private"
    assert payload["knowledge_source_ref"] == f"memory_node:{node.id}"
    assert payload["knowledge_index"] == {"eligible": False, "reason": "private_visibility"}
    assert payload["mutated_target_refs"] == [{
        "kind": "memory_node", "id": node.id, "role": "content",
        "visibility": "private", "knowledge_get": "private_visibility",
    }]
    assert not payload.get("visibility_fallback", False)


async def test_memory_ingest_source_reports_team_visibility(memory_ingest_tool):
    session, mcp_server = memory_ingest_tool
    payload = await mcp_server.async_tool_memory_ingest_source(
        content="Preserve the source spans for this shared team memory.",
        user_id=_TEST_USER_ID,
        org_id=_TEST_ORG_ID,
        visibility="team",
    )

    node = await session.get(MemoryNode, payload["content_node_id"])
    assert payload["visibility"] == node.visibility == "team"
    assert payload["knowledge_source_ref"] == f"memory_node:{node.id}"
    assert payload["knowledge_index"] == {"eligible": True, "reason": None}
    assert payload["mutated_target_refs"] == [{
        "kind": "memory_node", "id": node.id, "role": "content",
        "visibility": "team", "knowledge_get": "eligible",
    }]
    assert not payload.get("visibility_fallback", False)


async def test_memory_ingest_source_reports_invalid_visibility_fallback(memory_ingest_tool):
    session, mcp_server = memory_ingest_tool
    payload = await mcp_server.async_tool_memory_ingest_source(
        content="Preserve the source spans with a safe visibility fallback.",
        user_id=_TEST_USER_ID,
        org_id=_TEST_ORG_ID,
        visibility="invalid",
    )

    node = await session.get(MemoryNode, payload["content_node_id"])
    assert payload["visibility"] == node.visibility == "private"
    assert payload["knowledge_source_ref"] == f"memory_node:{node.id}"
    assert payload["knowledge_index"] == {"eligible": False, "reason": "private_visibility"}
    assert payload["mutated_target_refs"] == [{
        "kind": "memory_node", "id": node.id, "role": "content",
        "visibility": "private", "knowledge_get": "private_visibility",
    }]
    assert payload["visibility_fallback"] is True


def test_ingestion_and_connector_share_index_eligibility_rule():
    from brain.systems.knowledge.connectors import memory as connector
    from brain.systems.reconstructive_memory import ingestion

    # Ingestion always writes node_kind="content"; it cannot produce a cue as
    # the primary node. Keep its classifier identical to the read-side rule.
    assert ingestion.memory_node_index_exclusion_reason is connector.memory_node_index_exclusion_reason


async def test_ingestion_eligibility_observes_supersession_edges(monkeypatch):
    from brain.systems.knowledge.connectors import memory as connector
    from brain.systems.knowledge import memory_eligibility
    from brain.systems.reconstructive_memory import ingestion

    monkeypatch.setattr(memory_eligibility, "load_superseded_by", AsyncMock(return_value={42: 43}))
    node = SimpleNamespace(
        id=42, node_kind="content", visibility="team", archived_at=None, truth_status="active",
    )
    reason = await ingestion.memory_node_index_exclusion_reason(object(), node)
    assert reason == connector.MemoryIndexExclusionReason.ARCHIVED_OR_SUPERSEDED
