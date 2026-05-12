from sqlalchemy import inspect
from brain.platform.db.models.memory import Memory, Edge, Tag


def test_memory_has_all_baseline_columns():
    cols = {c.name for c in inspect(Memory).columns}
    expected = {
        "id", "content", "memory_type", "semantic_embedding",
        "salience",
        "source", "source_session", "tags", "created_at", "last_accessed",
        "access_count", "decay_eligible", "archived", "superseded_by",
        "memory_tier", "consolidated", "source_memory_ids", "scope",
        "user_id", "visibility", "org_id", "promoted_at", "promoted_by",
        "observed_at", "valid_from", "valid_until", "source_kind",
        "source_digest", "subject_type", "subject_ref", "staleness_score",
    }
    assert cols >= expected, f"Missing: {expected - cols}"


def test_memory_temporal_claim_columns_are_nullable_additive():
    columns = {c.name: c for c in inspect(Memory).columns}
    for name in {
        "observed_at",
        "source_kind",
        "source_digest",
        "subject_type",
        "subject_ref",
        "staleness_score",
    }:
        assert columns[name].nullable is True


def test_memory_tablename():
    assert Memory.__tablename__ == "memories"


def test_edge_columns():
    cols = {c.name for c in inspect(Edge).columns}
    assert cols >= {"id", "source_id", "target_id", "relationship", "weight", "created_at", "last_activated", "activation_count", "auto_generated"}


def test_edge_tablename():
    assert Edge.__tablename__ == "edges"


def test_tag_columns():
    cols = {c.name for c in inspect(Tag).columns}
    assert cols >= {"id", "memory_id", "tag"}


def test_tag_tablename():
    assert Tag.__tablename__ == "tags"
