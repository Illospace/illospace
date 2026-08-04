"""Strict serialized contract for knowledge-recall evaluation artifacts."""

from __future__ import annotations

from typing import Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from brain.systems.knowledge.search_contract import KnowledgeSearchScores

KNOWLEDGE_RECALL_ARTIFACT_SCHEMA_VERSION = 2


class _StrictKnowledgeRecallArtifactModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class KnowledgeRecallArtifactQuestionSet(_StrictKnowledgeRecallArtifactModel):
    id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    digest: str = Field(min_length=1)
    case_count: int = Field(ge=0)


class KnowledgeRecallInvalidArtifactConfiguration(
    _StrictKnowledgeRecallArtifactModel
):
    org_id: str = Field(min_length=1)
    k_values: tuple[int, ...]
    requested_search_limit: int


class KnowledgeRecallArtifactConfiguration(
    KnowledgeRecallInvalidArtifactConfiguration
):
    requested_search_limit: int = Field(ge=1)
    effective_search_limit: int = Field(ge=1)


class KnowledgeRecallCorpusFingerprint(_StrictKnowledgeRecallArtifactModel):
    """Digest and diagnostics for all knowledge rows visible to one org."""

    total_item_count: int = Field(ge=0)
    source_counts: dict[str, int]
    newest_source_updated_at: str | None
    newest_ingested_at: str | None
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")

    def changed_fields_from(
        self,
        baseline: KnowledgeRecallCorpusFingerprint,
    ) -> dict[str, dict[str, Any]]:
        """Return diagnostic and digest changes between two corpora."""

        baseline_values = baseline.model_dump(mode="json")
        candidate_values = self.model_dump(mode="json")
        return {
            field_name: {
                "baseline": baseline_value,
                "candidate": candidate_values[field_name],
            }
            for field_name, baseline_value in baseline_values.items()
            if baseline_value != candidate_values[field_name]
        }


class KnowledgeRecallArtifactEvidence(_StrictKnowledgeRecallArtifactModel):
    source: str = Field(min_length=1)
    source_ref: str = Field(min_length=1)


class KnowledgeRecallArtifactRankedResult(_StrictKnowledgeRecallArtifactModel):
    rank: int = Field(ge=1)
    evidence: KnowledgeRecallArtifactEvidence
    kind: str = Field(min_length=1)
    title: str
    matched: bool
    scores: KnowledgeSearchScores


class KnowledgeRecallArtifactCaseResult(_StrictKnowledgeRecallArtifactModel):
    case_id: str = Field(min_length=1)
    question: str = Field(min_length=1)
    acceptable_evidence: tuple[KnowledgeRecallArtifactEvidence, ...]
    origin: dict[str, Any]
    best_evidence_rank: int | None
    missed: bool
    reciprocal_rank: float
    hits_at_k: dict[int, bool]
    semantic_available: bool
    semantic_degraded_reason: str | None
    ranked_results: tuple[KnowledgeRecallArtifactRankedResult, ...]


class KnowledgeRecallArtifactSummary(_StrictKnowledgeRecallArtifactModel):
    total: int = Field(ge=0)
    missed: int = Field(ge=0)
    semantic_degraded_cases: int = Field(ge=0)
    hits_at_k: dict[int, int]
    recall_at_k: dict[int, float]
    mean_reciprocal_rank: float
    mean_reciprocal_rank_cutoff: int = Field(ge=1)


class KnowledgeRecallArtifactCaseError(_StrictKnowledgeRecallArtifactModel):
    case_id: str = Field(min_length=1)
    cause: str = Field(min_length=1)


class _KnowledgeRecallLegacyArtifact(_StrictKnowledgeRecallArtifactModel):
    suite: Literal["knowledge-recall"]
    generated_at: str = Field(min_length=1)
    live_database: bool
    question_set: KnowledgeRecallArtifactQuestionSet


