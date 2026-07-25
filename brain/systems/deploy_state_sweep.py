"""Deterministic persistence wiring for the deploy-state lifecycle.

The pure axis and ladder live in :mod:`brain.systems.deploy_state`.  This module
owns best-effort reads of GitHub-ticket records and monitored-alert threads,
plus optimistic writes through ``AsyncDomainService``. It never posts to Slack
and never lets GitHub, Slack, or record conflicts fail a coordinator sweep,
webhook ingestion, or notification tick.
"""

from __future__ import annotations

import inspect
import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Awaitable, Mapping, Protocol, Sequence

from sqlalchemy import and_, func, or_, select

from brain.platform.db.models.domain import (
    Domain,
    DomainEvent,
    DomainFieldDefinition,
    DomainObjectType,
    DomainRecord,
)
from brain.platform.db.models.run import AgentRun
from brain.systems.deploy_state import (
    DeployState,
    MergeKind,
    as_utc_datetime,
    classify_merge_event,
    derive_deploy_state,
)
from brain.systems.deploy_state_config import (
    deploy_feature_enabled,
    deploy_quiet_window,
    deploy_settle_window,
    deploy_ticket_object_keys,
)
from brain.systems.deploy_state_github import is_ancestor_of
from brain.systems.user_domains.service import AsyncDomainService


logger = logging.getLogger("illo.deploy_state")

DEPLOY_STATE_FIELD_DEFINITIONS: tuple[dict, ...] = (
    {"key": "rollbar_item", "name": "Rollbar Item", "field_type": "text"},
    {"key": "alert_last_seen_at", "name": "Alert Last Seen At", "field_type": "datetime"},
    {"key": "alert_occurrences", "name": "Alert Occurrences", "field_type": "number"},
    {"key": "fix_pr", "name": "Fix PR", "field_type": "text"},
    {"key": "fix_merge_sha", "name": "Fix Merge SHA", "field_type": "text"},
    {"key": "fix_merged_at", "name": "Fix Merged At", "field_type": "datetime"},
    {
        "key": "deploy_state",
        "name": "Deploy State",
        "field_type": "enum",
        "options": [state.value for state in DeployState],
    },
    {"key": "deployed_at", "name": "Deployed At", "field_type": "datetime"},
    {"key": "verified_at", "name": "Verified At", "field_type": "datetime"},
    {
        "key": "alert_slack_channel",
        "name": "Alert Slack Channel",
        "field_type": "text",
    },
    {
        "key": "alert_slack_thread_ts",
        "name": "Alert Slack Thread Timestamp",
        "field_type": "text",
    },
    {
        "key": "alert_slack_bot_user_id",
        "name": "Alert Slack Bot User Id",
        "field_type": "text",
    },
    {
        "key": "resolution_confirmed_ts",
        "name": "Resolution Confirming Slack Timestamp",
        "field_type": "text",
    },
    {
        "key": "resolution_reproduced_ts",
        "name": "Resolution Reproduced Slack Timestamp",
        "field_type": "text",
    },
    {
        "key": "promotion_recommended_at",
        "name": "Promotion Recommended At",
        "field_type": "datetime",
    },
)

