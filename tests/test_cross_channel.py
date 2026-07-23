"""Tests for cross-channel session recall at wake.

Uses the transactional db_session fixture — all writes are rolled back after each test.
Zero test data leaks to production DB.
"""

import hashlib
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import text

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 1))))

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DB_URL"),
    reason="TEST_DB_URL not set — run via ops/test-with-db.sh",
)

from brain.app.cli.session_hooks import get_cross_channel_context

ORG_ID = "10000000-0000-0000-0000-000000000001"
USER_ID = "20000000-0000-0000-0000-000000000001"
OTHER_ORG_ID = "10000000-0000-0000-0000-000000000002"
OTHER_USER_ID = "20000000-0000-0000-0000-000000000002"
OTHER_ORG_USER_ID = "20000000-0000-0000-0000-000000000003"


@pytest.fixture
async def seed_memories(db_session, unit_of_work_for_session):
    """Insert source-backed memories inside a rolled-back transaction."""
    now = datetime.now(timezone.utc)
    test_ids = []

    async def ensure_principal(org_id: str, user_id: str, slug: str, email: str) -> None:
        await db_session.execute(text("""
            INSERT INTO orgs (id, name, slug)
            VALUES (:org_id, :name, :slug)
            ON CONFLICT (id) DO NOTHING
        """), {"org_id": org_id, "name": slug, "slug": slug})
        await db_session.execute(text("""
            INSERT INTO users (id, org_id, name, email, role, approved)
            VALUES (:user_id, :org_id, :name, :email, 'owner', TRUE)
            ON CONFLICT (id) DO NOTHING
        """), {"user_id": user_id, "org_id": org_id, "name": email, "email": email})

    async def insert_memory(
        content: str,
        source_session: str,
        created_at: datetime,
        *,
        user_id: str = USER_ID,
        org_id: str = ORG_ID,
        visibility: str = "private",
        salience: float = 7,
    ) -> int:
        source_ref = f"run:cross-channel-test;session:{source_session}"
        source_digest = hashlib.sha256(
            f"{source_ref}:{content}".encode("utf-8")
        ).hexdigest()
        source_result = await db_session.execute(text("""
            INSERT INTO memory_sources (
                org_id, user_id, visibility, source_kind, source_ref,
                content_digest, raw_content, observed_at
            )
            VALUES (
                :org_id, :user_id, :visibility, 'session', :source_ref,
                :content_digest, :content, :created_at
            )
            RETURNING id
        """), {
            "org_id": org_id,
            "user_id": user_id,
            "visibility": visibility,
            "source_ref": source_ref,
            "content_digest": source_digest,
            "content": content,
            "created_at": created_at,
        })
        source_id = source_result.scalar_one()

        span_result = await db_session.execute(text("""
            INSERT INTO memory_spans (source_id, text, content_digest)
            VALUES (:source_id, :content, :content_digest)
            RETURNING id
        """), {
            "source_id": source_id,
            "content": content,
            "content_digest": source_digest,
        })
        source_span_id = span_result.scalar_one()

        result = await db_session.execute(text("""
            INSERT INTO memory_nodes (
                node_kind, content_kind, canonical_label, text, normalized_key,
                confidence, truth_status, freshness_status, created_at, user_id, org_id, visibility
            )
            VALUES (
                'content', 'episode', :content, :content, :normalized_key,
                :confidence, 'active', 'fresh', :created_at, :user_id, :org_id, :visibility
            )
            RETURNING id
        """), {
            "content": content,
            "normalized_key": f"{source_ref}:{content}".lower(),
            "confidence": salience / 10.0,
            "created_at": created_at,
            "user_id": user_id,
            "org_id": org_id,
            "visibility": visibility,
        })
        memory_id = result.scalar_one()

        await db_session.execute(text("""
            INSERT INTO memory_assertions (
                node_id, claim_text, confidence, truth_status, source_span_ids
            )
            VALUES (
                :node_id, :content, :confidence, 'active',
                jsonb_build_array(CAST(:source_span_id AS INTEGER))
            )
        """), {
            "node_id": memory_id,
            "content": content,
            "confidence": salience / 10.0,
            "source_span_id": source_span_id,
        })
        return memory_id

    await ensure_principal(ORG_ID, USER_ID, "cross-channel", "cross-channel@example.com")
    await ensure_principal(ORG_ID, OTHER_USER_ID, "cross-channel-user", "cross-channel-user@example.com")
    await ensure_principal(
        OTHER_ORG_ID,
        OTHER_ORG_USER_ID,
        "cross-channel-other",
        "cross-channel-other@example.com",
    )

    test_ids.append(await insert_memory(
        "discussed deployment strategy",
        "telegram:direct:123",
        now - timedelta(hours=2),
    ))
    test_ids.append(await insert_memory(
        "reviewed PR #42 feedback",
        "discord:channel:456",
        now - timedelta(hours=5),
        visibility="org",
        salience=6,
    ))
    test_ids.append(await insert_memory(
        "current session work",
        "telegram:direct:999",
        now - timedelta(hours=1),
        salience=8,
    ))
    test_ids.append(await insert_memory(
        "old discussion",
        "slack:channel:789",
        now - timedelta(hours=48),
        salience=5,
    ))
    test_ids.append(await insert_memory(
        "other user's private work",
        "discord:channel:private",
        now - timedelta(hours=1),
        user_id=OTHER_USER_ID,
        org_id=ORG_ID,
        salience=9,
    ))
    test_ids.append(await insert_memory(
        "other org shared work",
        "discord:channel:other-org",
        now - timedelta(hours=1),
        user_id=OTHER_ORG_USER_ID,
        org_id=OTHER_ORG_ID,
        visibility="org",
        salience=9,
    ))

    await db_session.flush()
    with patch("brain.app.cli.session_hooks.UnitOfWork", unit_of_work_for_session):
        yield test_ids


