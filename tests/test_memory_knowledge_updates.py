"""Memory API writes update the shared knowledge mirror before returning."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from brain.app.api.auth import get_current_user
from brain.app.api.deps import rate_limit
from brain.app.api.routers import memory as memory_router
from brain.platform.db.models.knowledge import KnowledgeItem, KnowledgeItemEmbedding
from brain.platform.db.models.reconstructive_memory import MemoryEdgeNode, MemoryNode
from brain.platform.db.repositories import unit_of_work
from brain.systems.knowledge import service
from brain.systems.knowledge.connectors.memory import (
    MemoryConnector,
    MemoryDraftOutcome,
    MemoryDraftSkipReason,
)
from brain.systems.knowledge.search import get_knowledge_items, search_knowledge
from tests.test_knowledge_index import (
    _ORG_ID,
    _memory_node,
    embedding_runtime,
    session,
)


_NODE_ID = 915
_REF = f"memory_node:{_NODE_ID}"
_CONTENT = "The zephyr launch detail is restricted."


@pytest.fixture
async def memory_client(session, embedding_runtime, monkeypatch):
    # Use the production UnitOfWork, including its commit/rollback behavior,
    # with separate sessions on the existing in-memory test database.
    monkeypatch.setattr(
        unit_of_work,
        "SessionFactory",
        async_sessionmaker(session.bind, expire_on_commit=False),
    )
    app = FastAPI()
    app.include_router(memory_router.router)
    app.dependency_overrides[get_current_user] = lambda: {
        "id": "owner",
        "org_id": _ORG_ID,
    }
    app.dependency_overrides[rate_limit] = lambda: None
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        yield client


async def _seed_memory(session, *, visibility="team"):
    session.add(_memory_node(
        _NODE_ID,
        datetime.now(timezone.utc),
        title=_CONTENT,
        text=_CONTENT,
        visibility=visibility,
        user_id="owner",
    ))
    await session.flush()
    await service.index_memory_node(session, node_id=_NODE_ID)
    await session.commit()


async def _set_visibility(client, endpoint, visibility):
    if endpoint == "patch":
        return await client.patch(
            f"/api/memory/{_NODE_ID}", json={"visibility": visibility},
        )
    return await client.post(
        f"/api/memory/{_NODE_ID}/promote", json={"visibility": visibility},
    )


async def _read_as_other_member(session):
    session.expire_all()
    return await get_knowledge_items(
        session, [_REF], org_id=_ORG_ID, user_id="other-member",
    )


async def _assert_withdrawn(session):
    result = await _read_as_other_member(session)
    assert result["results"] == []
    assert result["missing"] == [_REF]
    search = await search_knowledge(session, "zephyr", org_id=_ORG_ID)
    assert search["results"] == []
    mirror = await session.scalar(
        select(KnowledgeItem).where(KnowledgeItem.source_ref == _REF)
    )
    assert mirror.archived_at is not None
    assert mirror.raw_text == ""
    assert mirror.extra["mirror_status"] == "visibility_withdrawn"


async def test_patch_withdraws_memory_from_another_members_get_and_search(
    session, memory_client,
):
    await _seed_memory(session)
    assert (await _read_as_other_member(session))["results"]
    assert (await search_knowledge(session, "zephyr", org_id=_ORG_ID))["results"]

    response = await _set_visibility(memory_client, "patch", "private")

    assert response.status_code == 200, response.text
    assert response.json()["visibility"] == "private"
    await _assert_withdrawn(session)


async def test_promote_withdraws_memory_from_another_members_get_and_search(
    session, memory_client,
):
    await _seed_memory(session)
    assert (await _read_as_other_member(session))["results"]
    assert (await search_knowledge(session, "zephyr", org_id=_ORG_ID))["results"]

    response = await _set_visibility(memory_client, "promote", "private")

    assert response.status_code == 200, response.text
    assert response.json()["visibility"] == "private"
    await _assert_withdrawn(session)


async def test_patch_indexes_a_newly_shared_private_memory(session, memory_client):
    await _seed_memory(session, visibility="private")
    assert (await _read_as_other_member(session))["results"] == []

    response = await _set_visibility(memory_client, "patch", "team")

    assert response.status_code == 200, response.text
    result = await _read_as_other_member(session)
    assert [item["source_ref"] for item in result["results"]] == [_REF]
    assert result["results"][0]["summary"] == _CONTENT


async def test_promote_indexes_a_newly_shared_private_memory(session, memory_client):
    await _seed_memory(session, visibility="private")
    assert (await _read_as_other_member(session))["results"] == []

    response = await _set_visibility(memory_client, "promote", "team")

    assert response.status_code == 200, response.text
    result = await _read_as_other_member(session)
    assert [item["source_ref"] for item in result["results"]] == [_REF]
    assert result["results"][0]["summary"] == _CONTENT


@pytest.mark.parametrize("endpoint", ["patch", "promote"])
@pytest.mark.parametrize("retry_succeeds", [False, True])
async def test_failed_withdrawal_rolls_back_memory_visibility(
    session, memory_client, monkeypatch, endpoint, retry_succeeds,
):
    await _seed_memory(session)
    upsert = service._upsert_item

    async def fail_write(*args, **kwargs):
        if retry_succeeds and write.await_count == 2:
            return await upsert(*args, **kwargs)
        raise RuntimeError("mirror write failed")

    write = AsyncMock(side_effect=fail_write)
    monkeypatch.setattr(service, "_upsert_item", write)

    response = await _set_visibility(memory_client, endpoint, "private")

    assert response.status_code == 500
    # _ingest_drafts reports failure even when the lexical retry succeeds.
    # Both writes must roll back with the visibility change in either case.
    assert write.await_count == 2
    session.expire_all()
    node = await session.get(MemoryNode, _NODE_ID)
    assert node.visibility == "team"
    assert (await _read_as_other_member(session))["results"][0]["summary"] == _CONTENT


@pytest.mark.parametrize("endpoint", ["patch", "promote"])
async def test_failed_shared_index_write_does_not_fail_the_request(
    session, memory_client, monkeypatch, endpoint,
):
    await _seed_memory(session, visibility="private")
    write = AsyncMock(side_effect=RuntimeError("mirror write failed"))
    monkeypatch.setattr(service, "_upsert_item", write)

    response = await _set_visibility(memory_client, endpoint, "team")

    assert response.status_code == 200, response.text
    assert write.await_count == 2
    session.expire_all()
    assert (await session.get(MemoryNode, _NODE_ID)).visibility == "team"


@pytest.mark.parametrize(
    "updates",
    [
        {},
        {"tags": ["unmirrored"]},
        {"content": None, "scope": None},
        {"content": _CONTENT, "visibility": " TEAM ", "scope": " engineering "},
        {"scope": " "},
    ],
)
async def test_patch_without_a_mirrored_change_does_not_index(
    session, memory_client, monkeypatch, updates,
):
    await _seed_memory(session)
    index = AsyncMock()
    monkeypatch.setattr(service, "index_memory_node", index)

    response = await memory_client.patch(f"/api/memory/{_NODE_ID}", json=updates)

    assert response.status_code == 200, response.text
    index.assert_not_awaited()


async def test_patch_content_edit_updates_the_shared_mirror(session, memory_client):
    await _seed_memory(session)
    new_content = "The zephyr launch detail has changed."

    response = await memory_client.patch(
        f"/api/memory/{_NODE_ID}", json={"content": new_content},
    )

    assert response.status_code == 200, response.text
    result = await _read_as_other_member(session)
    assert result["results"][0]["summary"] == new_content
    assert result["results"][0]["title"] == new_content
    search = await search_knowledge(session, "zephyr", org_id=_ORG_ID)
    assert search["results"][0]["summary"] == new_content


async def test_stale_content_patch_cannot_restore_a_private_memory_mirror(
    session, embedding_runtime,
):
    """A content writer must honor another session's committed withdrawal."""
    await _seed_memory(session)
    node = await session.get(MemoryNode, _NODE_ID)
    assert node.visibility == "team"

    factory = async_sessionmaker(session.bind, expire_on_commit=False)
    async with factory() as other_session:
        current = await other_session.get(MemoryNode, _NODE_ID)
        current.visibility = "private"
        await other_session.flush()
        await service.reindex_updated_memory_node(other_session, node=current)
        await other_session.commit()

    assert node.visibility == "team"
    # Follow the content PATCH path with the first session's stale object.
    new_content = "The zephyr launch detail changed after withdrawal."
    node.text = new_content
    node.canonical_label = new_content[:240]
    await session.flush()
    assert await session.scalar(
        select(MemoryNode.visibility).where(MemoryNode.id == _NODE_ID)
    ) == "private"
    await service.reindex_updated_memory_node(session, node=node)
    await session.commit()

    assert node.visibility == "private"
    assert node.text == new_content
    assert node.canonical_label == new_content
    await _assert_withdrawn(session)


