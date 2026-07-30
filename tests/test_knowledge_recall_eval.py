from __future__ import annotations

from datetime import datetime, timezone
import json

import pytest
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler

from brain.platform.db.models.agent_run import AgentRunRow
from brain.platform.db.models.knowledge import KnowledgeItem
from brain.systems.knowledge.recall_eval import (
    EvidencePointer,
    KnowledgeRecallEvaluationError,
    KnowledgeRecallQuestion,
    KnowledgeRecallQuestionSet,
    load_knowledge_recall_question_set,
    run_knowledge_recall_eval,
)
from brain.systems.knowledge.recall_eval_harvester import (
    harvest_knowledge_recall_candidates,
)
from brain.systems.knowledge.search import (
    KNOWLEDGE_SEARCH_MAX_RESULTS,
    LEXICAL_WEIGHT,
    RECENCY_WEIGHT,
    SEMANTIC_WEIGHT,
)

_ORG_ID = "11111111-1111-4111-8111-111111111111"
_FIXED_TIME = "2026-07-30T12:00:00+00:00"


def _item(item_id: int, source_ref: str, title: str) -> KnowledgeItem:
    now = datetime(2026, 7, 30, tzinfo=timezone.utc)
    return KnowledgeItem(
        id=item_id,
        source="github",
        kind="issue",
        source_ref=source_ref,
        title=title,
        summary=title,
        raw_text=title,
        search_text=title,
        extra={"org_id": _ORG_ID},
        content_digest=f"digest-{item_id}",
        source_created_at=now,
        source_updated_at=now,
        ingested_at=now,
    )


def _search_row(item: KnowledgeItem, *, rank: int) -> dict:
    return {
        "id": item.id,
        "source": item.source,
        "kind": item.kind,
        "source_ref": item.source_ref,
        "title": item.title,
        "summary": item.summary,
        "resolution": item.resolution,
        "entities": list(item.entities or []),
        "extra": dict(item.extra or {}),
        "source_created_at": item.source_created_at.isoformat(),
        "source_updated_at": item.source_updated_at.isoformat(),
        "scores": {
            "rrf": round(1 / (60 + rank), 8),
            "channels": {
                "lexical": {
                    "rank": rank,
                    "score": round(1 / rank, 4),
                    "weight": 1.0,
                    "contribution": round(1 / (60 + rank), 8),
                },
                "semantic": None,
                "recency": {
                    "rank": rank,
                    "score": None,
                    "weight": 0.5,
                    "contribution": round(0.5 / (60 + rank), 8),
                },
            },
        },
    }


def _search_response(
    query: str,
    rows: list[KnowledgeItem],
    *,
    requested_limit: int,
    effective_limit: int | None = None,
    semantic_available: bool = True,
    semantic_degraded_reason: str | None = None,
) -> dict:
    return {
        "query": query,
        "org_id": _ORG_ID,
        "sources": [],
        "kinds": [],
        "semantic_available": semantic_available,
        "semantic_degraded_reason": semantic_degraded_reason,
        "weights": {
            "lexical": LEXICAL_WEIGHT,
            "semantic": SEMANTIC_WEIGHT,
            "recency": RECENCY_WEIGHT,
        },
        "requested_limit": requested_limit,
        "effective_limit": effective_limit or requested_limit,
        "results": [
            _search_row(item, rank=rank)
            for rank, item in enumerate(rows, start=1)
        ],
    }


def _question_set() -> KnowledgeRecallQuestionSet:
    return KnowledgeRecallQuestionSet(
        question_set_id="arithmetic-fixture",
        version="1",
        description="Known ranks for metric arithmetic.",
        cases=(
            KnowledgeRecallQuestion(
                case_id="rank-one",
                question="Where is alpha?",
                acceptable_evidence=(
                    EvidencePointer("github", "github:Illospace/illospace#1"),
                ),
            ),
            KnowledgeRecallQuestion(
                case_id="rank-four",
                question="Where is beta?",
                acceptable_evidence=(
                    EvidencePointer("github", "github:Illospace/illospace#2"),
                    EvidencePointer("github", "github:Illospace/illospace#22"),
                ),
            ),
            KnowledgeRecallQuestion(
                case_id="missed",
                question="Where is gamma?",
                acceptable_evidence=(
                    EvidencePointer("github", "github:Illospace/illospace#3"),
                ),
            ),
        ),
    )


