"""Tests for memory-DAG models, enums, and schema additions.

Uses in-memory SQLite — creates only the tables under test.
PostgreSQL-specific types (JSONB, UUID, ARRAY) are adapted for SQLite.
"""
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import JSON, String, Text, create_engine, event, inspect, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Session

from brain.platform.db.base import Base
from brain.platform.db.enums import HarvestType, PoolName
from brain.platform.db.models.memory import Memory
from brain.platform.db.models.memory_dag import MemorySummary, SummaryLineage
from brain.platform.db.models.memory_health import MemoryHealthLog, RetrievalPoolStats
from brain.platform.db.models.narrative import NarrativeSession, ProjectNarrative
from brain.platform.db.models.org import Org, User
from brain.platform.db.models.system import RetrievalDecision, RetrievalItemFeedback, RetrievalLog


# ---------------------------------------------------------------------------
# SQLite type adaptations for PG-specific column types
# ---------------------------------------------------------------------------

from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler

# Teach SQLite's type compiler how to render PG-specific types
SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"
SQLiteTypeCompiler.visit_UUID = lambda self, type_, **kw: "VARCHAR(36)"
SQLiteTypeCompiler.visit_ARRAY = lambda self, type_, **kw: "TEXT"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_engine():
    """In-memory SQLite with FK enforcement."""
    eng = create_engine("sqlite://", echo=False)

    @event.listens_for(eng, "connect")
    def _set_fk_pragma(dbapi_conn, _rec):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return eng


def _create_tables(engine, *models):
    """Create only the specified model tables."""
    tables = [m.__table__ for m in models]
    Base.metadata.create_all(engine, tables=tables)


@pytest.fixture()
def session():
    """Session with org + user helper rows pre-created."""
    eng = _make_engine()
    # Only create the specific tables we need (not all — some have PG-only computed cols)
    _create_tables(
        eng, Org, User, Memory,
        MemorySummary, SummaryLineage,
        ProjectNarrative, NarrativeSession,
        MemoryHealthLog, RetrievalPoolStats,
        RetrievalLog,
    )
    sess = Session(eng)
    # Seed an org + user for FK references.
    # Use hex (no dashes) because UUID(as_uuid=False) strips dashes on bind.
    org_id = uuid.uuid4().hex
    user_id = uuid.uuid4().hex
    sess.execute(text(
        "INSERT INTO orgs (id, name, slug) VALUES (:id, :name, :slug)"
    ), {"id": org_id, "name": "Test Org", "slug": "test-org"})
    sess.execute(text(
        "INSERT INTO users (id, org_id, name, email) VALUES (:id, :org_id, :name, :email)"
    ), {"id": user_id, "org_id": org_id, "name": "Tester", "email": "t@test.com"})
    sess.commit()
    sess.info["org_id"] = org_id
    sess.info["user_id"] = user_id
    yield sess
    sess.close()


# ---------------------------------------------------------------------------
# Enum tests
# ---------------------------------------------------------------------------

class TestEnums:
    def test_harvest_type_values(self):
        assert list(HarvestType) == [
            "fact", "decision", "preference", "commitment", "procedure",
            "correction", "lesson", "outcome", "unresolved", "raw_episode"
        ]

    def test_pool_name_values(self):
        assert list(PoolName) == ["exploit", "explore", "narrative"]

    def test_harvest_type_is_str(self):
        assert isinstance(HarvestType.DECISION, str)
        assert HarvestType.LESSON == "lesson"

    def test_pool_name_is_str(self):
        assert isinstance(PoolName.EXPLOIT, str)
        assert PoolName.NARRATIVE == "narrative"


# ---------------------------------------------------------------------------
# Memory column additions
# ---------------------------------------------------------------------------

