"""Harvest candidate knowledge-recall questions from sources that exist today.

Indexed GitHub, Slack, and shared-memory candidates are harvested from
``knowledge_items`` and carry provisional known-best pointers to their source
rows. GitHub candidates also include any resolving pull requests recorded by
the connector. AgentRun candidates are harvested from old
``agent_runs.input_message`` transcripts, but deliberately carry no known-best
evidence: a curator must attach stable ``source`` + ``source_ref`` pointers
before promoting one into the scored set.

The checked-in seed is hand-seeded from real closed GitHub incidents and their
resolving PRs; it is not claimed to be verbatim query-log output. There is no
knowledge query-log table in the repository, so this harvester does not pretend
to read one and does not introduce a schema change.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.contracts.statuses import (
    LEGACY_AGENT_RUN_STATUS_VALUES,
    TERMINAL_RUN_STATUS_VALUES,
)
from brain.platform.db.models.agent_run import AgentRunRow
from brain.platform.db.models.knowledge import KnowledgeItem
from brain.systems.knowledge.recall_eval import EvidencePointer

_QUESTION_PREFIXES = (
    "can ",
    "could ",
    "did ",
    "do ",
    "does ",
    "how ",
    "is ",
    "should ",
    "what ",
    "when ",
    "where ",
    "which ",
    "who ",
    "why ",
)
_TERMINAL_RUN_STATUSES = (
    *TERMINAL_RUN_STATUS_VALUES,
    *LEGACY_AGENT_RUN_STATUS_VALUES,
)


@dataclass(frozen=True)
class KnowledgeRecallCandidate:
    candidate_id: str
    question: str
    candidate_source: str
    acceptable_evidence: tuple[EvidencePointer, ...]
    ground_truth_status: str
    context: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "question": self.question,
            "candidate_source": self.candidate_source,
            "acceptable_evidence": [
                pointer.to_dict() for pointer in self.acceptable_evidence
            ],
            "ground_truth_status": self.ground_truth_status,
            "context": dict(self.context),
        }


@dataclass(frozen=True)
class KnowledgeRecallCandidateSet:
    generated_at: str
    org_id: str
    candidates: tuple[KnowledgeRecallCandidate, ...]

    def to_dict(self) -> dict[str, Any]:
        github_count = sum(
            candidate.candidate_source == "closed_github_issue"
            for candidate in self.candidates
        )
        run_count = sum(
            candidate.candidate_source == "agent_run_transcript"
            for candidate in self.candidates
        )
        evidence_source_counts: Counter[str] = Counter()
        for candidate in self.candidates:
            evidence_source_counts.update(
                {pointer.source for pointer in candidate.acceptable_evidence}
            )
        return {
            "artifact_type": "knowledge-recall-candidates",
            "schema_version": 1,
            "generated_at": self.generated_at,
            "summary": {
                "total": len(self.candidates),
                "closed_github_issues": github_count,
                "agent_run_transcripts": run_count,
                "needs_ground_truth": sum(
                    candidate.ground_truth_status == "needs_labeling"
                    for candidate in self.candidates
                ),
                "evidence_source_counts": dict(
                    sorted(evidence_source_counts.items())
                ),
            },
            "org_id": self.org_id,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
        }


def _github_evidence(item: KnowledgeItem) -> tuple[EvidencePointer, ...]:
    pointers = [
        EvidencePointer(source=item.source, source_ref=item.source_ref),
    ]
    extra = item.extra if isinstance(item.extra, Mapping) else {}
    for fixing_pr in extra.get("fixing_pull_requests") or []:
        if not isinstance(fixing_pr, Mapping):
            continue
        repo = str(fixing_pr.get("repo") or "").strip()
        try:
            number = int(fixing_pr.get("number") or 0)
        except (TypeError, ValueError):
            continue
        if not repo or number < 1:
            continue
        pointer = EvidencePointer(
            source="github",
            source_ref=f"github:{repo}#{number}",
        )
        if pointer not in pointers:
            pointers.append(pointer)
    return tuple(pointers)


def _single_item_evidence(item: KnowledgeItem) -> tuple[EvidencePointer, ...]:
    return (
        EvidencePointer(source=item.source, source_ref=item.source_ref),
    )


@dataclass(frozen=True)
class _KnowledgeItemSource:
    source: str
    candidate_source: str
    extra_predicates: tuple[Any, ...]
    evidence_builder: Callable[[KnowledgeItem], tuple[EvidencePointer, ...]]


# Exclude skills: connectors/skills.py writes global rows without org scope.
_HARVESTED_KNOWLEDGE_ITEM_SOURCES = (
    _KnowledgeItemSource(
        source="github",
        candidate_source="closed_github_issue",
        extra_predicates=(
            KnowledgeItem.kind == "issue",
            KnowledgeItem.extra["state"].as_string() == "closed",
        ),
        evidence_builder=_github_evidence,
    ),
    _KnowledgeItemSource(
        source="slack",
        candidate_source="slack_knowledge_item",
        extra_predicates=(),
        evidence_builder=_single_item_evidence,
    ),
    _KnowledgeItemSource(
        source="memory",
        candidate_source="memory_knowledge_item",
        extra_predicates=(),
        evidence_builder=_single_item_evidence,
    ),
)


def _knowledge_item_question(item: KnowledgeItem) -> str:
    extra = item.extra if isinstance(item.extra, Mapping) else {}
    distillation = extra.get("distillation")
    if isinstance(distillation, Mapping):
        question = str(distillation.get("question") or "").strip()
        if question:
            return question
    return str(item.title or "").strip()


def _looks_like_question(value: str) -> bool:
    clean = " ".join(value.split()).strip()
    lowered = clean.casefold()
    return bool(clean) and (
        "?" in clean or any(lowered.startswith(prefix) for prefix in _QUESTION_PREFIXES)
    )


async def _harvest_knowledge_item_candidates(
    session: AsyncSession,
    *,
    org_id: str,
    limit: int,
    source: _KnowledgeItemSource,
) -> list[KnowledgeRecallCandidate]:
    items = list(
        (
            await session.scalars(
                select(KnowledgeItem)
                .where(
                    KnowledgeItem.source == source.source,
                    KnowledgeItem.archived_at.is_(None),
                    KnowledgeItem.extra["org_id"].as_string() == org_id,
                    *source.extra_predicates,
                )
                .order_by(
                    KnowledgeItem.source_updated_at.desc(),
                    KnowledgeItem.id.desc(),
                )
                .limit(limit)
            )
        ).all()
    )
    candidates: list[KnowledgeRecallCandidate] = []
    for item in items:
        question = _knowledge_item_question(item)
        if not question:
            continue
        candidates.append(
            KnowledgeRecallCandidate(
                candidate_id=f"knowledge-item:{item.source_ref}",
                question=question,
                candidate_source=source.candidate_source,
                acceptable_evidence=source.evidence_builder(item),
                ground_truth_status="provisional",
                context={
                    "title": item.title,
                    "resolution": item.resolution,
                },
            )
        )
    return candidates


async def harvest_knowledge_recall_candidates(
    session: AsyncSession,
    *,
    org_id: str,
    limit_per_source: int = 25,
    generated_at: str | None = None,
) -> KnowledgeRecallCandidateSet:
    """Return org-scoped reviewable candidates with source-backed evidence."""

    clean_org_id = str(org_id or "").strip()
    if not clean_org_id:
        raise ValueError("org_id is required")
    limit = max(1, min(int(limit_per_source), 200))

    run_rows = list(
        (
            await session.scalars(
                select(AgentRunRow)
                .where(
                    AgentRunRow.org_id == clean_org_id,
                    AgentRunRow.status.in_(_TERMINAL_RUN_STATUSES),
                )
                .order_by(AgentRunRow.created_at.desc(), AgentRunRow.id.desc())
                .limit(limit * 4)
            )
        ).all()
    )

    candidates: list[KnowledgeRecallCandidate] = []
    for source in _HARVESTED_KNOWLEDGE_ITEM_SOURCES:
        candidates.extend(
            await _harvest_knowledge_item_candidates(
                session,
                org_id=clean_org_id,
                limit=limit,
                source=source,
            )
        )
    run_candidates = [
        KnowledgeRecallCandidate(
            candidate_id=f"agent-run:{run.id}",
            question=" ".join(str(run.input_message or "").split()),
            candidate_source="agent_run_transcript",
            acceptable_evidence=(),
            ground_truth_status="needs_labeling",
            context={
                "run_id": run.id,
                "status": run.status,
                "created_at": (
                    run.created_at.isoformat() if run.created_at is not None else None
                ),
            },
        )
        for run in run_rows
        if _looks_like_question(str(run.input_message or ""))
    ][:limit]
    candidates.extend(run_candidates)

    return KnowledgeRecallCandidateSet(
        generated_at=generated_at or datetime.now(timezone.utc).isoformat(),
        org_id=clean_org_id,
        candidates=tuple(candidates),
    )


__all__ = [
    "KnowledgeRecallCandidate",
    "KnowledgeRecallCandidateSet",
    "harvest_knowledge_recall_candidates",
]