class KnowledgeRecallLegacyValidArtifact(_KnowledgeRecallLegacyArtifact):
    """Strict format emitted before corpus fingerprints were introduced."""

    result_type: Literal["valid"]
    configuration: KnowledgeRecallArtifactConfiguration
    semantic_available: bool
    semantic_degraded_reason: str | None
    summary: KnowledgeRecallArtifactSummary
    results: tuple[KnowledgeRecallArtifactCaseResult, ...]


class KnowledgeRecallV1ValidArtifact(KnowledgeRecallLegacyValidArtifact):
    """Valid artifact emitted before recall engines were recorded."""

    schema_version: Literal[1]
    corpus_fingerprint: KnowledgeRecallCorpusFingerprint


class KnowledgeRecallValidArtifact(KnowledgeRecallV1ValidArtifact):
    """Current valid evaluation artifact."""

    schema_version: Literal[KNOWLEDGE_RECALL_ARTIFACT_SCHEMA_VERSION]
    engine: Literal["knowledge", "memory"]


class KnowledgeRecallLegacyInvalidArtifact(_KnowledgeRecallLegacyArtifact):
    """Strict invalid format emitted before corpus fingerprints existed."""

    result_type: Literal["invalid"]
    configuration: KnowledgeRecallInvalidArtifactConfiguration
    errors: tuple[KnowledgeRecallArtifactCaseError, ...]


class KnowledgeRecallV1InvalidArtifact(KnowledgeRecallLegacyInvalidArtifact):
    """Invalid artifact emitted before recall engines were recorded."""

    schema_version: Literal[1]
    corpus_fingerprint: KnowledgeRecallCorpusFingerprint


class KnowledgeRecallInvalidArtifact(KnowledgeRecallV1InvalidArtifact):
    """Current invalid evaluation artifact."""

    schema_version: Literal[KNOWLEDGE_RECALL_ARTIFACT_SCHEMA_VERSION]
    engine: Literal["knowledge", "memory"]


KnowledgeRecallValidArtifactContract = Union[
    KnowledgeRecallValidArtifact,
    KnowledgeRecallV1ValidArtifact,
    KnowledgeRecallLegacyValidArtifact,
]
KnowledgeRecallArtifact = Union[
    KnowledgeRecallValidArtifact,
    KnowledgeRecallV1ValidArtifact,
    KnowledgeRecallLegacyValidArtifact,
    KnowledgeRecallInvalidArtifact,
    KnowledgeRecallV1InvalidArtifact,
    KnowledgeRecallLegacyInvalidArtifact,
]

_ARTIFACT_ADAPTER = TypeAdapter(KnowledgeRecallArtifact)


def parse_knowledge_recall_artifact_json(payload: str) -> KnowledgeRecallArtifact:
    """Parse one JSON artifact through the canonical strict contract."""

    return _ARTIFACT_ADAPTER.validate_json(payload)


__all__ = [
    "KNOWLEDGE_RECALL_ARTIFACT_SCHEMA_VERSION",
    "KnowledgeRecallArtifact",
    "KnowledgeRecallArtifactCaseError",
    "KnowledgeRecallArtifactCaseResult",
    "KnowledgeRecallArtifactConfiguration",
    "KnowledgeRecallArtifactEvidence",
    "KnowledgeRecallArtifactQuestionSet",
    "KnowledgeRecallArtifactRankedResult",
    "KnowledgeRecallArtifactSummary",
    "KnowledgeRecallCorpusFingerprint",
    "KnowledgeRecallInvalidArtifact",
    "KnowledgeRecallInvalidArtifactConfiguration",
    "KnowledgeRecallLegacyInvalidArtifact",
    "KnowledgeRecallLegacyValidArtifact",
    "KnowledgeRecallV1InvalidArtifact",
    "KnowledgeRecallV1ValidArtifact",
    "KnowledgeRecallValidArtifact",
    "KnowledgeRecallValidArtifactContract",
    "parse_knowledge_recall_artifact_json",
]