_SLACK_THREAD_PAGE_LIMIT = 200
_SLACK_THREAD_MAX_PAGES = 3
_REPRODUCTION_PATTERNS = (
    re.compile(r"\bstill reproduc(?:e|es|ing|ible)\b"),
    re.compile(r"\bstill (?:broken|failing|happening|seeing)\b"),
    re.compile(r"\b(?:not|isn t|isnt) fix(?:ed)?\b"),
    re.compile(r"\bpas fix(?:e)?\b"),
    re.compile(r"\b(?:encore|toujours)\b.{0,40}\breprodu"),
    re.compile(r"\breprodu\w*\b.{0,40}\b(?:encore|toujours)\b"),
    re.compile(r"\b(?:ca|cela) (?:ne )?marche pas\b"),
)
_UNSHIPPED_PATTERNS = (
    re.compile(r"\b(?:not|isn t|isnt|pas) (?:yet )?deploy"),
    re.compile(r"\b(?:not|isn t|isnt|pas) (?:yet )?merge"),
    re.compile(r"\b(?:awaiting|pending|waiting for) deploy"),
    re.compile(r"\b(?:will|va|vais|allons) deploy"),
)
_VERIFICATION_PATTERNS = (
    re.compile(r"\bc est fix(?:e|ed)?\b"),
    re.compile(r"\b(?:ca|cela) a l air (?:d etre|detre) fix(?:e|ed)?\b"),
    re.compile(r"\blooks? (?:like (?:it|this) is )?fix(?:ed)?\b"),
    re.compile(r"\bseems? fix(?:ed)?\b"),
    re.compile(r"\bconfirm(?:ed|e)?\b.{0,30}\bfix(?:e|ed)?\b"),
    re.compile(r"\bverif(?:ied|ie|iee)\b"),
)
_DEPLOYMENT_RE = re.compile(r"\bdeploy(?:e|ed|ment)?\b")
_MERGE_OR_PROMOTION_RE = re.compile(r"\b(?:merge(?:d|e|ee)?|promot(?:ed|e|ee|ion))\b")
_PR_REFERENCE_RE = re.compile(r"\b(?:pr|pull request)\b|#[1-9][0-9]*\b")
_REPO_PR_RE = re.compile(
    r"\b(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(?P<number>[1-9][0-9]*)\b"
)
_GITHUB_PR_URL_RE = re.compile(
    r"https?://github\.com/(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/(?P<number>[1-9][0-9]*)\b",
    re.IGNORECASE,
)
_GITHUB_PR_KEY_RE = re.compile(
    r"\bgithub:(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+):pr:(?P<number>[1-9][0-9]*)\b",
    re.IGNORECASE,
)
_GITHUB_ISSUE_KEY_RE = re.compile(
    r"\bgithub:(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+):issue:[1-9][0-9]*\b",
    re.IGNORECASE,
)
_SHORT_PR_RE = re.compile(r"\b(?:pr|pull request)\s*#?(?P<number>[1-9][0-9]*)\b", re.IGNORECASE)
_BARE_PR_RE = re.compile(r"^(?P<number>[1-9][0-9]*)$")


@dataclass(frozen=True, slots=True)
class AlertThreadSource:
    """A tracker record and its monitored-alert Slack thread provenance."""

    record: DomainRecord
    channel_id: str
    thread_ts: str
    bot_user_id: str | None = None


@dataclass(frozen=True, slots=True)
class _ResolutionSignal:
    kind: str
    message_ts: str
    text: str
    source: AlertThreadSource
    fix_pr: str | None = None


class QuietCheck(Protocol):
    """Replaceable quiet-check seam; a future Rollbar implementation fits here."""

    def __call__(
        self,
        *,
        data: Mapping[str, object],
        deployed_at: datetime,
        settle_until: datetime,
        now: datetime,
    ) -> bool | None | Awaitable[bool | None]: ...


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, datetime | str):
        return None
    return as_utc_datetime(value)


def _iso(value: datetime | str | object) -> str | None:
    parsed = _parse_datetime(value)
    return parsed.isoformat() if parsed else None


def append_progress_note(data: Mapping[str, object], line: str) -> str | None:
    """Append one line using the tracker progress-note convention."""
    if "progress_note" not in data:
        return None
    current = str(data.get("progress_note") or "").rstrip()
    return f"{current}\n{line}".lstrip()


def _text(value: object) -> str:
    return str(value or "").strip()


