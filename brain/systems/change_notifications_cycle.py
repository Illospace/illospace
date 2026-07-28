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


async def _maybe_run_alert_resolution_harvest(session, org_id) -> dict | None:
    """Harvest alert-thread outcomes without blocking the notify tick."""
    import logging

    if session is None:
        return None
    try:
        from brain.systems.alert_resolution import run_alert_resolution_harvest

        async with session.begin_nested():
            return await run_alert_resolution_harvest(
                session,
                org_id=org_id,
            )
    except Exception:
        logging.getLogger("illo.notify").exception(
            "alert resolution harvest failed safely"
        )
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
    action = event_type.split(".")[-1] if event_type else "updated"
    noteworthy = None
    reason = str(getattr(row, "reason", "") or "")
    if reason.startswith("alert_resolution_harvest:"):
        _, outcome, message_ts = (reason.split(":", 2) + [""])[:3]
        action = {
            "deployed": "movement: fix deployed from the alert thread",
            "verified": "outcome: fix verified in the alert thread",
            "reproduced": "movement: fix claim reversed; still reproduces per alert thread",
        }.get(outcome, "movement: alert-thread resolution changed")
        if message_ts:
            action += f" (Slack ts {message_ts})"
        noteworthy = True
    normalized = {
        "event_type": event_type,
        "title": after.get("title") or data.get("title") or data.get("name") or data.get("summary"),
        "object_key": after.get("object_key"),
        "url": data.get("url") or data.get("html_url"),
        "labels": data.get("labels") or [],
        "priority": data.get("priority"),
        "action": action,
        "owner_id": data.get("owner_id"),
        "record_id": getattr(row, "record_id", None),
    }
    if noteworthy is not None:
        normalized["noteworthy"] = noteworthy
    return normalized


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


# At most this many packet refreshes per tick — each one re-gathers (live
# Slack/GitHub reads), and a gathering storm inside the notify tick is worse
# than a slightly stale packet. Deferrals are logged, never silent.
_MAX_PACKET_REFRESHES_PER_TICK = 5


async def _attach_and_refresh_packets(session, org_id, events) -> None:
    """Slice 06 (illo-handoff-packets): give digest/nudge lines their launch
    links, refreshing stale packets first (capped). Fully contained — any
    failure degrades to lines without links, never a dead tick."""
    import logging

    if session is None:
        return
    log = logging.getLogger("illo.notify")
    try:
        from brain.systems.briefing.mint import (
            find_packet_handoffs_for_jobs,
            refresh_packet_for_job,
        )
        from brain.systems.launch_handoffs import launch_handoff_url_for_id

        job_refs = []
        seen: set[str] = set()
        for event in events:
            record_id = event.get("record_id")
            if record_id:
                job_ref = f"domain_record:{record_id}"
                if job_ref not in seen:
                    seen.add(job_ref)
                    job_refs.append(job_ref)
        packet_rows = await find_packet_handoffs_for_jobs(session, org_id=org_id, job_refs=job_refs)

        # Refresh at most N unique jobs per tick — each refresh re-gathers
        # (live Slack/GitHub reads). A slot is consumed only by a refresh
        # that actually ran (ok) — permanently unrefreshable rows fail fast
        # and must not starve the healthy ones.
        refreshes_left = _MAX_PACKET_REFRESHES_PER_TICK
        deferred = 0
        for job_ref in job_refs:
            row = packet_rows.get(job_ref)
            if row is None:
                continue
            if refreshes_left > 0:
                result = await refresh_packet_for_job(session, org_id=org_id, handoff_row=row)
                if result.ok:
                    refreshes_left -= 1
                    if result.created:
                        packet_rows[job_ref] = result.handoff  # superseded → new link
            else:
                deferred += 1

        for event in events:
            record_id = event.get("record_id")
            row = packet_rows.get(f"domain_record:{record_id}") if record_id else None
            if row is None:
                continue
            event["launch_url"] = launch_handoff_url_for_id(row.id, target_tool=row.target_tool)
            event["packet_revision"] = (dict(row.metadata_ or {})).get("revision")
        if deferred:
            # No silent caps (spec invariant): say what was skipped.
            log.warning("notify tick deferred %s packet refreshes to the next tick", deferred)
    except Exception:  # noqa: BLE001
        log.exception("packet link attachment failed safely; lines go out without links")


