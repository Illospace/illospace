"""First-sentence content reuse must respect access and requested visibility."""

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.schema import CreateTable

from brain.platform.db.models.knowledge import KnowledgeItem, KnowledgeItemEmbedding
from brain.platform.db.models.reconstructive_memory import MemoryAssertionNode, MemoryNode
from brain.platform.db.repositories.reconstructive_memory import (
    MemoryNodeRepository,
    NodeDraft,
    memory_node_visibility_predicate,
)
from brain.systems.knowledge.memory_eligibility import memory_node_index_exclusion_reason
from brain.systems.reconstructive_memory.ingestion import ingest_memory_source
from tests.test_knowledge_memory_contract import embedding_runtime
from tests.test_reconstructive_memory import (
    _OTHER_USER_ID,
    _TEST_ORG_ID,
    _TEST_USER_ID,
    _session,
)


@pytest.fixture
async def session(async_sqlite_session_factory, sqlite_postgres_ddl_patch, monkeypatch):
    from brain.systems.reconstructive_memory import embeddings, ingestion

    monkeypatch.setattr(embeddings, "embed_node_texts", AsyncMock())
    monkeypatch.setattr(ingestion, "_index_committed_memory_node", AsyncMock())
    session = await _session(async_sqlite_session_factory)
    for table in (KnowledgeItem.__table__, KnowledgeItemEmbedding.__table__):
        await session.execute(CreateTable(table))
    return session


async def _ingest(session, content, *, user_id=_TEST_USER_ID, visibility="private"):
    return await ingest_memory_source(
        session,
        content=content,
        org_id=_TEST_ORG_ID,
        user_id=user_id,
        visibility=visibility,
        source_kind="manual_note",
    )


async def test_different_text_with_same_first_sentence_reuses_node_and_records_assertion(session):
    first_text = "Weekly update. The launch is scheduled for Monday."
    second_text = "Weekly update. The launch is scheduled for Friday."
    first = await _ingest(session, first_text)
    second = await _ingest(session, second_text)

    assert first.content_node_id == second.content_node_id
    node = await session.get(MemoryNode, first.content_node_id)
    assert node.text == first_text
    assert node.normalized_key == "weekly update."
    assert second.source_id != first.source_id
    assert second.span_ids != first.span_ids
    assert second.assertion_id != first.assertion_id
    assertion = await session.get(MemoryAssertionNode, second.assertion_id)
    assert assertion.node_id == node.id
    assert assertion.claim_text == second_text
    assert second.to_dict()["content_node_reused"] is True
    assert second.to_dict()["content_text_stored"] is False


async def test_identical_cleaned_text_same_owner_and_visibility_reuses_node(session):
    first = await _ingest(session, "Weekly update. The launch is scheduled for Monday.")
    second = await _ingest(session, "  Weekly update.\nThe launch is scheduled for Monday.  ")

    assert first.content_node_id == second.content_node_id
    assert first.assertion_id != second.assertion_id
    assert second.to_dict()["content_node_reused"] is True
    assert second.to_dict()["content_text_stored"] is True
    content_ids = list(await session.scalars(select(MemoryNode.id).where(
        MemoryNode.node_kind == "content",
    )))
    assert content_ids == [first.content_node_id]


async def test_first_ingest_stores_text_without_reusing_node(session):
    content = "Weekly update. The launch is scheduled for Monday."
    result = await _ingest(session, content)

    assert result.to_dict()["content_node_reused"] is False
    assert result.to_dict()["content_text_stored"] is True
    node = await session.get(MemoryNode, result.content_node_id)
    assert node.text == content
    assert node.normalized_key == "weekly update."


async def test_other_users_ingest_does_not_attach_assertion_to_private_node(session):
    first = await _ingest(session, "Weekly update. Alice's private launch plan.")
    second = await _ingest(
        session, "Weekly update. Bob's private launch plan.", user_id=_OTHER_USER_ID,
    )

    assert second.content_node_id != first.content_node_id
    node = await session.get(MemoryNode, second.content_node_id)
    assert node.user_id == _OTHER_USER_ID
    assert node.visibility == "private"
    first_assertions = list(await session.scalars(select(MemoryAssertionNode.id).where(
        MemoryAssertionNode.node_id == first.content_node_id,
    )))
    assert first_assertions == [first.assertion_id]
    second_assertion = await session.get(MemoryAssertionNode, second.assertion_id)
    assert second_assertion.node_id == second.content_node_id
    payload = second.to_dict()
    assert payload["content_node_reused"] is False
    assert payload["content_text_stored"] is True
    assert payload["knowledge_source_ref"] != f"memory_node:{first.content_node_id}"
    assert all(ref["id"] != first.content_node_id for ref in payload["mutated_target_refs"])


