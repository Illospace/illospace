"""Reconcile closed GitHub issues against the production deployment gate."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
from typing import Any

from brain.platform.db.models.domain import DomainRecord
from brain.systems import deploy_tracker
from brain.systems.deploy_state import DeployStateBatch
from brain.systems.production_gate_evidence import (
    ProductionEvidenceReader,
    StoredAlertEvidenceReader,
)
from brain.systems.production_gate_github import (
    BackendClosureGithubClient,
    CLOSURE_READ_AUTH_FAILURE_REASONS,
    ClosureReadFailure,
    ClosureGithubClient,
    FixingPullRequest,
    IssueClosure,
)
from brain.systems.production_gate_notifier import (
    post_production_gate_findings,
)
from brain.systems.production_gate_policy import (
    ProductionEvidence,
    ProductionGateFinding,
    classify_closure,
    matching_production_evidence,
    production_gate_progress_line,
    tracked_issue_identity,
)


TRACKER_DOMAIN_ID = 1
TRACKER_DOMAIN_SLUG = "github-ticket-tracker"
logger = logging.getLogger("illo.staging_only_closure")


@dataclass(frozen=True, slots=True)
class _ClosureReadOutcome:
    repo: str
    issue_number: int
    candidate: tuple[DomainRecord, IssueClosure] | None = None
    error: Exception | None = None


async def run_staging_only_closure_sweep(
    session: Any,
    *,
    org_id: str,
    github: ClosureGithubClient | None = None,
    production_evidence: ProductionEvidenceReader | None = None,
    slack: Any | None = None,
    notify: bool = True,
    now: datetime | None = None,
) -> dict[str, object]:
    """Reconcile closed tracker issues through shared tracker owners."""

    current_time = _utc(now) or datetime.now(timezone.utc)
    evidence_since = current_time - timedelta(hours=24)
    github = github or BackendClosureGithubClient(
        org_id=str(org_id),
        caller_label="staging_only_closure_sweep",
    )
    errors: list[str] = []
    field_summary = await deploy_tracker.ensure_production_gate_fields(
        session,
        org_id=str(org_id),
        domain_id=TRACKER_DOMAIN_ID,
        domain_slug=TRACKER_DOMAIN_SLUG,
    )
    records = await deploy_tracker.list_deploy_ticket_records(
        session,
        org_id=str(org_id),
        domain_id=TRACKER_DOMAIN_ID,
        domain_slug=TRACKER_DOMAIN_SLUG,
    )
    candidates = await _read_closed_issues(
        records,
        github=github,
        errors=errors,
    )
    states = await _read_deploy_states(
        candidates,
        github=github,
        errors=errors,
    )
    recent_evidence = await _read_production_evidence(
        session,
        reader=production_evidence or StoredAlertEvidenceReader(),
        org_id=str(org_id),
        since=evidence_since,
        until=current_time,
        errors=errors,
    )

    findings: list[ProductionGateFinding] = []
    surfaced: list[ProductionGateFinding] = []
    updated = 0
    for record, closure in candidates:
        decision = classify_closure(
            closure,
            states.observations_by_key,
        )
        if decision.deployed_pr is not None:
            pull_request = decision.deployed_pr
            transition = await deploy_tracker.mark_deployed(
                session,
                record,
                fix_pr=pull_request.canonical_ref,
                fix_merge_sha=pull_request.merge_commit_sha,
                reason=_transition_reason(
                    "production_closure",
                    closure,
                ),
            )
            updated += int(transition.changed)
            continue

        primary = decision.primary_nonproduction
        if primary is None:
            continue
        pull_request, observation = primary
        evidence = matching_production_evidence(
            dict(record.data or {}),
            closure,
            recent_evidence,
            since=evidence_since,
            until=current_time,
        )
        finding = ProductionGateFinding(
            record_id=record.id,
            closure=closure,
            pull_request=pull_request,
            observation=observation,
            production_evidence=evidence,
        )
        progress_lines = [
            production_gate_progress_line(
                closure,
                candidate_pr,
                candidate_observation,
            )
            for candidate_pr, candidate_observation in decision.nonproduction
        ]
        transition = await deploy_tracker.mark_prod_pending(
            session,
            record,
            fix_pr=pull_request.canonical_ref,
            fix_merge_sha=pull_request.merge_commit_sha,
            progress_lines=progress_lines,
            reason=_transition_reason(
                "staging_only_closure",
                closure,
            ),
        )
        updated += int(transition.changed)
        findings.append(finding)
        if transition.progress_added:
            surfaced.append(finding)
    if notify:
        posted, notification_errors = await post_production_gate_findings(
            surfaced,
            slack=slack,
        )
    else:
        posted, notification_errors = 0, []
    errors.extend(notification_errors)
    return {
        "examined": len(records),
        "closed": len(candidates),
        "updated": updated,
        "flagged": len(findings),
        "findings": [finding.summary() for finding in findings],
        "messages_posted": posted,
        "fields": field_summary,
        "errors": errors,
    }


async def _read_closed_issues(
    records: list[DomainRecord],
    *,
    github: ClosureGithubClient,
    errors: list[str],
) -> list[tuple[DomainRecord, IssueClosure]]:
    semaphore = asyncio.Semaphore(8)

    async def read(
        record: DomainRecord,
    ) -> _ClosureReadOutcome | None:
        identity = tracked_issue_identity(
            dict(record.data or {}),
            title=record.title,
        )
        if identity is None:
            return None
        repo, issue_number = identity
        try:
            async with semaphore:
                closure = await github.get_issue_closure(
                    repo=repo,
                    issue_number=issue_number,
                )
        except Exception as exc:  # noqa: BLE001 - isolate one repository read
            return _ClosureReadOutcome(
                repo=repo,
                issue_number=issue_number,
                error=exc,
            )
        if (
            closure is None
            or closure.state.casefold() != "closed"
            or not closure.fixing_pull_requests
        ):
            return _ClosureReadOutcome(repo=repo, issue_number=issue_number)
        return _ClosureReadOutcome(
            repo=repo,
            issue_number=issue_number,
            candidate=(record, closure),
        )

    results = await asyncio.gather(*(read(record) for record in records))
    attempts = [result for result in results if result is not None]
    failures = [result for result in attempts if result.error is not None]
    if failures and len(failures) == len(attempts):
        typed_failures = [
            result.error
            for result in failures
            if isinstance(result.error, ClosureReadFailure)
        ]
        reason_codes = {failure.reason_code for failure in typed_failures}
        if (
            len(typed_failures) == len(failures)
            and len(reason_codes) == 1
            and reason_codes <= CLOSURE_READ_AUTH_FAILURE_REASONS
        ):
            failure = typed_failures[0]
            logger.error(
                "closure authentication failed for all %s GitHub issue reads: %s",
                len(failures),
                failure.message,
            )
            errors.append(
                "github_issue_authentication_all_reads_failed:"
                f"count={len(failures)}:reason={failure.reason_code}:"
                f"status={failure.status_code}:{failure.message}"
            )
            return []
    for failure in failures:
        logger.warning(
            "closure read failed for %s#%s: %s",
            failure.repo,
            failure.issue_number,
            failure.error,
        )
        errors.append(
            f"github_issue:{failure.repo}#{failure.issue_number}:{failure.error}"
        )
    return [
        result.candidate
        for result in attempts
        if result.candidate is not None
    ]


async def _read_deploy_states(
    candidates: list[tuple[DomainRecord, IssueClosure]],
    *,
    github: ClosureGithubClient,
    errors: list[str],
) -> DeployStateBatch:
    if not candidates:
        return DeployStateBatch(
            {},
            observations_by_key={},
            observations_by_ref={},
        )
    refs = {
        (closure.repo, closure.number, pull_request.number): (
            pull_request.repo,
            pull_request.merge_commit_sha,
        )
        for _record, closure in candidates
        for pull_request in closure.fixing_pull_requests
    }
    try:
        return await github.derive_deploy_states(refs)
    except Exception as exc:  # noqa: BLE001 - no ancestry means no mutation
        logger.warning("closure ancestry batch failed safely: %s", exc)
        errors.append(f"github_ancestry:{exc}")
        return DeployStateBatch(
            {},
            observations_by_key={},
            observations_by_ref={},
        )


async def _read_production_evidence(
    session: Any,
    *,
    reader: ProductionEvidenceReader,
    org_id: str,
    since: datetime,
    until: datetime,
    errors: list[str],
) -> tuple[ProductionEvidence, ...]:
    try:
        return tuple(
            await reader.list_recent(
                session,
                org_id=org_id,
                since=since,
                until=until,
            )
        )
    except Exception as exc:  # noqa: BLE001 - severity degrades, ancestry does not
        logger.warning("closure production-evidence read failed safely: %s", exc)
        errors.append(f"production_evidence:{exc}")
        return ()


def _transition_reason(prefix: str, closure: IssueClosure) -> str:
    closed_at = closure.closed_at.isoformat() if closure.closed_at else "unknown"
    return f"{prefix}:{closure.repo}#{closure.number}:{closed_at}"


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
