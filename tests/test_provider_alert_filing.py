from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateTable

from brain.platform.db.models.provider_alert import (
    ProviderAlertFilingClaim, ProviderAlertOccurrence, ProviderAlertSurge,
)
from brain.platform.provider_alerts import classify_provider_alert_ingest
from brain.systems.cortex.project_context.github import GitHubConnectorError
from brain.systems.runs.execution_context import bind_agent_context
from brain.systems.runs.tool_catalog.handlers import github
from brain.systems.slack import provider_alert_filing as filing
from brain.systems.slack.provider_alert_surge import record_provider_alert_occurrence

ORG = "4b5e2f59-4f88-4956-a660-4f544ccffbd7"
REPO = "uwear-ai/uwear-backend"
NOW = datetime(2026, 9, 6, 17, 49, tzinfo=timezone.utc)
IDENTITY = filing.FilingIdentity(ORG, "uwear-api", "generation", "a" * 64)
ISSUE = {"number": 2021, "html_url": f"https://github.com/{REPO}/issues/2021"}


@pytest.fixture
async def filing_store(tmp_path, monkeypatch):
    """Real unique inserts and CAS writes, using independent SQLite sessions.

    No server is needed. The production PostgreSQL row locks additionally prevent
    lease takeover while an active owner is inside the remote side effect.
    """
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'filing.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        for model in (ProviderAlertFilingClaim, ProviderAlertOccurrence, ProviderAlertSurge):
            await connection.execute(CreateTable(model.__table__))
    control = {"now": NOW, "fail_commit": False}

    class TestUnitOfWork:
        async def __aenter__(self):
            self.session = sessions()
            return self

        async def __aexit__(self, exc_type, exc, tb):
            try:
                if exc_type:
                    await self.session.rollback()
                elif control["fail_commit"]:
                    control["fail_commit"] = False
                    await self.session.rollback()
                    raise RuntimeError("local finalization failed")
                else:
                    await self.session.commit()
            finally:
                await self.session.close()

    monkeypatch.setattr(filing, "UnitOfWork", TestUnitOfWork)
    monkeypatch.setattr(filing, "utcnow", lambda: control["now"])
    monkeypatch.setattr(github, "_github_token_candidates", AsyncMock(return_value=[
        {"token": "test-token", "source": "test", "key_name": None},
    ]))
    monkeypatch.setattr(filing, "async_add_repo_issue_comment", AsyncMock(return_value={}))
    monkeypatch.setattr(filing, "async_list_repo_issues", AsyncMock(return_value={"issues": []}))
    try:
        yield sessions, control
    finally:
        await engine.dispose()


async def _file(identity=IDENTITY, *, body="Occurrence evidence"):
    with bind_agent_context({"user_id": "test-user", "org_id": identity.org_id}):
        return json.loads(await github._handle_create_github_issue(
            repo=REPO, title="Generation failed", body=body,
            provider_alert={"service": identity.service, "subsystem": identity.subsystem,
                            "tracked_signature": identity.tracked_signature},
        ))


@pytest.mark.asyncio
async def test_four_messages_force_claim_conflict_and_share_one_ticket(filing_store, monkeypatch):
    sessions, _ = filing_store
    for index in range(4):
        alert = classify_provider_alert_ingest(
            f"<https://app.rollbar.com/a/uwear/fix/item/Uwear-API/{2278 + index}|"
            f"#{2278 + index} New error: TimeoutError: Garment description generation timed out>"
        )
        assert alert is not None
        async with sessions.begin() as session:
            await record_provider_alert_occurrence(
                session, org_id=ORG, channel_id="C_ALERTS", message_ts=f"1788716940.{index}",
                alert=alert, occurred_at=NOW,
            )
    identity = filing.FilingIdentity(ORG, alert.service, alert.subsystem, alert.signature)
    all_contended, owner_done = asyncio.Event(), asyncio.Event()
    acquired = filing.acquire_filing_claim
    losers = set()

    async def observe_claim(*args):
        claim = await acquired(*args)
        if not claim.acquired and claim.issue_number is None:
            losers.add(asyncio.current_task())
            if len(losers) == 3:
                all_contended.set()
        return claim

    async def create(*args, **kwargs):
        await all_contended.wait()
        return {"repo": REPO, "issue": dict(ISSUE)}

    monkeypatch.setattr(filing, "acquire_filing_claim", observe_claim)
    monkeypatch.setattr(filing, "_wait_for_filing", owner_done.wait)
    create_mock = AsyncMock(side_effect=create)
    monkeypatch.setattr(filing, "async_create_repo_issue", create_mock)

    async def run(index):
        result = await _file(identity, body=f"Message 1788716940.{index}, Rollbar #{2278 + index}")
        if result.get("reused") is False:
            owner_done.set()
        return result

    results = await asyncio.wait_for(asyncio.gather(*(run(i) for i in range(4))), timeout=10)
    assert len(losers) == 3  # All three went through the losing DB-write path.
    create_mock.assert_awaited_once()
    assert identity.marker in create_mock.await_args.kwargs["body"]
    assert {r["issue"]["number"] for r in results} == {2021}
    assert {r["filing_claim_id"] for r in results} == {results[0]["filing_claim_id"]}
    assert all(r["mutated_target_refs"] == [{"kind": "github_issue", "id": f"{REPO}#2021"}] for r in results)
    # Only the initial owner may create the associated tracker record. The other
    # three receive explicit reuse results and the same canonical external ref.
    assert sum(not result["reused"] for result in results) == 1
    assert filing.async_add_repo_issue_comment.await_count == 3
    async with sessions() as session:
        occurrences = (await session.scalars(select(ProviderAlertOccurrence))).all()
        claims = (await session.scalars(select(ProviderAlertFilingClaim))).all()
    assert len(occurrences) == 4
    assert len({o.slack_message_ts for o in occurrences}) == 4
    assert len({o.external_id for o in occurrences}) == 4
    assert len({o.signature for o in occurrences}) == 1
    assert len(claims) == 1
    assert (claims[0].repo, claims[0].issue_number, claims[0].issue_url, claims[0].state) == (
        REPO, 2021, ISSUE["html_url"], "filed",
    )