async def test_known_rankings_compute_recall_at_k_and_mrr_with_misses_in_denominator():
    distractors = [
        _item(index + 10, f"github:Illospace/illospace#{index + 10}", f"Noise {index}")
        for index in range(5)
    ]
    rank_one = _item(1, "github:Illospace/illospace#1", "Alpha evidence")
    rank_four = _item(2, "github:Illospace/illospace#22", "Beta evidence")

    async def stub_search(_session, query, *, org_id, limit):
        assert org_id == _ORG_ID
        assert limit == 5
        if query == "Where is alpha?":
            rows = [rank_one, *distractors[:4]]
        elif query == "Where is beta?":
            rows = [*distractors[:3], rank_four, distractors[3]]
        else:
            rows = distractors
        return _search_response(query, rows, requested_limit=limit)

    report = await run_knowledge_recall_eval(
        object(),
        org_id=_ORG_ID,
        question_set=_question_set(),
        k_values=(1, 5),
        search_limit=5,
        search=stub_search,
        generated_at=_FIXED_TIME,
    )
    payload = report.to_dict()

    assert payload["summary"] == {
        "evaluation_valid": True,
        "total": 3,
        "missed": 1,
        "search_errors": 0,
        "semantic_degraded_cases": 0,
        "hits_at_k": {"1": 1, "5": 2},
        "recall_at_k": {"1": 0.33333333, "5": 0.66666667},
        "mean_reciprocal_rank": 0.41666667,
        "mean_reciprocal_rank_cutoff": 5,
    }
    assert payload["results"][1]["best_evidence_rank"] == 4
    assert payload["results"][1]["ranked_results"][3]["matched"] is True
    missed = payload["results"][2]
    assert missed["missed"] is True
    assert missed["best_evidence_rank"] is None
    assert missed["reciprocal_rank"] == 0.0
    assert missed["hits_at_k"] == {"1": False, "5": False}


async def test_report_preserves_channel_attribution_and_marks_semantic_degradation():
    evidence = _item(1, "github:Illospace/illospace#1", "Alpha evidence")

    async def degraded_search(_session, query, *, org_id, limit):
        assert org_id == _ORG_ID
        assert limit == KNOWLEDGE_SEARCH_MAX_RESULTS
        return _search_response(
            query,
            [evidence],
            requested_limit=limit,
            semantic_available=False,
            semantic_degraded_reason="embedding runtime unavailable",
        )

    single_case = KnowledgeRecallQuestionSet(
        question_set_id="degraded",
        version="1",
        description="Semantic degradation fixture.",
        cases=(_question_set().cases[0],),
    )
    payload = (
        await run_knowledge_recall_eval(
            object(),
            org_id=_ORG_ID,
            question_set=single_case,
            search=degraded_search,
            generated_at=_FIXED_TIME,
        )
    ).to_dict()

    assert payload["semantic_available"] is False
    assert payload["semantic_degraded_reason"] == "embedding runtime unavailable"
    assert payload["summary"]["semantic_degraded_cases"] == 1
    case = payload["results"][0]
    assert case["semantic_available"] is False
    assert case["semantic_degraded_reason"] == "embedding runtime unavailable"
    scores = case["ranked_results"][0]["scores"]
    assert scores["rrf"] == 0.01639344
    assert scores["channels"]["lexical"]["rank"] == 1
    assert scores["channels"]["lexical"]["score"] == 1.0
    assert scores["channels"]["semantic"] is None
    assert scores["channels"]["recency"] == {
        "rank": 1,
        "score": None,
        "weight": 0.5,
        "contribution": 0.00819672,
    }