async def test_patch_scope_edit_updates_the_shared_mirror(session, memory_client):
    await _seed_memory(session)

    response = await memory_client.patch(
        f"/api/memory/{_NODE_ID}", json={"scope": " launch "},
    )

    assert response.status_code == 200, response.text
    result = await _read_as_other_member(session)
    assert result["results"][0]["extra"]["scope"] == "launch"


async def test_failed_content_index_write_still_saves_the_memory_edit(
    session, memory_client, monkeypatch,
):
    await _seed_memory(session)
    monkeypatch.setattr(
        service, "_upsert_item", AsyncMock(side_effect=RuntimeError("mirror write failed")),
    )
    new_content = "The zephyr launch detail has changed."

    response = await memory_client.patch(
        f"/api/memory/{_NODE_ID}", json={"content": new_content},
    )

    assert response.status_code == 200, response.text
    assert response.json()["content"] == new_content
    session.expire_all()
    assert (await session.get(MemoryNode, _NODE_ID)).text == new_content


async def test_promote_without_a_visibility_change_does_not_index(
    session, memory_client, monkeypatch,
):
    await _seed_memory(session)
    index = AsyncMock()
    monkeypatch.setattr(service, "index_memory_node", index)

    response = await _set_visibility(memory_client, "promote", " TEAM ")

    assert response.status_code == 200, response.text
    index.assert_not_awaited()