async def test_team_request_with_different_text_keeps_private_memory_out_of_shared_index(session):
    private_text = "Weekly update. Keep the draft private."
    shared_text = "Weekly update. Share the approved launch plan."
    first = await _ingest(session, private_text)
    shared = await _ingest(session, shared_text, visibility="team")

    assert first.content_node_id != shared.content_node_id
    private_node = await session.get(MemoryNode, first.content_node_id)
    shared_node = await session.get(MemoryNode, shared.content_node_id)
    assert private_node.visibility == "private"
    assert private_node.text == private_text
    assert shared_node.visibility == shared.to_dict()["visibility"] == "team"
    assert shared_node.text == shared_text
    assert shared.to_dict()["content_node_reused"] is False
    assert shared.to_dict()["content_text_stored"] is True
    assert await memory_node_index_exclusion_reason(session, private_node) == "private_visibility"
    assert await memory_node_index_exclusion_reason(session, shared_node) is None
    assert await session.scalar(select(KnowledgeItem.id).where(
        KnowledgeItem.raw_text == private_text,
        KnowledgeItem.archived_at.is_(None),
    )) is None
    visible_id = await session.scalar(select(MemoryNode.id).where(
        MemoryNode.id == shared.content_node_id,
        memory_node_visibility_predicate(org_id=_TEST_ORG_ID, user_id=_OTHER_USER_ID),
    ))
    assert visible_id == shared.content_node_id


@pytest.mark.parametrize("visibility", ["team", "org"])
async def test_owner_shares_identical_private_text_with_truthful_receipt_and_mirror(
    session, embedding_runtime, monkeypatch, visibility,
):
    from brain.systems.knowledge import service

    reindex = AsyncMock(wraps=service.reindex_updated_memory_node)
    monkeypatch.setattr(service, "reindex_updated_memory_node", reindex)
    content = "Weekly update. The launch is scheduled for Monday."
    first = await _ingest(session, content)
    shared = await _ingest(session, content, visibility=visibility)

    node = await session.get(MemoryNode, shared.content_node_id)
    assert shared.to_dict()["visibility"] == node.visibility == visibility
    assert shared.to_dict()["knowledge_index"] == {"eligible": True, "reason": None}
    assert shared.to_dict()["content_node_reused"] is True
    assert shared.to_dict()["content_text_stored"] is True
    assert shared.content_node_id == first.content_node_id
    reindex.assert_awaited_once_with(session, node=node)
    mirror = await session.scalar(select(KnowledgeItem).where(
        KnowledgeItem.source_ref == f"memory_node:{node.id}",
    ))
    assert mirror is not None
    assert mirror.raw_text == content
    assert mirror.archived_at is None


async def test_private_request_reuses_readable_team_node_without_narrowing_it(session):
    content = "Weekly update. The launch is scheduled for Monday."
    first = await _ingest(session, content, visibility="team")
    second = await _ingest(
        session, "Weekly update. The launch is scheduled for Friday.", user_id=_OTHER_USER_ID,
    )

    assert first.content_node_id == second.content_node_id
    node = await session.get(MemoryNode, first.content_node_id)
    assert node.text == content
    assert node.visibility == second.to_dict()["visibility"] == "team"
    assert second.to_dict()["content_node_reused"] is True
    assert second.to_dict()["content_text_stored"] is False


