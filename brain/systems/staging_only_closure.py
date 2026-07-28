"""Keep GitHub issue closure from outrunning production deployment."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
import re
from typing import Any, Protocol

from sqlalchemy import select

from brain.platform.db.models.domain import (
    Domain,
    DomainFieldDefinition,
    DomainObjectType,
    DomainRecord,
)
from brain.platform.db.models.provider_alert import ProviderAlertOccurrence
from brain.systems.deploy_record_contract import deploy_ticket_object_keys
from brain.systems.deploy_state import (
    DeployState,
    DeployStateBatch,
    DeployStateObservation,
)
from brain.systems.user_domains.service import AsyncDomainService


TRACKER_DOMAIN_ID = 1
SOFTWARE_CHANNEL = "#4_software"
logger = logging.getLogger("illo.staging_only_closure")

_ISSUE_REF_RE = re.compile(
    r"\bgithub:(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+):"
    r"issue:(?P<number>[1-9][0-9]*)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class FixingPullRequest:
    """One merged PR GitHub links as closing an issue."""

    repo: str
    number: int
    base_ref_name: str
    merge_commit_sha: str
    merged_at: datetime | None = None

    @property
    def canonical_ref(self) -> str:
        return f"{self.repo}#{self.number}"


@dataclass(frozen=True, slots=True)
class IssueClosure:
    """The closure facts required by the tracker sweep."""

    repo: str
    number: int
    title: str
    state: str
    closed_at: datetime | None
    closed_by: str | None
    fixing_pull_requests: tuple[FixingPullRequest, ...]


@dataclass(frozen=True, slots=True)
class ProductionEvidence:
    """One live-production signal that can raise a closure finding."""

    source: str
    reference: str
    signature: str
    occurred_at: datetime
    is_open: bool = False


class ClosureGithubClient(Protocol):
    async def get_issue_closure(
        self,
        *,
        repo: str,
        issue_number: int,
    ) -> IssueClosure | None: ...

    async def derive_deploy_states(
        self,
        refs: Mapping[object, tuple[str, str]],
    ) -> DeployStateBatch: ...


class BackendClosureGithubClient:
    """Runtime adapter over the shared backend GitHub credential/read path."""

    def __init__(self, *, org_id: str, user_id: str | None = None) -> None:
        self.org_id = str(org_id)
        self.user_id = user_id

    async def get_issue_closure(
        self,
        *,
        repo: str,
        issue_number: int,
    ) -> IssueClosure | None:
        from brain.systems.runs.tool_catalog.handlers.github import (
            github_issue_closure_for_backend,
        )

        payload = await github_issue_closure_for_backend(
            repo_slug=repo,
            issue_number=issue_number,
            org_id=self.org_id,
            user_id=self.user_id,
        )
        if payload is None:
            return None
        pulls: list[FixingPullRequest] = []
        for raw in payload.get("fixing_pull_requests") or []:
            if not isinstance(raw, Mapping):
                continue
            try:
                number = int(raw.get("number"))
            except (TypeError, ValueError):
                continue
            merge_sha = _text(raw.get("merge_commit_sha"))
            if number < 1 or not merge_sha:
                continue
            pulls.append(
                FixingPullRequest(
                    repo=_text(raw.get("repo")) or repo,
                    number=number,
                    base_ref_name=_text(raw.get("base_ref_name")),
                    merge_commit_sha=merge_sha,
                    merged_at=_parse_datetime(raw.get("merged_at")),
                )
            )
        return IssueClosure(
            repo=_text(payload.get("repo")) or repo,
            number=int(payload.get("number") or issue_number),
            title=_text(payload.get("title")),
            state=_text(payload.get("state")),
            closed_at=_parse_datetime(payload.get("closed_at")),
            closed_by=_text(payload.get("closed_by")) or None,
            fixing_pull_requests=tuple(pulls),
        )

    async def derive_deploy_states(
        self,
        refs: Mapping[object, tuple[str, str]],
    ) -> DeployStateBatch:
        from brain.systems.runs.tool_catalog.handlers.github import (
            github_deploy_states_for_backend,
        )

        return await github_deploy_states_for_backend(
            refs,
            org_id=self.org_id,
            user_id=self.user_id,
        )


class ProductionEvidenceReader(Protocol):
    async def list_recent(
        self,
        session: Any,
        *,
        org_id: str,
        since: datetime,
        until: datetime,
    ) -> Sequence[ProductionEvidence]: ...


class StoredAlertEvidenceReader:
    """Read normalized ``#alerts`` Rollbar occurrences already in the database."""

    async def list_recent(
        self,
        session: Any,
        *,
        org_id: str,
        since: datetime,
        until: datetime,
    ) -> Sequence[ProductionEvidence]:
        rows = (
            await session.scalars(
                select(ProviderAlertOccurrence)
                .where(
                    ProviderAlertOccurrence.org_id == str(org_id),
                    ProviderAlertOccurrence.occurred_at >= since,
                    ProviderAlertOccurrence.occurred_at <= until,
                )
                .order_by(
                    ProviderAlertOccurrence.occurred_at.asc(),
                    ProviderAlertOccurrence.id.asc(),
                )
            )
        ).all()
        return tuple(
            ProductionEvidence(
                source="#alerts",
                reference=row.external_id,
                signature=row.signature_title,
                occurred_at=_utc(row.occurred_at) or since,
            )
            for row in rows
        )