@pytest.mark.parametrize("endpoint", ["patch", "promote"])
async def test_withdrawal_never_loads_or_calls_the_embedding_provider(
    session, memory_client, monkeypatch, endpoint,
):
    await _seed_memory(session)
    runtime = AsyncMock(side_effect=AssertionError("must not load embedding runtime"))
    embed = MagicMock(side_effect=AssertionError("must not embed withdrawal"))
    monkeypatch.setattr(service.runtime_settings, "async_get_embedding_runtime_config", runtime)
    monkeypatch.setattr(service.embedding_client, "embed_document", embed)

    response = await _set_visibility(memory_client, endpoint, "private")

    assert response.status_code == 200, response.text
    runtime.assert_not_awaited()
    embed.assert_not_called()
    assert (await _read_as_other_member(session))["results"] == []


@pytest.mark.parametrize("endpoint", ["patch", "promote"])
async def test_resharing_restores_a_withdrawn_mirror(session, memory_client, endpoint):
    await _seed_memory(session)
    assert (await _set_visibility(memory_client, endpoint, "private")).status_code == 200
    await _assert_withdrawn(session)

    response = await _set_visibility(memory_client, endpoint, "org")

    assert response.status_code == 200, response.text
    result = await _read_as_other_member(session)
    assert result["results"][0]["summary"] == _CONTENT


@pytest.mark.parametrize("visibility, expected_status", [("private", 500), ("team", 200)])
async def test_index_query_error_fails_only_when_a_live_mirror_remains(
    session, memory_client, monkeypatch, visibility, expected_status,
):
    initial_visibility = "team" if visibility == "private" else "private"
    await _seed_memory(session, visibility=initial_visibility)

    async def fail_query(session, *, node_id):
        await session.execute(text("SELECT * FROM missing_memory_index_table"))

    monkeypatch.setattr(service, "index_memory_node", fail_query)

    response = await _set_visibility(memory_client, "patch", visibility)

    assert response.status_code == expected_status, response.text
    session.expire_all()
    node = await session.get(MemoryNode, _NODE_ID)
    assert node.visibility == (initial_visibility if expected_status == 500 else visibility)


async def test_shared_content_index_exception_rolls_back_the_edit(
    session, memory_client, monkeypatch,
):
    """An index exception cannot prove a live mirror is safe to retain."""
    await _seed_memory(session)
    index = AsyncMock(side_effect=RuntimeError("connector failed"))
    monkeypatch.setattr(service, "index_memory_node", index)

    response = await memory_client.patch(
        f"/api/memory/{_NODE_ID}", json={"content": "Changed shared content"},
    )

    assert response.status_code == 500
    index.assert_awaited_once()
    session.expire_all()
    node = await session.get(MemoryNode, _NODE_ID)
    assert node.visibility == "team"
    assert node.text == _CONTENT
    assert node.canonical_label == _CONTENT
    assert (await _read_as_other_member(session))["results"][0]["summary"] == _CONTENT


