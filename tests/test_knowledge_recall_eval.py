from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler

from brain.app.cli import knowledge_recall as knowledge_recall_cli
from brain.platform.db.models.agent_run import AgentRunRow
from brain.platform.db.models.knowledge import KnowledgeItem
from brain.systems.knowledge.recall_eval import (
    EvidencePointer,
    KnowledgeRecallCorpusFingerprint,
    KnowledgeRecallInvalidResult,
    KnowledgeRecallQuestion,
    KnowledgeRecallQuestionSet,
    KnowledgeRecallSuiteResult,
    build_knowledge_recall_corpus_fingerprint,
    load_knowledge_recall_question_set,
    run_knowledge_recall_eval,
)
from brain.systems.knowledge.recall_eval_harvester import (
    harvest_knowledge_recall_candidates,
)
from brain.systems.knowledge.search import (
    LEXICAL_WEIGHT,
    RECENCY_WEIGHT,
    SEMANTIC_WEIGHT,
    search_knowledge,
)
from brain.systems.knowledge.search_contract import (
    KNOWLEDGE_SEARCH_MAX_RESULTS,
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


async def test_known_rankings_compute_recall_at_k_and_mrr_with_misses_in_denominator(
    knowledge_session,
):
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
        knowledge_session,
        org_id=_ORG_ID,
        question_set=_question_set(),
        k_values=(1, 5),
        search_limit=5,
        search=stub_search,
        generated_at=_FIXED_TIME,
    )
    assert isinstance(report, KnowledgeRecallSuiteResult)
    payload = report.to_dict()

    assert payload["result_type"] == "valid"
    assert payload["corpus_fingerprint"] == KnowledgeRecallCorpusFingerprint(
        total_item_count=0,
        source_counts=(),
        newest_source_updated_at=None,
        newest_ingested_at=None,
    ).to_dict()
    assert payload["summary"] == {
        "total": 3,
        "missed": 1,
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


async def test_report_preserves_channel_attribution_and_marks_semantic_degradation(
    knowledge_session,
):
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
            knowledge_session,
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


async def test_malformed_search_hit_is_an_evaluation_error_not_a_miss(
    knowledge_session,
):
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
    report = await run_knowledge_recall_eval(
        knowledge_session,
        org_id=_ORG_ID,
        question_set=single_case,
        k_values=(1,),
        search_limit=1,
        search=malformed_search,
        generated_at=_FIXED_TIME,
    )
    assert isinstance(report, KnowledgeRecallInvalidResult)
    payload = report.to_dict()

    assert payload["result_type"] == "invalid"
    assert payload["corpus_fingerprint"] == KnowledgeRecallCorpusFingerprint(
        total_item_count=0,
        source_counts=(),
        newest_source_updated_at=None,
        newest_ingested_at=None,
    ).to_dict()
    assert "summary" not in payload
    assert "results" not in payload
    assert payload["errors"][0]["case_id"] == "rank-one"
    assert "source_ref" in payload["errors"][0]["cause"]
    assert "renamed_source_ref" in payload["errors"][0]["cause"]


async def test_one_search_failure_invalidates_the_whole_question_set(
    knowledge_session,
):
    evidence = _item(1, "github:Illospace/illospace#1", "Alpha evidence")

    async def partly_failing_search(_session, query, *, org_id, limit):
        assert org_id == _ORG_ID
        if query == "Where is beta?":
            raise RuntimeError("database unavailable")
        return _search_response(query, [evidence], requested_limit=limit)

    report = await run_knowledge_recall_eval(
        knowledge_session,
        org_id=_ORG_ID,
        question_set=_question_set(),
        k_values=(1,),
        search_limit=1,
        search=partly_failing_search,
        generated_at=_FIXED_TIME,
    )

    assert isinstance(report, KnowledgeRecallInvalidResult)
    payload = report.to_dict()
    assert "summary" not in payload
    assert "results" not in payload
    assert payload["errors"] == [
        {
            "case_id": "rank-four",
            "cause": "RuntimeError: database unavailable",
        }
    ]


async def test_eval_rejects_effective_depth_that_cannot_support_requested_k(
    knowledge_session,
):
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
    report = await run_knowledge_recall_eval(
        knowledge_session,
        org_id=_ORG_ID,
        question_set=single_case,
        k_values=(3,),
        search_limit=5,
        search=shallow_search,
        generated_at=_FIXED_TIME,
    )

    assert isinstance(report, KnowledgeRecallInvalidResult)
    assert report.to_dict()["errors"] == [
        {
            "case_id": "rank-one",
            "cause": (
                "ValueError: knowledge search effective_limit 2 "
                "cannot support recall@3"
            ),
        }
    ]


async def test_inconsistent_effective_depths_return_one_invalid_result(
    knowledge_session,
):
    evidence = _item(1, "github:Illospace/illospace#1", "Alpha evidence")
    cases = _question_set().cases[:2]

    async def inconsistent_search(_session, query, *, org_id, limit):
        assert org_id == _ORG_ID
        effective_limit = 4 if query == cases[0].question else 5
        return _search_response(
            query,
            [evidence],
            requested_limit=limit,
            effective_limit=effective_limit,
        )

    report = await run_knowledge_recall_eval(
        knowledge_session,
        org_id=_ORG_ID,
        question_set=KnowledgeRecallQuestionSet(
            question_set_id="inconsistent-depth",
            version="1",
            description="Inconsistent retrieval-depth fixture.",
            cases=cases,
        ),
        k_values=(3,),
        search_limit=5,
        search=inconsistent_search,
        generated_at=_FIXED_TIME,
    )

    assert isinstance(report, KnowledgeRecallInvalidResult)
    payload = report.to_dict()
    assert "summary" not in payload
    assert [error["case_id"] for error in payload["errors"]] == [
        "rank-one",
        "rank-four",
    ]
    assert all(
        "observed depths: 4, 5" in error["cause"]
        for error in payload["errors"]
    )


async def test_mrr_cutoff_uses_effective_retrieval_depth(knowledge_session):
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
            knowledge_session,
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


def test_cli_emits_invalid_result_and_returns_nonzero(monkeypatch, capsys):
    invalid_payload = {
        "result_type": "invalid",
        "suite": "knowledge-recall",
        "errors": [
            {
                "case_id": "rank-one",
                "cause": "RuntimeError: database unavailable",
            }
        ],
    }

    async def fake_run(_args):
        return invalid_payload

    monkeypatch.setattr(knowledge_recall_cli, "_run", fake_run)

    exit_code = knowledge_recall_cli.main(["eval", "--org-id", _ORG_ID])

    assert exit_code == 1
    assert json.loads(capsys.readouterr().out) == invalid_payload


def _comparison_artifact(
    *,
    corpus_fingerprint: KnowledgeRecallCorpusFingerprint,
    digest: str = "question-set-digest",
    org_id: str = _ORG_ID,
    k_values: tuple[int, ...] = (3, 10),
    effective_search_limit: int = 50,
    ranks: tuple[int | None, ...] = (1, 4),
    recall_at_k: tuple[float, ...] = (0.5, 1.0),
    mean_reciprocal_rank: float = 0.625,
) -> dict:
    return {
        "result_type": "valid",
        "suite": "knowledge-recall",
        "generated_at": _FIXED_TIME,
        "question_set": {"digest": digest},
        "configuration": {
            "org_id": org_id,
            "k_values": list(k_values),
            "requested_search_limit": effective_search_limit,
            "effective_search_limit": effective_search_limit,
        },
        "corpus_fingerprint": corpus_fingerprint.to_dict(),
        "summary": {
            "recall_at_k": {
                str(k): value for k, value in zip(k_values, recall_at_k, strict=True)
            },
            "mean_reciprocal_rank": mean_reciprocal_rank,
        },
        "results": [
            {
                "case_id": f"case-{index}",
                "best_evidence_rank": rank,
            }
            for index, rank in enumerate(ranks, start=1)
        ],
    }


def _write_artifact(path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_compare_labels_same_corpus_metric_change_as_ranking_attributable(
    tmp_path,
    capsys,
):
    corpus = KnowledgeRecallCorpusFingerprint(
        total_item_count=2,
        source_counts=(("github", 2),),
        newest_source_updated_at="2026-07-30T12:00:00+00:00",
        newest_ingested_at="2026-07-30T12:05:00+00:00",
    )
    baseline_path = tmp_path / "baseline.json"
    candidate_path = tmp_path / "candidate.json"
    _write_artifact(baseline_path, _comparison_artifact(corpus_fingerprint=corpus))
    _write_artifact(
        candidate_path,
        _comparison_artifact(
            corpus_fingerprint=corpus,
            ranks=(4, 2),
            recall_at_k=(0.5, 1.0),
            mean_reciprocal_rank=0.375,
        ),
    )

    exit_code = knowledge_recall_cli.main(
        [
            "compare",
            "--baseline",
            str(baseline_path),
            "--candidate",
            str(candidate_path),
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["comparability"]["verdict"] == "ranking-attributable"
    assert payload["comparability"]["differences"] == []
    assert payload["case_rank_deltas"][0]["best_evidence_rank"] == {
        "baseline": 1,
        "candidate": 4,
        "delta": 3,
        "change": "rank-changed",
    }
    assert payload["metric_deltas"]["mean_reciprocal_rank"]["delta"] == -0.25


def test_compare_labels_changed_corpus_and_names_changed_fields(tmp_path, capsys):
    baseline_corpus = KnowledgeRecallCorpusFingerprint(
        total_item_count=2,
        source_counts=(("github", 2),),
        newest_source_updated_at="2026-07-30T12:00:00+00:00",
        newest_ingested_at="2026-07-30T12:05:00+00:00",
    )
    candidate_corpus = KnowledgeRecallCorpusFingerprint(
        total_item_count=3,
        source_counts=(("github", 2), ("slack", 1)),
        newest_source_updated_at="2026-07-30T12:00:00+00:00",
        newest_ingested_at="2026-07-31T09:00:00+00:00",
    )
    baseline_path = tmp_path / "baseline.json"
    candidate_path = tmp_path / "candidate.json"
    _write_artifact(
        baseline_path,
        _comparison_artifact(corpus_fingerprint=baseline_corpus),
    )
    _write_artifact(
        candidate_path,
        _comparison_artifact(
            corpus_fingerprint=candidate_corpus,
            ranks=(2, 6),
            recall_at_k=(0.5, 1.0),
            mean_reciprocal_rank=0.33333333,
        ),
    )

    exit_code = knowledge_recall_cli.main(
        [
            "compare",
            "--baseline",
            str(baseline_path),
            "--candidate",
            str(candidate_path),
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    comparability = payload["comparability"]
    assert comparability["verdict"] == "corpus-attributable"
    assert comparability["differences"] == ["corpus_fingerprint"]
    assert set(comparability["corpus_changed_fields"]) == {
        "total_item_count",
        "source_counts",
        "newest_ingested_at",
        "fingerprint",
    }
    assert payload["metric_deltas"]["mean_reciprocal_rank"]["delta"] == -0.29166667


def test_compare_refuses_question_set_digest_mismatch(tmp_path, capsys):
    corpus = KnowledgeRecallCorpusFingerprint(
        total_item_count=0,
        source_counts=(),
        newest_source_updated_at=None,
        newest_ingested_at=None,
    )
    baseline_path = tmp_path / "baseline.json"
    candidate_path = tmp_path / "candidate.json"
    _write_artifact(baseline_path, _comparison_artifact(corpus_fingerprint=corpus))
    _write_artifact(
        candidate_path,
        _comparison_artifact(
            corpus_fingerprint=corpus,
            digest="different-question-set-digest",
        ),
    )

    exit_code = knowledge_recall_cli.main(
        [
            "compare",
            "--baseline",
            str(baseline_path),
            "--candidate",
            str(candidate_path),
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert payload["comparability"]["verdict"] == "not-comparable"
    assert payload["comparability"]["differences"] == ["question_set_digest"]
    assert "metric_deltas" not in payload
    assert "case_rank_deltas" not in payload


def test_compare_refuses_invalid_artifact(tmp_path, capsys):
    corpus = KnowledgeRecallCorpusFingerprint(
        total_item_count=0,
        source_counts=(),
        newest_source_updated_at=None,
        newest_ingested_at=None,
    )
    baseline_path = tmp_path / "baseline.json"
    candidate_path = tmp_path / "candidate.json"
    invalid_baseline = _comparison_artifact(corpus_fingerprint=corpus)
    invalid_baseline["result_type"] = "invalid"
    invalid_baseline.pop("summary")
    invalid_baseline.pop("results")
    invalid_baseline["errors"] = [
        {"case_id": "case-1", "cause": "RuntimeError: search failed"}
    ]
    _write_artifact(baseline_path, invalid_baseline)
    _write_artifact(candidate_path, _comparison_artifact(corpus_fingerprint=corpus))

    exit_code = knowledge_recall_cli.main(
        [
            "compare",
            "--baseline",
            str(baseline_path),
            "--candidate",
            str(candidate_path),
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert payload["comparability"]["verdict"] == "invalid-artifact"
    assert "baseline artifact is invalid" in payload["comparability"]["reason"]
    assert "metric_deltas" not in payload
    assert "case_rank_deltas" not in payload


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
async def knowledge_session(async_sqlite_session_factory, sqlite_postgres_ddl_patch):
    del sqlite_postgres_ddl_patch
    SQLiteTypeCompiler.visit_UUID = lambda self, type_, **kw: "VARCHAR(36)"
    return await async_sqlite_session_factory(
        [
            AgentRunRow.__table__,
            KnowledgeItem.__table__,
        ]
    )


async def test_corpus_fingerprint_uses_the_same_visible_rows_as_search(
    knowledge_session,
    monkeypatch,
):
    now = datetime(2026, 7, 30, 12, tzinfo=timezone.utc)
    visible_github = _item(1, "visible-github", "Shared corpus token one")
    visible_slack = _item(2, "visible-slack", "Shared corpus token two")
    visible_slack.source = "slack"
    visible_slack.source_updated_at = now + timedelta(hours=1)
    visible_slack.ingested_at = now + timedelta(hours=2)

    archived = _item(3, "archived", "Shared corpus token archived")
    archived.archived_at = now + timedelta(days=1)
    archived.source_updated_at = now + timedelta(days=10)
    archived.ingested_at = now + timedelta(days=10)

    other_org = _item(4, "other-org", "Shared corpus token other org")
    other_org.extra = {"org_id": "22222222-2222-4222-8222-222222222222"}
    other_org.source_updated_at = now + timedelta(days=20)
    other_org.ingested_at = now + timedelta(days=20)
    knowledge_session.add_all(
        [visible_github, visible_slack, archived, other_org]
    )
    await knowledge_session.flush()

    async def no_semantic_channel(*_args, **_kwargs):
        return [], {}, "semantic disabled for visibility test"

    monkeypatch.setattr(
        "brain.systems.knowledge.search._semantic_channel",
        no_semantic_channel,
    )
    fingerprint = await build_knowledge_recall_corpus_fingerprint(
        knowledge_session,
        org_id=_ORG_ID,
    )
    response = await search_knowledge(
        knowledge_session,
        "shared corpus token",
        org_id=_ORG_ID,
        limit=10,
    )

    assert {result["id"] for result in response["results"]} == {1, 2}
    assert fingerprint.to_dict() == {
        "total_item_count": 2,
        "source_counts": {"github": 1, "slack": 1},
        "newest_source_updated_at": "2026-07-30T13:00:00+00:00",
        "newest_ingested_at": "2026-07-30T14:00:00+00:00",
        "fingerprint": fingerprint.fingerprint,
    }


async def test_harvester_uses_indexed_github_provenance_and_labels_runs_for_review(
    knowledge_session,
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
    knowledge_session.add(issue)
    knowledge_session.add(
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
    await knowledge_session.flush()

    payload = (
        await harvest_knowledge_recall_candidates(
            knowledge_session,
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
    knowledge_session,
):
    for run_id in range(1, 4):
        knowledge_session.add(
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
    await knowledge_session.flush()

    payload = (
        await harvest_knowledge_recall_candidates(
            knowledge_session,
            org_id=_ORG_ID,
            limit_per_source=2,
            generated_at=_FIXED_TIME,
        )
    ).to_dict()

    assert payload["summary"]["agent_run_transcripts"] == 2
    assert len(payload["candidates"]) == 2