class TestMemoryNewColumns:
    def test_memory_has_harvest_columns(self):
        """Memory model exposes the new harvest_type, harvest_confidence, topic_tags."""
        mapper = inspect(Memory)
        col_names = {c.key for c in mapper.columns}
        assert "harvest_type" in col_names
        assert "harvest_confidence" in col_names
        assert "topic_tags" in col_names

    def test_memory_has_truth_maintenance_columns(self):
        mapper = inspect(Memory)
        col_names = {c.key for c in mapper.columns}
        expected = {
            "truth_status",
            "review_status",
            "confidence",
            "freshness_score",
            "source_type",
            "source_ref",
            "valid_from",
            "valid_until",
            "policy_kind",
            "policy_scope",
            "reviewed_at",
            "reviewed_by",
            "demoted_at",
            "demotion_reason",
            "memory_tier",
            "consolidated",
        }
        assert expected.issubset(col_names)

    def test_create_memory_with_harvest_fields(self, session):
        uid = session.info["user_id"]
        # Insert via raw SQL to avoid ARRAY default issues with SQLite
        session.execute(text(
            "INSERT INTO memories (content, memory_type, user_id, harvest_type, "
            "harvest_confidence, visibility) "
            "VALUES (:c, :mt, :uid, :ht, :hc, :v)"
        ), {"c": "test decision", "mt": "decision", "uid": uid,
            "ht": "decision", "hc": 0.85, "v": "private"})
        session.flush()
        row = session.execute(text("SELECT harvest_type, harvest_confidence FROM memories LIMIT 1")).fetchone()
        assert row[0] == "decision"
        assert row[1] == 0.85


# ---------------------------------------------------------------------------
# Org column additions
# ---------------------------------------------------------------------------

class TestOrgNewColumns:
    def test_org_has_memory_dag_columns(self):
        mapper = inspect(Org)
        col_names = {c.key for c in mapper.columns}
        assert "memory_model_config" in col_names
        assert "memory_token_budget" in col_names


# ---------------------------------------------------------------------------
# RetrievalLog column addition
# ---------------------------------------------------------------------------

class TestRetrievalLogOrgId:
    def test_retrieval_log_has_org_id(self):
        mapper = inspect(RetrievalLog)
        col_names = {c.key for c in mapper.columns}
        assert "org_id" in col_names


class TestRetrievalDecision:
    def test_retrieval_decision_columns(self):
        mapper = inspect(RetrievalDecision)
        col_names = {c.key for c in mapper.columns}
        assert {"stage", "policy_version", "user_id", "org_id", "selected_item_ids", "suppressed_item_ids", "decision_debug"}.issubset(col_names)


class TestRetrievalItemFeedback:
    def test_retrieval_item_feedback_columns(self):
        mapper = inspect(RetrievalItemFeedback)
        col_names = {c.key for c in mapper.columns}
        assert {"retrieval_decision_id", "memory_id", "summary_id", "user_id", "org_id", "preload_decision", "lazy_load_eligible", "feedback_at"}.issubset(col_names)


class TestMemoryTruthTables:
    def test_contradiction_columns(self):
        from brain.platform.db.models.memory import MemoryContradiction

        mapper = inspect(MemoryContradiction)
        col_names = {c.key for c in mapper.columns}
        assert {"left_memory_id", "right_memory_id", "contradiction_type", "evidence", "status"}.issubset(col_names)

    def test_review_columns(self):
        from brain.platform.db.models.memory import MemoryReview

        mapper = inspect(MemoryReview)
        col_names = {c.key for c in mapper.columns}
        assert {"memory_id", "action", "from_tier", "to_tier", "evidence"}.issubset(col_names)


# ---------------------------------------------------------------------------
# MemorySummary & SummaryLineage
# ---------------------------------------------------------------------------

class TestMemorySummary:
    def test_create_summary(self, session):
        uid = session.info["user_id"]
        oid = session.info["org_id"]
        s = MemorySummary(
            depth=0,
            content="Summary of recent decisions",
            token_count=42,
            user_id=uid,
            org_id=oid,
        )
        session.add(s)
        session.flush()
        assert s.id is not None
        assert s.depth == 0
        assert s.created_at is not None

    def test_summary_defaults(self, session):
        uid = session.info["user_id"]
        s = MemorySummary(
            depth=1,
            content="Higher summary",
            token_count=20,
            user_id=uid,
        )
        session.add(s)
        session.flush()
        assert s.visibility == "private"
        assert s.descendant_count == 0
        assert s.stale_at is None
        assert s.stale_reason is None


def _insert_memory(session, user_id, content="fact", memory_type="fact"):
    """Insert a memory via raw SQL to avoid ARRAY binding issues in SQLite."""
    session.execute(text(
        "INSERT INTO memories (content, memory_type, user_id, visibility) "
        "VALUES (:c, :mt, :uid, 'private')"
    ), {"c": content, "mt": memory_type, "uid": user_id})
    row = session.execute(text("SELECT last_insert_rowid()")).fetchone()
    return row[0]


