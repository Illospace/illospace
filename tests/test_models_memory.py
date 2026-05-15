from sqlalchemy import inspect
from brain.platform.db.models.memory import Memory


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