class TestGetCrossChannelContext:

    async def test_returns_memories_from_other_sessions(self, seed_memories):
        results = await get_cross_channel_context(
            "telegram:direct:999",
            hours=24,
            user_id=USER_ID,
            org_id=ORG_ID,
        )
        contents = [r["content"] for r in results]
        assert "discussed deployment strategy" in contents
        assert "reviewed PR #42 feedback" in contents

    async def test_excludes_current_session(self, seed_memories):
        results = await get_cross_channel_context(
            "telegram:direct:999",
            hours=24,
            user_id=USER_ID,
            org_id=ORG_ID,
        )
        contents = [r["content"] for r in results]
        assert "current session work" not in contents

    async def test_respects_hours_parameter(self, seed_memories):
        results = await get_cross_channel_context(
            "telegram:direct:999",
            hours=3,
            user_id=USER_ID,
            org_id=ORG_ID,
        )
        contents = [r["content"] for r in results]
        assert "discussed deployment strategy" in contents
        assert "reviewed PR #42 feedback" not in contents

    async def test_excludes_old_memories(self, seed_memories):
        results = await get_cross_channel_context(
            "telegram:direct:999",
            hours=24,
            user_id=USER_ID,
            org_id=ORG_ID,
        )
        contents = [r["content"] for r in results]
        assert "old discussion" not in contents

    async def test_applies_memory_visibility_scope(self, seed_memories):
        results = await get_cross_channel_context(
            "telegram:direct:999",
            hours=24,
            user_id=USER_ID,
            org_id=ORG_ID,
        )
        contents = [r["content"] for r in results]
        assert "other user's private work" not in contents
        assert "other org shared work" not in contents

        global_results = await get_cross_channel_context(
            "telegram:direct:999",
            hours=24,
            user_id=USER_ID,
            org_id=ORG_ID,
            allow_global=True,
        )
        global_contents = [r["content"] for r in global_results]
        assert "other user's private work" in global_contents
        assert "other org shared work" in global_contents
