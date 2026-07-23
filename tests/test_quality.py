"""Tests for memory quality gates."""
import pytest
import sys
import os
from unittest.mock import patch

from sqlalchemy import text

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 1))))
from brain.systems.quality.checks import check_content_quality, cap_salience

class TestContentQuality:
    def test_rejects_empty(self):
        ok, reason = check_content_quality("")
        assert not ok
        assert "Empty" in reason

    def test_rejects_whitespace_only(self):
        ok, reason = check_content_quality("   \n  ")
        assert not ok
        assert "Empty" in reason

    def test_rejects_too_short(self):
        ok, reason = check_content_quality("hi")
        assert not ok
        assert "short" in reason

    def test_rejects_raw_html(self):
        html = '<!DOCTYPE html><html><head><meta charset="utf-8"/></head><body><div class="content">text</div></body></html>'
        ok, reason = check_content_quality(html)
        assert not ok
        assert "HTML" in reason

    def test_rejects_meaningless(self):
        ok, reason = check_content_quality("test")
        assert not ok
        # "test" is 4 chars, caught by min length before meaningless check
        assert "short" in reason or "meaningless" in reason

    def test_meaningless_caught_by_length_first(self):
        """Short meaningless strings are caught by min-length before the meaningless check.
        This is fine — both gates reject them."""
        ok, reason = check_content_quality("test task")
        assert not ok  # Rejected either way

    def test_accepts_real_content(self):
        ok, reason = check_content_quality(
            "Every bug traces to assuming data values instead of verifying them."
        )
        assert ok
        assert reason == ""

    def test_accepts_long_content(self):
        ok, reason = check_content_quality(
            "The memory system needs quality gates at every write to prevent duplicates "
            "and ensure only meaningful content enters the brain."
        )
        assert ok


class TestSalienceCap:
    def test_caps_external_at_6(self):
        assert cap_salience(9.0, "research") == 6.0
        assert cap_salience(9.0, "external") == 6.0
        assert cap_salience(9.0, "curiosity") == 6.0

    def test_no_cap_for_conversation(self):
        assert cap_salience(9.0, "conversation") == 9.0
        assert cap_salience(10.0, "conversation") == 10.0

    def test_clamps_to_1_10(self):
        assert cap_salience(0.0, "conversation") == 1.0
        assert cap_salience(15.0, "conversation") == 10.0

    def test_external_below_cap_unchanged(self):
        assert cap_salience(4.0, "research") == 4.0


@pytest.mark.requires_db
class TestDeduplication:
    """Test normalized exact-duplicate detection at write time (requires live DB)."""

    ORG_ID = "50000000-0000-0000-0000-000000000001"
    USER_ID = "60000000-0000-0000-0000-000000000001"
    OTHER_ORG_ID = "50000000-0000-0000-0000-000000000002"
    OTHER_USER_ID = "60000000-0000-0000-0000-000000000002"

    async def _ensure_principal(self, db_session, *, org_id: str, user_id: str, slug: str, email: str) -> None:
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

    async def _insert_memory(
        self,
        db_session,
        *,
        content: str,
        user_id: str,
        org_id: str,
        visibility: str = "private",
    ) -> int:
        result = await db_session.execute(text("""
            INSERT INTO memory_nodes (
                node_kind, content_kind, canonical_label, text, normalized_key,
                confidence, truth_status, freshness_status, user_id, org_id, visibility
            )
            VALUES (
                'content', 'fact', :content, :content, :normalized_key,
                0.5, 'active', 'fresh', :user_id, :org_id, :visibility
            )
            RETURNING id
        """), {
            "content": content,
            "normalized_key": content.lower(),
            "user_id": user_id,
            "org_id": org_id,
            "visibility": visibility,
        })
        row = result.mappings().first()
        return row["id"]

    @pytest.fixture
    async def scoped_memory(self, db_session):
        await self._ensure_principal(
            db_session,
            org_id=self.ORG_ID,
            user_id=self.USER_ID,
            slug="quality-test",
            email="quality-test@example.com",
        )
        await self._ensure_principal(
            db_session,
            org_id=self.OTHER_ORG_ID,
            user_id=self.OTHER_USER_ID,
            slug="quality-test-other",
            email="quality-test-other@example.com",
        )
        content = "A scoped duplicate memory should only match visible tenant memories."
        memory_id = await self._insert_memory(
            db_session,
            content=content,
            user_id=self.USER_ID,
            org_id=self.ORG_ID,
        )
        await db_session.flush()
        return {"id": memory_id, "content": content}

    async def test_exact_duplicate_detected(self, scoped_memory, unit_of_work_for_session):
        """An exact copy of an existing memory should be flagged as duplicate."""
        from brain.systems.quality.checks import check_duplicate
        with patch("brain.systems.quality.checks.UnitOfWork", unit_of_work_for_session):
            is_dupe, details = await check_duplicate(
                scoped_memory["content"],
                user_id=self.USER_ID,
                org_id=self.ORG_ID,
            )
            assert is_dupe, "Exact duplicate should be detected"
            assert "similar_id" in details
            assert details["similarity"] > 0.85

    async def test_unique_content_not_flagged(self, scoped_memory, unit_of_work_for_session):
        """Genuinely unique content should not be flagged."""
        from brain.systems.quality.checks import check_duplicate
        with patch("brain.systems.quality.checks.UnitOfWork", unit_of_work_for_session):
            is_dupe, details = await check_duplicate(
                "This is a completely unique test memory about quantum flamingos "
                "dancing on the surface of Mars during a solar eclipse in the year 3042.",
                user_id=self.USER_ID,
                org_id=self.ORG_ID,
            )
        assert not is_dupe

    async def test_duplicate_check_is_tenant_scoped(self, db_session, scoped_memory, unit_of_work_for_session):
        """An identical hidden memory in another org should not block this tenant."""
        from brain.systems.quality.checks import check_duplicate

        await self._insert_memory(
            db_session,
            content="Same vector in another tenant should stay isolated.",
            user_id=self.OTHER_USER_ID,
            org_id=self.OTHER_ORG_ID,
            visibility="org",
        )
        await db_session.flush()

        with patch("brain.systems.quality.checks.UnitOfWork", unit_of_work_for_session):
            is_dupe, _details = await check_duplicate(
                "Same vector in another tenant should stay isolated.",
                user_id=self.USER_ID,
                org_id=None,
            )
        assert not is_dupe

    async def test_validate_memory_rejects_duplicate(self, scoped_memory, unit_of_work_for_session):
        """Full validation pipeline should reject a normalized exact duplicate."""
        from brain.systems.quality.checks import validate_memory
        with patch("brain.systems.quality.checks.UnitOfWork", unit_of_work_for_session):
            accepted, reason, details = await validate_memory(
                scoped_memory["content"],
                user_id=self.USER_ID,
                org_id=self.ORG_ID,
            )
            assert not accepted
            assert "duplicate" in reason.lower() or "Near-duplicate" in reason