async def test_repeated_disambiguated_ingest_reuses_accessible_node_with_different_text(session):
    first = await _ingest(session, "Weekly update. Alice's private launch plan.")
    second_text = "Weekly update. Bob's private launch plan."
    second = await _ingest(session, second_text, user_id=_OTHER_USER_ID)
    repeated = await _ingest(
        session, "Weekly update. Bob's revised launch plan.", user_id=_OTHER_USER_ID,
    )
    await session.flush()

    assert repeated.content_node_id == second.content_node_id != first.content_node_id
    node = await session.get(MemoryNode, second.content_node_id)
    assert node.normalized_key == f"weekly update.:{_OTHER_USER_ID}:private"
    assert node.text == second_text
    assert repeated.to_dict()["content_node_reused"] is True
    assert repeated.to_dict()["content_text_stored"] is False
    assert list(await session.scalars(select(MemoryNode.id).where(
        MemoryNode.node_kind == "content",
    ).order_by(MemoryNode.id))) == [first.content_node_id, second.content_node_id]


async def test_two_users_can_ingest_identical_private_text_without_key_conflict(session):
    content = "Weekly update. The launch is scheduled for Monday."
    first = await _ingest(session, content)
    second = await _ingest(session, content, user_id=_OTHER_USER_ID)
    await session.flush()

    assert first.content_node_id != second.content_node_id
    first_node = await session.get(MemoryNode, first.content_node_id)
    second_node = await session.get(MemoryNode, second.content_node_id)
    assert first_node.user_id == _TEST_USER_ID
    assert second_node.user_id == _OTHER_USER_ID
    assert first_node.visibility == second_node.visibility == "private"
    assert first_node.normalized_key != second_node.normalized_key
    assert first_node.text == second_node.text == content
    assert second.to_dict()["content_node_reused"] is False
    assert second.to_dict()["content_text_stored"] is True


async def test_ingests_still_share_cue_and_tag_nodes(session):
    first = await _ingest(session, "Weekly update. The launch is scheduled for Monday.")
    second = await _ingest(
        session, "Weekly update. The launch is scheduled for Friday.", user_id=_OTHER_USER_ID,
    )

    assert first.content_node_id != second.content_node_id
    assert first.tag_node_ids == second.tag_node_ids
    weekly_cue = await session.scalar(select(MemoryNode.id).where(
        MemoryNode.node_kind == "cue", MemoryNode.canonical_label == "weekly",
    ))
    assert weekly_cue in first.cue_node_ids
    assert weekly_cue in second.cue_node_ids


async def test_legacy_first_sentence_key_is_reused(session):
    content = "Weekly update. The launch is scheduled for Monday."
    legacy = await MemoryNodeRepository(session).upsert_node(
        draft=NodeDraft(node_kind="content", canonical_label="Weekly update.", text=content),
        org_id=_TEST_ORG_ID, user_id=_TEST_USER_ID,
    )
    result = await _ingest(session, "Weekly update. The launch is scheduled for Friday.")

    assert result.content_node_id == legacy.id
    assert legacy.normalized_key == "weekly update."
    assert legacy.text == content
    assert result.to_dict()["content_node_reused"] is True
    assert result.to_dict()["content_text_stored"] is False


@pytest.mark.parametrize(
    ("old_visibility", "requested_visibility", "reuse"),
    [("private", "team", False), ("team", "org", False),
     ("org", "team", True), ("team", "private", True)],
)
async def test_other_users_node_is_reused_only_with_sufficient_readable_visibility(
    session, old_visibility, requested_visibility, reuse,
):
    content = "Weekly update. The launch is scheduled for Monday."
    first = await _ingest(session, content, visibility=old_visibility)
    second = await _ingest(
        session, content, user_id=_OTHER_USER_ID, visibility=requested_visibility,
    )

    assert (first.content_node_id == second.content_node_id) is reuse
    first_node = await session.get(MemoryNode, first.content_node_id)
    assert first_node.visibility == old_visibility
    second_node = await session.get(MemoryNode, second.content_node_id)
    assert second_node.visibility == (old_visibility if reuse else requested_visibility)
    assert second_node.user_id == (_TEST_USER_ID if reuse else _OTHER_USER_ID)
    assert second.to_dict()["content_node_reused"] is reuse
    assert second.to_dict()["content_text_stored"] is True


async def test_missing_read_context_does_not_reuse_an_ownerless_private_node(session):
    content = "Weekly update. The launch is scheduled for Monday."
    first = await ingest_memory_source(session, content=content)
    second = await ingest_memory_source(session, content=content)

    assert first.content_node_id != second.content_node_id