async def test_malformed_search_hit_is_an_evaluation_error_not_a_miss():
    evidence = _item(1, "github:Illospace/illospace#1", "Alpha evidence")

    async def malformed_search(_session, query, *, org_id, limit):
        assert org_id == _ORG_ID
        payload = _search_response(query, [evidence], requested_limit=limit)
        payload["results"][0]["renamed_source_ref"] = payload["results"][0].pop(
            "source_ref"
        )
        return payload

    single_case = KnowledgeRecallQuestionSet(
        question_set_id="malformed",
        version="1",
        description="Producer drift fixture.",
        cases=(_question_set().cases[0],),
    )
    payload = (
        await run_knowledge_recall_eval(
            object(),
            org_id=_ORG_ID,
            question_set=single_case,
            k_values=(1,),
            search_limit=1,
            search=malformed_search,
            generated_at=_FIXED_TIME,
        )
    ).to_dict()

    assert payload["summary"]["evaluation_valid"] is False
    assert payload["summary"]["search_errors"] == 1
    assert payload["summary"]["missed"] == 0
    assert payload["summary"]["recall_at_k"] == {"1": None}
    assert payload["summary"]["mean_reciprocal_rank"] is None
    assert payload["summary"]["mean_reciprocal_rank_cutoff"] is None
    assert payload["results"][0]["missed"] is False
    assert "missing fields: source_ref" in payload["results"][0]["error"]


async def test_eval_rejects_effective_depth_that_cannot_support_requested_k():
    evidence = _item(1, "github:Illospace/illospace#1", "Alpha evidence")

    async def shallow_search(_session, query, *, org_id, limit):
        assert org_id == _ORG_ID
        return _search_response(
            query,
            [evidence],
            requested_limit=limit,
            effective_limit=2,
        )

    single_case = KnowledgeRecallQuestionSet(
        question_set_id="shallow",
        version="1",
        description="Retrieval-depth fixture.",
        cases=(_question_set().cases[0],),
    )
    with pytest.raises(
        KnowledgeRecallEvaluationError,
        match="effective_limit 2 cannot support recall@3",
    ):
        await run_knowledge_recall_eval(
            object(),
            org_id=_ORG_ID,
            question_set=single_case,
            k_values=(3,),
            search_limit=5,
            search=shallow_search,
            generated_at=_FIXED_TIME,
        )


async def test_mrr_cutoff_uses_effective_retrieval_depth():
    evidence = _item(1, "github:Illospace/illospace#1", "Alpha evidence")

    async def shallower_search(_session, query, *, org_id, limit):
        assert org_id == _ORG_ID
        return _search_response(
            query,
            [evidence],
            requested_limit=limit,
            effective_limit=4,
        )

    single_case = KnowledgeRecallQuestionSet(
        question_set_id="shallower",
        version="1",
        description="Actual MRR cutoff fixture.",
        cases=(_question_set().cases[0],),
    )
    payload = (
        await run_knowledge_recall_eval(
            object(),
            org_id=_ORG_ID,
            question_set=single_case,
            k_values=(3,),
            search_limit=5,
            search=shallower_search,
            generated_at=_FIXED_TIME,
        )
    ).to_dict()

    assert payload["configuration"]["requested_search_limit"] == 5
    assert payload["configuration"]["effective_search_limit"] == 4
    assert payload["summary"]["mean_reciprocal_rank_cutoff"] == 4


def test_question_set_is_data_backed_versioned_and_supports_multiple_evidence():
    question_set = load_knowledge_recall_question_set()

    assert question_set.question_set_id == "illospace-knowledge-recall-seed"
    assert question_set.version == "1"
    assert len(question_set.cases) == 5
    assert len(question_set.cases[1].acceptable_evidence) == 3
    assert all(
        case.origin["method"].startswith("hand_seeded")
        for case in question_set.cases
    )
    assert len(question_set.digest) == 64