@pytest.mark.asyncio
async def test_dead_owner_before_create_is_replaced_after_expiry(filing_store, monkeypatch):
    _, control = filing_store
    dead = await filing.acquire_filing_claim(IDENTITY, REPO)
    assert dead.acquired
    assert not (await filing.acquire_filing_claim(IDENTITY, REPO)).acquired
    control["now"] += timedelta(seconds=filing.CLAIM_TTL_SECONDS)
    create = AsyncMock(return_value={"repo": REPO, "issue": dict(ISSUE)})
    monkeypatch.setattr(filing, "async_create_repo_issue", create)
    result = await _file()
    assert result["issue"]["number"] == 2021
    assert result["filing_claim_id"] == dead.id
    create.assert_awaited_once()
    filing.async_list_repo_issues.assert_not_awaited()
    with pytest.raises(filing.FilingPendingError):
        await filing._create_or_reconcile(
            dead, IDENTITY, title="Generation failed", body="Occurrence evidence",
            labels=[], assignees=[], token="test-token",
        )
    create.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["commit", "process_death"])
async def test_create_survives_failed_finalization_and_reconciles(filing_store, monkeypatch, failure):
    sessions, control = filing_store
    remote = []

    async def create(*args, **kwargs):
        remote.append({**ISSUE, "body": kwargs["body"], "state": "closed"})
        if failure == "process_death":
            raise asyncio.CancelledError()
        control["fail_commit"] = True
        return {"repo": REPO, "issue": dict(ISSUE)}

    create_mock = AsyncMock(side_effect=create)
    monkeypatch.setattr(filing, "async_create_repo_issue", create_mock)
    if failure == "process_death":
        with pytest.raises(asyncio.CancelledError):
            await _file()
        control["now"] += timedelta(seconds=filing.CLAIM_TTL_SECONDS)
    else:
        assert (await _file())["filing_pending"] is True
    async with sessions() as session:
        row = await session.scalar(select(ProviderAlertFilingClaim))
        assert row.state == "creating"
        assert row.issue_number is None

    # Force pagination, and ensure even a closed issue on a later page is found.
    filing.async_list_repo_issues.side_effect = [
        {"issues": [{"number": 900, "body": "unrelated"}], "next_page": "page-two"},
        {"issues": remote},
    ]
    result = await _file(body="Retried occurrence")
    assert result["reused"] is True
    assert result["issue"]["number"] == 2021
    create_mock.assert_awaited_once()
    assert filing.async_list_repo_issues.await_args_list[1].kwargs["cursor"] == "page-two"
    assert all(c.kwargs["state"] == "all" for c in filing.async_list_repo_issues.await_args_list)
    async with sessions() as session:
        row = await session.scalar(select(ProviderAlertFilingClaim))
        assert row.state == "filed"
        assert row.issue_url == ISSUE["html_url"]


@pytest.mark.asyncio
async def test_stale_owner_cannot_release_or_prepare_successors_claim(filing_store):
    _, control = filing_store
    first = await filing.acquire_filing_claim(IDENTITY, REPO)
    control["now"] += timedelta(seconds=filing.CLAIM_TTL_SECONDS)
    successor = await filing.acquire_filing_claim(IDENTITY, REPO)
    assert successor.acquired
    await filing._release_claim(first)
    with pytest.raises(filing.FilingPendingError):
        await filing._prepare_create(first)
    current = await filing.acquire_filing_claim(IDENTITY, REPO)
    assert not current.acquired
    assert current.claimed_at == successor.claimed_at


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("org_id", "e54e4e0d-ac19-42dd-b356-9735ce68ae23"),
    ("service", "other-service"), ("subsystem", "other-subsystem"), ("tracked_signature", "b" * 64),
])
async def test_filing_identity_is_scoped_by_all_four_fields(filing_store, field, value):
    first = await filing.acquire_filing_claim(IDENTITY, REPO)
    other = await filing.acquire_filing_claim(replace(IDENTITY, **{field: value}), REPO)
    assert first.acquired and other.acquired
    assert first.id != other.id


