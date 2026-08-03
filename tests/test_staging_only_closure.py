"""Regression coverage for staging-only GitHub issue closures."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.dialects.sqlite.base import SQLiteDDLCompiler, SQLiteTypeCompiler

from brain.platform.db.models.domain import (
    Domain,
    DomainEvent,
    DomainFieldDefinition,
    DomainObjectType,
    DomainRecord,
    DomainRelation,
    DomainRelationType,
)
from brain.platform.db.models.provider_alert import ProviderAlertOccurrence
from brain.systems.deploy_state import (
    DeployState,
    DeployStateBatch,
    DeployStateObservation,
)
from brain.systems.deploy_state_github import AncestryObservation
from brain.systems.deploy_tracker import (
    PRODUCTION_GATE_FIELD,
    PRODUCTION_GATE_PENDING,
)
from brain.systems.github_read_failures import (
    GITHUB_READ_ACCESS_FORBIDDEN,
    GITHUB_READ_AUTHENTICATION_REQUIRED,
    GITHUB_READ_CONNECTOR_ERROR,
)
from brain.systems.production_gate_github import (
    ClosureReadFailure,
)
from brain.systems.staging_only_closure import (
    FixingPullRequest,
    IssueClosure,
    ProductionEvidence,
    run_staging_only_closure_sweep,
)
from brain.systems.user_domains.service import AsyncDomainService


ORG_ID = "11111111-1111-4111-8111-111111111111"
REPO = "uwear-ai/uwear-backend"
CLOSED_AT = datetime(2026, 7, 27, 9, 8, 19, tzinfo=timezone.utc)
FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "staging_only_closure"


def _patch_sqlite_for_pg_types() -> None:
    if not hasattr(SQLiteTypeCompiler, "visit_JSONB"):
        SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "TEXT"
    SQLiteTypeCompiler.visit_UUID = lambda self, type_, **kw: "TEXT"
    original = SQLiteDDLCompiler.get_column_default_string
    if getattr(original, "_staging_closure_patch", False):
        return

    def patched(self, column, **kw):
        result = original(self, column, **kw)
        if result:
            result = re.sub(r"::jsonb", "", result)
            result = result.replace("NOW()", "CURRENT_TIMESTAMP")
            result = result.replace("TRUE", "1").replace("FALSE", "0")
        return result

    patched._staging_closure_patch = True
    SQLiteDDLCompiler.get_column_default_string = patched


@pytest.fixture
async def session(async_sqlite_session_factory):
    _patch_sqlite_for_pg_types()
    return await async_sqlite_session_factory(
        [
            Domain.__table__,
            DomainObjectType.__table__,
            DomainFieldDefinition.__table__,
            DomainRecord.__table__,
            DomainRelationType.__table__,
            DomainRelation.__table__,
            DomainEvent.__table__,
            ProviderAlertOccurrence.__table__,
        ]
    )


async def _tracker(session):
    return await AsyncDomainService(session).create_domain(
        ORG_ID,
        name="GitHub ticket tracker",
        slug="github-ticket-tracker",
        objects=[
            {
                "key": "github_ticket",
                "name": "GitHub Ticket",
                "title_field": "title",
                "fields": [
                    {"key": "title", "field_type": "text", "required": True},
                    {"key": "external_id", "field_type": "text", "required": True},
                    {"key": "repo", "field_type": "text", "required": True},
                    {"key": "issue_number", "field_type": "number", "required": True},
                    {
                        "key": "status",
                        "field_type": "enum",
                        "options": ["Todo", "In Progress", "In Review", "Done"],
                    },
                    {
                        "key": PRODUCTION_GATE_FIELD,
                        "field_type": "enum",
                        "options": [PRODUCTION_GATE_PENDING],
                    },
                    {"key": "fix_pr", "field_type": "text"},
                    {"key": "fix_merge_sha", "field_type": "text"},
                    {"key": "error_signature", "field_type": "text"},
                    {"key": "rollbar_item", "field_type": "text"},
                    {"key": "assignee", "field_type": "text"},
                    {"key": "progress_note", "field_type": "long_text"},
                ],
            }
        ],
    )


async def _tracked_issue(session, domain, *, number: int, title: str):
    return await AsyncDomainService(session).create_record(
        ORG_ID,
        domain.id,
        "github_ticket",
        title=title,
        data={
            "title": title,
            "external_id": f"github:{REPO}:issue:{number}",
            "repo": REPO,
            "issue_number": number,
            "status": "Done",
            "assignee": "Axel",
            "progress_note": "Fix merged; awaiting sweep.",
        },
    )


def _deploy_batch(
    key: object,
    *,
    repo: str,
    sha: str,
    state: DeployState,
    in_staging: bool,
    in_main: bool,
    main_status: str,
) -> DeployStateBatch:
    observation = DeployStateObservation(
        state=state,
        in_staging=in_staging,
        in_main=in_main,
        comparisons=(
            AncestryObservation(
                branch="staging",
                is_ancestor=in_staging,
                status="behind" if in_staging else "diverged",
            ),
            AncestryObservation(
                branch="main",
                is_ancestor=in_main,
                status=main_status,
            ),
        ),
    )
    return DeployStateBatch(
        {key: state},
        observations_by_key={key: observation},
        observations_by_ref={(repo, sha): observation},
    )


def _deploy_batch_many(entries) -> DeployStateBatch:
    states = {}
    observations_by_key = {}
    observations_by_ref = {}
    for key, repo, sha, state, in_staging, in_main, main_status in entries:
        observation = DeployStateObservation(
            state=state,
            in_staging=in_staging,
            in_main=in_main,
            comparisons=(
                AncestryObservation(
                    branch="staging",
                    is_ancestor=in_staging,
                    status="behind" if in_staging else "diverged",
                ),
                AncestryObservation(
                    branch="main",
                    is_ancestor=in_main,
                    status=main_status,
                ),
            ),
        )
        states[key] = state
        observations_by_key[key] = observation
        observations_by_ref[(repo, sha)] = observation
    return DeployStateBatch(
        states,
        observations_by_key=observations_by_key,
        observations_by_ref=observations_by_ref,
    )


class _Github:
    def __init__(
        self,
        closure: IssueClosure,
        batch: DeployStateBatch,
        *,
        expected_refs=None,
    ):
        self.closure = closure
        self.batch = batch
        self.expected_refs = expected_refs or {
            (REPO, 1281, 1305): (REPO, "a" * 40),
        }

    async def get_issue_closure(self, *, repo: str, issue_number: int):
        assert (repo, issue_number) == (self.closure.repo, self.closure.number)
        return self.closure

    async def derive_deploy_states(self, refs):
        assert dict(refs) == self.expected_refs
        return self.batch


class _BatchGithub:
    def __init__(self, closures, batch, expected_refs):
        self.closures = {
            (closure.repo, closure.number): closure
            for closure in closures
        }
        self.batch = batch
        self.expected_refs = expected_refs

    async def get_issue_closure(self, *, repo: str, issue_number: int):
        return self.closures[(repo, issue_number)]

    async def derive_deploy_states(self, refs):
        assert dict(refs) == self.expected_refs
        return self.batch


class _AuthFailingGithub:
    def __init__(self):
        self.reads = []

    async def get_issue_closure(self, *, repo: str, issue_number: int):
        self.reads.append((repo, issue_number))
        raise ClosureReadFailure(
            reason_code=GITHUB_READ_ACCESS_FORBIDDEN,
            status_code=403,
            message="API rate limit exceeded for 207.134.142.114.",
        )

    async def derive_deploy_states(self, refs):
        raise AssertionError("No deploy-state reads are expected")


class _PerIssueGithub:
    def __init__(self, outcomes):
        self.outcomes = dict(outcomes)

    async def get_issue_closure(self, *, repo: str, issue_number: int):
        outcome = self.outcomes[(repo, issue_number)]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def derive_deploy_states(self, refs):
        raise AssertionError("No deploy-state reads are expected")


class _Slack:
    def __init__(self):
        self.posts = []

    async def post_message(self, **kwargs):
        self.posts.append(kwargs)
        return {"ok": True, "ts": "1785144000.000001"}


@pytest.mark.asyncio
async def test_all_issue_reads_failing_same_auth_reason_surface_one_sweep_error(
    session,
    caplog,
):
    domain = await _tracker(session)
    await _tracked_issue(
        session,
        domain,
        number=1281,
        title="PostgreSQL deadlock",
    )
    await _tracked_issue(
        session,
        domain,
        number=1282,
        title="PostgreSQL lock timeout",
    )
    github = _AuthFailingGithub()

    with caplog.at_level(logging.WARNING, logger="illo.staging_only_closure"):
        summary = await run_staging_only_closure_sweep(
            session,
            org_id=ORG_ID,
            github=github,
            production_evidence=_Evidence(()),
            notify=False,
            now=CLOSED_AT,
        )

    assert sorted(github.reads) == [(REPO, 1281), (REPO, 1282)]
    assert summary["examined"] == 2
    assert summary["closed"] == 0
    assert summary["errors"] == [
        "github_issue_authentication_all_reads_failed:"
        "count=2:reason=github_access_forbidden:"
        "status=403:API rate limit exceeded for 207.134.142.114."
    ]
    closure_logs = [
        record.getMessage()
        for record in caplog.records
        if record.name == "illo.staging_only_closure"
    ]
    assert closure_logs == [
        "closure authentication failed for all 2 GitHub issue reads: "
        "API rate limit exceeded for 207.134.142.114."
    ]


@pytest.mark.asyncio
async def test_mixed_success_and_auth_failure_does_not_report_all_reads_failed(
    session,
):
    domain = await _tracker(session)
    await _tracked_issue(session, domain, number=1281, title="Readable issue")
    await _tracked_issue(session, domain, number=1282, title="Unreadable issue")
    github = _PerIssueGithub(
        {
            (REPO, 1281): None,
            (REPO, 1282): ClosureReadFailure(
                reason_code=GITHUB_READ_ACCESS_FORBIDDEN,
                status_code=403,
                message="Access forbidden",
            ),
        }
    )

    summary = await run_staging_only_closure_sweep(
        session,
        org_id=ORG_ID,
        github=github,
        production_evidence=_Evidence(()),
        notify=False,
        now=CLOSED_AT,
    )

    assert summary["closed"] == 0
    assert summary["errors"] == [
        f"github_issue:{REPO}#1282:Access forbidden"
    ]


@pytest.mark.asyncio
async def test_different_auth_reasons_do_not_report_one_all_reads_failure(session):
    domain = await _tracker(session)
    await _tracked_issue(session, domain, number=1281, title="Missing credentials")
    await _tracked_issue(session, domain, number=1282, title="Forbidden credentials")
    github = _PerIssueGithub(
        {
            (REPO, 1281): ClosureReadFailure(
                reason_code=GITHUB_READ_AUTHENTICATION_REQUIRED,
                status_code=401,
                message="Authentication required",
            ),
            (REPO, 1282): ClosureReadFailure(
                reason_code=GITHUB_READ_ACCESS_FORBIDDEN,
                status_code=403,
                message="Access forbidden",
            ),
        }
    )

    summary = await run_staging_only_closure_sweep(
        session,
        org_id=ORG_ID,
        github=github,
        production_evidence=_Evidence(()),
        notify=False,
        now=CLOSED_AT,
    )

    assert summary["errors"] == [
        f"github_issue:{REPO}#1281:Authentication required",
        f"github_issue:{REPO}#1282:Access forbidden",
    ]


@pytest.mark.asyncio
async def test_non_auth_closure_failure_does_not_report_authentication_error(session):
    domain = await _tracker(session)
    await _tracked_issue(session, domain, number=1281, title="GitHub unavailable")
    github = _PerIssueGithub(
        {
            (REPO, 1281): ClosureReadFailure(
                reason_code=GITHUB_READ_CONNECTOR_ERROR,
                status_code=503,
                message="GitHub unavailable",
            )
        }
    )

    summary = await run_staging_only_closure_sweep(
        session,
        org_id=ORG_ID,
        github=github,
        production_evidence=_Evidence(()),
        notify=False,
        now=CLOSED_AT,
    )

    assert summary["errors"] == [
        f"github_issue:{REPO}#1281:GitHub unavailable"
    ]


class _ResolvableSlack(_Slack):
    def __init__(self):
        super().__init__()
        self.channel_reads = []

    async def conversations_list(self, **kwargs):
        self.channel_reads.append(kwargs)
        return {
            "channels": [{"id": "C_SOFTWARE", "name": "4_software"}],
            "response_metadata": {"next_cursor": ""},
        }


class _Evidence:
    def __init__(self, items):
        self.items = tuple(items)
        self.calls = []

    async def list_recent(self, session, *, org_id, since, until):
        self.calls.append((session, org_id, since, until))
        return self.items


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("deploy_state", "in_staging"),
    [
        (DeployState.STAGING, True),
        (DeployState.UNMERGED, False),
    ],
)
async def test_nonproduction_closure_returns_tracker_to_prod_pending_review(
    session,
    deploy_state,
    in_staging,
):
    domain = await _tracker(session)
    record = await _tracked_issue(
        session,
        domain,
        number=1281,
        title="PostgreSQL deadlock",
    )
    pr = FixingPullRequest(
        repo=REPO,
        number=1305,
        base_ref_name="staging",
        merge_commit_sha="a" * 40,
        merged_at=CLOSED_AT,
    )
    key = (REPO, 1281, 1305)
    github = _Github(
        IssueClosure(
            repo=REPO,
            number=1281,
            title=record.title,
            state="closed",
            closed_at=CLOSED_AT,
            closed_by="uwear-claw",
            fixing_pull_requests=(pr,),
        ),
        _deploy_batch(
            key,
            repo=REPO,
            sha=pr.merge_commit_sha,
            state=deploy_state,
            in_staging=in_staging,
            in_main=False,
            main_status="diverged",
        ),
    )

    await run_staging_only_closure_sweep(
        session,
        org_id=ORG_ID,
        github=github,
        slack=_Slack(),
        now=CLOSED_AT,
    )

    domain_id = domain.id
    record_id = record.id
    session.expire_all()
    reread = await AsyncDomainService(session).get_record(
        ORG_ID,
        domain_id,
        record_id,
    )
    assert reread.data["status"] == "In Review"
    assert reread.data[PRODUCTION_GATE_FIELD] == PRODUCTION_GATE_PENDING
    assert "deploy_state" not in reread.data
    assert reread.data["assignee"] == "Axel"
    assert reread.data["fix_pr"] == f"{REPO}#1305"
    assert reread.data["fix_merge_sha"] == "a" * 40
    assert "#1305" in reread.data["progress_note"]
    assert "`staging`" in reread.data["progress_note"]
    assert "main ancestry: diverged (not contained)" in reread.data["progress_note"]


@pytest.mark.asyncio
async def test_main_merged_closure_goes_done_without_software_message(session):
    domain = await _tracker(session)
    record = await _tracked_issue(
        session,
        domain,
        number=1290,
        title="Generation-result QA failures",
    )
    record.data = {
        **record.data,
        "status": "In Review",
        PRODUCTION_GATE_FIELD: PRODUCTION_GATE_PENDING,
    }
    await session.flush()
    pr = FixingPullRequest(
        repo=REPO,
        number=1310,
        base_ref_name="main",
        merge_commit_sha="b" * 40,
        merged_at=CLOSED_AT,
    )
    key = (REPO, 1290, 1310)
    github = _Github(
        IssueClosure(
            repo=REPO,
            number=1290,
            title=record.title,
            state="closed",
            closed_at=CLOSED_AT,
            closed_by="uwear-claw",
            fixing_pull_requests=(pr,),
        ),
        _deploy_batch(
            key,
            repo=REPO,
            sha=pr.merge_commit_sha,
            state=DeployState.DEPLOYED,
            in_staging=True,
            in_main=True,
            main_status="behind",
        ),
        expected_refs={key: (REPO, pr.merge_commit_sha)},
    )
    slack = _Slack()

    await run_staging_only_closure_sweep(
        session,
        org_id=ORG_ID,
        github=github,
        slack=slack,
        now=CLOSED_AT,
    )

    record_id = record.id
    domain_id = domain.id
    session.expire_all()
    reread = await AsyncDomainService(session).get_record(
        ORG_ID,
        domain_id,
        record_id,
    )
    assert reread.data["status"] == "Done"
    assert reread.data[PRODUCTION_GATE_FIELD] is None
    assert slack.posts == []


@pytest.mark.asyncio
async def test_nine_same_minute_closures_post_one_addressed_software_batch(session):
    domain = await _tracker(session)
    issue_prs = (
        (1281, 1305),
        (1290, 1310),
        (1277, 1302),
        (1278, 1309),
        (1279, 1309),
        (1280, 1304),
        (1257, 1301),
        (1108, 1311),
        (1109, 1276),
    )
    closures = []
    entries = []
    expected_refs = {}
    for index, (issue_number, pr_number) in enumerate(issue_prs):
        record = await _tracked_issue(
            session,
            domain,
            number=issue_number,
            title=f"Regression #{issue_number}",
        )
        sha = f"{index + 1:040x}"
        pr = FixingPullRequest(
            repo=REPO,
            number=pr_number,
            base_ref_name="staging",
            merge_commit_sha=sha,
            merged_at=CLOSED_AT,
        )
        closure = IssueClosure(
            repo=REPO,
            number=issue_number,
            title=record.title,
            state="closed",
            closed_at=CLOSED_AT.replace(second=19 + index),
            closed_by="uwear-claw",
            fixing_pull_requests=(pr,),
        )
        key = (REPO, issue_number, pr_number)
        closures.append(closure)
        expected_refs[key] = (REPO, sha)
        entries.append(
            (
                key,
                REPO,
                sha,
                DeployState.STAGING,
                True,
                False,
                "diverged",
            )
        )
    slack = _Slack()

    summary = await run_staging_only_closure_sweep(
        session,
        org_id=ORG_ID,
        github=_BatchGithub(
            closures,
            _deploy_batch_many(entries),
            expected_refs,
        ),
        slack=slack,
        now=CLOSED_AT,
    )

    assert summary["flagged"] == 9
    assert summary["messages_posted"] == 1
    assert len(slack.posts) == 1
    assert slack.posts[0]["channel"] == "#4_software"
    assert slack.posts[0]["text"].startswith("@uwear-claw:")
    for issue_number, pr_number in issue_prs:
        assert f"#{issue_number} is closed" in slack.posts[0]["text"]
        assert f"#{pr_number} is on `staging` only" in slack.posts[0]["text"]

    replay = await run_staging_only_closure_sweep(
        session,
        org_id=ORG_ID,
        github=_BatchGithub(
            closures,
            _deploy_batch_many(entries),
            expected_refs,
        ),
        slack=slack,
        now=CLOSED_AT,
    )
    assert replay["flagged"] == 9
    assert replay["updated"] == 0
    assert replay["messages_posted"] == 0
    assert len(slack.posts) == 1


@pytest.mark.asyncio
async def test_fixture_replay_reproduces_nine_flags_and_live_prod_escalation(session):
    fixture = json.loads(
        (FIXTURE_ROOT / "2026-07-27-nine.json").read_text(encoding="utf-8")
    )
    domain = await _tracker(session)
    closed_at = datetime.fromisoformat(
        fixture["closed_at"].replace("Z", "+00:00")
    )
    closures = []
    entries = []
    expected_refs = {}
    for item in fixture["issues"]:
        record = await _tracked_issue(
            session,
            domain,
            number=item["number"],
            title=item["title"],
        )
        record.data = {
            **record.data,
            "error_signature": item.get("error_signature"),
            "rollbar_item": item.get("rollbar_item"),
        }
        pr = FixingPullRequest(
            repo=fixture["repo"],
            number=item["pull_request"],
            base_ref_name=item["base_ref_name"],
            merge_commit_sha=item["merge_commit_sha"],
            merged_at=closed_at,
        )
        closure = IssueClosure(
            repo=fixture["repo"],
            number=item["number"],
            title=item["title"],
            state="closed",
            closed_at=closed_at,
            closed_by=fixture["closed_by"],
            fixing_pull_requests=(pr,),
        )
        key = (fixture["repo"], item["number"], item["pull_request"])
        closures.append(closure)
        expected_refs[key] = (fixture["repo"], item["merge_commit_sha"])
        entries.append(
            (
                key,
                fixture["repo"],
                item["merge_commit_sha"],
                DeployState.STAGING,
                True,
                False,
                item["main_ancestry_status"],
            )
        )
    await session.flush()
    evidence = _Evidence(
        [
            ProductionEvidence(
                source=item["source"],
                reference=item["reference"],
                signature=item["signature"],
                occurred_at=datetime.fromisoformat(
                    item["occurred_at"].replace("Z", "+00:00")
                ),
                is_open=item["is_open"],
            )
            for item in fixture["production_evidence"]
        ]
    )
    assert closed_at - evidence.items[0].occurred_at == timedelta(minutes=71)
    slack = _Slack()

    summary = await run_staging_only_closure_sweep(
        session,
        org_id=ORG_ID,
        github=_BatchGithub(
            closures,
            _deploy_batch_many(entries),
            expected_refs,
        ),
        production_evidence=evidence,
        slack=slack,
        now=closed_at,
    )

    assert len(summary["findings"]) == 9
    assert {finding["pr_number"] for finding in summary["findings"]} == {
        1305,
        1310,
        1302,
        1309,
        1304,
        1301,
        1311,
        1276,
    }
    assert {
        (finding["base_ref_name"], finding["main_ancestry"])
        for finding in summary["findings"]
    } == {("staging", "diverged (not contained)")}
    high = [
        finding
        for finding in summary["findings"]
        if finding["severity"] == "high"
    ]
    assert high == [
        {
            "record_id": high[0]["record_id"],
            "issue_number": 1281,
            "pr_number": 1305,
            "base_ref_name": "staging",
            "main_ancestry": "diverged (not contained)",
            "severity": "high",
            "production_evidence": "Uwear-API#2323",
        }
    ]
    assert len(slack.posts) == 1
    assert "PROD FAILURE STILL LIVE" in slack.posts[0]["text"]
    assert "Uwear-API#2323" in slack.posts[0]["text"]


@pytest.mark.asyncio
async def test_sweep_reconciles_production_gate_without_reviving_deploy_state(session):
    domain = await _tracker(session)
    service = AsyncDomainService(session)
    object_type = await service.get_object_type(domain.id, "github_ticket")
    production_gate = next(
        field
        for field in await service.list_fields(object_type.id)
        if field.key == PRODUCTION_GATE_FIELD
    )
    production_gate.field_type = "text"
    production_gate.options = []
    production_gate.archived_at = CLOSED_AT - timedelta(days=1)
    deploy_field = await service.add_field_definition(
        object_type,
        {
            "key": "deploy_state",
            "name": "Retired Deploy State",
            "field_type": "enum",
            "options": ["staging", "deployed", "prod_pending"],
        },
        emit_event=False,
    )
    deploy_field.options = ["staging", "deployed"]
    deploy_field.archived_at = CLOSED_AT - timedelta(days=1)
    await session.flush()
    record = await _tracked_issue(
        session,
        domain,
        number=1281,
        title="PostgreSQL deadlock",
    )
    pr = FixingPullRequest(
        repo=REPO,
        number=1305,
        base_ref_name="staging",
        merge_commit_sha="a" * 40,
        merged_at=CLOSED_AT,
    )
    key = (REPO, 1281, 1305)

    await run_staging_only_closure_sweep(
        session,
        org_id=ORG_ID,
        github=_Github(
            IssueClosure(
                repo=REPO,
                number=1281,
                title=record.title,
                state="closed",
                closed_at=CLOSED_AT,
                closed_by="uwear-claw",
                fixing_pull_requests=(pr,),
            ),
            _deploy_batch(
                key,
                repo=REPO,
                sha=pr.merge_commit_sha,
                state=DeployState.STAGING,
                in_staging=True,
                in_main=False,
                main_status="diverged",
            ),
        ),
        slack=_Slack(),
        now=CLOSED_AT,
    )

    await session.refresh(production_gate)
    await session.refresh(deploy_field)
    await session.refresh(record)
    assert production_gate.archived_at is None
    assert production_gate.field_type == "enum"
    assert PRODUCTION_GATE_PENDING in production_gate.options
    assert deploy_field.archived_at is not None
    assert PRODUCTION_GATE_PENDING not in deploy_field.options
    assert record.data[PRODUCTION_GATE_FIELD] == PRODUCTION_GATE_PENDING
    assert "deploy_state" not in record.data


@pytest.mark.asyncio
async def test_runtime_sweep_resolves_and_posts_to_software_channel(session, monkeypatch):
    import brain.systems.slack.client as runtime_client

    domain = await _tracker(session)
    record = await _tracked_issue(
        session,
        domain,
        number=1281,
        title="PostgreSQL deadlock",
    )
    pr = FixingPullRequest(
        repo=REPO,
        number=1305,
        base_ref_name="staging",
        merge_commit_sha="a" * 40,
        merged_at=CLOSED_AT,
    )
    key = (REPO, 1281, 1305)
    runtime_slack = _ResolvableSlack()
    monkeypatch.setattr(
        runtime_client,
        "slack_web_client_from_runtime",
        AsyncMock(return_value=runtime_slack),
    )

    summary = await run_staging_only_closure_sweep(
        session,
        org_id=ORG_ID,
        github=_Github(
            IssueClosure(
                repo=REPO,
                number=1281,
                title=record.title,
                state="closed",
                closed_at=CLOSED_AT,
                closed_by="uwear-claw",
                fixing_pull_requests=(pr,),
            ),
            _deploy_batch(
                key,
                repo=REPO,
                sha=pr.merge_commit_sha,
                state=DeployState.STAGING,
                in_staging=True,
                in_main=False,
                main_status="diverged",
            ),
        ),
        now=CLOSED_AT,
    )

    assert summary["messages_posted"] == 1
    assert runtime_slack.channel_reads
    assert runtime_slack.posts[0]["channel"] == "C_SOFTWARE"


@pytest.mark.asyncio
async def test_cold_start_sweep_updates_staging_closure_without_side_notice(session):
    domain = await _tracker(session)
    record = await _tracked_issue(
        session,
        domain,
        number=1281,
        title="PostgreSQL deadlock",
    )
    pr = FixingPullRequest(
        repo=REPO,
        number=1305,
        base_ref_name="staging",
        merge_commit_sha="a" * 40,
        merged_at=CLOSED_AT,
    )
    key = (REPO, 1281, 1305)
    slack = _Slack()

    summary = await run_staging_only_closure_sweep(
        session,
        org_id=ORG_ID,
        github=_Github(
            IssueClosure(
                repo=REPO,
                number=1281,
                title=record.title,
                state="closed",
                closed_at=CLOSED_AT,
                closed_by="uwear-claw",
                fixing_pull_requests=(pr,),
            ),
            _deploy_batch(
                key,
                repo=REPO,
                sha=pr.merge_commit_sha,
                state=DeployState.STAGING,
                in_staging=True,
                in_main=False,
                main_status="diverged",
            ),
        ),
        slack=slack,
        notify=False,
        now=CLOSED_AT,
    )

    await session.refresh(record)
    assert summary["updated"] == 1
    assert summary["flagged"] == 1
    assert summary["messages_posted"] == 0
    assert record.data["status"] == "In Review"
    assert record.data[PRODUCTION_GATE_FIELD] == PRODUCTION_GATE_PENDING
    assert record.data["fix_merge_sha"] == pr.merge_commit_sha
    assert "deploy_state" not in record.data
    assert slack.posts == []


@pytest.mark.asyncio
async def test_recent_stored_alert_occurrence_raises_finding_severity(session):
    domain = await _tracker(session)
    record = await _tracked_issue(
        session,
        domain,
        number=1281,
        title="PostgreSQL deadlock",
    )
    record.data = {
        **record.data,
        "error_signature": "DeadlockDetectedError",
        "rollbar_item": "Uwear-API#2323",
    }
    session.add(
        ProviderAlertOccurrence(
            org_id=ORG_ID,
            channel_id="C_ALERTS",
            slack_message_ts="1785139039.000001",
            service="Uwear-API",
            subsystem="database",
            external_id="Uwear-API#2323",
            signature="rollbar:2323",
            signature_title="DeadlockDetectedError",
            occurrence_milestone=None,
            is_new_error=False,
            is_new_signature=False,
            occurred_at=CLOSED_AT - timedelta(minutes=71),
        )
    )
    await session.flush()
    pr = FixingPullRequest(
        repo=REPO,
        number=1305,
        base_ref_name="staging",
        merge_commit_sha="a" * 40,
        merged_at=CLOSED_AT,
    )
    key = (REPO, 1281, 1305)
    slack = _Slack()

    summary = await run_staging_only_closure_sweep(
        session,
        org_id=ORG_ID,
        github=_Github(
            IssueClosure(
                repo=REPO,
                number=1281,
                title=record.title,
                state="closed",
                closed_at=CLOSED_AT,
                closed_by="uwear-claw",
                fixing_pull_requests=(pr,),
            ),
            _deploy_batch(
                key,
                repo=REPO,
                sha=pr.merge_commit_sha,
                state=DeployState.STAGING,
                in_staging=True,
                in_main=False,
                main_status="diverged",
            ),
        ),
        slack=slack,
        now=CLOSED_AT,
    )

    assert summary["findings"][0]["severity"] == "high"
    assert summary["findings"][0]["production_evidence"] == "Uwear-API#2323"
    assert "PROD FAILURE STILL LIVE" in slack.posts[0]["text"]
