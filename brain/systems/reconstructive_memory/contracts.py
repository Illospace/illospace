"""Contracts for reconstructive memory evidence packs."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class EvidenceItem:
    node_id: int
    assertion_id: int | None
    source_span_id: int | None
    role: str
    text: str
    source_text: str | None
    confidence: float
    semantic_score: float | None = None
    lexical_score: float = 0.0
    storage_confidence: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "assertion_id": self.assertion_id,
            "source_span_id": self.source_span_id,
            "role": self.role,
            "text": self.text,
            "source_text": self.source_text,
            "confidence": self.confidence,
            "semantic_score": self.semantic_score,
            "lexical_score": self.lexical_score,
            "storage_confidence": self.storage_confidence,
        }


@dataclass(frozen=True)
class ReconstructionTraceStep:
    action_kind: str
    reason: str
    selected_node_ids: tuple[int, ...] = ()
    output: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_kind": self.action_kind,
            "reason": self.reason,
            "selected_node_ids": list(self.selected_node_ids),
            "output": dict(self.output),
        }


@dataclass(frozen=True)
class EvidencePack:
    reconstruction_run_id: int
    query: str
    confidence: float
    supporting_evidence: tuple[EvidenceItem, ...] = ()
    contradicting_evidence: tuple[EvidenceItem, ...] = ()
    trajectory: tuple[ReconstructionTraceStep, ...] = ()
    unresolved_questions: tuple[str, ...] = ()

    @property
    def answer_context(self) -> list[dict[str, Any]]:
        return [item.to_dict() for item in self.supporting_evidence]

    def to_dict(self) -> dict[str, Any]:
        return {
            "reconstruction_run_id": self.reconstruction_run_id,
            "query": self.query,
            "confidence": self.confidence,
            "answer_context": self.answer_context,
            "supporting_evidence": [item.to_dict() for item in self.supporting_evidence],
            "contradicting_evidence": [item.to_dict() for item in self.contradicting_evidence],
            "trajectory": [step.to_dict() for step in self.trajectory],
            "unresolved_questions": list(self.unresolved_questions),
        }