async def test_failed_edge_only_supersession_rolls_back_memory_content(
    session, memory_client, monkeypatch,
):
    await _seed_memory(session)
    replacement_id = _NODE_ID + 1
    session.add(_memory_node(replacement_id, datetime.now(timezone.utc)))
    await session.flush()
    session.add(MemoryEdgeNode(
        source_node_id=_NODE_ID, target_node_id=replacement_id,
        edge_kind="superseded_by", org_id=_ORG_ID,
        visibility="org", created_by="test",
    ))
    await session.commit()
    write = AsyncMock(side_effect=RuntimeError("mirror archive write failed"))
    monkeypatch.setattr(service, "_upsert_item", write)

    response = await memory_client.patch(
        f"/api/memory/{_NODE_ID}", json={"content": "Superseded content edit"},
    )

    assert response.status_code == 500
    assert write.await_count == 2
    assert write.await_args.kwargs["draft"].archived_at is not None
    session.expire_all()
    node = await session.get(MemoryNode, _NODE_ID)
    assert node.truth_status == "active"
    assert node.visibility == "team"
    assert node.text == _CONTENT
    mirror = await session.scalar(
        select(KnowledgeItem).where(KnowledgeItem.source_ref == _REF)
    )
    assert mirror.archived_at is None
    assert mirror.raw_text == _CONTENT


@pytest.mark.parametrize("endpoint", ["patch", "promote"])
async def test_missing_withdrawal_draft_with_a_live_mirror_fails_the_request(
    session, memory_client, monkeypatch, endpoint,
):
    await _seed_memory(session)
    draft = AsyncMock(return_value=MemoryDraftOutcome(
        skip_reason=MemoryDraftSkipReason.NOT_A_CANDIDATE,
    ))
    monkeypatch.setattr(MemoryConnector, "outcome_for_node", draft)

    response = await _set_visibility(memory_client, endpoint, "private")

    assert response.status_code == 500
    draft.assert_awaited_once()
    session.expire_all()
    assert (await session.get(MemoryNode, _NODE_ID)).visibility == "team"
    assert (await _read_as_other_member(session))["results"][0]["summary"] == _CONTENT


@pytest.mark.parametrize("visibility", ["team", "org"])
async def test_orgless_shared_node_keeps_the_connectors_skip_behavior(
    session, embedding_runtime, visibility,
):
    await _seed_memory(session, visibility=visibility)
    node = await session.get(MemoryNode, _NODE_ID)
    node.org_id = None
    node.text = "A shared node without an organization is left to the connector."
    await session.flush()
    stats = await service.index_memory_node(session, node_id=_NODE_ID)
    assert stats.draft is None
    assert stats.skip_reason == MemoryDraftSkipReason.SHARED_WITHOUT_ORG

    await service.reindex_updated_memory_node(session, node=node)

    mirror = await session.scalar(
        select(KnowledgeItem).where(KnowledgeItem.source_ref == _REF)
    )
    assert mirror.archived_at is None
    assert mirror.raw_text == _CONTENT


@pytest.mark.parametrize("index_path", ["immediate", "sweep"])
@pytest.mark.parametrize("retirement", ["withdrawn", "archived"])
async def test_archiving_drafts_skip_embedding_and_retain_existing_vectors(
    session, embedding_runtime, monkeypatch, index_path, retirement,
):
    await _seed_memory(session)
    vectors_query = select(KnowledgeItemEmbedding.id, KnowledgeItemEmbedding.content_digest)
    old_vectors = (await session.execute(vectors_query)).all()
    assert len(old_vectors) == 1
    node = await session.get(MemoryNode, _NODE_ID)
    if retirement == "withdrawn":
        node.visibility = "private"
    else:
        node.archived_at = datetime.now(timezone.utc)
    await session.flush()
    runtime = AsyncMock(side_effect=AssertionError("must not load embedding runtime"))
    embed = MagicMock(side_effect=AssertionError("must not embed archived drafts"))
    monkeypatch.setattr(service.runtime_settings, "async_get_embedding_runtime_config", runtime)
    monkeypatch.setattr(service.embedding_client, "embed_document", embed)

    if index_path == "immediate":
        stats = await service.index_memory_node(session, node_id=_NODE_ID)
        assert stats.failed == 0
        assert stats.draft.archived_at is not None
    else:
        result = await service.sync_connector(session, MemoryConnector())
        assert result.stats["failed"] == 0

    runtime.assert_not_awaited()
    embed.assert_not_called()
    mirror = await session.scalar(
        select(KnowledgeItem).where(KnowledgeItem.source_ref == _REF)
    )
    assert mirror.archived_at is not None
    assert (await session.execute(vectors_query)).all() == old_vectors
    assert (await _read_as_other_member(session))["results"] == []
