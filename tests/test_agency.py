"""Tests for bounded agency candidates, budgets, and scheduler handoff."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, event
from sqlalchemy.dialects.sqlite.base import SQLiteDDLCompiler, SQLiteTypeCompiler
from sqlalchemy.orm import Session

from brain.platform.db.models.agency import AgencyApproval, AgencyBudget, AgencyBudgetEvent, AgencyCandidate
from brain.platform.db.models.scheduler import SchedulerJob, SchedulerLease, SchedulerRun, SchedulerRunStep

ORG_1_ID = "00000000-0000-0000-0000-000000000001"
ORG_2_ID = "00000000-0000-0000-0000-000000000002"
ORG_3_ID = "00000000-0000-0000-0000-000000000003"
USER_1_ID = "10000000-0000-0000-0000-000000000001"
USER_2_ID = "10000000-0000-0000-0000-000000000002"


def _patch_sqlite_for_pg_types():
    if not hasattr(SQLiteTypeCompiler, "visit_JSONB"):
        SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "TEXT"
    SQLiteTypeCompiler.visit_UUID = lambda self, type_, **kw: "TEXT"

    original = SQLiteDDLCompiler.get_column_default_string

    def _patched(self, column, **kw):
        result = original(self, column, **kw)
        if result:
            result = re.sub(r"::jsonb", "", result)
        return result

    SQLiteDDLCompiler.get_column_default_string = _patched


def _register_sqlite_functions(dbapi_conn, connection_record):
    dbapi_conn.create_function("NOW", 0, lambda: datetime.utcnow().isoformat())


@pytest.fixture
def engine():
    _patch_sqlite_for_pg_types()
    eng = create_engine("sqlite://", echo=False)
    event.listen(eng, "connect", _register_sqlite_functions)

    meta = MetaData()
    Table("orgs", meta, Column("id", String, primary_key=True))
    Table("users", meta, Column("id", String, primary_key=True))
    Table("agent_runs", meta, Column("id", Integer, primary_key=True))
    meta.create_all(eng)

    SchedulerJob.__table__.create(eng, checkfirst=True)
    SchedulerLease.__table__.create(eng, checkfirst=True)
    SchedulerRun.__table__.create(eng, checkfirst=True)
    SchedulerRunStep.__table__.create(eng, checkfirst=True)
    AgencyCandidate.__table__.create(eng, checkfirst=True)
    AgencyBudget.__table__.create(eng, checkfirst=True)
    from brain.platform.db.models.agency import AgencyDecision

    AgencyDecision.__table__.create(eng, checkfirst=True)
    AgencyApproval.__table__.create(eng, checkfirst=True)
    AgencyBudgetEvent.__table__.create(eng, checkfirst=True)
    return eng


@pytest.fixture
def session(engine):
    s = Session(engine)
    yield s
    s.close()


def _mock_uow(session: Session):
    uow = MagicMock()
    uow.__enter__ = MagicMock(return_value=uow)
    uow.__exit__ = MagicMock(return_value=False)
    uow.session = session
    return uow


def test_reflection_candidates_capture_evidence_and_dedupe(session):
    from brain.systems.agency.core import mirror_reflection_result

    reflection = {
        "skill_refinements": [
            {
                "skill_name": "debugging",
                "change_type": "refine_procedure",
                "change": "Add a verification step",
                "reason": "Repeated misses",
                "new_procedure": "1. Reproduce\n2. Verify\n3. Fix",
            }
        ],
        "new_skills_proposed": [
            {
                "name": "nightly-audit",
                "description": "Review nightly signals",
                "initial_procedure": "1. Review logs\n2. Cluster signals\n3. Propose action",
            }
        ],
        "system_proposals": [
            {"area": "memory", "proposal": "Tighten retrieval feedback"},
        ],
    }
    context = {
        "agent_runses": [{"id": 11, "status": "failed", "error_classification": {"category": "timeout"}}],
        "skill_executions": [{"id": 21, "skill_name": "debugging", "outcome": "failure"}],
        "new_memories": [{"id": 31, "memory_type": "lesson", "source": "nightly_reflection"}],
        "retrievals": [{"query_text": "missed memories", "results_returned": 0}],
    }

    with patch("brain.systems.agency.core.UnitOfWork", return_value=_mock_uow(session)):
        results = mirror_reflection_result(reflection=reflection, context=context, target_date=datetime(2026, 4, 21, tzinfo=timezone.utc))

    assert len(results) == 3
    assert session.query(AgencyCandidate).count() == 3
    candidate = session.query(AgencyCandidate).first()
    assert candidate.source_refs
    assert any(ref.get("kind") == "agent_runs" for ref in candidate.source_refs)

    with patch("brain.systems.agency.core.UnitOfWork", return_value=_mock_uow(session)):
        mirror_reflection_result(reflection=reflection, context=context, target_date=datetime(2026, 4, 21, tzinfo=timezone.utc))

    assert session.query(AgencyCandidate).count() == 3


def test_budget_gating_blocks_on_exhausted_budget(session, monkeypatch):
    from brain.systems.agency.core import record_candidate
    from brain.systems.agency.policy import evaluate_candidate

    monkeypatch.setattr(
        "brain.systems.agency.policy.get_agency_runtime_settings",
        lambda: {"recommendation_mode": False, "auto_execute_read_only": True},
    )

    budget = AgencyBudget(
        scope_type="org",
        scope_id=ORG_1_ID,
        drive_type="competence",
        window_start=datetime(2026, 4, 21, tzinfo=timezone.utc),
        window_end=datetime(2026, 4, 22, tzinfo=timezone.utc),
        max_candidates=1,
        max_auto_exec=1,
        max_estimated_cost=1.0,
        max_estimated_tokens=100,
        require_review_above_risk="high",
        auto_execute_enabled=True,
        cooldown_hours=12,
        consumed_candidates=1,
        consumed_auto_exec=0,
        consumed_cost=0.0,
        consumed_tokens=0,
        active=True,
    )
    session.add(budget)
    session.flush()

    with patch("brain.systems.agency.core.UnitOfWork", return_value=_mock_uow(session)):
        candidate = record_candidate(
            drive_type="competence",
            source_type="nightly_reflection",
            source_refs=[{"kind": "agent_runs", "id": 42}],
            proposal_kind="system_proposal",
            proposed_run_payload={"proposal": "improve reviews"},
            org_id=ORG_1_ID,
            target_binding_id=None,
            risk_class="low",
            estimated_tokens=20,
        )

    decision = evaluate_candidate(session, candidate, now=datetime(2026, 4, 21, 12, tzinfo=timezone.utc))
    assert decision["decision"] == "block"
    assert decision["reason_code"] == "candidate_budget_exhausted"


def test_unresolved_target_and_disabled_auto_exec_block_handoff(session, monkeypatch):
    from brain.systems.agency.core import record_candidate
    from brain.systems.agency.handoff import build_scheduler_handoff
    from brain.systems.agency.policy import evaluate_candidate

    monkeypatch.setattr(
        "brain.systems.agency.policy.get_agency_runtime_settings",
        lambda: {
            "recommendation_mode": False,
            "auto_execute_read_only": True,
            "auto_execute_practice_runs": True,
            "auto_execute_repo_local": False,
        },
    )

    budget = AgencyBudget(
        scope_type="user",
        scope_id=USER_1_ID,
        drive_type="integrity",
        window_start=datetime(2026, 4, 21, tzinfo=timezone.utc),
        window_end=datetime(2026, 4, 22, tzinfo=timezone.utc),
        max_candidates=10,
        max_auto_exec=2,
        max_estimated_cost=2.0,
        max_estimated_tokens=500,
        require_review_above_risk="high",
        auto_execute_enabled=True,
        cooldown_hours=12,
        consumed_candidates=0,
        consumed_auto_exec=0,
        consumed_cost=0.0,
        consumed_tokens=0,
        active=True,
    )
    session.add(budget)
    session.flush()

    with patch("brain.systems.agency.core.UnitOfWork", return_value=_mock_uow(session)):
        unresolved = record_candidate(
            drive_type="integrity",
            source_type="nightly_implement",
            source_refs=[{"kind": "memory", "id": 7}],
            proposal_kind="implement_proposal",
            proposed_run_payload={"action": "append", "target_file": None},
            user_id=USER_1_ID,
            risk_class="medium",
            reversibility_class="practice_safe",
            target_binding_id=None,
        )

    decision = evaluate_candidate(session, unresolved, now=datetime(2026, 4, 21, 12, tzinfo=timezone.utc))
    assert decision["decision"] == "block"
    assert decision["reason_code"] == "unresolved_target"
    assert build_scheduler_handoff(unresolved, decision) is None

    with patch("brain.systems.agency.core.UnitOfWork", return_value=_mock_uow(session)):
        bounded = record_candidate(
            drive_type="integrity",
            source_type="nightly_implement",
            source_refs=[{"kind": "memory", "id": 8}],
            proposal_kind="implement_proposal",
            proposed_run_payload={"action": "append", "target_file": "repo/notes.txt"},
            user_id=USER_1_ID,
            risk_class="medium",
            target_binding_id="repo/notes.txt",
        )

    with patch("brain.systems.agency.policy.get_agency_runtime_settings", lambda: {"recommendation_mode": False, "auto_execute_read_only": False}):
        denied = evaluate_candidate(session, bounded, now=datetime(2026, 4, 21, 12, tzinfo=timezone.utc))
    assert denied["decision"] == "recommend"
    assert denied["reason_code"] == "auto_exec_disabled"
    assert build_scheduler_handoff(bounded, denied) is None


def test_budget_reserve_and_release_tracks_consumption(session, monkeypatch):
    from brain.systems.agency.core import record_candidate, release_budget, reserve_auto_exec_budget, reserve_candidate_budget

    monkeypatch.setattr(
        "brain.systems.agency.policy.get_agency_runtime_settings",
        lambda: {
            "recommendation_mode": False,
            "auto_execute_read_only": True,
            "auto_execute_practice_runs": True,
            "auto_execute_repo_local": False,
        },
    )

    budget = AgencyBudget(
        scope_type="org",
        scope_id=ORG_1_ID,
        drive_type="curiosity",
        window_start=datetime(2026, 4, 21, tzinfo=timezone.utc),
        window_end=datetime(2026, 4, 22, tzinfo=timezone.utc),
        max_candidates=4,
        max_auto_exec=2,
        max_estimated_cost=5.0,
        max_estimated_tokens=200,
        require_review_above_risk="high",
        auto_execute_enabled=True,
        cooldown_hours=12,
        consumed_candidates=0,
        consumed_auto_exec=0,
        consumed_cost=0.0,
        consumed_tokens=0,
        active=True,
    )
    session.add(budget)
    session.flush()

    with patch("brain.systems.agency.core.UnitOfWork", return_value=_mock_uow(session)):
        candidate = record_candidate(
            drive_type="curiosity",
            source_type="curiosity_reading",
            source_refs=[{"kind": "reading_source", "url": "https://example.com"}],
            proposal_kind="curiosity_followup",
            proposed_run_payload={"item_title": "Example", "concrete_application": "Inspect again"},
            org_id=ORG_1_ID,
            risk_class="low",
            reversibility_class="read_only",
            estimated_cost=1.5,
            estimated_tokens=40,
        )

    reserve_candidate_budget(session, candidate, now=datetime(2026, 4, 21, 12, tzinfo=timezone.utc))
    assert budget.consumed_candidates == 1

    reserve_auto_exec_budget(session, candidate, now=datetime(2026, 4, 21, 12, tzinfo=timezone.utc))
    assert budget.consumed_auto_exec == 1
    assert budget.consumed_cost == 1.5
    assert budget.consumed_tokens == 40

    release_budget(
        session,
        candidate,
        candidate_slots=1,
        auto_exec_slots=1,
        cost=1.5,
        tokens=40,
        now=datetime(2026, 4, 21, 12, tzinfo=timezone.utc),
    )
    assert budget.consumed_candidates == 0
    assert budget.consumed_auto_exec == 0
    assert budget.consumed_cost == 0.0
    assert budget.consumed_tokens == 0
    assert session.query(AgencyBudgetEvent).count() == 3


def test_cooldown_and_dedupe_block_repeated_evaluation(session, monkeypatch):
    from brain.systems.agency.core import record_candidate
    from brain.systems.agency.policy import evaluate_candidate

    monkeypatch.setattr(
        "brain.systems.agency.policy.get_agency_runtime_settings",
        lambda: {
            "recommendation_mode": False,
            "auto_execute_read_only": True,
            "auto_execute_practice_runs": True,
            "auto_execute_repo_local": False,
        },
    )

    budget = AgencyBudget(
        scope_type="user",
        scope_id=USER_1_ID,
        drive_type="prevention",
        window_start=datetime(2026, 4, 21, tzinfo=timezone.utc),
        window_end=datetime(2026, 4, 22, tzinfo=timezone.utc),
        max_candidates=4,
        max_auto_exec=1,
        max_estimated_cost=1.0,
        max_estimated_tokens=100,
        require_review_above_risk="high",
        auto_execute_enabled=True,
        cooldown_hours=6,
        consumed_candidates=0,
        consumed_auto_exec=0,
        consumed_cost=0.0,
        consumed_tokens=0,
        active=True,
    )
    session.add(budget)
    session.flush()

    suppression_until = datetime(2026, 4, 21, 18, tzinfo=timezone.utc)
    with patch("brain.systems.agency.core.UnitOfWork", return_value=_mock_uow(session)):
        candidate = record_candidate(
            drive_type="prevention",
            source_type="nightly_guardian",
            source_refs=[{"kind": "violation_log", "context": "repeat timeout"}],
            proposal_kind="guardian_rule",
            proposed_run_payload={"context": "repeat timeout", "count": 4},
            user_id=USER_1_ID,
            risk_class="low",
            suppression_until=suppression_until,
        )
        duplicate = record_candidate(
            drive_type="prevention",
            source_type="nightly_guardian",
            source_refs=[{"kind": "violation_log", "context": "repeat timeout"}],
            proposal_kind="guardian_rule",
            proposed_run_payload={"context": "repeat timeout", "count": 4},
            user_id=USER_1_ID,
            risk_class="low",
            suppression_until=suppression_until,
        )

    assert candidate.id == duplicate.id
    decision = evaluate_candidate(session, candidate, now=datetime(2026, 4, 21, 12, tzinfo=timezone.utc))
    assert decision["decision"] == "block"
    assert decision["reason_code"] == "cooldown_active"


def test_safe_candidate_materializes_scheduler_handoff(session, monkeypatch):
    from brain.systems.agency.core import evaluate_and_record_candidate

    monkeypatch.setattr(
        "brain.systems.agency.policy.get_agency_runtime_settings",
        lambda: {
            "recommendation_mode": False,
            "auto_execute_read_only": True,
            "auto_execute_practice_runs": True,
            "auto_execute_repo_local": False,
        },
    )

    budget = AgencyBudget(
        scope_type="org",
        scope_id=ORG_2_ID,
        drive_type="curiosity",
        window_start=datetime(2026, 4, 21, tzinfo=timezone.utc),
        window_end=datetime(2026, 4, 22, tzinfo=timezone.utc),
        max_candidates=5,
        max_auto_exec=2,
        max_estimated_cost=3.0,
        max_estimated_tokens=100,
        require_review_above_risk="high",
        auto_execute_enabled=True,
        cooldown_hours=12,
        consumed_candidates=0,
        consumed_auto_exec=0,
        consumed_cost=0.0,
        consumed_tokens=0,
        active=True,
    )
    session.add(budget)
    session.flush()

    with patch("brain.systems.agency.core.UnitOfWork", return_value=_mock_uow(session)):
        candidate, decision = evaluate_and_record_candidate(
            drive_type="curiosity",
            source_type="curiosity_reading",
            source_refs=[{"kind": "reading_source", "url": "https://example.com/post"}],
            proposal_kind="curiosity_followup",
            proposed_run_payload={
                "item_title": "Practice-safe read",
                "item_url": "https://example.com/post",
                "concrete_application": "Review the pattern",
                "worth_deep_dive": True,
            },
            org_id=ORG_2_ID,
            risk_class="low",
            reversibility_class="read_only",
            estimated_cost=0.2,
            estimated_tokens=12,
            now=datetime(2026, 4, 21, 12, tzinfo=timezone.utc),
        )

    assert decision.decision == "approve"
    assert decision.scheduler_run_id is not None
    assert session.query(SchedulerJob).count() == 1
    assert session.query(SchedulerRun).count() == 1
    run = session.get(SchedulerRun, decision.scheduler_run_id)
    assert run is not None
    assert run.status == "settled_success"
    assert run.result_summary["handler_result"]["status"] == "recorded"
    assert run.lease_id is not None
    lease = session.get(SchedulerLease, run.lease_id)
    assert lease is not None
    assert lease.released_at is not None
    assert session.query(SchedulerRunStep).count() == 1
    assert budget.consumed_candidates == 1
    assert budget.consumed_auto_exec == 1
    assert candidate.status == "auto_executed"
    assert decision.budget_snapshot["scheduler_run"]["status"] == "settled_success"
    assert session.query(AgencyApproval).count() == 1
    assert session.query(AgencyBudgetEvent).count() == 2


def test_repo_local_candidate_stays_recommendation_only_when_auto_exec_not_allowed(session, monkeypatch):
    from brain.systems.agency.core import record_candidate
    from brain.systems.agency.policy import evaluate_candidate

    monkeypatch.setattr(
        "brain.systems.agency.policy.get_agency_runtime_settings",
        lambda: {
            "recommendation_mode": False,
            "auto_execute_read_only": True,
            "auto_execute_practice_runs": True,
            "auto_execute_repo_local": True,
        },
    )

    budget = AgencyBudget(
        scope_type="org",
        scope_id=ORG_3_ID,
        drive_type="integrity",
        window_start=datetime(2026, 4, 21, tzinfo=timezone.utc),
        window_end=datetime(2026, 4, 22, tzinfo=timezone.utc),
        max_candidates=5,
        max_auto_exec=2,
        max_estimated_cost=3.0,
        max_estimated_tokens=100,
        require_review_above_risk="high",
        auto_execute_enabled=True,
        cooldown_hours=12,
        consumed_candidates=0,
        consumed_auto_exec=0,
        consumed_cost=0.0,
        consumed_tokens=0,
        active=True,
    )
    session.add(budget)
    session.flush()

    with patch("brain.systems.agency.core.UnitOfWork", return_value=_mock_uow(session)):
        candidate = record_candidate(
            drive_type="integrity",
            source_type="nightly_implement",
            source_refs=[{"kind": "memory", "id": 99}],
            proposal_kind="implement_proposal",
            proposed_run_payload={"action": "append", "target_file": "repo/notes.txt"},
            org_id=ORG_3_ID,
            target_binding_id="repo/notes.txt",
            risk_class="medium",
            reversibility_class="repo_local",
        )

    decision = evaluate_candidate(session, candidate, now=datetime(2026, 4, 21, 12, tzinfo=timezone.utc))
    assert decision["decision"] == "recommend"
    assert decision["reason_code"] == "auto_exec_class_disabled"
    assert session.query(SchedulerRun).count() == 0


def test_unresolved_target_blocks_without_scheduler_handoff(session, monkeypatch):
    from brain.systems.agency.core import record_candidate
    from brain.systems.agency.policy import evaluate_candidate

    monkeypatch.setattr(
        "brain.systems.agency.policy.get_agency_runtime_settings",
        lambda: {
            "recommendation_mode": False,
            "auto_execute_read_only": True,
            "auto_execute_practice_runs": True,
            "auto_execute_repo_local": False,
        },
    )

    budget = AgencyBudget(
        scope_type="user",
        scope_id="user-2",
        drive_type="integrity",
        window_start=datetime(2026, 4, 21, tzinfo=timezone.utc),
        window_end=datetime(2026, 4, 22, tzinfo=timezone.utc),
        max_candidates=10,
        max_auto_exec=2,
        max_estimated_cost=2.0,
        max_estimated_tokens=500,
        require_review_above_risk="high",
        auto_execute_enabled=True,
        cooldown_hours=12,
        consumed_candidates=0,
        consumed_auto_exec=0,
        consumed_cost=0.0,
        consumed_tokens=0,
        active=True,
    )
    session.add(budget)
    session.flush()

    with patch("brain.systems.agency.core.UnitOfWork", return_value=_mock_uow(session)):
        candidate = record_candidate(
            drive_type="integrity",
            source_type="nightly_implement",
            source_refs=[{"kind": "memory", "id": 7}],
            proposal_kind="implement_proposal",
            proposed_run_payload={"action": "append", "target_file": None},
            user_id=USER_2_ID,
            risk_class="medium",
            reversibility_class="practice_safe",
            target_binding_id=None,
        )

    decision = evaluate_candidate(session, candidate, now=datetime(2026, 4, 21, 12, tzinfo=timezone.utc))
    assert decision["decision"] == "block"
    assert decision["reason_code"] == "unresolved_target"
    assert session.query(SchedulerRun).count() == 0
