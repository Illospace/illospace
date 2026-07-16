"""Post-commit brief delivery (the packet-brief outbox state machine).

Contract under test (spec: illo-handoff-packets, post-slice-05 hardening):
``pending`` is claim-then-send (the claim commits BEFORE the send, so
pending provably means "never sent"); a stale ``posting`` claim is a crashed
worker and is re-sent ONLY after the origin thread is read and shown not to
contain the handoff id (the deterministic ``packet-brief:<handoff_id>``
identity rides in the brief's launch URL) — worker crashes can never
duplicate the message. Definite Slack rejects requeue or fail loudly;
ambiguous transport errors stay claimed for the next disambiguating pass.
Everything is contained: a delivery failure is a log line plus a retry,
never a raise.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from brain.platform.db.models.launch_handoff import LaunchHandoff
from brain.platform.db.models.packet_delivery import PacketBriefDelivery
from brain.systems.briefing.deliver import (
    DELIVERY_SWEEP_LIMIT,
    MAX_DELIVERY_ATTEMPTS,
    STALE_POSTING_GRACE,
    deliver_pending_briefs,
    delivery_idempotency_key,
    transfer_pending_delivery,
)
from brain.systems.slack.client import SlackApiError

_ORG = str(uuid.uuid4())
_NOW = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
async def session(async_sqlite_session_factory, sqlite_postgres_ddl_patch):
    return await async_sqlite_session_factory([
        LaunchHandoff.__table__,
        PacketBriefDelivery.__table__,
    ])


class _SessionLease:
    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *_exc):
        return False


def _factory(session):
    return lambda: _SessionLease(session)


class FakePoster:
    def __init__(self, fail_with: Exception | None = None):
        self.sent: list[dict] = []
        self.fail_with = fail_with

    async def __call__(self, *, channel, text, thread_ts):
        if self.fail_with is not None:
            raise self.fail_with
        self.sent.append({"channel": channel, "text": text, "thread_ts": thread_ts})
        return {"ok": True, "ts": "1752600100.0"}


class FakeThread:
    def __init__(self, messages=(), fail: bool = False, complete: bool = True):
        self.messages = list(messages)
        self.fail = fail
        self.complete = complete
        self.reads = 0

    async def __call__(self, *, channel, thread_ts):
        self.reads += 1
        if self.fail:
            raise RuntimeError("replies API down")
        return {"messages": list(self.messages), "complete": self.complete}


async def _seed(session, *, state="pending", attempts=0, claimed_at=None,
                handoff_id=None, brief=None, bot_user_id=None) -> PacketBriefDelivery:
    handoff_id = handoff_id or str(uuid.uuid4())
    row = PacketBriefDelivery(
        org_id=_ORG,
        handoff_id=handoff_id,
        state=state,
        idempotency_key=delivery_idempotency_key(handoff_id),
        channel="C0PROD",
        thread_ts="1752600000.0",
        bot_user_id=bot_user_id,
        brief=brief or f"New packet — Launch: https://illo/api/launch-handoffs/{handoff_id}/launch",
        attempts=attempts,
        claimed_at=claimed_at,
    )
    session.add(row)
    await session.flush()
    await session.commit()
    return row


async def _fresh(session, row) -> PacketBriefDelivery:
    await session.refresh(row)  # CAS updates bypass the ORM identity map
    return row


async def test_pending_posts_once_and_marks_posted(session):
    row = await _seed(session)
    poster = FakePoster()
    summary = await deliver_pending_briefs(
        org_id=_ORG, session_factory=_factory(session), poster=poster, now=_NOW
    )
    assert summary["posted"] == 1
    assert len(poster.sent) == 1
    assert poster.sent[0]["channel"] == "C0PROD"
    assert poster.sent[0]["thread_ts"] == "1752600000.0"
    row = await _fresh(session, row)
    assert row.state == "posted"
    assert row.attempts == 1
    assert row.posted_message_ts == "1752600100.0"
    assert row.posted_at is not None


async def test_pending_never_reads_the_thread(session):
    """pending means the claim was never taken, so the send happens blind —
    the disambiguation read is reserved for crashed claims."""
    await _seed(session)
    thread = FakeThread(messages=[])
    await deliver_pending_briefs(
        org_id=_ORG, session_factory=_factory(session), poster=FakePoster(),
        thread_reader=thread, now=_NOW,
    )
    assert thread.reads == 0


async def test_stale_posting_with_brief_already_in_thread_never_resends(session):
    """THE crash window: the worker died between the send and the
    posted-mark. The retry must find the brief in the thread and mark
    posted without a second message."""
    handoff_id = str(uuid.uuid4())
    row = await _seed(
        session, state="posting", attempts=1,
        claimed_at=_NOW - STALE_POSTING_GRACE - timedelta(minutes=1),
        handoff_id=handoff_id,
    )
    poster = FakePoster()
    thread = FakeThread(messages=[
        {"ts": "1752600050.0", "user": "B0ILLO",
         "text": f"New packet — Launch: https://illo/api/launch-handoffs/{handoff_id}/launch"},
    ])
    summary = await deliver_pending_briefs(
        org_id=_ORG, session_factory=_factory(session), poster=poster,
        thread_reader=thread, now=_NOW,
    )
    assert summary["already_posted"] == 1
    assert poster.sent == []  # never re-sent
    row = await _fresh(session, row)
    assert row.state == "posted"
    assert row.posted_message_ts == "1752600050.0"  # the found message


async def test_stale_posting_not_in_thread_sends_once(session):
    """The other side of the crash window: the worker died BEFORE the send
    reached Slack. The thread shows nothing, so the retry sends."""
    row = await _seed(
        session, state="posting", attempts=1,
        claimed_at=_NOW - STALE_POSTING_GRACE - timedelta(minutes=1),
    )
    poster = FakePoster()
    thread = FakeThread(messages=[{"ts": "1", "user": "jb", "text": "unrelated chatter"}])
    summary = await deliver_pending_briefs(
        org_id=_ORG, session_factory=_factory(session), poster=poster,
        thread_reader=thread, now=_NOW,
    )
    assert summary["posted"] == 1
    assert len(poster.sent) == 1
    row = await _fresh(session, row)
    assert row.state == "posted"
    assert row.attempts == 2


async def test_fresh_posting_claim_is_invisible_to_the_sweep(session):
    """A recent claim belongs to a live worker — the selection itself skips
    it (it must neither be stomped nor consume the sweep limit)."""
    row = await _seed(
        session, state="posting", attempts=1,
        claimed_at=_NOW - timedelta(seconds=30),
    )
    poster = FakePoster()
    thread = FakeThread()
    summary = await deliver_pending_briefs(
        org_id=_ORG, session_factory=_factory(session), poster=poster,
        thread_reader=thread, now=_NOW,
    )
    assert summary["selected"] == 0
    assert poster.sent == [] and thread.reads == 0
    row = await _fresh(session, row)
    assert row.state == "posting" and row.attempts == 1


async def test_stuck_rows_cannot_starve_pending_work(session):
    """The sweep limit applies AFTER filtering to claimable rows: a pile of
    fresh in-flight claims must not push deliverable pending rows out."""
    for _ in range(DELIVERY_SWEEP_LIMIT):
        await _seed(session, state="posting", attempts=1,
                    claimed_at=_NOW - timedelta(seconds=5))
    pending = await _seed(session)  # newest row, but the only claimable one
    poster = FakePoster()
    summary = await deliver_pending_briefs(
        org_id=_ORG, session_factory=_factory(session), poster=poster, now=_NOW,
    )
    assert summary["selected"] == 1 and summary["posted"] == 1
    assert (await _fresh(session, pending)).state == "posted"


async def test_disambiguation_trusts_only_bot_authored_messages(session):
    """A human pasting the same launch URL into the thread must not satisfy
    the already-posted check — the brief itself never arrived."""
    handoff_id = str(uuid.uuid4())
    row = await _seed(
        session, state="posting", attempts=1,
        claimed_at=_NOW - STALE_POSTING_GRACE - timedelta(minutes=1),
        handoff_id=handoff_id, bot_user_id="B0ILLO",
    )
    poster = FakePoster()
    thread = FakeThread(messages=[
        {"ts": "1752600050.0", "user": "U0HUMAN",  # a human, not Illo
         "text": f"see https://illo/api/launch-handoffs/{handoff_id}/launch"},
    ])
    summary = await deliver_pending_briefs(
        org_id=_ORG, session_factory=_factory(session), poster=poster,
        thread_reader=thread, now=_NOW,
    )
    assert summary["posted"] == 1  # human paste ignored; the brief still goes out
    assert len(poster.sent) == 1
    assert (await _fresh(session, row)).state == "posted"


async def test_attempt_cap_on_posting_row_checks_thread_before_failing(session):
    """The final ambiguous send may have landed: a capped posting row gets
    one last disambiguation and is recorded posted, not failed."""
    handoff_id = str(uuid.uuid4())
    row = await _seed(
        session, state="posting", attempts=MAX_DELIVERY_ATTEMPTS,
        claimed_at=_NOW - STALE_POSTING_GRACE - timedelta(minutes=1),
        handoff_id=handoff_id,
    )
    poster = FakePoster()
    thread = FakeThread(messages=[
        {"ts": "1752600070.0", "user": "B0ILLO",
         "text": f"New packet — Launch: https://illo/api/launch-handoffs/{handoff_id}/launch"},
    ])
    summary = await deliver_pending_briefs(
        org_id=_ORG, session_factory=_factory(session), poster=poster,
        thread_reader=thread, now=_NOW,
    )
    assert summary["already_posted"] == 1 and summary["failed"] == 0
    assert poster.sent == []
    row = await _fresh(session, row)
    assert row.state == "posted"
    assert row.posted_message_ts == "1752600070.0"


async def test_claim_posts_the_current_committed_brief_not_the_snapshot(session):
    """Drift repair may rewrite a pending brief between the sweep's snapshot
    and its claim — the send must carry the row's CURRENT content (the
    fence-verified re-read), never the pre-claim snapshot."""
    row = await _seed(session, brief="stale V1 brief")

    calls = {"n": 0}

    class InterleavingFactory:
        """First factory() call serves the selection snapshot; the rewrite
        lands right after it, BEFORE the claim transaction."""

        def __call__(self):
            calls["n"] += 1
            if calls["n"] == 2:  # selection done; claim about to start
                row.brief = "fresh V2 brief"
                # synchronous mutation on the shared sqlite session is
                # committed by the claim's own commit below
            return _SessionLease(session)

    poster = FakePoster()
    summary = await deliver_pending_briefs(
        org_id=_ORG, session_factory=InterleavingFactory(), poster=poster, now=_NOW,
    )
    assert summary["posted"] == 1
    assert poster.sent[0]["text"] == "fresh V2 brief"


async def test_incomplete_thread_read_never_blind_posts(session):
    """A truncated read can't prove the brief absent — an unread tail may
    hide it. 'Not found' in an incomplete read must stay claimed, not send."""
    row = await _seed(
        session, state="posting", attempts=1,
        claimed_at=_NOW - STALE_POSTING_GRACE - timedelta(minutes=1),
    )
    poster = FakePoster()
    thread = FakeThread(messages=[{"ts": "1", "user": "jb", "text": "unrelated"}],
                        complete=False)
    summary = await deliver_pending_briefs(
        org_id=_ORG, session_factory=_factory(session), poster=poster,
        thread_reader=thread, now=_NOW,
    )
    assert summary["undecided"] == 1
    assert poster.sent == []
    assert (await _fresh(session, row)).state == "posting"


async def test_disambiguation_read_failure_stays_posting(session):
    """Can't read the thread → can't prove the brief isn't there → never
    send. The row stays claimed for the next sweep."""
    row = await _seed(
        session, state="posting", attempts=1,
        claimed_at=_NOW - STALE_POSTING_GRACE - timedelta(minutes=1),
    )
    poster = FakePoster()
    summary = await deliver_pending_briefs(
        org_id=_ORG, session_factory=_factory(session), poster=poster,
        thread_reader=FakeThread(fail=True), now=_NOW,
    )
    assert summary["undecided"] == 1
    assert poster.sent == []
    row = await _fresh(session, row)
    assert row.state == "posting"


async def test_definite_slack_reject_requeues_with_error(session):
    row = await _seed(session)
    summary = await deliver_pending_briefs(
        org_id=_ORG, session_factory=_factory(session),
        poster=FakePoster(fail_with=SlackApiError("ratelimited")), now=_NOW,
    )
    assert summary["requeued"] == 1
    row = await _fresh(session, row)
    assert row.state == "pending"  # definite reject: provably not posted
    assert row.attempts == 1
    assert "ratelimited" in (row.last_error or "")


async def test_permanent_slack_reject_fails_loudly(session, caplog):
    row = await _seed(session)
    with caplog.at_level(logging.ERROR, logger="brain.systems.briefing.deliver"):
        summary = await deliver_pending_briefs(
            org_id=_ORG, session_factory=_factory(session),
            poster=FakePoster(fail_with=SlackApiError("channel_not_found")), now=_NOW,
        )
    assert summary["failed"] == 1
    row = await _fresh(session, row)
    assert row.state == "failed"
    assert any("FAILED permanently" in r.getMessage() for r in caplog.records)


async def test_ambiguous_transport_error_stays_posting(session):
    """A timeout may have delivered: the row must NOT go back to pending
    (pending is send-blind) — it stays posting for the disambiguating pass."""
    row = await _seed(session)
    summary = await deliver_pending_briefs(
        org_id=_ORG, session_factory=_factory(session),
        poster=FakePoster(fail_with=TimeoutError("socket timeout")), now=_NOW,
    )
    assert summary["undecided"] == 1
    row = await _fresh(session, row)
    assert row.state == "posting"
    assert row.attempts == 1


async def test_attempt_cap_fails_loudly_instead_of_churning(session, caplog):
    row = await _seed(session, attempts=MAX_DELIVERY_ATTEMPTS)
    poster = FakePoster()
    with caplog.at_level(logging.ERROR, logger="brain.systems.briefing.deliver"):
        summary = await deliver_pending_briefs(
            org_id=_ORG, session_factory=_factory(session), poster=poster, now=_NOW,
        )
    assert summary["failed"] == 1
    assert poster.sent == []
    row = await _fresh(session, row)
    assert row.state == "failed"
    assert any("FAILED permanently" in r.getMessage() for r in caplog.records)


async def test_targeted_delivery_only_touches_named_handoffs(session):
    target = await _seed(session)
    other = await _seed(session)
    poster = FakePoster()
    summary = await deliver_pending_briefs(
        org_id=_ORG, handoff_ids=[str(target.handoff_id)],
        session_factory=_factory(session), poster=poster, now=_NOW,
    )
    assert summary["selected"] == 1 and summary["posted"] == 1
    assert (await _fresh(session, target)).state == "posted"
    assert (await _fresh(session, other)).state == "pending"


async def test_sweep_is_org_scoped(session):
    mine = await _seed(session)
    foreign = PacketBriefDelivery(
        org_id=str(uuid.uuid4()), handoff_id=str(uuid.uuid4()), state="pending",
        idempotency_key="packet-brief:x", channel="C9", thread_ts="9.0", brief="b",
    )
    session.add(foreign)
    await session.commit()
    poster = FakePoster()
    summary = await deliver_pending_briefs(
        org_id=_ORG, session_factory=_factory(session), poster=poster, now=_NOW,
    )
    assert summary["selected"] == 1
    assert (await _fresh(session, mine)).state == "posted"
    assert (await _fresh(session, foreign)).state == "pending"


async def test_delivery_pass_is_totally_contained(caplog):
    def exploding_factory():
        raise RuntimeError("no database today")

    with caplog.at_level(logging.WARNING, logger="brain.systems.briefing.deliver"):
        summary = await deliver_pending_briefs(
            org_id=_ORG, session_factory=exploding_factory, now=_NOW,
        )
    assert summary["selected"] == 0  # and, critically, nothing raised
    assert any("failed safely" in r.getMessage() for r in caplog.records)


async def test_one_bad_row_does_not_starve_the_rest(session):
    """Per-row containment: a poster blowing up on one row must not stop
    the sweep from delivering the next one."""
    bad = await _seed(session)
    good = await _seed(session)

    class SelectivePoster(FakePoster):
        async def __call__(self, *, channel, text, thread_ts):
            if str(bad.handoff_id) in text:
                raise SlackApiError("ratelimited")
            return await super().__call__(channel=channel, text=text, thread_ts=thread_ts)

    poster = SelectivePoster()
    summary = await deliver_pending_briefs(
        org_id=_ORG, session_factory=_factory(session), poster=poster, now=_NOW,
    )
    assert summary["posted"] == 1 and summary["requeued"] == 1
    assert (await _fresh(session, good)).state == "posted"
    assert (await _fresh(session, bad)).state == "pending"


async def test_attempt_cap_with_unreadable_thread_stays_posting(session, caplog):
    """A capped posting row whose final look can't read the thread must NOT
    be recorded failed — the last ambiguous send may have landed. It stays
    posting: retried loudly, never resolved by guesswork."""
    row = await _seed(
        session, state="posting", attempts=MAX_DELIVERY_ATTEMPTS,
        claimed_at=_NOW - STALE_POSTING_GRACE - timedelta(minutes=1),
    )
    poster = FakePoster()
    summary = await deliver_pending_briefs(
        org_id=_ORG, session_factory=_factory(session), poster=poster,
        thread_reader=FakeThread(fail=True), now=_NOW,
    )
    assert summary["undecided"] == 1 and summary["failed"] == 0
    assert poster.sent == []
    assert (await _fresh(session, row)).state == "posting"


async def test_savepoint_rollback_keeps_the_fast_path_queue(session):
    """Only a ROOT rollback proves the queued deliveries dead. A later,
    unrelated savepoint rolling back must not strip earlier mints of their
    fast path (their rows still commit; visibility-polling covers the rest)."""
    from brain.systems.briefing.deliver import _INFO_QUEUE_KEY, schedule_post_commit_delivery

    armed = schedule_post_commit_delivery(session, org_id=_ORG, handoff_ids=["h1"])
    assert armed is True
    assert session.sync_session.info[_INFO_QUEUE_KEY] == [(_ORG, "h1")]

    try:  # an unrelated savepoint fails and rolls back
        async with session.begin_nested():
            raise RuntimeError("unrelated savepoint failure")
    except RuntimeError:
        pass
    assert session.sync_session.info[_INFO_QUEUE_KEY] == [(_ORG, "h1")]  # kept

    await session.rollback()  # the ROOT transaction dies
    assert session.sync_session.info[_INFO_QUEUE_KEY] == []  # cleared


async def test_uncommitted_outbox_rows_are_invisible_to_the_deliverer(tmp_path, sqlite_postgres_ddl_patch):
    """The whole point of the outbox, proven with REAL independent sessions
    on a file-backed database: a delivery recorded inside a still-open
    transaction is invisible to the deliverer; only the commit publishes it."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.schema import CreateTable

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/outbox.db")
    try:
        async with engine.begin() as conn:
            await conn.execute(CreateTable(PacketBriefDelivery.__table__, if_not_exists=True))
        factory = async_sessionmaker(engine, expire_on_commit=False)

        writer = factory()
        handoff_id = str(uuid.uuid4())
        writer.add(PacketBriefDelivery(
            org_id=_ORG, handoff_id=handoff_id, state="pending",
            idempotency_key=delivery_idempotency_key(handoff_id),
            channel="C0PROD", thread_ts="1752600000.0",
            brief=f"Launch: https://illo/api/launch-handoffs/{handoff_id}/launch",
        ))
        await writer.flush()  # INSERT sent, transaction still open

        poster = FakePoster()
        summary = await deliver_pending_briefs(
            org_id=_ORG, session_factory=factory, poster=poster, now=_NOW,
        )
        assert summary["selected"] == 0  # not committed → not deliverable
        assert poster.sent == []

        await writer.commit()
        summary = await deliver_pending_briefs(
            org_id=_ORG, session_factory=factory, poster=poster, now=_NOW,
        )
        assert summary["posted"] == 1
        assert len(poster.sent) == 1
        await writer.close()
    finally:
        await engine.dispose()


async def test_transfer_moves_only_pending_obligations(session):
    """Supersede carry-over (unit): pending moves to the new handoff with
    the new brief; posted stays where it is and transfers nothing."""
    pending = await _seed(session)
    new_id = str(uuid.uuid4())
    moved = await transfer_pending_delivery(
        session, org_id=_ORG, prior_handoff_id=str(pending.handoff_id),
        new_handoff_id=new_id, new_brief=f"fresh brief {new_id}",
    )
    await session.flush()
    assert moved is True
    assert pending.state == "superseded"
    from sqlalchemy import select

    rows = list((await session.scalars(
        select(PacketBriefDelivery).where(PacketBriefDelivery.handoff_id == new_id)
    )).all())
    assert len(rows) == 1
    assert rows[0].state == "pending"
    assert rows[0].brief == f"fresh brief {new_id}"
    assert rows[0].channel == pending.channel and rows[0].thread_ts == pending.thread_ts

    posted = await _seed(session, state="posted")
    moved = await transfer_pending_delivery(
        session, org_id=_ORG, prior_handoff_id=str(posted.handoff_id),
        new_handoff_id=str(uuid.uuid4()), new_brief="never used",
    )
    assert moved is False
    assert posted.state == "posted"
