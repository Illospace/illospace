"""Notify-cycle wrapper: read domain changes → decide → post to Slack.

This is the thin, integration side of the notify-loop. The pure decision +
formatting is ``brain/systems/change_notifications.render_outbound`` (unit-tested);
here we only read ``domain_events`` since the last run, count the unclaimed pool,
and post the resulting messages. ``post`` is injectable so the orchestration is
testable without Slack.

Wiring (runtime): register a cycle whose program calls
``run_notify_cycle(session, org_id=…, channel_id=<the team channel>,
since=<cycle.last_run_at>)`` on its cadence (default ~30 min). Urgent items post
immediately; a quiet interval posts nothing. The channel is the team channel Illo
already works in — not a new channel, not DMs.
"""

from __future__ import annotations

from brain.systems.change_notifications import DEFAULT_URGENT_TERMS, render_outbound


async def _maybe_run_deploy_verification(session, org_id) -> dict | None:
    """Run the additive close-only verifier when deploy-state is armed."""
    import logging
    from datetime import datetime, timezone

    from brain.systems.deploy_state_config import deploy_feature_enabled

    if session is None or not deploy_feature_enabled():
        return None
    try:
        from brain.systems.deploy_state_sweep import run_deploy_verification

        async with session.begin_nested():
            return await run_deploy_verification(
                session,
                org_id=org_id,
                now=datetime.now(timezone.utc),
            )
    except Exception:
        logging.getLogger("illo.notify").exception("deploy verification failed safely")
        return None


def _normalize_event(row) -> dict:
    """Map a DomainEvent row to the notify event shape. Defensive: the record
    ``after`` snapshot is domain-schema-specific, so every field has a fallback."""
    after = getattr(row, "after", None) or {}
    # serialize_record (the `after` snapshot) puts id/object_key/title at the top
    # level and every user-defined field (url, labels, priority, owner_id, …)
    # under "data" — so read those from there, not the top level.
    data = after.get("data") or {}
    event_type = getattr(row, "event_type", "") or ""
    return {
        "event_type": event_type,
        "title": after.get("title") or data.get("title") or data.get("name") or data.get("summary"),
        "object_key": after.get("object_key"),
        "url": data.get("url") or data.get("html_url"),
        "labels": data.get("labels") or [],
        "priority": data.get("priority"),
        "action": event_type.split(".")[-1] if event_type else "updated",
        "owner_id": data.get("owner_id"),
        "record_id": getattr(row, "record_id", None),
    }


async def _load_change_events(session, org_id, since, *, limit: int = 500) -> list:
    import logging

    from sqlalchemy import select

    from brain.platform.db.models.domain import DomainEvent

    stmt = select(DomainEvent).where(DomainEvent.org_id == org_id)
    if since is not None:
        stmt = stmt.where(DomainEvent.created_at > since)
    stmt = stmt.order_by(DomainEvent.created_at.asc()).limit(limit)
    rows = (await session.execute(stmt)).scalars().all()
    if len(rows) >= limit:
        # Non-silent: a full page means more changes exist than this tick surfaced.
        logging.getLogger("illo.notify").warning(
            "notify-cycle hit the %s-event page limit; remaining changes surface next tick", limit
        )
    return [_normalize_event(r) for r in rows]


async def _count_unclaimed(session, org_id) -> int:
    """Count open items parked in the unclaimed pool.

    The pool is the configured ``ILLO_UNCLAIMED_POOL_USER_ID`` owner (see
    ``inbound/service.py``). Unset, or no session -> 0 (pool off)."""
    import os

    pool_user = os.environ.get("ILLO_UNCLAIMED_POOL_USER_ID", "").strip()
    if not pool_user or session is None:
        return 0

    from sqlalchemy import func, select

    from brain.platform.db.models.idea import Idea

    stmt = (
        select(func.count())
        .select_from(Idea)
        .where(
            Idea.user_id == pool_user,
            Idea.org_id == org_id,
            Idea.archived_at.is_(None),
        )
    )
    return int((await session.execute(stmt)).scalar() or 0)


async def _default_post(channel_id, text) -> None:
    from brain.systems.slack.client import slack_web_client_from_env

    client = slack_web_client_from_env()
    await client.post_message(channel=channel_id, text=text)


async def _fill_owner_labels(session, events) -> None:
    """Best-effort ``owner_id`` -> display name so digests don't print raw UUIDs.
    Guarded: no session or no owners -> no-op; any failure degrades to the id."""
    if session is None:
        return
    owner_ids = {e.get("owner_id") for e in events if e.get("owner_id")}
    if not owner_ids:
        return
    try:
        from sqlalchemy import select

        from brain.platform.db.models.org import User

        rows = (
            await session.execute(
                select(User.id, User.name, User.email).where(User.id.in_(owner_ids))
            )
        ).all()
        labels = {str(r.id): (r.name or r.email or str(r.id)) for r in rows}
        for event in events:
            oid = event.get("owner_id")
            if oid and not event.get("owner_label"):
                event["owner_label"] = labels.get(str(oid))
    except Exception:
        pass  # degrade to the raw id rather than break the tick


async def run_notify_cycle(
    session,
    *,
    org_id,
    channel_id,
    since=None,
    urgent_terms=DEFAULT_URGENT_TERMS,
    post=None,
) -> dict:
    """One notify-cycle tick. Returns a small summary of what was sent."""
    verification = await _maybe_run_deploy_verification(session, org_id)
    events = await _load_change_events(session, org_id, since)
    await _fill_owner_labels(session, events)
    unclaimed = await _count_unclaimed(session, org_id)
    outbound = render_outbound(events, unclaimed_count=unclaimed, urgent_terms=urgent_terms)

    sender = post or _default_post
    for message in outbound["immediate"]:
        await sender(channel_id, message)
    if outbound["digest"]:
        await sender(channel_id, outbound["digest"])

    summary = {
        "events": len(events),
        "immediate": len(outbound["immediate"]),
        "digest_posted": bool(outbound["digest"]),
        "unclaimed": unclaimed,
    }
    if verification is not None:
        summary["verification"] = verification
    return summary
