"""Tests for memory DAG, narrative, and health repositories using in-memory SQLite."""
import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine, text, event
from sqlalchemy.dialects.postgresql import JSONB, ARRAY
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
from sqlalchemy.orm import Session

from brain.platform.db.base import Base
from brain.platform.db.models.memory_dag import MemorySummary, SummaryLineage
from brain.platform.db.models.narrative import NarrativeSession, ProjectNarrative
from brain.platform.db.models.memory_health import MemoryHealthLog, RetrievalPoolStats
from brain.platform.db.repositories.memory_dag import MemorySummaryRepository
from brain.platform.db.repositories.narratives import NarrativeRepository
from brain.platform.db.repositories.memory_health import (
    MemoryHealthRepository,
    RetrievalPoolStatsRepository,
)

# Ensure all models are imported so Base.metadata knows about them
import brain.platform.db.models.memory  # noqa: F401
import brain.platform.db.models.org  # noqa: F401


_TEST_USER_ID = "00000000-0000-0000-0000-000000000002"
_TEST_ORG_ID = "00000000-0000-0000-0000-000000000001"


def _patch_sqlite_for_pg_types():
    """Teach SQLiteTypeCompiler to handle PG-specific types as TEXT."""
    for name in ("visit_JSONB", "visit_ARRAY", "visit_VECTOR", "visit_UUID"):
        if not hasattr(SQLiteTypeCompiler, name):
            setattr(SQLiteTypeCompiler, name, lambda self, type_, **kw: "TEXT")

    # Also patch the UUID type's result/bind processors to be pass-through on
    # SQLite, since the default processors try to parse/format real UUIDs.
    from sqlalchemy.dialects.postgresql import UUID as PgUUID
    _orig_result_processor = PgUUID.result_processor
    _orig_bind_processor = PgUUID.bind_processor

    def _noop_result_processor(self, dialect, coltype):
        if dialect.name == "sqlite":
            return None  # no-op: return raw value as-is
        return _orig_result_processor(self, dialect, coltype)

    def _noop_bind_processor(self, dialect):
        if dialect.name == "sqlite":
            return None  # no-op: pass value as-is
        return _orig_bind_processor(self, dialect)

    PgUUID.result_processor = _noop_result_processor
    PgUUID.bind_processor = _noop_bind_processor


