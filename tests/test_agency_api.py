"""API tests for agency review and budget ledger routes."""
from __future__ import annotations

import re
from collections.abc import Generator
from datetime import datetime, timedelta, timezone

from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, event
from sqlalchemy.dialects.sqlite.base import SQLiteDDLCompiler, SQLiteTypeCompiler
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

from brain.app.api.auth import get_current_user
from brain.app.api.deps import get_db, rate_limit
from brain.app.api.main import app
from brain.platform.db.models.agency import (
    AgencyApproval,
    AgencyBudget,
    AgencyBudgetEvent,
    AgencyCandidate,
    AgencyDecision,
)
from brain.platform.db.models.scheduler import SchedulerJob, SchedulerLease, SchedulerRun, SchedulerRunStep

ORG_1_ID = "00000000-0000-0000-0000-000000000001"
OWNER_ID = "10000000-0000-0000-0000-000000000001"
MEMBER_ID = "10000000-0000-0000-0000-000000000002"


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


def _build_session() -> Session:
    _patch_sqlite_for_pg_types()
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    event.listen(engine, "connect", _register_sqlite_functions)

    stub = MetaData()
    Table("orgs", stub, Column("id", String, primary_key=True))
    Table("users", stub, Column("id", String, primary_key=True))
    Table("agent_runs", stub, Column("id", Integer, primary_key=True))
    stub.create_all(engine)

    SchedulerJob.__table__.create(engine, checkfirst=True)
    SchedulerLease.__table__.create(engine, checkfirst=True)
    SchedulerRun.__table__.create(engine, checkfirst=True)
    SchedulerRunStep.__table__.create(engine, checkfirst=True)
    AgencyCandidate.__table__.create(engine, checkfirst=True)
    AgencyBudget.__table__.create(engine, checkfirst=True)
    AgencyDecision.__table__.create(engine, checkfirst=True)
    AgencyApproval.__table__.create(engine, checkfirst=True)
    AgencyBudgetEvent.__table__.create(engine, checkfirst=True)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    return session


def _override_db(session: Session):
    class _AsyncSession:
        async def run_sync(self, fn):
            return fn(session)

    async def _db() -> Generator[_AsyncSession, None, None]:
        try:
            yield _AsyncSession()
            session.commit()
        except Exception:
            session.rollback()
            raise

    return _db


def _owner_user() -> dict[str, object]:
    return {
        "id": OWNER_ID,
        "org_id": ORG_1_ID,
        "role": "owner",
        "principal_type": "human",
        "permissions": ["scheduler:manage"],
    }


def _member_user() -> dict[str, object]:
    return {
        "id": MEMBER_ID,
        "org_id": ORG_1_ID,
        "role": "member",
        "principal_type": "human",
        "permissions": [],
    }


def _seed_candidate(session: Session) -> AgencyCandidate:
    now = datetime.now(timezone.utc)
    budget = AgencyBudget(
        scope_type="org",
        scope_id=ORG_1_ID,
        drive_type="curiosity",
        window_start=now - timedelta(days=1),
        window_end=now + timedelta(days=1),
        max_candidates=3,
        max_auto_exec=0,
        max_estimated_cost=0.0,
        max_estimated_tokens=0,
        require_review_above_risk="medium",
        auto_execute_enabled=False,
        cooldown_hours=4,
        consumed_candidates=0,
        consumed_auto_exec=0,
        consumed_cost=0.0,
        consumed_tokens=0,
        active=True,
    )
    candidate = AgencyCandidate(
        candidate_key="candidate-api-1",
        drive_type="curiosity",
        source_type="curiosity_reading",
        source_refs=[{"kind": "reading_source", "url": "https://example.com"}],
        org_id=ORG_1_ID,
        proposal_kind="curiosity_followup",
        proposed_run_payload={"title": "Review this"},
        risk_class="low",
        reversibility_class="read_only",
        estimated_cost=0.1,
        estimated_tokens=10,
        status="proposed",
    )
    session.add_all([budget, candidate])
    session.commit()
    return candidate


def _request_as(session: Session, user: dict[str, object], method: str, path: str, **kwargs):
    app.dependency_overrides[get_db] = _override_db(session)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[rate_limit] = lambda: None
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            return client.request(method, path, **kwargs)
    finally:
        app.dependency_overrides.clear()


def test_openapi_registers_agency_routes():
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/api/openapi.json")

    assert response.status_code == 200
    paths = response.json()["paths"]
    assert "/api/agency/candidates" in paths
    assert "/api/agency/candidates/{candidate_id}/approve" in paths
    assert "/api/agency/budget-events" in paths


def test_owner_approval_records_approval_and_budget_event():
    session = _build_session()
    candidate = _seed_candidate(session)

    response = _request_as(
        session,
        _owner_user(),
        "POST",
        f"/api/agency/candidates/{candidate.id}/approve",
        json={"reason": "Evidence is safe and useful."},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["candidate"]["status"] == "approved"
    assert payload["approval"]["actor_role"] == "owner"
    assert payload["decision"]["decision"] == "approve"
    assert payload["decision"]["budget_snapshot"]["reservation"]["reserved_candidates"] == 1
    assert session.query(AgencyApproval).count() == 1
    assert session.query(AgencyBudgetEvent).count() == 1
    assert session.query(AgencyBudget).first().consumed_candidates == 1

    detail = _request_as(session, _owner_user(), "GET", f"/api/agency/candidates/{candidate.id}")
    assert detail.status_code == 200
    assert detail.json()["approvals"][0]["reason"] == "Evidence is safe and useful."


def test_member_cannot_approve_agency_candidate():
    session = _build_session()
    candidate = _seed_candidate(session)

    response = _request_as(
        session,
        _member_user(),
        "POST",
        f"/api/agency/candidates/{candidate.id}/approve",
        json={"reason": "I should not be allowed."},
    )

    assert response.status_code == 403
    assert session.query(AgencyApproval).count() == 0