@pytest.mark.asyncio
async def test_pending_claim_does_not_fall_through_to_unclaimed_create(filing_store, monkeypatch):
    await filing.acquire_filing_claim(IDENTITY, REPO)
    monkeypatch.setattr(filing, "WAIT_ATTEMPTS", 1)
    monkeypatch.setattr(filing, "_wait_for_filing", AsyncMock())
    create = AsyncMock()
    monkeypatch.setattr(github, "async_create_repo_issue", create)
    monkeypatch.setattr(filing, "async_create_repo_issue", create)
    result = await _file()
    assert result["filing_pending"] and result["retryable"]
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_reconciliation_never_creates_blindly(filing_store, monkeypatch):
    claim = await filing.acquire_filing_claim(IDENTITY, REPO)
    await filing._prepare_create(claim)
    await filing._release_claim(claim)
    filing.async_list_repo_issues.side_effect = GitHubConnectorError(status_code=502, message="unavailable")
    create = AsyncMock()
    monkeypatch.setattr(filing, "async_create_repo_issue", create)
    result = await _file()
    assert result["status_code"] == 502
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_timed_out_post_keeps_lease_and_reconciles_after_expiry(filing_store, monkeypatch):
    _, control = filing_store
    create = AsyncMock(side_effect=GitHubConnectorError(status_code=502, message="timed out"))
    monkeypatch.setattr(filing, "async_create_repo_issue", create)
    monkeypatch.setattr(filing, "WAIT_ATTEMPTS", 1)
    monkeypatch.setattr(filing, "_wait_for_filing", AsyncMock())
    assert (await _file())["filing_pending"]
    assert (await _file())["filing_pending"]
    create.assert_awaited_once()
    control["now"] += timedelta(seconds=filing.CLAIM_TTL_SECONDS)
    filing.async_list_repo_issues.return_value = {"issues": [{**ISSUE, "body": IDENTITY.marker}]}
    assert (await _file())["issue"]["number"] == 2021
    create.assert_awaited_once()


@pytest.mark.asyncio
async def test_canonical_repo_cannot_be_changed_by_another_run(filing_store, monkeypatch):
    claim = await filing.acquire_filing_claim(IDENTITY, REPO)
    with pytest.raises(ValueError, match="belongs to"):
        await filing.acquire_filing_claim(IDENTITY, "uwear-ai/other")
    await filing._release_claim(claim)
    create = AsyncMock(return_value={"repo": REPO, "issue": dict(ISSUE)})
    monkeypatch.setattr(filing, "async_create_repo_issue", create)
    await _file()
    canonical = await filing.acquire_filing_claim(IDENTITY, "uwear-ai/other")
    assert canonical.repo == REPO
    assert canonical.issue_number == 2021
    assert not canonical.acquired
    create.assert_awaited_once()


@pytest.mark.asyncio
async def test_missing_reconciliation_match_allows_retry_before_remote_create(filing_store, monkeypatch):
    _, control = filing_store
    claim = await filing.acquire_filing_claim(IDENTITY, REPO)
    await filing._prepare_create(claim)  # Crash immediately before the POST.
    control["now"] += timedelta(seconds=filing.CLAIM_TTL_SECONDS)
    create = AsyncMock(return_value={"repo": REPO, "issue": dict(ISSUE)})
    monkeypatch.setattr(filing, "async_create_repo_issue", create)
    assert (await _file())["issue"]["number"] == 2021
    filing.async_list_repo_issues.assert_awaited_once()
    create.assert_awaited_once()


@pytest.mark.asyncio
async def test_evidence_failure_preserves_canonical_filing(filing_store, monkeypatch):
    create = AsyncMock(return_value={"repo": REPO, "issue": dict(ISSUE)})
    monkeypatch.setattr(filing, "async_create_repo_issue", create)
    await _file()
    filing.async_add_repo_issue_comment.side_effect = GitHubConnectorError(status_code=403, message="denied")
    result = await _file()
    assert result["reused"]
    assert result["occurrence_evidence_appended"] is False
    assert result["issue"]["number"] == 2021
    create.assert_awaited_once()


def test_provider_alert_argument_is_optional_and_validates_only_when_supplied():
    from brain.systems.runs.tool_catalog.definitions.github import GITHUB_TOOLS
    definition = next(t for t in GITHUB_TOOLS if t["name"] == "create_github_issue")
    assert "provider_alert" in definition["input_schema"]["properties"]
    assert definition["input_schema"]["required"] == ["repo", "title"]
    with pytest.raises(ValueError, match="tracked_signature"):
        filing.FilingIdentity.parse(ORG, {"service": "api", "subsystem": "jobs"})


@pytest.mark.asyncio
async def test_malformed_identity_cannot_create_an_unclaimed_issue(filing_store, monkeypatch):
    create = AsyncMock()
    monkeypatch.setattr(github, "async_create_repo_issue", create)
    monkeypatch.setattr(filing, "async_create_repo_issue", create)
    with bind_agent_context({"org_id": ORG}):
        result = json.loads(await github._handle_create_github_issue(
            repo=REPO, title="Failure", provider_alert={"service": "api"},
        ))
    assert "error" in result
    create.assert_not_awaited()