def _text(value: object) -> str:
    return str(value or "").strip()


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return _utc(value)
    text = _text(value)
    if not text:
        return None
    try:
        return _utc(datetime.fromisoformat(text.replace("Z", "+00:00")))
    except ValueError:
        return None


def _signature(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", _text(value).casefold())


def _issue_identity(record: DomainRecord) -> tuple[str, int] | None:
    data = dict(record.data or {})
    candidates = (
        data.get("external_id"),
        data.get("ref"),
        data.get("url"),
        record.title,
    )
    for candidate in candidates:
        match = _ISSUE_REF_RE.search(_text(candidate))
        if match:
            return match.group("repo"), int(match.group("number"))

    repo = _text(data.get("repo"))
    raw_number = data.get("issue_number")
    if not repo or raw_number in (None, "") or isinstance(raw_number, bool):
        return None
    try:
        number = int(raw_number)
    except (TypeError, ValueError):
        return None
    return (repo, number) if number > 0 else None


async def _tracked_issue_records(session: Any, *, org_id: str) -> list[DomainRecord]:
    return list(
        (
            await session.scalars(
                select(DomainRecord)
                .join(
                    DomainObjectType,
                    DomainObjectType.id == DomainRecord.object_type_id,
                )
                .join(Domain, Domain.id == DomainRecord.domain_id)
                .where(
                    DomainRecord.org_id == str(org_id),
                    DomainRecord.domain_id == TRACKER_DOMAIN_ID,
                    Domain.archived_at.is_(None),
                    DomainObjectType.key.in_(deploy_ticket_object_keys()),
                    DomainObjectType.archived_at.is_(None),
                    DomainRecord.archived_at.is_(None),
                )
                .order_by(DomainRecord.id.asc())
            )
        ).all()
    )


async def ensure_closure_workflow_fields(
    session: Any,
    *,
    org_id: str,
) -> dict[str, int]:
    """Provision/revive the non-ancestry ``prod_pending`` workflow marker."""

    object_types = (
        await session.scalars(
            select(DomainObjectType)
            .join(Domain, Domain.id == DomainObjectType.domain_id)
            .where(
                Domain.org_id == str(org_id),
                Domain.id == TRACKER_DOMAIN_ID,
                Domain.slug == "github-ticket-tracker",
                Domain.archived_at.is_(None),
                DomainObjectType.key.in_(deploy_ticket_object_keys()),
                DomainObjectType.archived_at.is_(None),
            )
            .order_by(DomainObjectType.id)
        )
    ).all()
    service = AsyncDomainService(session)
    changed = 0
    for object_type in object_types:
        fields = (
            await session.scalars(
                select(DomainFieldDefinition)
                .where(
                    DomainFieldDefinition.object_type_id == object_type.id,
                    DomainFieldDefinition.key == "deploy_state",
                )
                .order_by(DomainFieldDefinition.id)
            )
        ).all()
        if not fields:
            await service.add_field_definition(
                object_type,
                {
                    "key": "deploy_state",
                    "name": "Deploy State",
                    "field_type": "enum",
                    "options": ["prod_pending"],
                },
                emit_event=False,
            )
            changed += 1
            continue
        field = fields[0]
        options = list(field.options or [])
        desired_options = [*options]
        if "prod_pending" not in desired_options:
            desired_options.append("prod_pending")
        if (
            field.archived_at is not None
            or field.field_type != "enum"
            or desired_options != options
        ):
            field.archived_at = None
            field.field_type = "enum"
            field.required = False
            field.options = desired_options
            changed += 1
    if changed:
        await session.flush()
    return {"object_types": len(object_types), "fields_changed": changed}


def _latest_branch_result(
    observation: DeployStateObservation,
    branch: str,
) -> str:
    return observation.branch_ancestry_result(branch)


def _progress_line(
    closure: IssueClosure,
    pr: FixingPullRequest,
    observation: DeployStateObservation,
) -> str:
    closed_at = _utc(closure.closed_at)
    timestamp = closed_at.isoformat() if closed_at else "unknown time"
    return (
        f"Staging-only closure detected at {timestamp}: PR #{pr.number} "
        f"({pr.canonical_ref}) merged to `{pr.base_ref_name}`; "
        f"main ancestry: {_latest_branch_result(observation, 'main')}. "
        "Action: promote/verify."
    )


def _append_once(current: object, line: str) -> tuple[str, bool]:
    existing = _text(current)
    if line in existing:
        return existing, False
    return f"{existing}\n{line}".lstrip(), True


def _matching_production_evidence(
    record: DomainRecord,
    closure: IssueClosure,
    evidence: Sequence[ProductionEvidence],
    *,
    since: datetime,
    until: datetime,
) -> ProductionEvidence | None:
    data = dict(record.data or {})
    references = {
        _signature(data.get("rollbar_item")),
        _signature(data.get("alert_external_id")),
    }
    signatures = {
        _signature(data.get("error_signature")),
        _signature(data.get("signature")),
        _signature(closure.title),
    }
    references.discard("")
    signatures.discard("")
    for item in evidence:
        occurred_at = _utc(item.occurred_at)
        if occurred_at is None or not since <= occurred_at <= until:
            continue
        source = _text(item.source).casefold()
        if source == "rollbar":
            if not item.is_open:
                continue
        elif source not in {"#alerts", "alerts", "slack:#alerts"}:
            continue
        reference = _signature(item.reference)
        item_signature = _signature(item.signature)
        if reference and reference in references:
            return item
        if item_signature and any(
            item_signature == signature
            or item_signature in signature
            or signature in item_signature
            for signature in signatures
        ):
            return item
    return None


async def _update_record(
    session: Any,
    *,
    record: DomainRecord,
    closure: IssueClosure,
    nonproduction: Sequence[
        tuple[FixingPullRequest, DeployStateObservation]
    ],
) -> tuple[bool, bool]:
    data = dict(record.data or {})
    note = _text(data.get("progress_note"))
    is_new = False
    for candidate_pr, candidate_observation in nonproduction:
        note, appended = _append_once(
            note,
            _progress_line(
                closure,
                candidate_pr,
                candidate_observation,
            ),
        )
        is_new = is_new or appended
    pr, _observation = max(
        nonproduction,
        key=lambda item: (
            _utc(item[0].merged_at) or datetime.min.replace(tzinfo=timezone.utc),
            item[0].number,
        ),
    )
    patch: dict[str, object] = {
        "status": "In Review",
        "deploy_state": "prod_pending",
        "fix_pr": pr.canonical_ref,
        "fix_merge_sha": pr.merge_commit_sha,
        "progress_note": note,
    }
    if all(data.get(key) == value for key, value in patch.items()):
        return is_new, False
    await AsyncDomainService(session).update_record(
        str(record.org_id),
        record.domain_id,
        record.id,
        data_patch=patch,
        expected_version=record.version,
        actor_kind="system",
        reason=(
            f"staging_only_closure:{closure.repo}#{closure.number}:"
            f"{closure.closed_at.isoformat() if closure.closed_at else 'unknown'}"
        ),
    )
    return is_new, True


async def _mark_deployed(
    session: Any,
    *,
    record: DomainRecord,
    closure: IssueClosure,
    pr: FixingPullRequest,
) -> bool:
    data = dict(record.data or {})
    patch: dict[str, object] = {
        "status": "Done",
        "fix_pr": pr.canonical_ref,
        "fix_merge_sha": pr.merge_commit_sha,
    }
    if "deploy_state" in data:
        patch["deploy_state"] = None
    if all(data.get(key) == value for key, value in patch.items()):
        return False
    await AsyncDomainService(session).update_record(
        str(record.org_id),
        record.domain_id,
        record.id,
        data_patch=patch,
        expected_version=record.version,
        actor_kind="system",
        reason=(
            f"production_closure:{closure.repo}#{closure.number}:"
            f"{closure.closed_at.isoformat() if closure.closed_at else 'unknown'}"
        ),
    )
    return True


def _render_batch(
    closures: Sequence[
        tuple[IssueClosure, FixingPullRequest, ProductionEvidence | None]
    ],
) -> str:
    closer = next(
        (
            _text(closure.closed_by)
            for closure, _pr, _evidence in closures
            if _text(closure.closed_by)
        ),
        "closer",
    )
    lines = [f"@{closer}: closed issues still need production promotion/verification:"]
    for closure, pr, evidence in closures:
        severity = ""
        evidence_suffix = ""
        if evidence is not None:
            severity = "⚠️ *PROD FAILURE STILL LIVE* — "
            evidence_suffix = (
                f" Live evidence: {evidence.source} {evidence.reference} "
                f"at {_utc(evidence.occurred_at).isoformat()}."
            )
        lines.append(
            f"• {severity}#{closure.number} is closed, but #{pr.number} is on "
            f"`{pr.base_ref_name}` only and `main` does not contain it; "
            f"the action is promote/verify.{evidence_suffix}"
        )
    return "\n".join(lines)


def _batch_key(closure: IssueClosure) -> tuple[str, datetime | None]:
    closed_at = _utc(closure.closed_at)
    minute = closed_at.replace(second=0, microsecond=0) if closed_at else None
    return _text(closure.closed_by).casefold(), minute


async def _resolve_channel(client: Any, channel: str) -> str:
    list_channels = getattr(client, "conversations_list", None)
    if not channel.startswith("#") or not callable(list_channels):
        return channel
    name = channel.removeprefix("#")
    cursor: str | None = None
    seen: set[str] = set()
    while True:
        response = await list_channels(
            types="public_channel,private_channel",
            limit=200,
            cursor=cursor,
            exclude_archived=True,
        )
        for candidate in response.get("channels") or []:
            if (
                isinstance(candidate, Mapping)
                and _text(candidate.get("name")) == name
            ):
                return _text(candidate.get("id")) or channel
        metadata = response.get("response_metadata")
        metadata = metadata if isinstance(metadata, Mapping) else {}
        next_cursor = _text(metadata.get("next_cursor"))
        if not next_cursor or next_cursor in seen:
            return channel
        seen.add(next_cursor)
        cursor = next_cursor


async def run_staging_only_closure_sweep(
    session: Any,
    *,
    org_id: str,
    github: ClosureGithubClient | None = None,
    production_evidence: ProductionEvidenceReader | None = None,
    slack: Any | None = None,
    now: datetime | None = None,
) -> dict[str, object]:
    """Reconcile closed Domain-1 issues against production ancestry."""

    current_time = _utc(now) or datetime.now(timezone.utc)
    evidence_since = current_time - timedelta(hours=24)
    github = github or BackendClosureGithubClient(org_id=str(org_id))
    errors: list[str] = []
    field_summary = await ensure_closure_workflow_fields(
        session,
        org_id=str(org_id),
    )
    records = await _tracked_issue_records(session, org_id=org_id)
    closure_semaphore = asyncio.Semaphore(8)

    async def read_closure(
        record: DomainRecord,
    ) -> tuple[DomainRecord, IssueClosure] | None:
        identity = _issue_identity(record)
        if identity is None:
            return None
        repo, issue_number = identity
        try:
            async with closure_semaphore:
                closure = await github.get_issue_closure(
                    repo=repo,
                    issue_number=issue_number,
                )
        except Exception as exc:  # noqa: BLE001 - isolate one repository read
            logger.warning(
                "closure read failed for %s#%s: %s",
                repo,
                issue_number,
                exc,
            )
            errors.append(f"github_issue:{repo}#{issue_number}:{exc}")
            return None
        if (
            closure is None
            or _text(closure.state).casefold() != "closed"
            or not closure.fixing_pull_requests
        ):
            return None
        return record, closure

    read_results = await asyncio.gather(
        *(read_closure(record) for record in records)
    )
    candidates = [
        result
        for result in read_results
        if result is not None
    ]

    refs: dict[tuple[str, int, int], tuple[str, str]] = {}
    for _record, closure in candidates:
        for pr in closure.fixing_pull_requests:
            if not _text(pr.merge_commit_sha):
                continue
            key = (closure.repo, closure.number, pr.number)
            refs[key] = (pr.repo, pr.merge_commit_sha)
    try:
        states = await github.derive_deploy_states(refs)
    except Exception as exc:  # noqa: BLE001 - no ancestry means no closure mutation
        logger.warning("closure ancestry batch failed safely: %s", exc)
        errors.append(f"github_ancestry:{exc}")
        states = DeployStateBatch(
            {},
            observations_by_key={},
            observations_by_ref={},
        )
    try:
        recent_evidence = tuple(
            await (production_evidence or StoredAlertEvidenceReader()).list_recent(
                session,
                org_id=str(org_id),
                since=evidence_since,
                until=current_time,
            )
        )
    except Exception as exc:  # noqa: BLE001 - severity degrades, ancestry does not
        logger.warning("closure production-evidence read failed safely: %s", exc)
        errors.append(f"production_evidence:{exc}")
        recent_evidence = ()

    surfaced: list[
        tuple[IssueClosure, FixingPullRequest, ProductionEvidence | None]
    ] = []
    findings: list[dict[str, object]] = []
    updated = 0
    for record, closure in candidates:
        nonproduction: list[
            tuple[FixingPullRequest, DeployStateObservation]
        ] = []
        deployed_pr: FixingPullRequest | None = None
        for pr in closure.fixing_pull_requests:
            key = (closure.repo, closure.number, pr.number)
            observation = states.observations_by_key.get(key)
            if observation is None:
                continue
            if observation.state is DeployState.DEPLOYED:
                deployed_pr = pr
            elif observation.state in {
                DeployState.STAGING,
                DeployState.UNMERGED,
            }:
                nonproduction.append((pr, observation))
        if deployed_pr is not None:
            updated += int(
                await _mark_deployed(
                    session,
                    record=record,
                    closure=closure,
                    pr=deployed_pr,
                )
            )
            continue
        if not nonproduction:
            continue
        nonproduction.sort(
            key=lambda item: (
                _utc(item[0].merged_at)
                or datetime.min.replace(tzinfo=timezone.utc),
                item[0].number,
            ),
            reverse=True,
        )
        pr, observation = nonproduction[0]
        live_evidence = _matching_production_evidence(
            record,
            closure,
            recent_evidence,
            since=evidence_since,
            until=current_time,
        )
        findings.append(
            {
                "record_id": record.id,
                "issue_number": closure.number,
                "pr_number": pr.number,
                "base_ref_name": pr.base_ref_name,
                "main_ancestry": _latest_branch_result(observation, "main"),
                "severity": "high" if live_evidence is not None else "normal",
                "production_evidence": (
                    live_evidence.reference
                    if live_evidence is not None
                    else None
                ),
            }
        )
        is_new, changed = await _update_record(
            session,
            record=record,
            closure=closure,
            nonproduction=nonproduction,
        )
        updated += int(changed)
        if is_new:
            surfaced.append((closure, pr, live_evidence))

    posted = 0
    if surfaced and slack is None:
        try:
            from brain.systems.slack.client import slack_web_client_from_runtime

            slack = await slack_web_client_from_runtime(
                requested_by="staging_only_closure_sweep",
                reason=(
                    "Post one batched promote/verify action for prematurely "
                    "closed GitHub issues."
                ),
            )
        except Exception as exc:  # noqa: BLE001 - tracker corrections remain valid
            logger.warning("staging-only closure Slack client unavailable: %s", exc)
            errors.append(f"slack_client:{exc}")
    if surfaced and slack is not None:
        groups: dict[
            tuple[str, datetime | None],
            list[tuple[IssueClosure, FixingPullRequest, ProductionEvidence | None]],
        ] = {}
        for item in surfaced:
            groups.setdefault(_batch_key(item[0]), []).append(item)
        try:
            software_channel = await _resolve_channel(slack, SOFTWARE_CHANNEL)
        except Exception as exc:  # noqa: BLE001
            logger.warning("staging-only closure channel lookup failed: %s", exc)
            errors.append(f"slack_channel:{exc}")
            software_channel = SOFTWARE_CHANNEL
        for group in groups.values():
            try:
                await slack.post_message(
                    channel=software_channel,
                    text=_render_batch(group),
                )
                posted += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("staging-only closure Slack post failed: %s", exc)
                errors.append(f"slack_post:{exc}")
    return {
        "examined": len(records),
        "closed": len(candidates),
        "updated": updated,
        "flagged": len(findings),
        "findings": findings,
        "messages_posted": posted,
        "fields": field_summary,
        "errors": errors,
    }