def _normalized_human_text(value: object) -> str:
    decomposed = unicodedata.normalize("NFKD", _text(value).casefold())
    ascii_text = "".join(char for char in decomposed if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9#/:._-]+", " ", ascii_text).strip()


def _slack_ts_key(value: object) -> Decimal:
    try:
        return Decimal(_text(value))
    except (InvalidOperation, ValueError):
        return Decimal("-1")


def _slack_ts_iso(value: str) -> str | None:
    timestamp = _slack_ts_key(value)
    if timestamp < 0:
        return None
    try:
        return datetime.fromtimestamp(float(timestamp), tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _human_reply(message: Mapping[str, object], *, bot_user_id: str | None) -> bool:
    user_id = _text(message.get("user"))
    if not user_id or message.get("bot_id"):
        return False
    if _text(message.get("subtype")).casefold() == "bot_message":
        return False
    return not bot_user_id or user_id != bot_user_id


def github_repo_from_issue_text(text: str) -> str | None:
    """Extract ``owner/repo`` from a tracker GitHub issue key."""
    match = _GITHUB_ISSUE_KEY_RE.search(_text(text))
    return match.group("repo") if match else None


def fix_pr_from_text(text: str, *, repo: str | None) -> str | None:
    """Normalize a supported PR reference to ``owner/repo#N``."""
    url_match = _GITHUB_PR_URL_RE.search(text)
    if url_match:
        return f"{url_match.group('repo')}#{url_match.group('number')}"
    key_match = _GITHUB_PR_KEY_RE.search(text)
    if key_match:
        return f"{key_match.group('repo')}#{key_match.group('number')}"
    repo_match = _REPO_PR_RE.search(text)
    if repo_match:
        return f"{repo_match.group('repo')}#{repo_match.group('number')}"
    short_match = _SHORT_PR_RE.search(text)
    clean_repo = _text(repo)
    if short_match and clean_repo:
        return f"{clean_repo}#{short_match.group('number')}"
    bare_match = _BARE_PR_RE.fullmatch(_text(text))
    if bare_match and clean_repo:
        return f"{clean_repo}#{bare_match.group('number')}"
    return None


def _resolution_kind(text: str) -> str | None:
    normalized = _normalized_human_text(text)
    if any(pattern.search(normalized) for pattern in _REPRODUCTION_PATTERNS):
        return "reproduced"
    if any(pattern.search(normalized) for pattern in _UNSHIPPED_PATTERNS):
        return None
    if any(pattern.search(normalized) for pattern in _VERIFICATION_PATTERNS):
        return "verified"
    if _DEPLOYMENT_RE.search(normalized):
        return "deployed"
    if _MERGE_OR_PROMOTION_RE.search(normalized) and _PR_REFERENCE_RE.search(normalized):
        return "deployed"
    return None


def _resolution_signals(
    messages: Sequence[Mapping[str, object]],
    *,
    source: AlertThreadSource,
) -> list[_ResolutionSignal]:
    repo = _text((source.record.data or {}).get("repo")) or None
    signals: list[_ResolutionSignal] = []
    for message in messages:
        if not _human_reply(message, bot_user_id=source.bot_user_id):
            continue
        message_ts = _text(message.get("ts"))
        text = _text(message.get("text"))
        if not text or _slack_ts_key(message_ts) < 0:
            continue
        kind = _resolution_kind(text)
        if kind is None:
            continue
        signals.append(
            _ResolutionSignal(
                kind=kind,
                message_ts=message_ts,
                text=text,
                source=source,
                fix_pr=(
                    fix_pr_from_text(text, repo=repo)
                    if kind in {"deployed", "verified"}
                    else None
                ),
            )
        )
    return signals


def alert_thread_source_from_run(
    record: DomainRecord,
    run: AgentRun,
) -> AlertThreadSource | None:
    """Recover monitored-alert provenance retained on the creating/updating run."""
    metadata = dict(getattr(run, "metadata_", None) or {})
    if not (
        metadata.get("slack_monitor") is True
        or _text(metadata.get("origin")) == "slack_channel_monitor"
    ):
        return None
    target_ref = dict(getattr(run, "target_ref", None) or {})
    raw_trigger = target_ref.get("slack_trigger") or metadata.get("slack_trigger")
    if not isinstance(raw_trigger, Mapping):
        return None
    trigger = dict(raw_trigger)
    channel_id = _text(trigger.get("channel_id") or target_ref.get("channel_id"))
    thread_ts = _text(
        trigger.get("thread_ts")
        or trigger.get("message_ts")
        or target_ref.get("thread_ts")
        or target_ref.get("message_ts")
    )
    if not channel_id or not thread_ts:
        return None
    return AlertThreadSource(
        record=record,
        channel_id=channel_id,
        thread_ts=thread_ts,
        bot_user_id=_text(trigger.get("bot_user_id")) or None,
    )


async def _alert_thread_sources(session, *, org_id: str) -> list[AlertThreadSource]:
    records = await ticket_records(session, org_id=org_id)
    if not records:
        return []
    records_by_id = {record.id: record for record in records}
    sources: list[AlertThreadSource] = []
    seen: set[tuple[int, str, str]] = set()

    # Once harvested, retain coordinates on the record so provenance survives
    # run/event retention and future refreshes do not depend on an old join.
    for record in records:
        data = record.data or {}
        channel_id = _text(data.get("alert_slack_channel"))
        thread_ts = _text(data.get("alert_slack_thread_ts"))
        if channel_id and thread_ts:
            key = (record.id, channel_id, thread_ts)
            seen.add(key)
            sources.append(
                AlertThreadSource(
                    record=record,
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                    bot_user_id=_text(data.get("alert_slack_bot_user_id")) or None,
                )
            )

    rows = (
        await session.execute(
            select(DomainEvent.record_id, AgentRun)
            .join(AgentRun, AgentRun.id == DomainEvent.run_id)
            .where(
                DomainEvent.org_id == str(org_id),
                DomainEvent.record_id.in_(records_by_id),
                DomainEvent.event_type.in_({"record.created", "record.updated"}),
            )
            .order_by(DomainEvent.created_at.asc(), DomainEvent.id.asc())
        )
    ).all()
    for record_id, run in rows:
        record = records_by_id.get(record_id)
        if record is None:
            continue
        source = alert_thread_source_from_run(record, run)
        if source is None:
            continue
        key = (record.id, source.channel_id, source.thread_ts)
        if key in seen:
            if source.bot_user_id:
                for index, existing in enumerate(sources):
                    if (
                        existing.record.id,
                        existing.channel_id,
                        existing.thread_ts,
                    ) == key:
                        sources[index] = source
                        break
            continue
        seen.add(key)
        sources.append(source)
    return sources


async def _read_slack_thread(client: Any, source: AlertThreadSource) -> list[Mapping[str, object]]:
    messages: list[Mapping[str, object]] = []
    cursor: str | None = None
    for _page in range(_SLACK_THREAD_MAX_PAGES):
        payload = await client.conversation_replies(
            channel=source.channel_id,
            thread_ts=source.thread_ts,
            limit=_SLACK_THREAD_PAGE_LIMIT,
            cursor=cursor,
        )
        raw_messages = payload.get("messages") or []
        messages.extend(message for message in raw_messages if isinstance(message, Mapping))
        cursor = _text((payload.get("response_metadata") or {}).get("next_cursor")) or None
        if cursor is None:
            break
    return messages


def _matching_enum_option(field: DomainFieldDefinition | None, *candidates: str) -> str | None:
    if field is None:
        return None
    options = {
        _text(option).casefold(): _text(option)
        for option in (field.options or [])
        if _text(option)
    }
    for candidate in candidates:
        if candidate.casefold() in options:
            return options[candidate.casefold()]
    return None


def _resolution_note(signal: _ResolutionSignal) -> str:
    quote = re.sub(r"\s+", " ", signal.text).strip()[:300]
    if signal.kind == "verified":
        movement = "Alert-thread outcome verified by a human"
    elif signal.kind == "deployed":
        movement = "Alert-thread movement: fix deployed per a human"
    else:
        movement = "Alert-thread reproduce report; fix remains open"
    return f'{movement} at Slack ts {signal.message_ts}: "{quote}"'


async def _resolution_patch(
    session,
    *,
    record: DomainRecord,
    signal: _ResolutionSignal,
    latest_confirmation: _ResolutionSignal | None,
    latest_deployment: _ResolutionSignal | None,
    latest_fix_pr: str | None,
) -> dict[str, object]:
    fields = {
        field.key: field
        for field in await AsyncDomainService(session).list_fields(record.object_type_id)
    }
    data = record.data or {}
    patch: dict[str, object] = {
        "alert_slack_channel": signal.source.channel_id,
        "alert_slack_thread_ts": signal.source.thread_ts,
    }
    if signal.source.bot_user_id:
        patch["alert_slack_bot_user_id"] = signal.source.bot_user_id
    if latest_confirmation is not None:
        patch["resolution_confirmed_ts"] = latest_confirmation.message_ts
        if latest_fix_pr:
            patch["fix_pr"] = latest_fix_pr

    signal_iso = _slack_ts_iso(signal.message_ts)
    if signal.kind == "verified":
        patch["deploy_state"] = DeployState.VERIFIED.value
        if signal_iso:
            patch["verified_at"] = signal_iso
        deployed_iso = _slack_ts_iso(latest_deployment.message_ts) if latest_deployment else None
        if not data.get("deployed_at") and (deployed_iso or signal_iso):
            patch["deployed_at"] = deployed_iso or signal_iso
        status = _matching_enum_option(fields.get("status"), "Done")
    elif signal.kind == "deployed":
        patch.update({"deploy_state": DeployState.DEPLOYED.value, "verified_at": None})
        if signal_iso:
            patch["deployed_at"] = signal_iso
        status = _matching_enum_option(fields.get("status"), "In Review", "In Progress", "Todo")
    else:
        patch.update(
            {
                "deploy_state": None,
                "verified_at": None,
                "resolution_reproduced_ts": signal.message_ts,
            }
        )
        status = _matching_enum_option(fields.get("status"), "Todo", "In Progress", "Blocked")
    if status is not None:
        patch["status"] = status
    note = append_progress_note(data, _resolution_note(signal))
    if note is not None:
        patch["progress_note"] = note
    return {key: value for key, value in patch.items() if key in fields}


async def run_alert_resolution_harvest(
    session,
    *,
    org_id: str,
    slack_client: Any | None = None,
    sources: Sequence[AlertThreadSource] | None = None,
) -> dict:
    """Harvest human resolution from monitored-alert threads without posting.

    This is deliberately a Slack read path: the only client operation used is
    ``conversations.replies``. A later human reproduction report wins over an
    earlier deploy/fix claim, and processed message timestamps make replay
    idempotent so an old confirmation cannot re-close a subsequently reopened
    tracker record.
    """
    summary = {
        "examined": 0,
        "threads_read": 0,
        "messages_read": 0,
        "updated": 0,
        "deployed": 0,
        "verified": 0,
        "reproduced": 0,
        "skipped": 0,
        "movements": [],
        "errors": [],
    }
    if not deploy_feature_enabled():
        summary["disabled"] = True
        return summary
    try:
        summary["fields"] = await ensure_deploy_state_fields(session, org_id=org_id)
        resolved_sources = list(sources) if sources is not None else await _alert_thread_sources(
            session, org_id=org_id
        )
    except Exception as exc:
        logger.exception("alert resolution harvest setup failed safely")
        summary["errors"].append(str(exc))
        return summary
    if not resolved_sources:
        return summary
    if slack_client is None:
        try:
            from brain.systems.slack.client import slack_web_client_from_runtime

            slack_client = await slack_web_client_from_runtime(
                requested_by="alert_resolution_harvest",
                reason="Read monitored-alert thread replies for human resolution.",
            )
        except Exception as exc:
            logger.warning("alert resolution Slack read unavailable: %s", exc)
            summary["errors"].append(str(exc))
            return summary

    grouped: dict[int, list[AlertThreadSource]] = {}
    for source in resolved_sources:
        grouped.setdefault(source.record.id, []).append(source)
    for record_sources in grouped.values():
        summary["examined"] += 1
        signals: list[_ResolutionSignal] = []
        record = record_sources[0].record
        for source in record_sources:
            try:
                messages = await _read_slack_thread(slack_client, source)
            except Exception as exc:
                logger.warning(
                    "alert resolution thread read failed for record %s: %s",
                    record.id,
                    exc,
                )
                summary["errors"].append(record.id)
                continue
            summary["threads_read"] += 1
            summary["messages_read"] += len(messages)
            signals.extend(_resolution_signals(messages, source=source))
        if not signals:
            summary["skipped"] += 1
            continue
        signals.sort(key=lambda item: _slack_ts_key(item.message_ts))
        signal = signals[-1]
        confirmations = [item for item in signals if item.kind in {"deployed", "verified"}]
        latest_confirmation = confirmations[-1] if confirmations else None
        deployments = [item for item in confirmations if item.kind == "deployed"]
        latest_deployment = deployments[-1] if deployments else None
        latest_fix_pr = next(
            (item.fix_pr for item in reversed(confirmations) if item.fix_pr),
            None,
        )

        data = record.data or {}
        processed_ts = max(
            _slack_ts_key(data.get("resolution_confirmed_ts")),
            _slack_ts_key(data.get("resolution_reproduced_ts")),
        )
        if _slack_ts_key(signal.message_ts) <= processed_ts:
            summary["skipped"] += 1
            continue
        patch = await _resolution_patch(
            session,
            record=record,
            signal=signal,
            latest_confirmation=latest_confirmation,
            latest_deployment=latest_deployment,
            latest_fix_pr=latest_fix_pr,
        )
        if not patch:
            summary["skipped"] += 1
            continue
        try:
            await update_record(
                session,
                record,
                patch,
                reason=f"alert_resolution_harvest:{signal.kind}:{signal.message_ts}",
            )
        except Exception as exc:
            logger.warning("alert resolution update failed for record %s: %s", record.id, exc)
            summary["errors"].append(record.id)
            continue
        summary["updated"] += 1
        summary[signal.kind] += 1
        summary["movements"].append(
            {
                "record_id": record.id,
                "outcome": signal.kind,
                "message_ts": signal.message_ts,
            }
        )
    return summary


async def ensure_deploy_state_fields(session, *, org_id: str) -> dict:
    """Idempotently add optional deploy fields to every active ticket type."""
    object_types = (
        await session.scalars(
            select(DomainObjectType)
            .join(Domain, Domain.id == DomainObjectType.domain_id)
            .where(
                Domain.org_id == str(org_id),
                Domain.archived_at.is_(None),
                DomainObjectType.key.in_(deploy_ticket_object_keys()),
                DomainObjectType.archived_at.is_(None),
            )
            .order_by(DomainObjectType.id)
        )
    ).all()
    service = AsyncDomainService(session)
    added = 0
    for object_type in object_types:
        existing = {
            field.key
            for field in await service.list_fields(object_type.id)
        }
        for payload in DEPLOY_STATE_FIELD_DEFINITIONS:
            if payload["key"] in existing:
                continue
            await service.add_field_definition(object_type, dict(payload), emit_event=False)
            existing.add(str(payload["key"]))
            added += 1
    return {"object_types": len(object_types), "fields_added": added}


def _json_text(session, key: str):
    bind = session.get_bind()
    if bind is not None and bind.dialect.name == "sqlite":
        return func.json_extract(DomainRecord.data, f"$.{key}")
    return DomainRecord.data[key].as_string()


async def ticket_records(
    session,
    *,
    org_id: str,
    states: set[str] | None = None,
    fix_pr_prefix: str | None = None,
    fix_pr: str | None = None,
) -> list[DomainRecord]:
    """Select ticket records by deploy-state and/or fix-PR identity.

    Selection keys on where the FIX lives (``fix_pr`` is repo-qualified), not
    on the ticket's own ``repo`` field — an app ticket fixed by a backend PR
    must be swept by the backend promotion, not the app one.
    """
    stmt = (
        select(DomainRecord)
        .join(DomainObjectType, DomainObjectType.id == DomainRecord.object_type_id)
        .join(Domain, Domain.id == DomainRecord.domain_id)
        .where(
            DomainRecord.org_id == str(org_id),
            DomainRecord.archived_at.is_(None),
            Domain.archived_at.is_(None),
            DomainObjectType.key.in_(deploy_ticket_object_keys()),
            DomainObjectType.archived_at.is_(None),
        )
    )
    conditions = []
    if states:
        state_arm = _json_text(session, "deploy_state").in_(states)
        if fix_pr_prefix:
            state_arm = and_(
                state_arm,
                _json_text(session, "fix_pr").like(f"{fix_pr_prefix}%"),
            )
        conditions.append(state_arm)
    if fix_pr:
        conditions.append(_json_text(session, "fix_pr") == fix_pr)
    if conditions:
        stmt = stmt.where(or_(*conditions))
    return list((await session.scalars(stmt.order_by(DomainRecord.id))).all())


async def update_record(
    session,
    record: DomainRecord,
    patch: dict,
    *,
    reason: str,
) -> None:
    async with session.begin_nested():
        await AsyncDomainService(session).update_record(
            str(record.org_id),
            record.domain_id,
            record.id,
            data_patch=patch,
            expected_version=record.version,
            actor_kind="system",
            reason=reason,
        )


async def _check_ancestry(
    repo: str,
    sha: str,
    branch: str,
    tokens: Sequence[str | None],
) -> bool | None:
    """Try ordered read identities until ancestry is determinate."""
    for token in tokens:
        if token is None:
            result = await is_ancestor_of(repo, sha, branch)
        else:
            result = await is_ancestor_of(repo, sha, branch, token=token)
        if result is not None:
            return result
    return None


def _new_summary(kind: MergeKind | str) -> dict:
    return {
        "merge_kind": str(kind),
        "examined": 0,
        "updated": 0,
        "deployed": 0,
        "prod_pending": 0,
        "staging": 0,
        "indeterminate": 0,
        "skipped": 0,
        "errors": [],
    }


async def _sweep_main_merge(
    session,
    *,
    org_id: str,
    repo: str,
    event: Mapping,
    ancestry_tokens: Sequence[str | None],
    summary: dict,
) -> None:
    kind = classify_merge_event(event)
    fix_pr = f"{repo}#{event.get('number')}" if kind is MergeKind.HOTFIX and event.get("number") else None
    records = await ticket_records(
        session,
        org_id=org_id,
        states={DeployState.STAGING.value, DeployState.PROD_PENDING.value},
        fix_pr_prefix=f"{repo}#",
        fix_pr=fix_pr,
    )
    for record in records:
        summary["examined"] += 1
        data = record.data or {}
        sha = str(data.get("fix_merge_sha") or "").strip()
        if fix_pr and data.get("fix_pr") == fix_pr and event.get("merge_commit_sha"):
            sha = str(event["merge_commit_sha"])
        if not sha:
            summary["skipped"] += 1
            continue
        in_main = await _check_ancestry(repo, sha, "main", ancestry_tokens)
        if in_main is None:
            summary["indeterminate"] += 1
            continue
        patch: dict = {}
        if in_main:
            deployed_at = _iso(event.get("merged_at"))
            if not deployed_at:
                summary["skipped"] += 1
                continue
            patch.update({"deploy_state": DeployState.DEPLOYED.value, "deployed_at": deployed_at})
            if fix_pr and data.get("fix_pr") == fix_pr:
                patch.update({"fix_merge_sha": sha, "fix_merged_at": deployed_at})
            note = append_progress_note(data, f"deployed to main at {deployed_at}")
            if note is not None:
                patch["progress_note"] = note
            bucket = "deployed"
        elif data.get("deploy_state") == DeployState.STAGING.value:
            in_staging = await _check_ancestry(repo, sha, "staging", ancestry_tokens)
            if in_staging is None:
                summary["indeterminate"] += 1
                continue
            if not in_staging:
                summary["skipped"] += 1
                continue
            patch["deploy_state"] = DeployState.PROD_PENDING.value
            note = append_progress_note(data, "fix confirmed on staging; awaiting promotion")
            if note is not None:
                patch["progress_note"] = note
            bucket = "prod_pending"
        else:
            summary["skipped"] += 1
            continue
        try:
            await update_record(session, record, patch, reason=f"deploy_sweep:{kind.value}")
        except Exception as exc:
            logger.warning("deploy sweep skipped record %s: %s", record.id, exc)
            summary["errors"].append(record.id)
            continue
        summary["updated"] += 1
        summary[bucket] += 1


async def _sweep_staging_merge(
    session,
    *,
    org_id: str,
    repo: str,
    event: Mapping,
    ancestry_tokens: Sequence[str | None],
    summary: dict,
) -> None:
    number = event.get("number")
    sha = str(event.get("merge_commit_sha") or "").strip()
    merged_at = _iso(event.get("merged_at"))
    if not number or not sha or not merged_at:
        summary["skipped"] += 1
        return
    fix_pr = f"{repo}#{number}"
    records = await ticket_records(session, org_id=org_id, fix_pr=fix_pr)
    for record in records:
        summary["examined"] += 1
        data = record.data or {}
        in_main = await _check_ancestry(repo, sha, "main", ancestry_tokens)
        state = derive_deploy_state(
            merged=True,
            base_ref="staging",
            in_staging=True,
            in_main=in_main,
        )
        if state is None:
            state = DeployState.STAGING
        patch = {
            "fix_merge_sha": sha,
            "fix_merged_at": merged_at,
            "deploy_state": state.value,
        }
        if state is DeployState.DEPLOYED:
            patch["deployed_at"] = merged_at
        note = append_progress_note(data, f"fix {fix_pr} merged to staging at {merged_at}")
        if note is not None:
            patch["progress_note"] = note
        try:
            await update_record(session, record, patch, reason="deploy_sweep:fix_to_staging")
        except Exception as exc:
            logger.warning("deploy sweep skipped record %s: %s", record.id, exc)
            summary["errors"].append(record.id)
            continue
        summary["updated"] += 1
        summary[state.value] += 1
        if in_main is None:
            summary["indeterminate"] += 1


async def run_deploy_sweep(
    session,
    *,
    org_id: str,
    repo: str,
    merge_event: Mapping,
    ancestry_tokens: Sequence[str | None] | None = None,
) -> dict:
    """Apply one merged-PR event, returning telemetry and never raising."""
    kind = classify_merge_event(merge_event)
    summary = _new_summary(kind)
    if not deploy_feature_enabled(repo):
        summary["disabled"] = True
        return summary
    try:
        tokens = tuple(ancestry_tokens or (None,))
        summary["fields"] = await ensure_deploy_state_fields(session, org_id=org_id)
        if kind in {MergeKind.PROMOTION, MergeKind.HOTFIX}:
            await _sweep_main_merge(
                session,
                org_id=org_id,
                repo=repo,
                event=merge_event,
                ancestry_tokens=tokens,
                summary=summary,
            )
        elif kind is MergeKind.FIX_TO_STAGING:
            await _sweep_staging_merge(
                session,
                org_id=org_id,
                repo=repo,
                event=merge_event,
                ancestry_tokens=tokens,
                summary=summary,
            )
    except Exception as exc:
        logger.exception("deploy sweep failed safely for %s", repo)
        summary["errors"].append(str(exc))
    return summary


def infer_quiet_from_record(
    *,
    data: Mapping[str, object],
    deployed_at: datetime,
    settle_until: datetime,
    now: datetime,
) -> bool | None:
    """Default quiet check: infer from the last alert occurrence we ingested."""
    raw_last_seen = data.get("alert_last_seen_at")
    if raw_last_seen in {None, ""}:
        return True
    last_seen = _parse_datetime(raw_last_seen)
    if last_seen is None:
        return None
    return last_seen <= settle_until


async def _check_quiet(check: QuietCheck, **kwargs) -> bool | None:
    result = check(**kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


async def run_deploy_verification(
    session,
    *,
    org_id: str,
    now: datetime,
    quiet_check: QuietCheck = infer_quiet_from_record,
) -> dict:
    """Close only deployed tickets that stayed quiet through the full window."""
    summary = {
        "examined": 0,
        "eligible": 0,
        "verified": 0,
        "not_quiet": 0,
        "indeterminate": 0,
        "skipped": 0,
        "errors": [],
    }
    if not deploy_feature_enabled():
        summary["disabled"] = True
        return summary
    try:
        summary["fields"] = await ensure_deploy_state_fields(session, org_id=org_id)
        records = await ticket_records(
            session,
            org_id=org_id,
            states={DeployState.DEPLOYED.value},
        )
    except Exception as exc:
        logger.exception("deploy verification setup failed safely")
        summary["errors"].append(str(exc))
        return summary

    current = _parse_datetime(now)
    if current is None:
        summary["errors"].append("invalid now")
        return summary
    settle = deploy_settle_window()
    quiet_window = deploy_quiet_window()
    for record in records:
        summary["examined"] += 1
        data = record.data or {}
        deployed_at = _parse_datetime(data.get("deployed_at"))
        if deployed_at is None:
            summary["skipped"] += 1
            continue
        settle_until = deployed_at + settle
        if current < settle_until + quiet_window:
            summary["skipped"] += 1
            continue
        summary["eligible"] += 1
        try:
            quiet = await _check_quiet(
                quiet_check,
                data=data,
                deployed_at=deployed_at,
                settle_until=settle_until,
                now=current,
            )
        except Exception as exc:
            logger.warning("quiet check failed for record %s: %s", record.id, exc)
            summary["indeterminate"] += 1
            continue
        if quiet is None:
            summary["indeterminate"] += 1
            continue
        if quiet is False:
            summary["not_quiet"] += 1
            continue
        deployed_iso = deployed_at.isoformat()
        patch = {
            "deploy_state": DeployState.VERIFIED.value,
            "verified_at": current.isoformat(),
            "status": "Done",
        }
        note = append_progress_note(
            data,
            f"verified quiet since deploy at {deployed_iso}",
        )
        if note is not None:
            patch["progress_note"] = note
        try:
            await update_record(session, record, patch, reason="deploy_verification")
        except Exception as exc:
            logger.warning("deploy verification skipped record %s: %s", record.id, exc)
            summary["errors"].append(record.id)
            continue
        summary["verified"] += 1
    return summary