class TestSummaryLineage:
    def test_lineage_with_child_memory(self, session):
        uid = session.info["user_id"]
        mem_id = _insert_memory(session, uid)
        s = MemorySummary(depth=0, content="sum", token_count=5, user_id=uid)
        session.add(s)
        session.flush()
        link = SummaryLineage(summary_id=s.id, child_memory_id=mem_id)
        session.add(link)
        session.flush()
        assert link.id is not None
        assert link.child_summary_id is None

    def test_lineage_with_child_summary(self, session):
        uid = session.info["user_id"]
        s1 = MemorySummary(depth=0, content="low", token_count=5, user_id=uid)
        s2 = MemorySummary(depth=1, content="high", token_count=10, user_id=uid)
        session.add_all([s1, s2])
        session.flush()
        link = SummaryLineage(summary_id=s2.id, child_summary_id=s1.id)
        session.add(link)
        session.flush()
        assert link.child_memory_id is None

    def test_lineage_check_constraint_both_null_rejected(self, session):
        """Both child columns NULL should violate the CHECK constraint."""
        uid = session.info["user_id"]
        s = MemorySummary(depth=0, content="x", token_count=1, user_id=uid)
        session.add(s)
        session.flush()
        link = SummaryLineage(summary_id=s.id)
        session.add(link)
        with pytest.raises(Exception):
            session.flush()
        session.rollback()

    def test_lineage_check_constraint_both_set_rejected(self, session):
        """Both child columns set should violate the CHECK constraint."""
        uid = session.info["user_id"]
        mem_id = _insert_memory(session, uid)
        s1 = MemorySummary(depth=0, content="a", token_count=1, user_id=uid)
        s2 = MemorySummary(depth=1, content="b", token_count=1, user_id=uid)
        session.add_all([s1, s2])
        session.flush()
        link = SummaryLineage(
            summary_id=s2.id, child_memory_id=mem_id, child_summary_id=s1.id
        )
        session.add(link)
        with pytest.raises(Exception):
            session.flush()
        session.rollback()


# ---------------------------------------------------------------------------
# ProjectNarrative & NarrativeSession
# ---------------------------------------------------------------------------

class TestProjectNarrative:
    def test_create_narrative(self, session):
        uid = session.info["user_id"]
        oid = session.info["org_id"]
        n = ProjectNarrative(
            topic_slug="auth-system",
            title="Auth System Arc",
            arc_summary="We built OAuth, then switched to JWT.",
            user_id=uid,
            org_id=oid,
        )
        session.add(n)
        session.flush()
        assert n.id is not None
        assert n.created_at is not None
        assert n.updated_at is not None
        assert n.visibility == "private"
        assert n.stale_at is None
        assert n.stale_reason is None


class TestNarrativeSession:
    def test_create_narrative_session(self, session):
        uid = session.info["user_id"]
        n = ProjectNarrative(
            topic_slug="deploy",
            title="Deploy Arc",
            arc_summary="Deployment pipeline evolution",
            user_id=uid,
        )
        session.add(n)
        session.flush()
        ns = NarrativeSession(
            narrative_id=n.id,
            session_id="sess-abc-123",
            session_date=datetime.now(timezone.utc),
            summary="Added Docker support",
        )
        session.add(ns)
        session.flush()
        assert ns.id is not None
        assert ns.created_at is not None


# ---------------------------------------------------------------------------
# MemoryHealthLog & RetrievalPoolStats
# ---------------------------------------------------------------------------

class TestMemoryHealthLog:
    def test_create_health_log(self, session):
        oid = session.info["org_id"]
        h = MemoryHealthLog(
            check_type="orphan_scan",
            status="ok",
            details={"orphans_found": 0},
            org_id=oid,
        )
        session.add(h)
        session.flush()
        assert h.id is not None
        assert h.created_at is not None


class TestRetrievalPoolStats:
    def test_create_pool_stats(self, session):
        oid = session.info["org_id"]
        ps = RetrievalPoolStats(
            pool_name="exploit",
            hit_count=10,
            miss_count=2,
            window_start=datetime.now(timezone.utc),
            org_id=oid,
        )
        session.add(ps)
        session.flush()
        assert ps.id is not None
        assert ps.hit_count == 10