@pytest.fixture
def session():
    """In-memory SQLite with manually created tables (no FK enforcement)."""
    _patch_sqlite_for_pg_types()
    eng = create_engine("sqlite://", echo=False)

    # Create stub parent tables with plain TEXT columns
    # (avoids UUID type processor issues on orgs/users PKs)
    with eng.connect() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS orgs (
                id TEXT PRIMARY KEY, name TEXT, slug TEXT, created_at TEXT,
                memory_model_config TEXT, memory_token_budget INTEGER
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY, org_id TEXT, name TEXT, email TEXT,
                color TEXT, role TEXT, password_hash TEXT, vault_salt BLOB,
                attribution_enabled INTEGER, approved INTEGER,
                default_api_key_id INTEGER, created_at TEXT
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL, memory_type TEXT NOT NULL,
                semantic_embedding TEXT, emotional_embedding TEXT,
                salience REAL DEFAULT 5.0, emotion_valence REAL DEFAULT 0.0,
                emotion_arousal REAL DEFAULT 0.0, emotion_label TEXT,
                source TEXT, source_session TEXT, tags TEXT, source_memory_ids TEXT,
                last_accessed TEXT, access_count INTEGER DEFAULT 0,
                decay_eligible INTEGER DEFAULT 1, superseded_by INTEGER,
                memory_tier TEXT DEFAULT 'episodic', consolidated INTEGER DEFAULT 0,
                scope TEXT DEFAULT 'personal', user_id TEXT NOT NULL,
                visibility TEXT DEFAULT 'private', org_id TEXT,
                harvest_type TEXT, harvest_confidence REAL, topic_tags TEXT,
                promoted_at TEXT, promoted_by TEXT,
                truth_status TEXT DEFAULT 'unknown',
                review_status TEXT DEFAULT 'unreviewed',
                confidence REAL DEFAULT 0.5,
                freshness_score REAL DEFAULT 0.5,
                observed_at TEXT,
                staleness_score REAL,
                source_type TEXT DEFAULT 'direct',
                source_kind TEXT,
                source_ref TEXT,
                source_digest TEXT,
                subject_type TEXT,
                subject_ref TEXT,
                valid_from TEXT, valid_until TEXT, policy_kind TEXT,
                policy_scope TEXT, reviewed_at TEXT, reviewed_by TEXT,
                demoted_at TEXT, demotion_reason TEXT,
                archived INTEGER DEFAULT 0,
                created_at TEXT
            )
        """))
        conn.commit()

    # Create ORM-managed tables that don't have PG-specific computed columns
    for name in [
        "memory_summaries", "summary_lineage",
        "project_narratives", "narrative_sessions",
        "memory_health_log", "retrieval_pool_stats",
    ]:
        table = Base.metadata.tables.get(name)
        if table is not None:
            table.create(eng, checkfirst=True)

    s = Session(eng)
    yield s
    s.close()


# ── Helpers ──────────────────────────────────────────────────────

def _make_memory_row(session, content="test memory", *, source_session=None):
    """Insert a memory via raw SQL to avoid ORM ARRAY/UUID issues on SQLite."""
    result = session.execute(
        text("""
            INSERT INTO memories (content, memory_type, user_id, visibility, source_session)
            VALUES (:c, :t, :u, :v, :source_session)
        """),
        {
            "c": content,
            "t": "episodic",
            "u": _TEST_USER_ID,
            "v": "private",
            "source_session": source_session,
        },
    )
    session.flush()
    return result.lastrowid


def _make_summary(repo, session, depth=0, content="summary", **kwargs):
    defaults = dict(
        depth=depth,
        content=content,
        token_count=50,
        user_id=_TEST_USER_ID,
    )
    defaults.update(kwargs)
    s = repo.create(**defaults)
    session.flush()
    return s


# ======================================================================
# MemorySummaryRepository
# ======================================================================


@pytest.fixture
def dag_repo(session):
    return MemorySummaryRepository(session)


def test_list_by_depth(dag_repo, session):
    _make_summary(dag_repo, session, depth=0, content="d0-a")
    _make_summary(dag_repo, session, depth=0, content="d0-b")
    _make_summary(dag_repo, session, depth=1, content="d1-a")

    d0 = dag_repo.list_by_depth(0)
    assert len(d0) == 2

    d1 = dag_repo.list_by_depth(1)
    assert len(d1) == 1
    assert d1[0].content == "d1-a"


def test_list_by_depth_with_org(dag_repo, session):
    _make_summary(dag_repo, session, depth=0, org_id=_TEST_ORG_ID)
    _make_summary(dag_repo, session, depth=0, org_id=None)

    result = dag_repo.list_by_depth(0, org_id=_TEST_ORG_ID)
    assert len(result) == 1


def test_list_by_depth_min_count_met(dag_repo, session):
    _make_summary(dag_repo, session, depth=0)
    _make_summary(dag_repo, session, depth=0)
    _make_summary(dag_repo, session, depth=0)

    result = dag_repo.list_by_depth_min_count(0, min_count=3)
    assert len(result) == 3


def test_list_by_depth_min_count_not_met(dag_repo, session):
    _make_summary(dag_repo, session, depth=0)

    result = dag_repo.list_by_depth_min_count(0, min_count=5)
    assert len(result) == 0


def test_add_child_memory(dag_repo, session):
    summary = _make_summary(dag_repo, session, depth=0)
    mem_id = _make_memory_row(session)
    lineage = dag_repo.add_child_memory(summary.id, mem_id)

    assert lineage.summary_id == summary.id
    assert lineage.child_memory_id == mem_id
    assert lineage.child_summary_id is None


def test_add_child_summary(dag_repo, session):
    parent = _make_summary(dag_repo, session, depth=1, content="parent")
    child = _make_summary(dag_repo, session, depth=0, content="child")
    lineage = dag_repo.add_child_summary(parent.id, child.id)

    assert lineage.summary_id == parent.id
    assert lineage.child_summary_id == child.id
    assert lineage.child_memory_id is None


def test_get_children(dag_repo, session):
    parent = _make_summary(dag_repo, session, depth=1)
    child1 = _make_summary(dag_repo, session, depth=0, content="c1")
    child2 = _make_summary(dag_repo, session, depth=0, content="c2")
    mem_id = _make_memory_row(session)

    dag_repo.add_child_summary(parent.id, child1.id)
    dag_repo.add_child_summary(parent.id, child2.id)
    dag_repo.add_child_memory(parent.id, mem_id)

    children = dag_repo.get_children(parent.id)
    assert len(children) == 3


def test_get_parent_of_memory(dag_repo, session):
    summary = _make_summary(dag_repo, session, depth=0)
    mem_id = _make_memory_row(session)
    dag_repo.add_child_memory(summary.id, mem_id)

    lineage = dag_repo.get_parent_of_memory(mem_id)
    assert lineage is not None
    assert lineage.summary_id == summary.id


def test_get_parent_of_memory_none(dag_repo, session):
    mem_id = _make_memory_row(session)
    assert dag_repo.get_parent_of_memory(mem_id) is None


def test_mark_stale_for_memory_marks_transitive_summaries(dag_repo, session):
    child = _make_summary(dag_repo, session, depth=0, content="child summary")
    parent = _make_summary(dag_repo, session, depth=1, content="parent summary")
    mem_id = _make_memory_row(session)
    dag_repo.add_child_memory(child.id, mem_id)
    dag_repo.add_child_summary(parent.id, child.id)

    count = dag_repo.mark_stale_for_memory(mem_id, "source memory demoted")
    session.refresh(child)
    session.refresh(parent)

    assert count == 2
    assert child.stale_at is not None
    assert parent.stale_at is not None
    assert child.stale_reason == "source memory demoted"


def test_expand_breadcrumb(dag_repo, session):
    m1_id = _make_memory_row(session, content="mem-a")
    m2_id = _make_memory_row(session, content="mem-b")

    results = dag_repo.expand_breadcrumb(0, [m1_id, m2_id])
    assert len(results) == 2


def test_expand_breadcrumb_empty(dag_repo, session):
    assert dag_repo.expand_breadcrumb(0, []) == []


# ======================================================================
# NarrativeRepository
# ======================================================================


@pytest.fixture
def narr_repo(session):
    return NarrativeRepository(session)


def _make_narrative(repo, session, slug="test-topic", **kwargs):
    defaults = dict(
        topic_slug=slug,
        title="Test Narrative",
        arc_summary="An evolving arc",
        user_id=_TEST_USER_ID,
    )
    defaults.update(kwargs)
    n = repo.create(**defaults)
    session.flush()
    return n


def test_mark_narrative_stale_for_memory_source_session(narr_repo, session):
    narrative = _make_narrative(narr_repo, session)
    narr_repo.add_session_entry(
        narrative_id=narrative.id,
        session_id="session-1",
        session_date=datetime.now(timezone.utc),
        summary="Session summary",
    )
    mem_id = _make_memory_row(session, source_session="session-1")

    count = narr_repo.mark_stale_for_memory(mem_id, "source memory quarantined")
    session.refresh(narrative)

    assert count == 1
    assert narrative.stale_at is not None
    assert narrative.stale_reason == "source memory quarantined"


def test_create_and_get_by_slug(narr_repo, session):
    _make_narrative(narr_repo, session, slug="deploy-infra")
    found = narr_repo.get_by_slug("deploy-infra")
    assert found is not None
    assert found.topic_slug == "deploy-infra"


def test_get_by_slug_not_found(narr_repo):
    assert narr_repo.get_by_slug("nonexistent") is None


def test_find_by_topic_fuzzy(narr_repo, session):
    _make_narrative(narr_repo, session, slug="auth-flow", title="Authentication Flow")
    _make_narrative(narr_repo, session, slug="deploy", title="Deployment Pipeline")

    results = narr_repo.find_by_topic_fuzzy("auth")
    assert len(results) == 1
    assert results[0].topic_slug == "auth-flow"


def test_list_active(narr_repo, session):
    _make_narrative(narr_repo, session, slug="a")
    _make_narrative(narr_repo, session, slug="b")
    results = narr_repo.list_active()
    assert len(results) == 2


def test_add_session_entry_and_get_ordered(narr_repo, session):
    narr = _make_narrative(narr_repo, session)

    # Add entries out of order
    d2 = datetime(2026, 3, 15, tzinfo=timezone.utc)
    d1 = datetime(2026, 3, 10, tzinfo=timezone.utc)

    narr_repo.add_session_entry(narr.id, "sess-2", d2, "Second session")
    narr_repo.add_session_entry(narr.id, "sess-1", d1, "First session")

    entries = narr_repo.get_session_entries(narr.id)
    assert len(entries) == 2
    assert entries[0].session_id == "sess-1"  # earlier date first
    assert entries[1].session_id == "sess-2"


# ======================================================================
# MemoryHealthRepository
# ======================================================================


@pytest.fixture
def health_repo(session):
    return MemoryHealthRepository(session)


def test_log_check(health_repo, session):
    entry = health_repo.log_check("orphan_scan", "ok", {"count": 0})
    assert entry.id is not None
    assert entry.check_type == "orphan_scan"
    assert entry.status == "ok"


# ======================================================================
# RetrievalPoolStatsRepository
# ======================================================================


@pytest.fixture
def pool_repo(session):
    return RetrievalPoolStatsRepository(session)


def test_get_pool_ratios_defaults(pool_repo):
    ratios = pool_repo.get_pool_ratios()
    assert ratios == {"recency": 0.60, "semantic": 0.25, "narrative": 0.15}


def test_record_outcome(pool_repo, session):
    row = pool_repo.record_outcome("recency", hit=True)
    assert row.hit_count == 1
    assert row.miss_count == 0

    # Record another outcome in the same window
    row2 = pool_repo.record_outcome("recency", hit=False)
    assert row2.hit_count == 1
    assert row2.miss_count == 1


def test_get_pool_ratios_with_data(pool_repo, session):
    # Record outcomes for all three pools
    pool_repo.record_outcome("recency", hit=True)
    pool_repo.record_outcome("recency", hit=True)
    pool_repo.record_outcome("recency", hit=False)  # 2/3 = 0.667

    pool_repo.record_outcome("semantic", hit=True)
    pool_repo.record_outcome("semantic", hit=False)
    pool_repo.record_outcome("semantic", hit=False)  # 1/3 = 0.333

    pool_repo.record_outcome("narrative", hit=False)
    pool_repo.record_outcome("narrative", hit=False)  # 0/2 = 0.0 -> floor 0.10

    ratios = pool_repo.get_pool_ratios()

    # All values should sum to ~1.0
    assert abs(sum(ratios.values()) - 1.0) < 0.01

    # Recency should have highest ratio, narrative lowest
    assert ratios["recency"] > ratios["semantic"] > ratios["narrative"]

    # Narrative should be at floor (10%)
    assert ratios["narrative"] >= 0.09  # rounding tolerance


# ======================================================================
# MemoryRepository truth maintenance
# ======================================================================


def test_memory_repository_prefers_reviewed_active_and_filters_quarantine(session, monkeypatch):
    from brain.platform.db.repositories.memories import MemoryRepository

    monkeypatch.setenv("MEMORY_QUARANTINE_FILTER_ENABLED", "1")
    repo = MemoryRepository(session)
    uid = _TEST_USER_ID

    reviewed_id = session.execute(
        text("""
            INSERT INTO memories (
                content, memory_type, user_id, visibility, salience,
                truth_status, review_status, confidence, freshness_score, reviewed_at
            ) VALUES (
                :content, :memory_type, :user_id, :visibility, :salience,
                :truth_status, :review_status, :confidence, :freshness_score, :reviewed_at
            )
        """),
        {
            "content": "Reviewed active memory",
            "memory_type": "lesson",
            "user_id": uid,
            "visibility": "private",
            "salience": 4.0,
            "truth_status": "reviewed",
            "review_status": "reviewed",
            "confidence": 0.92,
            "freshness_score": 0.85,
            "reviewed_at": datetime.now(timezone.utc),
        },
    ).lastrowid
    raw_id = session.execute(
        text("""
            INSERT INTO memories (
                content, memory_type, user_id, visibility, salience,
                truth_status, review_status, confidence, freshness_score
            ) VALUES (
                :content, :memory_type, :user_id, :visibility, :salience,
                :truth_status, :review_status, :confidence, :freshness_score
            )
        """),
        {
            "content": "Raw memory",
            "memory_type": "lesson",
            "user_id": uid,
            "visibility": "private",
            "salience": 10.0,
            "truth_status": "unknown",
            "review_status": "unreviewed",
            "confidence": 0.45,
            "freshness_score": 0.7,
        },
    ).lastrowid
    session.execute(
        text("""
            INSERT INTO memories (
                content, memory_type, user_id, visibility, salience,
                truth_status, review_status, confidence, freshness_score,
                demoted_at, valid_until
            ) VALUES (
                :content, :memory_type, :user_id, :visibility, :salience,
                :truth_status, :review_status, :confidence, :freshness_score,
                :demoted_at, :valid_until
            )
        """),
        {
            "content": "Quarantined memory",
            "memory_type": "lesson",
            "user_id": uid,
            "visibility": "private",
            "salience": 9.0,
            "truth_status": "quarantined",
            "review_status": "rejected",
            "confidence": 0.1,
            "freshness_score": 0.1,
            "demoted_at": datetime.now(timezone.utc),
            "valid_until": datetime.now(timezone.utc),
        },
    )
    session.flush()

    results = repo.list_active(limit=10)
    assert [item.content for item in results] == ["Reviewed active memory", "Raw memory"]


def test_truth_snapshot_reports_contradiction_counts(session):
    from brain.platform.db.repositories.memories import MemoryRepository

    repo = MemoryRepository(session)
    uid = _TEST_USER_ID
    memory_id = session.execute(
        text("""
            INSERT INTO memories (
                content, memory_type, user_id, visibility, salience,
                truth_status, review_status, confidence, freshness_score, reviewed_at
            ) VALUES (
                :content, :memory_type, :user_id, :visibility, :salience,
                :truth_status, :review_status, :confidence, :freshness_score, :reviewed_at
            )
        """),
        {
            "content": "Truth snapshot memory",
            "memory_type": "lesson",
            "user_id": uid,
            "visibility": "private",
            "salience": 4.0,
            "truth_status": "reviewed",
            "review_status": "reviewed",
            "confidence": 0.9,
            "freshness_score": 0.8,
            "reviewed_at": datetime.now(timezone.utc),
        },
    ).lastrowid
    session.flush()

    open_contradiction = MagicMock(status="open")
    resolved_contradiction = MagicMock(status="resolved")
    with patch.object(repo, "list_contradictions", return_value=[open_contradiction, resolved_contradiction]), \
        patch.object(repo, "list_reviews", return_value=[]):
        snapshot = repo.get_truth_snapshot(memory_id, include_records=True)

    assert snapshot["state"]["open_contradiction_count"] == 1
    assert snapshot["state"]["resolved_contradiction_count"] == 1
    assert snapshot["state"]["contradiction_status"] == "open"
    assert snapshot["state"]["is_reviewed_active"] is False
