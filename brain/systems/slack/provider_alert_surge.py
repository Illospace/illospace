"""Durable rolling-window surge detection for monitored provider alerts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import logging
from typing import Any

from sqlalchemy import select

from brain.platform.db.models.provider_alert import (
    ProviderAlertOccurrence,
    ProviderAlertSurge,
)
from brain.platform.db.repositories.unit_of_work import UnitOfWork
from brain.platform.provider_alerts import (
    ProviderAlertIngest,
    ProviderAlertSurgePolicy,
    classify_provider_alert_ingest,
    provider_alert_surge_policy,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ProviderAlertSurgeSnapshot:
    id: int
    service: str
    subsystem: str
    window_started_at: datetime
    opened_at: datetime
    last_seen_at: datetime
    trigger_reason: str
    message_count: int
    signatures: tuple[dict[str, str], ...]
    external_ids: tuple[str, ...]
    owner: str
    next_action: str
    material_channel: str
    material_message_ts: str | None
    material_post_claimed_at: datetime | None
    material_posted_at: datetime | None


@dataclass(frozen=True, slots=True)
class ProviderAlertIngestResult:
    alert: ProviderAlertIngest
    surge: ProviderAlertSurgeSnapshot | None
    material_posted: bool = False
    material_post_error: str | None = None


def _utcnow(now: datetime | None = None) -> datetime:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _json_list(value: str) -> list[Any]:
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _snapshot(row: ProviderAlertSurge) -> ProviderAlertSurgeSnapshot:
    signatures = tuple(
        {
            "signature": str(item.get("signature") or ""),
            "title": str(item.get("title") or ""),
        }
        for item in _json_list(row.signatures_json)
        if isinstance(item, dict) and item.get("signature")
    )
    return ProviderAlertSurgeSnapshot(
        id=int(row.id),
        service=row.service,
        subsystem=row.subsystem,
        window_started_at=_utcnow(row.window_started_at),
        opened_at=_utcnow(row.opened_at),
        last_seen_at=_utcnow(row.last_seen_at),
        trigger_reason=row.trigger_reason,
        message_count=int(row.message_count),
        signatures=signatures,
        external_ids=tuple(str(value) for value in _json_list(row.external_ids_json)),
        owner=row.owner,
        next_action=row.next_action,
        material_channel=row.material_channel,
        material_message_ts=row.material_message_ts,
        material_post_claimed_at=(
            _utcnow(row.material_post_claimed_at)
            if row.material_post_claimed_at
            else None
        ),
        material_posted_at=(
            _utcnow(row.material_posted_at) if row.material_posted_at else None
        ),
    )


def _surge_payload(snapshot: ProviderAlertSurgeSnapshot) -> dict[str, Any]:
    return {
        "id": snapshot.id,
        "service": snapshot.service,
        "subsystem": snapshot.subsystem,
        "window_started_at": snapshot.window_started_at.isoformat(),
        "opened_at": snapshot.opened_at.isoformat(),
        "last_seen_at": snapshot.last_seen_at.isoformat(),
        "trigger_reason": snapshot.trigger_reason,
        "message_count": snapshot.message_count,
        "signatures": list(snapshot.signatures),
        "external_ids": list(snapshot.external_ids),
        "owner": snapshot.owner,
        "next_action": snapshot.next_action,
        "material_channel": snapshot.material_channel,
        "material_message_ts": snapshot.material_message_ts,
        "material_post_claimed_at": (
            snapshot.material_post_claimed_at.isoformat()
            if snapshot.material_post_claimed_at
            else None
        ),
        "material_posted_at": (
            snapshot.material_posted_at.isoformat()
            if snapshot.material_posted_at
            else None
        ),
    }


def _surge_query(
    *,
    org_id: str,
    channel_id: str,
    alert: ProviderAlertIngest,
):
    return select(ProviderAlertSurge).where(
        ProviderAlertSurge.org_id == org_id,
        ProviderAlertSurge.source_channel_id == channel_id,
        ProviderAlertSurge.service == alert.service,
    )


def _window_signature_data(
    occurrences: list[ProviderAlertOccurrence],
) -> tuple[list[dict[str, str]], list[str]]:
    signatures: dict[str, str] = {}
    external_ids: set[str] = set()
    for occurrence in occurrences:
        signatures.setdefault(occurrence.signature, occurrence.signature_title)
        external_ids.add(occurrence.external_id)
    return (
        [
            {"signature": signature, "title": title}
            for signature, title in sorted(signatures.items(), key=lambda item: item[1])
        ],
        sorted(external_ids),
    )


def _trigger_reason(
    occurrences: list[ProviderAlertOccurrence],
    *,
    alert: ProviderAlertIngest,
    policy: ProviderAlertSurgePolicy,
) -> str | None:
    if len(occurrences) >= policy.message_threshold:
        return "message_volume"
    if (
        alert.occurrence_milestone is not None
        and alert.occurrence_milestone >= policy.milestone_threshold
    ):
        return "occurrence_milestone"
    new_signatures = {
        occurrence.signature
        for occurrence in occurrences
        if occurrence.subsystem == alert.subsystem and occurrence.is_new_signature
    }
    if len(new_signatures) >= policy.new_signature_threshold:
        return "distinct_new_signatures"
    return None


async def _existing_occurrence(
    session: Any,
    *,
    org_id: str,
    channel_id: str,
    message_ts: str,
) -> ProviderAlertOccurrence | None:
    return await session.scalar(
        select(ProviderAlertOccurrence).where(
            ProviderAlertOccurrence.org_id == org_id,
            ProviderAlertOccurrence.channel_id == channel_id,
            ProviderAlertOccurrence.slack_message_ts == message_ts,
        )
    )


async def record_provider_alert_occurrence(
    session: Any,
    *,
    org_id: str,
    channel_id: str,
    message_ts: str,
    alert: ProviderAlertIngest,
    occurred_at: datetime | None = None,
    policy: ProviderAlertSurgePolicy | None = None,
) -> tuple[ProviderAlertSurgeSnapshot | None, bool]:
    """Record one message and atomically claim at most one open material surge."""

    current_time = _utcnow(occurred_at)
    surge_policy = policy or provider_alert_surge_policy()
    if await _existing_occurrence(
        session,
        org_id=org_id,
        channel_id=channel_id,
        message_ts=message_ts,
    ):
        return None, False

    prior_signature = await session.scalar(
        select(ProviderAlertOccurrence.id)
        .where(
            ProviderAlertOccurrence.org_id == org_id,
            ProviderAlertOccurrence.service == alert.service,
            ProviderAlertOccurrence.subsystem == alert.subsystem,
            ProviderAlertOccurrence.signature == alert.signature,
        )
        .limit(1)
    )
    occurrence = ProviderAlertOccurrence(
        org_id=org_id,
        channel_id=channel_id,
        slack_message_ts=message_ts,
        service=alert.service,
        subsystem=alert.subsystem,
        external_id=alert.external_id,
        signature=alert.signature,
        signature_title=alert.signature_title,
        occurrence_milestone=alert.occurrence_milestone,
        is_new_error=alert.is_new_error,
        is_new_signature=prior_signature is None,
        occurred_at=current_time,
    )
    session.add(occurrence)
    await session.flush()

    window_start = current_time - timedelta(minutes=surge_policy.window_minutes)
    occurrences = list(
        (
            await session.scalars(
                select(ProviderAlertOccurrence)
                .where(
                    ProviderAlertOccurrence.org_id == org_id,
                    ProviderAlertOccurrence.channel_id == channel_id,
                    ProviderAlertOccurrence.service == alert.service,
                    ProviderAlertOccurrence.occurred_at >= window_start,
                    ProviderAlertOccurrence.occurred_at <= current_time,
                )
                .order_by(
                    ProviderAlertOccurrence.occurred_at.asc(),
                    ProviderAlertOccurrence.id.asc(),
                )
            )
        ).all()
    )
    reason = _trigger_reason(
        occurrences,
        alert=alert,
        policy=surge_policy,
    )
    row = await session.scalar(
        _surge_query(
            org_id=org_id,
            channel_id=channel_id,
            alert=alert,
        ).with_for_update()
    )
    active = bool(
        row is not None and _utcnow(row.last_seen_at) >= window_start
    )
    if not active and reason is None:
        return None, False

    signatures, external_ids = _window_signature_data(occurrences)
    surge_subsystem = (
        alert.service if reason == "message_volume" else alert.subsystem
    )
    if row is None:
        row = ProviderAlertSurge(
            org_id=org_id,
            source_channel_id=channel_id,
            service=alert.service,
            subsystem=surge_subsystem,
            window_started_at=_utcnow(occurrences[0].occurred_at),
            opened_at=current_time,
            last_seen_at=current_time,
            trigger_reason=str(reason),
            message_count=len(occurrences),
            signatures_json=json.dumps(signatures, sort_keys=True),
            external_ids_json=json.dumps(external_ids),
            owner=surge_policy.owner,
            next_action=surge_policy.next_action,
            material_channel=surge_policy.material_channel,
        )
        session.add(row)
    elif active:
        row.last_seen_at = current_time
        row.message_count = len(occurrences)
        row.signatures_json = json.dumps(signatures, sort_keys=True)
        row.external_ids_json = json.dumps(external_ids)
    else:
        row.subsystem = surge_subsystem
        row.window_started_at = _utcnow(occurrences[0].occurred_at)
        row.opened_at = current_time
        row.last_seen_at = current_time
        row.trigger_reason = str(reason)
        row.message_count = len(occurrences)
        row.signatures_json = json.dumps(signatures, sort_keys=True)
        row.external_ids_json = json.dumps(external_ids)
        row.owner = surge_policy.owner
        row.next_action = surge_policy.next_action
        row.material_channel = surge_policy.material_channel
        row.material_message_ts = None
        row.material_post_claimed_at = None
        row.material_posted_at = None
    should_post = (
        row.material_posted_at is None and row.material_post_claimed_at is None
    )
    if should_post:
        row.material_post_claimed_at = current_time
    await session.flush()
    return _snapshot(row), should_post


def render_provider_alert_surge(snapshot: ProviderAlertSurgeSnapshot) -> str:
    signature_lines = ", ".join(
        f"{item['title']} ({item['signature'][:10]})"
        for item in snapshot.signatures
    )
    external_ids = ", ".join(snapshot.external_ids)
    return "\n".join(
        (
            "*Material incident — alert surge*",
            f"Subsystem: {snapshot.subsystem}",
            (
                "Window: "
                f"{snapshot.window_started_at.isoformat()} to "
                f"{snapshot.last_seen_at.isoformat()} "
                f"({snapshot.message_count} alerts; trigger={snapshot.trigger_reason})"
            ),
            f"Signatures: {signature_lines}",
            f"Rollbar items: {external_ids}",
            f"Owner: {snapshot.owner}",
            f"Next action: {snapshot.next_action}",
        )
    )


async def _resolve_material_channel(client: Any, configured: str) -> str:
    channel = str(configured or "").strip()
    if not channel.startswith("#"):
        return channel
    list_channels = getattr(client, "conversations_list", None)
    if not callable(list_channels):
        return channel
    target_name = channel.removeprefix("#")
    cursor: str | None = None
    seen_cursors: set[str] = set()
    while True:
        response = await list_channels(
            types="public_channel,private_channel",
            limit=200,
            cursor=cursor,
            exclude_archived=True,
        )
        for candidate in response.get("channels") or []:
            if not isinstance(candidate, dict):
                continue
            if str(candidate.get("name") or "") == target_name:
                return str(candidate.get("id") or channel)
        metadata = response.get("response_metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        next_cursor = str(metadata.get("next_cursor") or "").strip()
        if not next_cursor or next_cursor in seen_cursors:
            return channel
        seen_cursors.add(next_cursor)
        cursor = next_cursor


def _posted_message_ts(response: Any) -> str | None:
    if not isinstance(response, dict):
        return None
    direct = str(response.get("ts") or "").strip()
    if direct:
        return direct
    message = response.get("message")
    if isinstance(message, dict):
        return str(message.get("ts") or "").strip() or None
    return None


async def _post_material_surge(
    client: Any,
    surge: ProviderAlertSurgeSnapshot,
) -> tuple[str, str | None]:
    material_channel = await _resolve_material_channel(
        client,
        surge.material_channel,
    )
    response = await client.post_message(
        channel=material_channel,
        text=render_provider_alert_surge(surge),
    )
    return material_channel, _posted_message_ts(response)


async def _record_material_surge_posted(
    session: Any,
    *,
    surge: ProviderAlertSurgeSnapshot,
    material_channel: str,
    message_ts: str | None,
    posted_at: datetime | None,
) -> ProviderAlertSurgeSnapshot:
    row = await session.scalar(
        select(ProviderAlertSurge)
        .where(ProviderAlertSurge.id == surge.id)
        .with_for_update()
    )
    if row is None:
        return surge
    row.material_channel = material_channel
    row.material_message_ts = message_ts
    row.material_posted_at = _utcnow(posted_at)
    await session.flush()
    return _snapshot(row)


async def handle_provider_alert_ingest(
    session: Any,
    client: Any,
    *,
    org_id: str,
    channel_id: str,
    message_ts: str,
    text: str,
    occurred_at: datetime | None = None,
) -> ProviderAlertIngestResult | None:
    """Classify, persist, and material-post one incoming monitored alert."""

    alert = classify_provider_alert_ingest(text)
    if alert is None:
        return None
    policy = provider_alert_surge_policy()
    surge, should_post = await record_provider_alert_occurrence(
        session,
        org_id=org_id,
        channel_id=channel_id,
        message_ts=message_ts,
        alert=alert,
        occurred_at=occurred_at,
        policy=policy,
    )
    if surge is None or not should_post:
        return ProviderAlertIngestResult(alert=alert, surge=surge)

    try:
        material_channel, posted_message_ts = await _post_material_surge(
            client,
            surge,
        )
        surge = await _record_material_surge_posted(
            session,
            surge=surge,
            material_channel=material_channel,
            message_ts=posted_message_ts,
            posted_at=occurred_at,
        )
        return ProviderAlertIngestResult(
            alert=alert,
            surge=surge,
            material_posted=True,
        )
    except Exception as exc:
        logger.exception("provider alert surge material post failed")
        return ProviderAlertIngestResult(
            alert=alert,
            surge=surge,
            material_post_error=str(exc),
        )


async def handle_provider_alert_ingest_durable(
    client: Any,
    *,
    org_id: str,
    channel_id: str,
    message_ts: str,
    text: str,
    occurred_at: datetime | None = None,
) -> ProviderAlertIngestResult | None:
    """Commit the one-post claim before Slack delivery and run admission."""

    alert = classify_provider_alert_ingest(text)
    if alert is None:
        return None
    policy = provider_alert_surge_policy()
    async with UnitOfWork() as uow:
        surge, should_post = await record_provider_alert_occurrence(
            uow.session,
            org_id=org_id,
            channel_id=channel_id,
            message_ts=message_ts,
            alert=alert,
            occurred_at=occurred_at,
            policy=policy,
        )
    if surge is None or not should_post:
        return ProviderAlertIngestResult(alert=alert, surge=surge)

    try:
        material_channel, posted_message_ts = await _post_material_surge(
            client,
            surge,
        )
    except Exception as exc:
        logger.exception("provider alert surge material post failed")
        return ProviderAlertIngestResult(
            alert=alert,
            surge=surge,
            material_post_error=str(exc),
        )

    try:
        async with UnitOfWork() as uow:
            surge = await _record_material_surge_posted(
                uow.session,
                surge=surge,
                material_channel=material_channel,
                message_ts=posted_message_ts,
                posted_at=occurred_at,
            )
    except Exception as exc:
        # The committed claim remains authoritative if Slack accepted the post
        # but finalization failed, preventing a redelivery duplicate.
        logger.exception("provider alert surge post finalization failed")
        return ProviderAlertIngestResult(
            alert=alert,
            surge=surge,
            material_posted=True,
            material_post_error=str(exc),
        )
    return ProviderAlertIngestResult(
        alert=alert,
        surge=surge,
        material_posted=True,
    )


async def list_open_provider_alert_surges(
    session: Any,
    *,
    org_id: str,
    now: datetime | None = None,
    policy: ProviderAlertSurgePolicy | None = None,
) -> list[dict[str, Any]]:
    """Return queryable material surges still open at the requested instant."""

    current_time = _utcnow(now)
    surge_policy = policy or provider_alert_surge_policy()
    cutoff = current_time - timedelta(minutes=surge_policy.window_minutes)
    rows = list(
        (
            await session.scalars(
                select(ProviderAlertSurge)
                .where(
                    ProviderAlertSurge.org_id == org_id,
                    ProviderAlertSurge.opened_at <= current_time,
                    ProviderAlertSurge.last_seen_at >= cutoff,
                )
                .order_by(
                    ProviderAlertSurge.opened_at.asc(),
                    ProviderAlertSurge.id.asc(),
                )
            )
        ).all()
    )
    return [_surge_payload(_snapshot(row)) for row in rows]


__all__ = [
    "ProviderAlertIngestResult",
    "ProviderAlertSurgeSnapshot",
    "handle_provider_alert_ingest",
    "handle_provider_alert_ingest_durable",
    "list_open_provider_alert_surges",
    "record_provider_alert_occurrence",
    "render_provider_alert_surge",
]
