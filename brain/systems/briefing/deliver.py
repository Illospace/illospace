"""Illo Brain — post-commit delivery of handoff-packet briefs (outbox).

Why this exists (spec: illo-handoff-packets, post-slice-05 hardening; Codex
review finding 1, 2026-07-16): the mint runs inside the run's terminal-status
transaction. Posting the Slack brief from inside that transaction meant a
later commit failure left a live Slack message pointing at a rolled-back
handoff row — and the crash-retry re-minted a new row and posted a second
brief. The fix is a transactional outbox:

- **Record** (:func:`record_brief_delivery`): the mint's savepoint persists
  one ``PacketBriefDelivery`` row per handoff id, atomically with the
  handoff row, idea stamp, and run status. No commit → no record → no post.
- **Deliver** (:func:`deliver_pending_briefs`): strictly after that commit,
  a claim → verify → post → mark state machine sends the brief, each step in
  its own short transaction on its own session. Two triggers share it: the
  post-commit fast path (:func:`schedule_post_commit_delivery`) for
  same-second UX, and the slice-06 notify cycle as the guaranteed sweep.

Crash-safety contract (the reason ``posting`` exists): the claim commits
BEFORE the send, so ``pending`` provably means "never sent" and is safe to
send blind. A worker crash after claiming leaves ``posting``, which is
ambiguous — the send may or may not have reached Slack — so a stale
``posting`` row is re-sent ONLY after a disambiguation read of the origin
thread. The deterministic idempotency identifier is
``packet-brief:<handoff_id>``; the brief's launch URL embeds the handoff id,
and only messages authored by the recorded bot identity count. When the read
fails or is incomplete, the row stays ``posting`` for the next sweep:
delivery may be late, but a crashed worker is never re-sent blind.

Concurrency notes (cross-family review, 2026-07-16):

- Claims go through a ``FOR UPDATE SKIP LOCKED`` subselect: a row a live
  transaction (e.g. the notify tick's refresh-supersede) still holds is
  SKIPPED, never awaited — the sweep runs inside that tick, so blocking on
  its locks would self-deadlock the tick.
- ``claimed_at`` doubles as the fencing token: every post-claim transition
  requires the claim stamp it was made under, so a paused worker whose lease
  expired cannot stomp the reclaimer's state. The fence is re-verified
  immediately before every send (again after the disambiguation read, whose
  network latency is unbounded). The truly unpreventable residue: a send
  whose interval from that final fence check until the message becomes
  visible in Slack outlives the lease (worker suspension or a glacial
  in-flight request) can race a reclaimer's thread read into a duplicate —
  closing it needs provider-side idempotency Slack does not offer.
- After winning a claim the worker posts the row's CURRENT committed
  payload (re-read under the fence), not its pre-claim snapshot, so a
  drift-repair that landed in between is honored.
- SQLite (unit tests, fakes) compiles the FOR UPDATE clauses away; like the
  mint's advisory lock, the races and lock-waits these guard against are
  cross-connection, which those environments don't exercise. Production is
  PostgreSQL.

Total containment: nothing here may break a mint, a tick, or run-status
persistence. Every failure degrades to a log line plus a later retry.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from sqlalchemy import and_, or_, select, update

from brain.platform.db.models.packet_delivery import PacketBriefDelivery

logger = logging.getLogger(__name__)

# One sweep processes at most this many rows — the notify tick must stay
# bounded even after a long outage backlog. Deferrals surface next tick.
# Fresh (in-flight) claims are filtered OUT before the limit applies, so
# stuck rows can never starve deliverable ones.
DELIVERY_SWEEP_LIMIT = 10

# A ``posting`` claim older than this is treated as a crashed worker and
# becomes reclaimable (with the disambiguation read). Generous vs. the
# ~seconds a healthy post takes, tiny vs. the notify cadence.
STALE_POSTING_GRACE = timedelta(minutes=10)

# After this many claims the row goes ``failed`` (ERROR log, no silent
# infinite churn) — after one last disambiguation read, so an ambiguous
# send that actually landed is recorded as posted, not failed.
MAX_DELIVERY_ATTEMPTS = 8

# Slack rejects that cannot succeed on retry with the same inputs.
PERMANENT_SLACK_ERRORS = frozenset({"channel_not_found", "is_archived", "msg_too_long", "no_text"})

_DISAMBIGUATION_THREAD_LIMIT = 200
_DISAMBIGUATION_MAX_PAGES = 10  # 2000 messages; deeper threads read as "incomplete"

# The fast-path task polls for the outbox row to become visible (committed):
# the after_commit event also fires on savepoint releases (verified on
# SQLAlchemy 2.0.49), so the dispatch may run before the outer commit.
_VISIBILITY_POLL_ATTEMPTS = 5
_VISIBILITY_POLL_DELAY_SECONDS = 1.5

_INFO_QUEUE_KEY = "illo_brief_delivery_queue"
_INFO_ARMED_KEY = "illo_brief_delivery_listeners_armed"


@dataclass(frozen=True)
class DeliveryTarget:
    """Where the brief goes: the public origin thread, snapshotted at mint
    time (mint holds the inbound event; delivery must not need it again).
    ``bot_user_id`` is Illo's Slack identity from the same provenance — the
    disambiguation read trusts only messages it authored."""

    channel: str
    thread_ts: str
    bot_user_id: str | None = None


def delivery_idempotency_key(handoff_id: Any) -> str:
    return f"packet-brief:{handoff_id}"


# --- mint-savepoint writes (caller's session, caller's savepoint) ---------
#
# These run inside mint_packet_for_job's write-phase savepoint. The
# transfer's read is a locking read (FOR UPDATE): a plain read could see
# ``pending``, lose the CPU to a claimer that posts the old brief, then
# flush ``superseded`` over the claimer's state and spawn a replacement —
# two Slack replies. The locking read serializes against the claim CAS.


async def record_brief_delivery(
    session: Any, *, org_id: str, handoff_id: str, target: DeliveryTarget, brief: str
) -> PacketBriefDelivery:
    """Persist the delivery obligation for a freshly created handoff."""
    row = PacketBriefDelivery(
        org_id=str(org_id),
        handoff_id=str(handoff_id),
        state="pending",
        idempotency_key=delivery_idempotency_key(handoff_id),
        channel=target.channel,
        thread_ts=target.thread_ts,
        bot_user_id=target.bot_user_id,
        brief=brief,
    )
    session.add(row)
    return row


async def _delivery_for_handoff(
    session: Any, handoff_id: str, *, for_update: bool = False
) -> PacketBriefDelivery | None:
    # populate_existing: the lock is only as good as the attributes read
    # under it — an identity-map copy cached before a concurrent claim
    # committed would still say "pending" and re-open the transfer/claim
    # double-post (verification finding, 2026-07-16).
    stmt = (
        select(PacketBriefDelivery)
        .where(PacketBriefDelivery.handoff_id == str(handoff_id))
        .execution_options(populate_existing=True)
    )
    if for_update:
        stmt = stmt.with_for_update()
    return (await session.execute(stmt)).scalars().first()


async def transfer_pending_delivery(
    session: Any,
    *,
    org_id: str,
    prior_handoff_id: str,
    new_handoff_id: str,
    new_brief: str | None,
) -> bool:
    """Supersede carry-over: an obligation that never went out follows the
    superseding row (new brief, new launch URL) so the origin thread still
    gets its one triage-moment reply — with current truth, not a dead link.

    Only ``pending`` moves. ``posted`` means the thread already has its brief
    (the noise gate holds — a supersede posts nothing new on its own), and
    ``posting`` means a send may be in flight, so the old claim keeps the
    obligation and resolves it against the old content. The FOR UPDATE read
    serializes this decision against a concurrent claim.
    """
    prior = await _delivery_for_handoff(session, prior_handoff_id, for_update=True)
    if prior is None or prior.state != "pending":
        return False
    prior.state = "superseded"
    prior.last_error = None
    existing = await _delivery_for_handoff(session, new_handoff_id)
    if existing is not None:
        return True  # the new mint recorded its own delivery; just retire the old one
    if not new_brief:
        logger.warning(
            "pending brief for handoff %s superseded by %s without a replacement brief; "
            "obligation dropped", prior_handoff_id, new_handoff_id,
        )
        return True
    session.add(
        PacketBriefDelivery(
            org_id=str(org_id),
            handoff_id=str(new_handoff_id),
            state="pending",
            idempotency_key=delivery_idempotency_key(new_handoff_id),
            channel=prior.channel,
            thread_ts=prior.thread_ts,
            bot_user_id=prior.bot_user_id,
            brief=new_brief,
        )
    )
    return True


async def refresh_pending_delivery_brief(session: Any, *, handoff_id: str, brief: str) -> None:
    """Drift repair rewrote the row's content in place (same idempotency
    key); an undelivered brief must describe what the row now says."""
    row = await _delivery_for_handoff(session, handoff_id, for_update=True)
    if row is not None and row.state == "pending":
        row.brief = brief


# --- the post-commit engine (its own sessions, its own transactions) ------


def _default_session_factory() -> Callable[[], Any] | None:
    try:
        from brain.platform.db import SessionFactory

        return SessionFactory
    except Exception as exc:  # noqa: BLE001 — no platform DB → sweep is a no-op
        logger.debug("brief delivery has no session factory (%s)", exc)
        return None


async def _default_poster(*, channel: str, text: str, thread_ts: str) -> dict[str, Any]:
    from brain.systems.slack.client import slack_web_client_from_env

    client = slack_web_client_from_env()
    return await client.post_message(channel=channel, text=text, thread_ts=thread_ts)


async def _default_thread_reader(*, channel: str, thread_ts: str) -> dict[str, Any]:
    """Raw thread read for crash disambiguation (distinct from gather's
    excerpting reader): walk the WHOLE thread or say so. ``complete=False``
    means an unread tail may hide the brief — the caller must then treat
    "not found" as "cannot decide", never as "absent"."""
    from brain.systems.slack.client import slack_web_client_from_env

    client = slack_web_client_from_env()
    messages: list[dict[str, Any]] = []
    cursor: str | None = None
    complete = False
    for _page in range(_DISAMBIGUATION_MAX_PAGES):
        payload = await client.conversation_replies(
            channel=channel, thread_ts=thread_ts,
            limit=_DISAMBIGUATION_THREAD_LIMIT, cursor=cursor,
        )
        messages.extend(dict(m) for m in payload.get("messages") or [])
        cursor = str(((payload.get("response_metadata") or {}).get("next_cursor")) or "")
        if not cursor:
            complete = True
            break
    return {"messages": messages, "complete": complete}


def _utc(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _snapshot(row: PacketBriefDelivery) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "org_id": str(row.org_id),
        "handoff_id": str(row.handoff_id),
        "state": str(row.state),
        "channel": str(row.channel),
        "thread_ts": str(row.thread_ts),
        "bot_user_id": str(row.bot_user_id or "") or None,
        "brief": str(row.brief),
        "attempts": int(row.attempts or 0),
        "claimed_at": row.claimed_at,
    }


def _claimable_clause(cutoff: datetime):
    """Rows a deliverer may touch NOW: never-claimed, or a lease that
    expired. Fresh in-flight claims stay invisible so they can neither be
    stomped nor consume the sweep limit (starvation finding)."""
    return or_(
        PacketBriefDelivery.state == "pending",
        and_(
            PacketBriefDelivery.state == "posting",
            PacketBriefDelivery.claimed_at.is_not(None),
            PacketBriefDelivery.claimed_at <= cutoff,
        ),
    )


async def _cas(
    factory: Callable[[], Any],
    delivery_id: str,
    *,
    where: list[Any],
    values: dict[str, Any],
    skip_locked: bool = False,
) -> bool:
    """One compare-and-swap in its own committed transaction. The rowcount
    is the winner test — a concurrent claimer, an expired fence, or a
    supersede transfer makes it 0 and the caller walks away.

    ``skip_locked`` (claims only): route through a ``FOR UPDATE SKIP
    LOCKED`` subselect so a row a live transaction holds (the notify tick's
    own refresh!) is skipped instead of awaited — the sweep runs inside
    that tick, and blocking on its locks would deadlock the tick against
    itself. SQLite ignores the FOR UPDATE clause, which is correct there
    (single-writer, and the tests exercise no cross-connection locks).
    """
    async with factory() as session:
        conditions = [PacketBriefDelivery.id == str(delivery_id), *where]
        if skip_locked:
            locked_id = (
                select(PacketBriefDelivery.id)
                .where(*conditions)
                .with_for_update(skip_locked=True)
                .scalar_subquery()
            )
            stmt = update(PacketBriefDelivery).where(
                PacketBriefDelivery.id.in_(select(locked_id))
            )
        else:
            stmt = update(PacketBriefDelivery).where(*conditions)
        result = await session.execute(
            stmt.values(**values).execution_options(synchronize_session=False)
        )
        await session.commit()
        return int(result.rowcount or 0) == 1


async def _verified_reread(
    factory: Callable[[], Any], delivery_id: str, *, claim_stamp: datetime
) -> dict[str, Any] | None:
    """Post-claim re-read under the fence: returns the row's CURRENT
    committed payload (a drift repair may have landed between snapshot and
    claim), or None when the fence is gone (someone else owns the row)."""
    async with factory() as session:
        row = (
            await session.execute(
                select(PacketBriefDelivery)
                .where(PacketBriefDelivery.id == str(delivery_id))
                # DB truth even on a session whose identity map already holds
                # this row with attributes from before the CAS committed.
                .execution_options(populate_existing=True)
            )
        ).scalars().first()
    if row is None or str(row.state) != "posting":
        return None
    if _utc(row.claimed_at) != _utc(claim_stamp):
        return None
    return _snapshot(row)


def _found_in_thread(
    messages: list[dict[str, Any]], handoff_id: str, *, bot_user_id: str | None
) -> str | None:
    """The brief carries the handoff id in its launch URL. Only messages
    authored by the recorded bot identity count — a human pasting the same
    launch URL must not satisfy the check (the brief itself never arrived).
    Without a recorded identity, any author matches (fail-open is the
    duplicate-safe direction here)."""
    marker = str(handoff_id).lower()
    for message in messages:
        if bot_user_id and str(message.get("user") or "") != bot_user_id:
            continue
        if marker in str(message.get("text") or "").lower():
            return str(message.get("ts") or "") or "found"
    return None


async def _disambiguate(
    item: dict[str, Any],
    *,
    thread_reader: Callable[..., Awaitable[dict[str, Any]]],
) -> tuple[str, str | None]:
    """Read the origin thread: ('posted', ts) when the brief is provably
    there, ('absent', None) when the COMPLETE thread provably lacks it,
    ('unknown', None) when the read failed or was truncated."""
    handoff_id = item["handoff_id"]
    try:
        read = await thread_reader(channel=item["channel"], thread_ts=item["thread_ts"])
    except Exception as exc:  # noqa: BLE001 — can't decide → stay posting, retry later
        logger.warning(
            "brief delivery for handoff %s: disambiguation read failed (%s)", handoff_id, exc,
        )
        return "unknown", None
    messages = list(read.get("messages") or [])
    found_ts = _found_in_thread(messages, handoff_id, bot_user_id=item.get("bot_user_id"))
    if found_ts:
        return "posted", found_ts
    if not read.get("complete", False):
        logger.warning(
            "brief delivery for handoff %s: thread read incomplete; cannot prove "
            "the brief absent", handoff_id,
        )
        return "unknown", None
    return "absent", None


async def _deliver_one(
    item: dict[str, Any],
    *,
    factory: Callable[[], Any],
    poster: Callable[..., Awaitable[dict[str, Any]]],
    thread_reader: Callable[..., Awaitable[dict[str, Any]]],
    now: datetime,
    summary: dict[str, int],
) -> None:
    delivery_id = item["id"]
    handoff_id = item["handoff_id"]
    must_disambiguate = item["state"] == "posting"
    fence = [
        PacketBriefDelivery.state == "posting",
        PacketBriefDelivery.claimed_at == now,  # our claim stamp = the fencing token
    ]

    if item["attempts"] >= MAX_DELIVERY_ATTEMPTS:
        # One last honest look before recording a permanent failure: the
        # final ambiguous send may in fact have landed. Both cap CASes are
        # skip-locked for the same reason claims are: the notify tick's
        # refresh may hold this row's lock (its transfer takes a FOR UPDATE
        # read even on non-pending rows), and the sweep it awaits must skip,
        # never block.
        outcome, found_ts = ("absent", None)
        if must_disambiguate:
            outcome, found_ts = await _disambiguate(item, thread_reader=thread_reader)
        if outcome == "unknown":
            # Can't read the thread → can't prove the final send didn't land.
            # Stays posting (retried, loudly) rather than recording a failure
            # that might be a success.
            summary["undecided"] += 1
            return
        if outcome == "posted":
            await _cas(
                factory, delivery_id,
                where=[PacketBriefDelivery.state.in_(("pending", "posting"))],
                values={
                    "state": "posted", "posted_at": now, "last_error": None,
                    "posted_message_ts": found_ts if found_ts != "found" else None,
                },
                skip_locked=True,
            )
            summary["already_posted"] += 1
            return
        if await _cas(
            factory, delivery_id,
            where=[PacketBriefDelivery.state.in_(("pending", "posting"))],
            values={"state": "failed", "last_error": f"gave up after {item['attempts']} attempts"},
            skip_locked=True,
        ):
            logger.error(
                "brief for handoff %s FAILED permanently after %s attempts (last error stands); "
                "the packet row and launch link are unaffected", handoff_id, item["attempts"],
            )
            summary["failed"] += 1
        return

    if must_disambiguate:
        claimed = await _cas(
            factory, delivery_id,
            where=[
                PacketBriefDelivery.state == "posting",
                PacketBriefDelivery.claimed_at == item["claimed_at"],
            ],
            values={"state": "posting", "claimed_at": now, "attempts": item["attempts"] + 1},
            skip_locked=True,
        )
    else:
        claimed = await _cas(
            factory, delivery_id,
            where=[PacketBriefDelivery.state == "pending"],
            values={"state": "posting", "claimed_at": now, "attempts": item["attempts"] + 1},
            skip_locked=True,
        )
    if not claimed:
        summary["lost_claim"] += 1
        return

    # Fence-verified re-read: freshest committed payload, and the last
    # possible moment a paused-then-resumed worker discovers it lost its
    # lease BEFORE talking to Slack.
    current = await _verified_reread(factory, delivery_id, claim_stamp=now)
    if current is None:
        summary["lost_claim"] += 1
        return
    item = {**item, **{k: current[k] for k in ("channel", "thread_ts", "brief", "bot_user_id")}}

    if must_disambiguate:
        # The prior claim died mid-flight: the send may have reached Slack.
        outcome, found_ts = await _disambiguate(item, thread_reader=thread_reader)
        if outcome == "unknown":
            summary["undecided"] += 1
            return  # stays posting under our stamp; next sweep retries
        if outcome == "posted":
            await _cas(
                factory, delivery_id, where=fence,
                values={
                    "state": "posted", "posted_at": now, "last_error": None,
                    "posted_message_ts": found_ts if found_ts != "found" else None,
                },
            )
            logger.info(
                "brief for handoff %s was already in the thread (crashed claim); marked posted "
                "without re-sending", handoff_id,
            )
            summary["already_posted"] += 1
            return
        # The thread read is network I/O of unbounded real-world latency:
        # re-verify the fence so a lease that expired DURING the read can't
        # slip a late send past a newer claimant.
        if await _verified_reread(factory, delivery_id, claim_stamp=now) is None:
            summary["lost_claim"] += 1
            return

    from brain.systems.slack.client import SlackApiError

    try:
        response = await poster(channel=item["channel"], text=item["brief"], thread_ts=item["thread_ts"])
    except SlackApiError as exc:
        # A definite reject: Slack answered, nothing was posted.
        if exc.error in PERMANENT_SLACK_ERRORS:
            await _cas(
                factory, delivery_id, where=fence,
                values={"state": "failed", "last_error": str(exc)},
            )
            logger.error(
                "brief for handoff %s FAILED permanently: %s (channel %s)",
                handoff_id, exc, item["channel"],
            )
            summary["failed"] += 1
        else:
            await _cas(
                factory, delivery_id, where=fence,
                values={"state": "pending", "claimed_at": None, "last_error": str(exc)},
            )
            logger.warning(
                "brief delivery for handoff %s rejected by Slack (%s); requeued (attempt %s/%s)",
                handoff_id, exc, item["attempts"] + 1, MAX_DELIVERY_ATTEMPTS,
            )
            summary["requeued"] += 1
        return
    except Exception as exc:  # noqa: BLE001 — ambiguous (timeout, transport): may have landed
        logger.warning(
            "brief delivery for handoff %s ambiguous (%s: %s); left in posting for the "
            "disambiguating retry", handoff_id, type(exc).__name__, exc,
        )
        summary["undecided"] += 1
        return

    marked = await _cas(
        factory, delivery_id, where=fence,
        values={
            "state": "posted", "posted_at": now, "last_error": None,
            "posted_message_ts": str(response.get("ts") or "") or None,
        },
    )
    if not marked:
        # The message IS in the thread; only the bookkeeping lost its fence
        # (a reclaimer took over mid-send). Its disambiguation read finds
        # the message and records posted — never a second send.
        logger.warning("brief for handoff %s posted but the posted-mark lost its fence", handoff_id)
    summary["posted"] += 1


async def deliver_pending_briefs(
    *,
    org_id: str | None = None,
    handoff_ids: list[str] | None = None,
    session_factory: Callable[[], Any] | None = None,
    poster: Callable[..., Awaitable[dict[str, Any]]] | None = None,
    thread_reader: Callable[..., Awaitable[dict[str, Any]]] | None = None,
    now: datetime | None = None,
    limit: int = DELIVERY_SWEEP_LIMIT,
) -> dict[str, int]:
    """Deliver undelivered briefs: the fast path targets ``handoff_ids`` it
    just minted; the notify-cycle sweep passes ``org_id`` only. Never raises;
    the summary says what happened (all keys are counts)."""
    summary = {
        "selected": 0, "posted": 0, "already_posted": 0, "requeued": 0,
        "failed": 0, "undecided": 0, "in_flight": 0, "lost_claim": 0,
    }
    try:
        factory = session_factory or _default_session_factory()
        if factory is None:
            return summary
        moment = now or datetime.now(timezone.utc)

        stmt = (
            select(PacketBriefDelivery)
            .where(_claimable_clause(moment - STALE_POSTING_GRACE))
            .order_by(PacketBriefDelivery.created_at.asc())
            .limit(max(1, int(limit)))
            .execution_options(populate_existing=True)  # DB truth over identity map
        )
        if org_id:
            stmt = stmt.where(PacketBriefDelivery.org_id == str(org_id))
        if handoff_ids:
            stmt = stmt.where(
                PacketBriefDelivery.handoff_id.in_([str(h) for h in handoff_ids])
            )
        async with factory() as session:
            candidates = [_snapshot(row) for row in (await session.execute(stmt)).scalars().all()]

        summary["selected"] = len(candidates)
        for item in candidates:
            try:
                await _deliver_one(
                    item,
                    factory=factory,
                    poster=poster or _default_poster,
                    thread_reader=thread_reader or _default_thread_reader,
                    now=moment,
                    summary=summary,
                )
            except Exception as exc:  # noqa: BLE001 — one bad row must not starve the rest
                logger.warning(
                    "brief delivery for handoff %s failed unexpectedly: %s",
                    item.get("handoff_id"), exc,
                )
        return summary
    except Exception as exc:  # noqa: BLE001 — total containment
        logger.warning("brief delivery pass failed safely: %s", exc)
        return summary


# --- the post-commit fast path ---------------------------------------------
#
# after_commit is NOT outer-commit-only: on the installed SQLAlchemy it also
# fires when a savepoint is released (verified empirically, 2026-07-16), and
# ``in_transaction()`` cannot tell the two apart from inside the callback.
# So the dispatch is deliberately event-time agnostic: arming QUEUES the
# handoff ids in ``session.info``; any commit-ish fire drains the queue into
# a task; the task then polls (bounded) for the rows to become VISIBLE in a
# fresh session — visibility, not event timing, is the truth about the
# commit. A ROOT rollback clears the queue (nothing queued can ever commit);
# a savepoint rollback keeps it, because entries from surviving savepoints
# still commit and dead entries self-clean (their rows never become visible,
# so the task expires quietly). An early drain whose outer commit outlasts
# the poll budget loses only the fast path — the sweep delivers.

_POST_COMMIT_TASKS: set[asyncio.Task] = set()


def _reap_delivery_task(task: asyncio.Task) -> None:
    _POST_COMMIT_TASKS.discard(task)
    try:
        exc = task.exception()
    except asyncio.CancelledError:
        return
    if exc is not None:
        logger.warning("post-commit brief delivery task died: %s", exc)


async def _deliver_when_visible(org_id: str, handoff_ids: list[str]) -> None:
    """Poll until the just-minted outbox rows are committed-visible, then
    deliver. Gives up quietly after the poll budget — the notify-cycle
    sweep remains the guaranteed path."""
    for attempt in range(_VISIBILITY_POLL_ATTEMPTS):
        summary = await deliver_pending_briefs(org_id=org_id, handoff_ids=handoff_ids)
        if summary["selected"]:
            return
        if attempt < _VISIBILITY_POLL_ATTEMPTS - 1:
            await asyncio.sleep(_VISIBILITY_POLL_DELAY_SECONDS)
    logger.debug(
        "fast-path delivery for handoffs %s saw no committed rows; leaving them to the sweep",
        handoff_ids,
    )


def _spawn_delivery_task(org_id: str, handoff_ids: list[str]) -> None:
    try:
        task = asyncio.ensure_future(_deliver_when_visible(org_id, handoff_ids))
        _POST_COMMIT_TASKS.add(task)
        task.add_done_callback(_reap_delivery_task)
    except Exception:  # noqa: BLE001 — the sweep remains the guaranteed path
        logger.warning("post-commit brief delivery task failed to start", exc_info=True)


def schedule_post_commit_delivery(session: Any, *, org_id: str, handoff_ids: list[str]) -> bool:
    """Queue a fast-path delivery dispatch for the session's next commit.

    One pair of listeners per session, armed once (``session.info`` flag);
    subsequent calls only extend the queue — no per-mint listener
    accumulation. Rollback clears the queue. Failure to arm is logged and
    the notify-cycle sweep delivers instead. Returns True when queued."""
    try:
        sync_session = getattr(session, "sync_session", None)
        if sync_session is None:
            logger.debug("session has no sync_session; brief delivery deferred to the sweep")
            return False
        loop = asyncio.get_running_loop()
        queue: list[tuple[str, str]] = sync_session.info.setdefault(_INFO_QUEUE_KEY, [])
        queue.extend((str(org_id), str(h)) for h in handoff_ids)

        if sync_session.info.get(_INFO_ARMED_KEY):
            return True

        def _drain_into_tasks(target_session: Any) -> None:
            drained = list(target_session.info.get(_INFO_QUEUE_KEY) or [])
            target_session.info[_INFO_QUEUE_KEY] = []
            by_org: dict[str, list[str]] = {}
            for org, handoff_id in drained:
                bucket = by_org.setdefault(org, [])
                if handoff_id not in bucket:
                    bucket.append(handoff_id)
            for org, ids in by_org.items():
                try:
                    # threadsafe: also correct same-thread, and the commit may
                    # run on a session driven by another loop/thread (the
                    # inline-runner topology).
                    loop.call_soon_threadsafe(_spawn_delivery_task, org, ids)
                except Exception:  # noqa: BLE001 — loop gone → sweep delivers
                    logger.warning(
                        "post-commit brief dispatch could not be scheduled", exc_info=True
                    )

        def _on_rollback(target_session: Any, previous: Any) -> None:
            # Root rollback: the whole transaction is dead, nothing queued
            # will ever become visible — clear. A NESTED (savepoint) rollback
            # must NOT clear: entries queued by other, surviving savepoints
            # still commit, and any entry whose own savepoint died is
            # self-cleaning (its row never becomes visible, so the task
            # expires without posting).
            if getattr(previous, "nested", False):
                return
            if target_session.info.get(_INFO_QUEUE_KEY):
                target_session.info[_INFO_QUEUE_KEY] = []

        from sqlalchemy import event

        event.listen(sync_session, "after_commit", _drain_into_tasks)
        event.listen(sync_session, "after_soft_rollback", _on_rollback)
        sync_session.info[_INFO_ARMED_KEY] = True
        return True
    except Exception as exc:  # noqa: BLE001 — never break the mint
        logger.warning(
            "post-commit brief dispatch not armed (%s); the notify-cycle sweep delivers instead",
            exc,
        )
        return False


__all__ = [
    "DeliveryTarget",
    "DELIVERY_SWEEP_LIMIT",
    "MAX_DELIVERY_ATTEMPTS",
    "PERMANENT_SLACK_ERRORS",
    "STALE_POSTING_GRACE",
    "delivery_idempotency_key",
    "deliver_pending_briefs",
    "record_brief_delivery",
    "refresh_pending_delivery_brief",
    "schedule_post_commit_delivery",
    "transfer_pending_delivery",
]