def test_question_set_rejects_a_case_without_known_best_evidence(tmp_path):
    path = tmp_path / "invalid.json"
    path.write_text(
        json.dumps(
            {
                "question_set_id": "invalid",
                "version": "1",
                "cases": [
                    {
                        "case_id": "missing-ground-truth",
                        "question": "What is missing?",
                        "acceptable_evidence": [],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="at least one pointer"):
        load_knowledge_recall_question_set(path)


@pytest.fixture
async def harvest_session(async_sqlite_session_factory, sqlite_postgres_ddl_patch):
    del sqlite_postgres_ddl_patch
    SQLiteTypeCompiler.visit_UUID = lambda self, type_, **kw: "VARCHAR(36)"
    return await async_sqlite_session_factory(
        [
            AgentRunRow.__table__,
            KnowledgeItem.__table__,
        ]
    )


async def test_harvester_uses_indexed_github_provenance_and_labels_runs_for_review(
    harvest_session,
):
    issue = _item(586, "github:Illospace/illospace#586", "Mirror the produced kind")
    issue.extra = {
        "org_id": _ORG_ID,
        "state": "closed",
        "distillation": {
            "question": "Which memory node kind should the mirror index?",
        },
        "fixing_pull_requests": [
            {
                "repo": "Illospace/illospace",
                "number": 607,
            }
        ],
    }
    issue.resolution = "Resolved by merged PR Illospace/illospace#607"
    harvest_session.add(issue)
    harvest_session.add(
        AgentRunRow(
            id=44,
            org_id=_ORG_ID,
            thread_id="thread-44",
            profile="default",
            recipe="fast",
            status="blocked",
            input_message="Why did the deploy lose all AgentRun capacity?",
            target_ref={},
            workspace_ref={},
            model_policy={},
            metadata_={},
        )
    )
    await harvest_session.flush()

    payload = (
        await harvest_knowledge_recall_candidates(
            harvest_session,
            org_id=_ORG_ID,
            limit_per_source=5,
            generated_at=_FIXED_TIME,
        )
    ).to_dict()

    assert payload["artifact_type"] == "knowledge-recall-candidates"
    assert payload["schema_version"] == 1
    assert "suite" not in payload
    assert payload["summary"] == {
        "total": 2,
        "closed_github_issues": 1,
        "agent_run_transcripts": 1,
        "needs_ground_truth": 1,
    }
    github_candidate, run_candidate = payload["candidates"]
    assert (
        github_candidate["question"]
        == "Which memory node kind should the mirror index?"
    )
    assert github_candidate["acceptable_evidence"] == [
        {
            "source": "github",
            "source_ref": "github:Illospace/illospace#586",
        },
        {
            "source": "github",
            "source_ref": "github:Illospace/illospace#607",
        },
    ]
    assert github_candidate["ground_truth_status"] == "provisional"
    assert run_candidate["question"] == "Why did the deploy lose all AgentRun capacity?"
    assert run_candidate["acceptable_evidence"] == []
    assert run_candidate["ground_truth_status"] == "needs_labeling"


async def test_harvester_caps_run_candidates_independently(
    harvest_session,
):
    for run_id in range(1, 4):
        harvest_session.add(
            AgentRunRow(
                id=run_id,
                org_id=_ORG_ID,
                thread_id=f"thread-{run_id}",
                profile="default",
                recipe="fast",
                status="completed",
                input_message=f"What happened in run {run_id}?",
                target_ref={},
                workspace_ref={},
                model_policy={},
                metadata_={},
            )
        )
    await harvest_session.flush()

    payload = (
        await harvest_knowledge_recall_candidates(
            harvest_session,
            org_id=_ORG_ID,
            limit_per_source=2,
            generated_at=_FIXED_TIME,
        )
    ).to_dict()

    assert payload["summary"]["agent_run_transcripts"] == 2
    assert len(payload["candidates"]) == 2
