"""Atomic ownership of the initial GitHub filing for a provider-alert signature.

The unique row owns the side effect, following the material-post claim-column
pattern. Acquisition commits before calling GitHub. A fenced row lock spans the
remote operation and finalization, so an expired, still-running owner cannot
race its successor. Recovery uses a durable issue-body marker and the repository
issues endpoint (including closed issues), never the search index.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
from typing import Any, Mapping

from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from brain.kernel.common.time import utcnow
from brain.platform.db.models.provider_alert import ProviderAlertFilingClaim
from brain.platform.db.repositories.unit_of_work import UnitOfWork
from brain.systems.cortex.project_context.github import (
    GitHubConnectorError,
    async_add_repo_issue_comment,
    async_create_repo_issue,
    async_list_repo_issues,
)

CLAIM_TTL_SECONDS = 60
WAIT_ATTEMPTS = 60
logger = logging.getLogger(__name__)


class FilingPendingError(Exception):
    """Retry the same claim; do not create another issue or tracker record."""


@dataclass(frozen=True)
class FilingIdentity:
    org_id: str
    service: str
    subsystem: str
    tracked_signature: str

    @classmethod
    def parse(cls, org_id: str | None, value: Mapping[str, Any]) -> FilingIdentity:
        if not org_id:
            raise ValueError("provider_alert requires an organization context")
        fields = {}
        for name, limit in (("service", 120), ("subsystem", 120), ("tracked_signature", 64)):
            raw = value.get(name)
            if not isinstance(raw, str) or not raw.strip() or len(raw.strip()) > limit:
                raise ValueError(f"provider_alert requires {name} (1–{limit} characters)")
            fields[name] = raw.strip()
        return cls(org_id=org_id, **fields)

    @property
    def marker(self) -> str:
        key = json.dumps([self.org_id, self.service, self.subsystem, self.tracked_signature])
        return f"<!-- illo-provider-alert-filing:{hashlib.sha256(key.encode()).hexdigest()} -->"


@dataclass(frozen=True)
class FilingClaim:
    id: int
    repo: str
    claimed_at: datetime | None
    issue_number: int | None
    issue_url: str | None
    acquired: bool
    reconcile: bool = False

    def payload(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "issue": {"number": self.issue_number, "html_url": self.issue_url},
            "filing_claim_id": self.id,
            "filing_state": "filed",
            "reused": True,
        }


def _snapshot(row: ProviderAlertFilingClaim, *, acquired: bool, reconcile: bool = False) -> FilingClaim:
    return FilingClaim(row.id, row.repo, row.claimed_at, row.issue_number, row.issue_url, acquired, reconcile)


async def acquire_filing_claim(identity: FilingIdentity, repo: str) -> FilingClaim:
    """Insert the canonical key, then compare-and-set its expiring claim column."""
    now = utcnow()
    async with UnitOfWork() as uow:
        session = uow.session
        insert = sqlite_insert if session.get_bind().dialect.name == "sqlite" else postgresql_insert
        key = dict(org_id=identity.org_id, service=identity.service,
                   subsystem=identity.subsystem, tracked_signature=identity.tracked_signature)
        await session.execute(
            insert(ProviderAlertFilingClaim).values(**key, repo=repo)
            .on_conflict_do_nothing(index_elements=list(key))
        )
        row = await session.scalar(select(ProviderAlertFilingClaim).filter_by(**key).with_for_update())
        assert row is not None
        if row.state == "filed":
            return _snapshot(row, acquired=False)
        if row.repo != repo:
            raise ValueError(f"This provider-alert claim belongs to {row.repo}; retry with that repo")
        reconcile = row.state == "creating"
        # The SQL predicate also enforces arbitration in SQLite, whose FOR UPDATE
        # is a no-op. PostgreSQL uses the same predicate plus its row lock.
        won = await session.scalar(
            update(ProviderAlertFilingClaim)
            .where(
                ProviderAlertFilingClaim.id == row.id,
                ProviderAlertFilingClaim.state != "filed",
                or_(ProviderAlertFilingClaim.claimed_at.is_(None),
                    ProviderAlertFilingClaim.claimed_at <= now - timedelta(seconds=CLAIM_TTL_SECONDS)),
            )
            .values(claimed_at=now)
            .returning(ProviderAlertFilingClaim.id)
            .execution_options(synchronize_session=False)
        )
        await session.refresh(row)
        return _snapshot(row, acquired=won is not None, reconcile=reconcile)


def _owns(row: ProviderAlertFilingClaim, claim: FilingClaim) -> bool:
    def aware(value: datetime | None) -> datetime | None:
        return value.replace(tzinfo=timezone.utc) if value is not None and value.tzinfo is None else value
    return (
        claim.acquired
        and claim.claimed_at is not None
        and row.state != "filed"
        and aware(row.claimed_at) == aware(claim.claimed_at)
    )


async def _prepare_create(claim: FilingClaim) -> None:
    # Commit the uncertainty BEFORE the request. A crash or rollback after the
    # request must leave a durable instruction to reconcile on the next attempt.
    async with UnitOfWork() as uow:
        row = await uow.session.scalar(
            select(ProviderAlertFilingClaim).where(ProviderAlertFilingClaim.id == claim.id).with_for_update()
        )
        if row is None or not _owns(row, claim):
            raise FilingPendingError("Provider-alert filing ownership changed; retry the same claim")
        row.state = "creating"
        await uow.session.flush()


async def _release_claim(claim: FilingClaim) -> None:
    async with UnitOfWork() as uow:
        row = await uow.session.scalar(
            select(ProviderAlertFilingClaim).where(ProviderAlertFilingClaim.id == claim.id).with_for_update()
        )
        if row is not None and _owns(row, claim):
            row.claimed_at = None
            await uow.session.flush()


async def _find_filed_issue(repo: str, marker: str, token: str) -> dict[str, Any] | None:
    cursor = None
    while True:
        page = await async_list_repo_issues(
            repo, token=token, state="all", limit=100, cursor=cursor, body_limit=65536,
        )
        for issue in page["issues"]:
            if marker in str(issue.get("body") or ""):
                return {"repo": repo, "issue": issue}
        cursor = page.get("next_page")
        if not cursor:
            return None


async def _create_or_reconcile(
    claim: FilingClaim, identity: FilingIdentity, *,
    title: str, body: str | None, labels: list[str], assignees: list[str], token: str,
) -> dict[str, Any]:
    await _prepare_create(claim)
    async with UnitOfWork() as uow:
        row = await uow.session.scalar(
            select(ProviderAlertFilingClaim).where(ProviderAlertFilingClaim.id == claim.id).with_for_update()
        )
        if row is None or not _owns(row, claim):
            raise FilingPendingError("Provider-alert filing ownership changed; retry the same claim")
        payload = await _find_filed_issue(claim.repo, identity.marker, token) if claim.reconcile else None
        reused = payload is not None
        if payload is None:
            payload = await async_create_repo_issue(
                claim.repo, title=title, body=f"{identity.marker}\n\n{body or ''}",
                labels=labels, assignees=assignees, token=token,
            )
        issue = payload.get("issue") or {}
        number = issue.get("number")
        if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
            raise ValueError("GitHub returned no issue number; retry the same provider-alert claim")
        row.issue_number = number
        row.issue_url = issue.get("html_url") or f"https://github.com/{claim.repo}/issues/{number}"
        row.state = "filed"
        row.claimed_at = None
        await uow.session.flush()
        payload.update(filing_claim_id=row.id, filing_state="filed", reused=reused)
    return payload


async def create_provider_alert_issue(
    identity: FilingIdentity, repo: str, *,
    title: str, body: str | None, labels: list[str], assignees: list[str], token: str,
) -> dict[str, Any]:
    for _ in range(WAIT_ATTEMPTS):
        claim = await acquire_filing_claim(identity, repo)
        if claim.issue_number is not None:
            payload = claim.payload()
            break
        if claim.acquired:
            try:
                payload = await _create_or_reconcile(
                    claim, identity, title=title, body=body, labels=labels, assignees=assignees, token=token,
                )
            except Exception as exc:
                # A timed-out POST may still be running remotely. Keep the lease
                # until expiry before attempting reconciliation in that case.
                if isinstance(exc, GitHubConnectorError) and exc.status_code >= 500:
                    raise
                # Leave 'creating' intact. Release only our generation, and never
                # hide the original error if even the release cannot be saved.
                try:
                    await _release_claim(claim)
                except Exception:
                    logger.exception("provider alert filing claim release failed")
                raise
            break
        await _wait_for_filing()
    else:
        raise FilingPendingError("Provider-alert filing is in progress; retry the same claim")

    if payload["reused"]:
        evidence = body or title
        try:
            await async_add_repo_issue_comment(
                payload["repo"], payload["issue"]["number"],
                body=f"Additional provider-alert occurrence:\n\n{evidence}", token=token,
            )
            payload["occurrence_evidence_appended"] = True
        except Exception as exc:
            # Filing is already durable. Report the evidence failure without
            # allowing token fallback to repeat the initial create.
            payload["occurrence_evidence_appended"] = False
            payload["occurrence_evidence_error"] = str(exc)
    return payload


async def _wait_for_filing() -> None:
    await asyncio.sleep(0.25)
