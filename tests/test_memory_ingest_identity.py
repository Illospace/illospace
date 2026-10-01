"""Content identity must preserve text, access, and requested visibility."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
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


async def test_different_text_with_same_first_sentence_preserves_both_nodes(session):
    first_text = "Weekly update. The launch is scheduled for Monday."
    second_text = "Weekly update. The launch is scheduled for Friday."
    first = await _ingest(session, first_text)
    second = await _ingest(session, second_text)

    assert first.content_node_id != second.content_node_id
    first_node = await session.get(MemoryNode, first.content_node_id)
    second_node = await session.get(MemoryNode, second.content_node_id)
    assert first_node.text == first_text
    assert second_node.text == second_text
    assert first_node.canonical_label == second_node.canonical_label == "Weekly update."
    assert first_node.truth_status == second_node.truth_status == "active"


async def test_team_request_matching_private_header_is_team_readable(session):
    first = await _ingest(session, "Weekly update. Keep the draft private.")
    shared = await _ingest(
        session, "Weekly update. Share the approved launch plan.", visibility="team",
    )

    node = await session.get(MemoryNode, shared.content_node_id)
    assert node.visibility == "team"
    visible_id = await session.scalar(select(MemoryNode.id).where(
        MemoryNode.id == shared.content_node_id,
        memory_node_visibility_predicate(org_id=_TEST_ORG_ID, user_id=_OTHER_USER_ID),
    ))
    assert visible_id == shared.content_node_id
    assert first.content_node_id != shared.content_node_id


async def test_other_users_ingest_does_not_attach_assertion_to_private_node(session):
    first = await _ingest(session, "Weekly update. Alice's private launch plan.")
    second = await _ingest(
        session, "Weekly update. Bob's private launch plan.", user_id=_OTHER_USER_ID,
    )

    assert second.content_node_id != first.content_node_id
    node = await session.get(MemoryNode, second.content_node_id)
    assert node.user_id == _OTHER_USER_ID
    first_assertions = list(await session.scalars(select(MemoryAssertionNode.id).where(
        MemoryAssertionNode.node_id == first.content_node_id,
    )))
    assert first_assertions == [first.assertion_id]
    second_assertion = await session.get(MemoryAssertionNode, second.assertion_id)
    assert second_assertion.node_id == second.content_node_id


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


async def test_existing_label_key_is_not_backfilled_or_reused(session):
    content = "Weekly update. The launch is scheduled for Monday."
    legacy = await MemoryNodeRepository(session).upsert_node(
        draft=NodeDraft(node_kind="content", canonical_label="Weekly update.", text=content),
        org_id=_TEST_ORG_ID, user_id=_TEST_USER_ID,
    )
    first = await _ingest(session, content)
    repeated = await _ingest(session, content)

    assert first.content_node_id != legacy.id
    assert repeated.content_node_id == first.content_node_id
    assert legacy.normalized_key == "weekly update."
    assert legacy.text == content


async def test_text_edits_cannot_cause_reuse_through_a_stale_digest_key(session):
    content = "Weekly update. The launch is scheduled for Monday."
    first = await _ingest(session, content)
    first_node = await session.get(MemoryNode, first.content_node_id)
    first_node.text = "Weekly update. The launch was moved to Friday."
    second = await _ingest(session, content)
    second_node = await session.get(MemoryNode, second.content_node_id)
    second_node.text = "Weekly update. The launch was moved to Saturday."
    third = await _ingest(session, content)
    repeated = await _ingest(session, content)

    assert len({first.content_node_id, second.content_node_id, third.content_node_id}) == 3
    assert repeated.content_node_id == third.content_node_id
    assert (await session.get(MemoryNode, third.content_node_id)).text == content


async def test_missing_read_context_does_not_reuse_an_ownerless_private_node(session):
    content = "Weekly update. The launch is scheduled for Monday."
    first = await ingest_memory_source(session, content=content)
    second = await ingest_memory_source(session, content=content)

    assert first.content_node_id != second.content_node_id


@pytest.mark.parametrize("reader", ["gate", "checks"])
async def test_duplicate_readers_recognize_digest_keys_and_legacy_keys(session, monkeypatch, reader):
    from brain.systems.quality import checks, gate

    @asynccontextmanager
    async def local_uow():
        yield SimpleNamespace(session=session)

    monkeypatch.setattr(gate, "UnitOfWork", local_uow)
    monkeypatch.setattr(checks, "UnitOfWork", local_uow)
    content = "Weekly update. The launch is scheduled for Monday."
    ingested = await _ingest(session, content)
    legacy = await MemoryNodeRepository(session).upsert_node(
        draft=NodeDraft(node_kind="content", canonical_label="A legacy memory remains readable."),
        org_id=_TEST_ORG_ID, user_id=_TEST_USER_ID,
    )

    async def duplicate_id(value):
        if reader == "gate":
            found = await gate._check_near_duplicate(value)
            return found["id"] if found else None
        found, details = await checks.check_duplicate(
            value, user_id=_TEST_USER_ID, org_id=_TEST_ORG_ID,
        )
        return details["similar_id"] if found else None

    assert await duplicate_id(content.upper()) == ingested.content_node_id
    assert await duplicate_id(legacy.canonical_label) == legacy.id
    assert await duplicate_id("Weekly update. The launch is scheduled for Friday.") is None


async def test_identical_cleaned_text_same_owner_and_visibility_reuses_node(session):
    first = await _ingest(session, "Weekly update. The launch is scheduled for Monday.")
    second = await _ingest(session, "  Weekly update.\nThe launch is scheduled for Monday.  ")

    assert first.content_node_id == second.content_node_id
    assert first.assertion_id != second.assertion_id
    content_ids = list(await session.scalars(select(MemoryNode.id).where(
        MemoryNode.node_kind == "content",
    )))
    assert content_ids == [first.content_node_id]


@pytest.mark.parametrize("visibility", ["team", "org"])
async def test_owner_shares_identical_private_text_with_truthful_receipt_and_mirror(
    session, embedding_runtime, visibility,
):
    content = "Weekly update. The launch is scheduled for Monday."
    first = await _ingest(session, content)
    shared = await _ingest(session, content, visibility=visibility)

    node = await session.get(MemoryNode, shared.content_node_id)
    assert shared.to_dict()["visibility"] == node.visibility == visibility
    assert shared.to_dict()["knowledge_index"] == {"eligible": True, "reason": None}
    assert shared.content_node_id == first.content_node_id
    mirror = await session.scalar(select(KnowledgeItem).where(
        KnowledgeItem.source_ref == f"memory_node:{node.id}",
    ))
    assert mirror is not None
    assert mirror.raw_text == content
    assert mirror.archived_at is None


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


async def test_two_users_can_ingest_identical_private_text_without_key_conflict(session):
    content = "Weekly update. The launch is scheduled for Monday."
    first = await _ingest(session, content)
    second = await _ingest(session, content, user_id=_OTHER_USER_ID)
    repeated = await _ingest(session, content, user_id=_OTHER_USER_ID)
    await session.flush()

    assert first.content_node_id != second.content_node_id
    assert repeated.content_node_id == second.content_node_id
    first_node = await session.get(MemoryNode, first.content_node_id)
    second_node = await session.get(MemoryNode, second.content_node_id)
    assert first_node.user_id == _TEST_USER_ID
    assert second_node.user_id == _OTHER_USER_ID
    assert first_node.visibility == second_node.visibility == "private"
    assert first_node.normalized_key != second_node.normalized_key
    assert first_node.text == second_node.text == content
    first_assertions = list(await session.scalars(select(MemoryAssertionNode.id).where(
        MemoryAssertionNode.node_id == first.content_node_id,
    )))
    assert first_assertions == [first.assertion_id]
