"""Durable signature throttle and acknowledgement gate for provider alerts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import re
from typing import Any, Mapping, Sequence

from sqlalchemy import select

from brain.platform.db.models.provider_alert import ProviderAlertLedger
from brain.platform.db.repositories.unit_of_work import UnitOfWork
from brain.platform.provider_alerts import ProviderAlertDecision


_ACK_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bknown(?: issue| incident| benign)?\b",
        r"\bbenign\b",
        r"\b(?:please\s+)?stop(?: posting| alerting)?\b",
        r"\bstand down\b",
        r"\bfalse alarm\b",
        r"\b(?:safe to )?ignore\b",
        r"\bno action (?:needed|required)\b",
    )
)
_NEGATED_ACK = re.compile(
    r"\b(?:(?:not|isn't|is not|wasn't|was not)\s+(?:known|benign|a false alarm)|no known)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ProviderAlertAcknowledgement:
    user: str
    ts: str
    text: str


@dataclass(frozen=True)
class ProviderAlertLedgerSnapshot:
    occurrence_count: int
    occurrences_at_last_post: int
    last_posted_at: datetime | None
    slack_thread_ts: str | None
    acknowledged_at: datetime | None
    acknowledged_by: str | None
    acknowledgement: str | None


@dataclass(frozen=True)
class ProviderAlertPostGate:
    suppress: bool
    reason: str | None = None
    delta_line: str | None = None
    acknowledged_by: str | None = None
    acknowledged_at: str | None = None


def _utcnow(now: datetime | None = None) -> datetime:
    value = now or datetime.now(timezone.utc)
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _message_key(message: Mapping[str, Any]) -> tuple[int, Decimal, str]:
    ts = str(message.get("ts") or "").strip()
    try:
        return (0, Decimal(ts), "")
    except InvalidOperation:
        return (1, Decimal(0), ts)


def _human_message(message: Mapping[str, Any], *, illo_user_id: str | None) -> bool:
    user = str(message.get("user") or "").strip()
    if not user or message.get("bot_id") or message.get("bot_profile") or message.get("app_id"):
        return False
    return not illo_user_id or user.casefold() != str(illo_user_id).strip().casefold()


def _author(message: Mapping[str, Any]) -> str:
    profile = message.get("user_profile")
    if isinstance(profile, Mapping):
        for key in ("display_name", "real_name", "name"):
            value = " ".join(str(profile.get(key) or "").split())
            if value:
                return value[:120]
    return str(message.get("user") or "human")[:120]


def find_provider_alert_acknowledgement(
    messages: Sequence[Mapping[str, Any]],
    *,
    illo_user_id: str | None = None,
) -> ProviderAlertAcknowledgement | None:
    """Return the latest explicit human known/stop/benign acknowledgement."""

    acknowledgement = None
    for message in sorted(messages, key=_message_key):
        if not isinstance(message, Mapping) or not _human_message(
            message,
            illo_user_id=illo_user_id,
        ):
            continue
        text = " ".join(str(message.get("text") or "").split())
        if _NEGATED_ACK.search(text) or "?" in text:
            continue
        if any(pattern.search(text) for pattern in _ACK_PATTERNS):
            acknowledgement = ProviderAlertAcknowledgement(
                user=_author(message),
                ts=str(message.get("ts") or "").strip(),
                text=text[:500],
            )
    return acknowledgement


async def _read_acknowledgement(
    client: Any,
    snapshot: ProviderAlertLedgerSnapshot | None,
    *,
    channel_id: str,
    illo_user_id: str | None,
) -> ProviderAlertAcknowledgement | None:
    if snapshot is None or not snapshot.slack_thread_ts:
        return None
    read_replies = getattr(client, "conversation_replies", None)
    if not callable(read_replies):
        return None
    messages: list[Mapping[str, Any]] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()
    while True:
        response = await read_replies(
            channel=channel_id,
            thread_ts=snapshot.slack_thread_ts,
            limit=200,
            cursor=cursor,
        )
        if not isinstance(response, Mapping):
            break
        messages.extend(
            message
            for message in response.get("messages") or []
            if isinstance(message, Mapping)
        )
        metadata = response.get("response_metadata")
        metadata = metadata if isinstance(metadata, Mapping) else {}
        next_cursor = str(metadata.get("next_cursor") or "").strip()
        if not next_cursor or next_cursor in seen_cursors:
            break
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    return find_provider_alert_acknowledgement(messages, illo_user_id=illo_user_id)


def _ledger_query(*, org_id: str, channel_id: str, signature: str):
    return select(ProviderAlertLedger).where(
        ProviderAlertLedger.org_id == org_id,
        ProviderAlertLedger.channel_id == channel_id,
        ProviderAlertLedger.signature == signature,
    )


def _snapshot(row: ProviderAlertLedger) -> ProviderAlertLedgerSnapshot:
    return ProviderAlertLedgerSnapshot(
        occurrence_count=int(row.occurrence_count or 0),
        occurrences_at_last_post=int(row.occurrences_at_last_post or 0),
        last_posted_at=row.last_posted_at,
        slack_thread_ts=row.slack_thread_ts,
        acknowledged_at=row.acknowledged_at,
        acknowledged_by=row.acknowledged_by,
        acknowledgement=row.acknowledgement,
    )


async def load_provider_alert_ledger(
    *,
    org_id: str,
    channel_id: str,
    signature: str,
) -> ProviderAlertLedgerSnapshot | None:
    """Load one signature row in a fresh transaction for every post attempt."""

    async with UnitOfWork() as uow:
        row = await uow.session.scalar(
            _ledger_query(
                org_id=org_id,
                channel_id=channel_id,
                signature=signature,
            )
        )
        return _snapshot(row) if row is not None else None


async def gate_provider_alert_post(
    client: Any,
    *,
    org_id: str,
    channel_id: str,
    illo_user_id: str | None,
    decision: ProviderAlertDecision,
    now: datetime | None = None,
) -> ProviderAlertPostGate:
    """Persist this occurrence and suppress acknowledged/recent duplicates."""

    current_time = _utcnow(now)
    snapshot = await load_provider_alert_ledger(
        org_id=org_id,
        channel_id=channel_id,
        signature=decision.signature,
    )
    acknowledgement = await _read_acknowledgement(
        client,
        snapshot,
        channel_id=channel_id,
        illo_user_id=illo_user_id,
    )
    async with UnitOfWork() as uow:
        row = await uow.session.scalar(
            _ledger_query(
                org_id=org_id,
                channel_id=channel_id,
                signature=decision.signature,
            ).with_for_update()
        )
        if row is None:
            uow.session.add(
                ProviderAlertLedger(
                    org_id=org_id,
                    channel_id=channel_id,
                    signature=decision.signature,
                    classification=decision.classification,
                    severity=decision.severity,
                    rule_id=decision.rule_id,
                    occurrence_count=1,
                    occurrences_at_last_post=0,
                    first_seen_at=current_time,
                    last_seen_at=current_time,
                )
            )
            await uow.session.flush()
            return ProviderAlertPostGate(suppress=False)
        row.occurrence_count = int(row.occurrence_count or 0) + 1
        row.last_seen_at = current_time
        row.classification = decision.classification
        row.severity = decision.severity
        row.rule_id = decision.rule_id
        if acknowledgement is not None:
            if row.acknowledged_at is None or row.acknowledgement != acknowledgement.text:
                row.acknowledged_at = current_time
            row.acknowledged_by = acknowledgement.user
            row.acknowledgement = acknowledgement.text
        delta = max(
            1,
            int(row.occurrence_count or 0) - int(row.occurrences_at_last_post or 0),
        )
        delta_line = f"still ongoing, +{delta} since last"
        if row.acknowledged_at is not None:
            return ProviderAlertPostGate(
                suppress=True,
                reason="signature_acknowledged",
                delta_line=delta_line,
                acknowledged_by=row.acknowledged_by,
                acknowledged_at=row.acknowledged_at.isoformat(),
            )
        last_posted_at = row.last_posted_at
        if last_posted_at is not None:
            last_posted_at = _utcnow(last_posted_at)
            if current_time - last_posted_at <= timedelta(minutes=decision.throttle_minutes):
                return ProviderAlertPostGate(
                    suppress=True,
                    reason="duplicate_within_throttle",
                    delta_line=delta_line,
                )
        return ProviderAlertPostGate(suppress=False)


async def record_provider_alert_posted(
    *,
    org_id: str,
    channel_id: str,
    message_ts: str | None,
    thread_ts: str | None,
    decision: ProviderAlertDecision,
    now: datetime | None = None,
) -> None:
    """Record a confirmed Slack post; failed deliveries never consume throttle."""

    current_time = _utcnow(now)
    async with UnitOfWork() as uow:
        row = await uow.session.scalar(
            _ledger_query(
                org_id=org_id,
                channel_id=channel_id,
                signature=decision.signature,
            ).with_for_update()
        )
        if row is None:
            row = ProviderAlertLedger(
                org_id=org_id,
                channel_id=channel_id,
                signature=decision.signature,
                classification=decision.classification,
                severity=decision.severity,
                rule_id=decision.rule_id,
                occurrence_count=1,
                occurrences_at_last_post=1,
                first_seen_at=current_time,
                last_seen_at=current_time,
            )
            uow.session.add(row)
        else:
            row.classification = decision.classification
            row.severity = decision.severity
            row.rule_id = decision.rule_id
            row.last_seen_at = current_time
            row.occurrences_at_last_post = int(row.occurrence_count or 1)
        row.last_posted_at = current_time
        row.slack_message_ts = message_ts
        row.slack_thread_ts = thread_ts or message_ts
        await uow.session.flush()


__all__ = [
    "ProviderAlertAcknowledgement",
    "ProviderAlertLedgerSnapshot",
    "ProviderAlertPostGate",
    "find_provider_alert_acknowledgement",
    "gate_provider_alert_post",
    "load_provider_alert_ledger",
    "record_provider_alert_posted",
]