async def _deliver_pending_briefs_safely(org_id) -> dict | None:
    """Post-slice-05 hardening: the sweep half of the packet-brief outbox.
    The mint records deliveries inside its transaction and an after-commit
    fast path usually posts them within a second; this sweep is the
    guaranteed retry for anything a crash or Slack outage left behind
    (pending or stale-posting rows). Own sessions, fully contained — a
    delivery failure degrades to a log line, never a dead tick."""
    import logging

    try:
        from brain.systems.briefing.deliver import deliver_pending_briefs

        return await deliver_pending_briefs(org_id=org_id)
    except Exception:  # noqa: BLE001
        logging.getLogger("illo.notify").exception("packet brief delivery sweep failed safely")
        return None


async def _deliver_pending_obligation_notices_safely(org_id) -> dict | None:
    """Guaranteed sweep for committed run-deferral notice outbox rows."""

    import logging

    try:
        from brain.systems.runs.obligation_notices import (
            deliver_pending_obligation_notices,
        )

        return await deliver_pending_obligation_notices(org_id=str(org_id))
    except Exception:  # noqa: BLE001
        logging.getLogger("illo.notify").exception(
            "obligation notice delivery sweep failed safely"
        )
        return None


async def _default_post(channel_id, text) -> None:
    from brain.systems.slack.client import slack_web_client_from_runtime

    client = await slack_web_client_from_runtime(
        requested_by="change_notifications", reason="Post a change-notification digest line."
    )
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
    deliver_briefs=None,
) -> dict:
    """One notify-cycle tick. Returns a small summary of what was sent."""
    resolution_harvest = await _maybe_run_alert_resolution_harvest(session, org_id)
    events = await _load_change_events(session, org_id, since)
    await _fill_owner_labels(session, events)
    await _attach_and_refresh_packets(session, org_id, events)
    # After the refresh: a supersede may have just moved a pending brief to
    # its new revision, and this sweep should send THAT one.
    deliveries = None
    notice_deliveries = None
    if session is not None:
        try:
            deliveries = await (deliver_briefs or _deliver_pending_briefs_safely)(org_id)
        except Exception:  # noqa: BLE001 — a delivery failure never kills the tick
            import logging

            logging.getLogger("illo.notify").exception("brief delivery sweep failed safely")
        # Test and specialist callers may inject only the packet sweep. The
        # production path owns both transactional outboxes.
        if deliver_briefs is None:
            try:
                notice_deliveries = await _deliver_pending_obligation_notices_safely(
                    org_id
                )
            except Exception:  # noqa: BLE001
                import logging

                logging.getLogger("illo.notify").exception(
                    "obligation notice delivery sweep failed safely"
                )
    unclaimed = await _count_unclaimed(session, org_id)
    outbound = render_outbound(events, unclaimed_count=unclaimed, urgent_terms=urgent_terms)

    import logging

    sender = post or _default_post
    log = logging.getLogger("illo.notify")
    post_failures = 0
    digest_posted = False
    # A failed send (token unavailable, Slack rejection) must not kill the
    # tick or the messages behind it — count it, log it, keep going
    # (cross-family review finding, 2026-07-16: this path went live with the
    # runtime-token resolver and previously had no containment).
    for message in outbound["immediate"]:
        try:
            await sender(channel_id, message)
        except Exception:  # noqa: BLE001
            post_failures += 1
            log.warning("notify post failed; continuing with remaining messages", exc_info=True)
    if outbound["digest"]:
        try:
            await sender(channel_id, outbound["digest"])
            digest_posted = True
        except Exception:  # noqa: BLE001
            post_failures += 1
            log.warning("notify digest post failed", exc_info=True)

    summary = {
        "events": len(events),
        "immediate": len(outbound["immediate"]),
        "digest_posted": digest_posted,
        "unclaimed": unclaimed,
    }
    if post_failures:
        summary["post_failures"] = post_failures
    if resolution_harvest is not None:
        summary["resolution_harvest"] = resolution_harvest
    if deliveries and deliveries.get("selected"):
        summary["brief_deliveries"] = deliveries
    if notice_deliveries and notice_deliveries.get("selected"):
        summary["obligation_notice_deliveries"] = notice_deliveries
    return summary
