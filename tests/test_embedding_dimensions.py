"""Embedding dimension registry and pgvector typmod policy tests."""

import pytest
from sqlalchemy import inspect

from brain.kernel import config
from brain.platform.db.models.idea import Idea
from brain.platform.db.models.memory import Memory
from brain.platform.db.models.memory_dag import MemorySummary
from brain.platform.db.models.narrative import ProjectNarrative
from brain.platform.db.models.skill import Skill


def _vector_dim(model, column_name: str) -> int:
    column = inspect(model).columns[column_name]
    return column.type.dim


def test_embedding_vector_registry_defines_required_families():
    registry = config.embedding_vector_registry()

    assert set(registry) == {
        "memory.semantic",
        "summary.semantic",
        "narrative.semantic",
        "skill.semantic",
        "skill.task_centroid",
        "idea.embedding",
    }

    shared = config.EMBEDDING_DIM
    assert registry["memory.semantic"].dimensions == shared
    assert registry["summary.semantic"].dimensions == shared
    assert registry["narrative.semantic"].dimensions == shared
    assert registry["skill.semantic"].dimensions == shared
    assert registry["skill.task_centroid"].dimensions == shared

    assert registry["idea.embedding"].dimensions == 1536
    assert registry["idea.embedding"].provider_specific is True


def test_model_vector_dimensions_match_registry():
    assert _vector_dim(Memory, "semantic_embedding") == config.get_embedding_dimension(
        "memory.semantic"
    )
    assert _vector_dim(MemorySummary, "semantic_embedding") == config.get_embedding_dimension(
        "summary.semantic"
    )
    assert _vector_dim(ProjectNarrative, "semantic_embedding") == config.get_embedding_dimension(
        "narrative.semantic"
    )
    assert _vector_dim(Skill, "embedding") == config.get_embedding_dimension("skill.semantic")
    assert _vector_dim(Skill, "task_centroid") == config.get_embedding_dimension(
        "skill.task_centroid"
    )
    assert _vector_dim(Idea, "embedding") == config.get_embedding_dimension("idea.embedding")


def test_vector_type_dimension_parser_handles_schema_qualified_pgvector_types():
    assert config.parse_vector_type_dimension("vector(2000)") == 2000
    assert config.parse_vector_type_dimension("public.vector(1536)") == 1536
    assert config.parse_vector_type_dimension("VECTOR(32)") == 32
    assert config.parse_vector_type_dimension("vector") is None
    assert config.parse_vector_type_dimension(None) is None


class _FakeResult:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _FakeConnection:
    def __init__(self, vector_types: dict[tuple[str, str], str | None]):
        self._vector_types = vector_types

    async def execute(self, _statement, params):
        return _FakeResult((self._vector_types.get((params["table"], params["column"])),))


async def test_validate_embedding_vector_typmods_accepts_registry_matching_database():
    vector_types = {
        (spec.table, spec.column): f"vector({spec.dimensions})"
        for spec in config.embedding_database_vector_specs()
    }

    await config.validate_embedding_vector_typmods(_FakeConnection(vector_types))


async def test_validate_embedding_vector_typmods_rejects_drift_with_clear_message():
    vector_types = {
        (spec.table, spec.column): f"vector({spec.dimensions})"
        for spec in config.embedding_database_vector_specs()
    }
    vector_types[("memory_summaries", "semantic_embedding")] = "vector(1999)"

    with pytest.raises(config.EmbeddingDimensionError) as exc:
        await config.validate_embedding_vector_typmods(_FakeConnection(vector_types))

    message = str(exc.value)
    assert "summary.semantic memory_summaries.semantic_embedding" in message
    assert "expected vector(" in message
    assert "Stage a migration and re-embedding plan" in message


def test_embedding_dim_baseline_uses_registry_models_without_silent_nulling():
    from brain.platform.db.base import Base

    for spec in config.embedding_database_vector_specs():
        column = Base.metadata.tables[spec.table].c[spec.column]
        assert getattr(column.type, "dim", None) == spec.dimensions
