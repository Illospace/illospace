"""Slice 05 (illo-handoff-packets): triage-moment minting.

Contract under test: gather→compose→create→stamp→record-delivery
orchestration with a hard noise gate (reuse records nothing), supersede via
the EXISTING status vocabulary (archived + superseded_by — never a new
status), total failure containment (a packet bug can never break triage),
stamps in Illo-owned idea state, and per-owner launch targets from
config-with-default.

Post-slice-05 hardening: mint NEVER posts to Slack in-transaction — it
persists one pending ``PacketBriefDelivery`` per created handoff inside its
savepoint (rolled back with everything else on failure), and the post-commit
deliverer (tests/test_briefing_deliver.py) sends it. The ``posts`` fixture
now guards that nothing here ever talks to Slack synchronously.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from brain.systems.briefing import DossierBudget
from brain.systems.briefing.gather import SlackThreadRead
from brain.systems.briefing.mint import (
    MintResult,
    Readers,
    build_packet_for_job,
    mint_packet_after_triage,
    mint_packet_for_job,
)
from brain.systems.launch_handoffs import (
    LaunchHandoffCreateInput,
    create_launch_handoff_with_status,
)

_T0 = datetime(2026, 7, 9, 9, 0, tzinfo=timezone.utc)
_ORG = "org-1"
_IDEA_ID = "0f6f3f7e-0000-0000-0000-00000000aaaa"
_EVENT_ID = "0f6f3f7e-0000-0000-0000-00000000bbbb"
_OWNER = "8b6f3f7e-0000-0000-0000-000000000001"


def _idea(**overrides):
    base = dict(
        id=_IDEA_ID,
        org_id=_ORG,
        origin="inbound_signal",
        origin_ref=f"inbound_event:{_EVENT_ID}",
        title="Maison L. melted hands",
        description="customer batch regression uwear/uwear-backend#347",
        updated_at=_T0,
        agent_details={
            "inbound_triage": {"event_id": _EVENT_ID},
            "task_domain": "engineering",
            "assignment": {"owner_id": _OWNER, "basis": "rule", "unclaimed": False},
        },
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _event(channel_type: str = "channel", *, org_id: str = _ORG):
    return SimpleNamespace(
        id=_EVENT_ID,
        org_id=org_id,
        envelope={
            "kind": "slack.message",
            "summary": "customer says half the batch melted again",
            "payload": {
                "channel_id": "C0PROD",
                "channel_type": channel_type,
                "thread_ts": "1751964840.0",
                "message_ts": "1751964840.0",
                "bot_user_id": "B0ILLO",  # ingress always resolves this
            },
            "hints": {},
        },
    )


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None

    def scalar_one(self):
        return self._rows[0]

    def first(self):
        return self._rows[0] if self._rows else None

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _NestedTx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class FakeSession:
    """Canned reads + in-memory handoff store honoring the idempotency key."""

    def __init__(self, ideas=(), events=(), users=()):
        self._ideas = list(ideas)
        self._events = {str(e.id): e for e in events}
        self._users = list(users)
        self.handoffs: list[SimpleNamespace] = []
        self.deliveries: list = []  # PacketBriefDelivery rows the mint records
        self._next_id = 1
        self.flush_count = 0

    def begin_nested(self):
        return _NestedTx()

    async def get(self, model, key):
        if model.__name__ == "InboundEventRow":
            return self._events.get(str(key))
        if model.__name__ == "LaunchHandoff":
            for row in self.handoffs:
                if str(row.id) == str(key):
                    return row
        return None

    async def scalar(self, stmt):
        return (await self.execute(stmt)).scalar_one_or_none()

    async def execute(self, stmt):
        params = stmt.compile().params
        values = {str(v) for v in params.values() if not isinstance(v, (list, tuple, set))}
        entity = stmt.column_descriptions[0]["entity"]
        name = getattr(entity, "__name__", None) or str(stmt.column_descriptions[0].get("name"))
        if name == "Idea" and getattr(stmt, "_for_update_arg", None) is not None:
            self.locked_idea_selects = getattr(self, "locked_idea_selects", 0) + 1
        if name == "Idea":
            rows = [
                i for i in self._ideas
                if str(i.org_id) in values
                and (
                    str(i.id) in values
                    or str(getattr(i, "origin_ref", "")) in values
                    or str(dict(getattr(i, "agent_details", None) or {}).get("packet", {}).get("handoff_id")) in values
                )
            ]
        elif name == "LaunchHandoff":
            rows = [
                h for h in self.handoffs
                if str(h.org_id) in values and str(h.idempotency_key) in values
            ]
        elif name == "PacketBriefDelivery":  # deliver's savepoint helpers select by handoff_id
            rows = [d for d in self.deliveries if str(d.handoff_id) in values]
        elif name == "User":  # the label lookup selects User columns
            rows = [u for u in self._users if str(u.id) in values]
        else:  # pragma: no cover
            rows = []
        return _Result(rows)

    def add(self, row):
        # Store the LIVE object (SQLAlchemy identity-map semantics): later
        # mutations through the returned row must be visible in the store.
        if type(row).__name__ == "Idea":
            # The actionable-lane hook creates the job-home idea itself; the
            # ORM id default fires at INSERT, which this fake must emulate.
            if not getattr(row, "id", None):
                row.id = f"idea-{self._next_id}"
                self._next_id += 1
            self._ideas.append(row)
            return
        if type(row).__name__ == "PacketBriefDelivery":
            if not getattr(row, "id", None):
                row.id = f"del-{self._next_id}"
                self._next_id += 1
            self.deliveries.append(row)
            return
        if not getattr(row, "id", None):
            row.id = f"hf-{self._next_id}"
            self._next_id += 1
        if getattr(row, "status", None) is None:
            row.status = "open"
        if getattr(row, "launch_count", None) is None:
            row.launch_count = 0
        self.handoffs.append(row)

    async def flush(self):
        self.flush_count += 1
        if getattr(self, "fail_next_flush_with", None) is not None:
            exc = self.fail_next_flush_with
            self.fail_next_flush_with = None
            raise exc

    async def rollback(self):
        pass


class FakeSlackReader:
    async def read_thread(self, *, channel, thread_ts, limit):
        return SlackThreadRead(
            messages=({"ts": "1751964840.0", "user": "jb", "text": "half the batch melted"},),
            total=1,
            channel=channel,
        )


class NoGithub:
    async def read_ref(self, *, repo_slug, number):
        return None


def _readers():
    return Readers(slack=FakeSlackReader(), github=NoGithub())


@pytest.fixture(autouse=True)
def _no_real_slack_post(monkeypatch):
    """mint's Slack reply goes through slack_web_client_from_runtime — stub it."""
    posts: list[dict] = []

    class FakeClient:
        async def post_message(self, *, channel, text, thread_ts=None):
            posts.append({"channel": channel, "text": text, "thread_ts": thread_ts})
            return {"ok": True}

    import brain.systems.slack.client as slack_client

    async def fake_from_runtime(**_kwargs):
        return FakeClient()

    monkeypatch.setattr(slack_client, "slack_web_client_from_runtime", fake_from_runtime)
    return posts


@pytest.fixture()
def posts(_no_real_slack_post):
    return _no_real_slack_post


async def test_create_with_status_flags_created_vs_reused():
    session = FakeSession()
    payload = LaunchHandoffCreateInput(
        org_id=_ORG, created_by_user_id=None, title="t", instructions="i",
        idempotency_key="job:abc",
    )
    row, created = await create_launch_handoff_with_status(session, payload)
    assert created is True
    again, created_again = await create_launch_handoff_with_status(session, payload)
    assert created_again is False
    assert str(again.id) == str(row.id)


async def test_triage_mint_is_silent_and_stamps_idea(posts):
    idea = _idea()
    session = FakeSession(ideas=[idea], events=[_event()],
                          users=[SimpleNamespace(id=_OWNER, name="Axel", email=None)])
    result = await mint_packet_after_triage(
        session, event=_event(), run_row=SimpleNamespace(id=1), attribution=None,
        readers=_readers(),
    )
    assert result.ok and result.created
    # Briefs retired (Reda, 2026-07-30): the ticket IS the handoff, so the
    # lanes owe NO Slack post and record NO delivery — the packet minting,
    # stamping and launch URL are the whole contract.
    assert result.delivery == "skipped:briefs_retired"
    assert posts == []
    assert session.deliveries == []
    assert "Launch: http" in result.human_brief
    assert "→ Axel" in result.human_brief
    stamp = idea.agent_details["packet"]
    assert stamp["handoff_id"] == str(result.handoff.id)
    assert stamp["owner_user_id"] == _OWNER
    assert stamp["revision"] == (result.handoff.metadata_ or {}).get("revision")


async def test_remint_of_unchanged_truth_is_silent(posts):
    idea = _idea()
    session = FakeSession(ideas=[idea], events=[_event()],
                          users=[SimpleNamespace(id=_OWNER, name="Axel", email=None)])
    first = await mint_packet_after_triage(
        session, event=_event(), run_row=SimpleNamespace(id=1), readers=_readers())
    second = await mint_packet_after_triage(
        session, event=_event(), run_row=SimpleNamespace(id=2), readers=_readers())
    assert first.created and second.ok and not second.created
    assert second.reason == "reused"
    assert second.delivery == "none"
    assert session.deliveries == []  # briefs retired: no lane ever records one
    assert len(session.handoffs) == 1
    assert posts == []


async def test_changed_truth_supersedes_with_existing_vocabulary(posts):
    idea = _idea()
    session = FakeSession(ideas=[idea], events=[_event()],
                          users=[SimpleNamespace(id=_OWNER, name="Axel", email=None)])
    first = await mint_packet_after_triage(
        session, event=_event(), run_row=SimpleNamespace(id=1), readers=_readers())
    # Truth changes → different dossier → new revision/key.
    idea.title = "Maison L. melted hands — rerun requested"
    second = await mint_packet_after_triage(
        session, event=_event(), run_row=SimpleNamespace(id=2), readers=_readers())
    assert second.created and second.delivery == "skipped:briefs_retired"
    old = next(h for h in session.handoffs if str(h.id) == str(first.handoff.id))
    new = next(h for h in session.handoffs if str(h.id) == str(second.handoff.id))
    assert old.status == "archived"  # NEVER an invented status
    assert (old.metadata_ or {}).get("superseded_by") == str(new.id)
    assert (new.metadata_ or {}).get("supersedes") == str(old.id)
    assert idea.agent_details["packet"]["handoff_id"] == str(new.id)
    # Briefs retired: neither mint records an obligation, and the supersede
    # transfer finds nothing pending to carry.
    assert session.deliveries == []
    assert posts == []


async def test_mint_failure_is_contained(posts):
    class ExplodingSlack:
        async def read_thread(self, **_kwargs):
            raise RuntimeError("boom")

    idea = _idea(agent_details={"inbound_triage": {}, "assignment": {}})

    class BrokenSession(FakeSession):
        async def execute(self, stmt):  # any DB touch explodes
            raise RuntimeError("db down")

    result = await mint_packet_after_triage(
        BrokenSession(), event=_event(), run_row=SimpleNamespace(id=1),
        readers=Readers(slack=ExplodingSlack(), github=None),
    )
    assert result.ok is False
    assert "db down" in result.reason
    assert posts == []  # and, critically, nothing raised


async def test_non_public_provenance_mints_but_never_records_delivery(posts):
    idea = _idea()
    session = FakeSession(ideas=[idea], events=[_event(channel_type="group")],
                          users=[SimpleNamespace(id=_OWNER, name="Axel", email=None)])
    result = await mint_packet_after_triage(
        session, event=_event(channel_type="group"), run_row=SimpleNamespace(id=1),
        readers=_readers(),
    )
    assert result.ok and result.created
    # With briefs retired the provenance never matters — every lane mint is
    # silent, public channel or not.
    assert result.delivery == "skipped:briefs_retired"
    assert session.deliveries == []  # nothing owed, ever — not even later
    assert posts == []
    assert len(session.handoffs) == 1  # the packet still exists for the digest


async def test_unclaimed_items_get_packets(posts):
    idea = _idea(agent_details={
        "inbound_triage": {"event_id": _EVENT_ID},
        "task_domain": "engineering",
        "assignment": {"owner_id": None, "basis": "unassigned", "unclaimed": True},
    })
    session = FakeSession(ideas=[idea], events=[_event()])
    result = await mint_packet_after_triage(
        session, event=_event(), run_row=SimpleNamespace(id=1), readers=_readers())
    assert result.ok and result.created
    assert "→ unclaimed" in result.human_brief


async def test_owner_target_map_consumed(monkeypatch, posts):
    monkeypatch.setenv("ILLO_MEMBER_AGENT_TARGETS", f"{_OWNER}=claude")
    idea = _idea()
    session = FakeSession(ideas=[idea], events=[_event()],
                          users=[SimpleNamespace(id=_OWNER, name="Reda", email=None)])
    result = await mint_packet_after_triage(
        session, event=_event(), run_row=SimpleNamespace(id=1), readers=_readers())
    assert result.handoff.target_tool == "claude"
    assert "target=claude" in result.launch_url


async def test_record_job_ref_from_attribution(posts):
    idea = _idea()
    session = FakeSession(ideas=[idea], events=[_event()],
                          users=[SimpleNamespace(id=_OWNER, name="Axel", email=None)])
    attribution = {"target_refs": [{"kind": "domain_record", "id": "1238", "source": "t"}]}
    result = await mint_packet_after_triage(
        session, event=_event(), run_row=SimpleNamespace(id=1),
        attribution=attribution, readers=_readers(),
    )
    assert result.ok
    assert (result.handoff.metadata_ or {}).get("job_ref") == "domain_record:1238"


async def test_no_idea_for_event_is_a_quiet_skip(posts):
    session = FakeSession(events=[_event()])
    result = await mint_packet_after_triage(
        session, event=_event(), run_row=SimpleNamespace(id=1), readers=_readers())
    assert result.ok is False
    assert result.reason == "no triage idea for event"
    assert posts == []


async def test_own_brief_in_thread_does_not_rotate_revision(posts):
    """The self-echo regression (review finding 1): Illo's posted brief must
    not feed the next gather, or every reconcile re-mints and re-posts."""
    idea = _idea()
    session = FakeSession(ideas=[idea], events=[_event()],
                          users=[SimpleNamespace(id=_OWNER, name="Axel", email=None)])

    class EchoingSlack:
        """After the first mint, the thread contains Illo's own reply."""

        def __init__(self):
            self.extra: list[dict] = []

        async def read_thread(self, *, channel, thread_ts, limit):
            base = [{"ts": "1751964840.0", "user": "jb", "text": "half the batch melted"}]
            return SlackThreadRead(
                messages=tuple(base + self.extra), total=1 + len(self.extra), channel=channel
            )

    slack = EchoingSlack()
    readers = Readers(slack=slack, github=NoGithub())
    first = await mint_packet_after_triage(
        session, event=_event(), run_row=SimpleNamespace(id=1), readers=readers)
    assert first.created and session.deliveries == []
    # A historical Illo brief (from the pre-retirement era) sitting in the
    # thread under the BOT user id must still be echo-filtered.
    slack.extra.append({"ts": "1751964900.0", "user": "B0ILLO",
                        "text": first.human_brief})
    second = await mint_packet_after_triage(
        session, event=_event(), run_row=SimpleNamespace(id=2), readers=readers)
    assert not second.created  # bot message filtered → same dossier → same key
    assert len(session.handoffs) == 1


async def test_integrity_race_reselects_as_reused_and_stamps(posts):
    """Losing the (org, idempotency_key) race must re-select the winner's
    row, stamp the idea, and post nothing."""
    from sqlalchemy.exc import IntegrityError

    idea = _idea()
    winner_session = FakeSession(ideas=[idea], events=[_event()],
                                 users=[SimpleNamespace(id=_OWNER, name="Axel", email=None)])
    first = await mint_packet_after_triage(
        winner_session, event=_event(), run_row=SimpleNamespace(id=1), readers=_readers())
    assert first.created and winner_session.deliveries == []

    # The loser: same content, but its insert flush raises IntegrityError.
    idea.agent_details = {**idea.agent_details}
    idea.agent_details.pop("packet", None)  # pretend loser hasn't stamped yet
    winner_session.fail_next_flush_with = IntegrityError("dup", None, Exception("uq"))
    second = await mint_packet_after_triage(
        winner_session, event=_event(), run_row=SimpleNamespace(id=2), readers=_readers())
    assert second.ok and not second.created
    assert second.delivery == "none"  # loser records nothing
    assert idea.agent_details["packet"]["handoff_id"] == str(first.handoff.id)


async def test_stamp_reads_go_through_a_row_locked_idea(posts):
    idea = _idea()
    session = FakeSession(ideas=[idea], events=[_event()],
                          users=[SimpleNamespace(id=_OWNER, name="Axel", email=None)])
    await mint_packet_after_triage(
        session, event=_event(), run_row=SimpleNamespace(id=1), readers=_readers())
    assert getattr(session, "locked_idea_selects", 0) >= 1  # spec-pinned serialization


async def test_owner_label_lookup_failure_uses_human_safe_fallback(posts):
    idea = _idea()
    session = FakeSession(ideas=[idea], events=[_event()], users=[])  # no User row
    result = await mint_packet_after_triage(
        session, event=_event(), run_row=SimpleNamespace(id=1), readers=_readers())
    header = result.human_brief.splitlines()[0]
    assert "unclaimed" not in header  # assigned item never reads as unclaimed
    assert "assigned teammate" in header
    assert _OWNER not in result.human_brief


class CleanGithub:
    async def read_ref(self, *, repo_slug, number):
        return {"kind": "github_pr", "title": "Restore backfill", "body": "b",
                "state": "open", "body_total_chars": 1}


class DownGithub:
    async def read_ref(self, *, repo_slug, number):
        raise RuntimeError("down")


def _clean_readers():
    """Readers whose gather yields ZERO source notes — refresh requires a
    clean gather (degraded views must not supersede healthy rows)."""
    return Readers(slack=FakeSlackReader(), github=CleanGithub())


async def _seed_legacy_brief(session, handoff, brief: str):
    """Record a pre-retirement pending brief the way the lanes used to.

    The live lanes record no deliveries anymore; these rows exist only as
    legacy outbox state whose drain/transfer semantics must keep holding."""
    from brain.systems.briefing.deliver import DeliveryTarget, record_brief_delivery

    await record_brief_delivery(
        session, org_id=_ORG, handoff_id=str(handoff.id),
        target=DeliveryTarget(channel="C0PROD", thread_ts="1751964840.0",
                              bot_user_id="B0ILLO"),
        brief=brief,
    )


async def test_refresh_unchanged_truth_is_silent_reuse(posts):
    from brain.systems.briefing.mint import refresh_packet_for_job

    idea = _idea()
    session = FakeSession(ideas=[idea], events=[_event()],
                          users=[SimpleNamespace(id=_OWNER, name="Axel", email=None)])
    first = await mint_packet_after_triage(
        session, event=_event(), run_row=SimpleNamespace(id=1), readers=_clean_readers())
    await _seed_legacy_brief(session, first.handoff, first.human_brief)
    result = await refresh_packet_for_job(
        session, org_id=_ORG, handoff_row=first.handoff, readers=_clean_readers())
    assert result.ok and not result.created
    assert result.delivery == "none"  # refresh never records a new post
    assert len(session.deliveries) == 1  # the legacy obligation, untouched
    assert session.deliveries[0].state == "pending"
    assert len(session.handoffs) == 1
    assert posts == []


async def test_refresh_skips_on_degraded_gather(posts):
    from brain.systems.briefing.mint import refresh_packet_for_job

    idea = _idea()
    session = FakeSession(ideas=[idea], events=[_event()],
                          users=[SimpleNamespace(id=_OWNER, name="Axel", email=None)])
    first = await mint_packet_after_triage(
        session, event=_event(), run_row=SimpleNamespace(id=1), readers=_clean_readers())
    # Now GitHub is down: the degraded view must NOT supersede the healthy row.
    result = await refresh_packet_for_job(
        session, org_id=_ORG, handoff_row=first.handoff,
        readers=Readers(slack=FakeSlackReader(), github=DownGithub()),
    )
    assert result.ok is False
    assert result.reason == "degraded gather; not refreshing"
    assert len(session.handoffs) == 1  # healthy row untouched
    live = session.handoffs[0]
    assert live.status != "archived"


async def test_refresh_changed_truth_supersedes_and_carries_pending_brief(posts):
    """Refresh never creates a NEW post — but a LEGACY pre-retirement brief
    that never went out must still follow the superseding row (fresh brief,
    fresh launch URL), or the drain would later post a dead link."""
    from brain.systems.briefing.mint import refresh_packet_for_job

    idea = _idea()
    session = FakeSession(ideas=[idea], events=[_event()],
                          users=[SimpleNamespace(id=_OWNER, name="Axel", email=None)])
    first = await mint_packet_after_triage(
        session, event=_event(), run_row=SimpleNamespace(id=1), readers=_clean_readers())
    await _seed_legacy_brief(session, first.handoff, first.human_brief)
    idea.title = "Maison L. melted hands — rerun verified"
    result = await refresh_packet_for_job(
        session, org_id=_ORG, handoff_row=first.handoff, readers=_clean_readers())
    assert result.ok and result.created
    old = next(h for h in session.handoffs if str(h.id) == str(first.handoff.id))
    assert old.status == "archived"
    assert (old.metadata_ or {}).get("superseded_by") == str(result.handoff.id)
    assert idea.agent_details["packet"]["handoff_id"] == str(result.handoff.id)
    by_handoff = {str(d.handoff_id): d for d in session.deliveries}
    assert by_handoff[str(old.id)].state == "superseded"
    carried = by_handoff[str(result.handoff.id)]
    assert carried.state == "pending"
    assert carried.channel == "C0PROD" and carried.thread_ts == "1751964840.0"
    assert str(result.handoff.id) in carried.brief  # refreshed content, live link
    assert posts == []


async def test_refresh_supersede_of_posted_brief_carries_nothing(posts):
    """Once the thread has its reply, supersede must not schedule another
    one — digest/nudge lines carry the new link (the slice-06 stance)."""
    from brain.systems.briefing.mint import refresh_packet_for_job

    idea = _idea()
    session = FakeSession(ideas=[idea], events=[_event()],
                          users=[SimpleNamespace(id=_OWNER, name="Axel", email=None)])
    first = await mint_packet_after_triage(
        session, event=_event(), run_row=SimpleNamespace(id=1), readers=_clean_readers())
    await _seed_legacy_brief(session, first.handoff, first.human_brief)
    session.deliveries[0].state = "posted"  # the deliverer already sent it
    idea.title = "Maison L. melted hands — rerun verified"
    result = await refresh_packet_for_job(
        session, org_id=_ORG, handoff_row=first.handoff, readers=_clean_readers())
    assert result.ok and result.created
    assert len(session.deliveries) == 1  # no new obligation
    assert session.deliveries[0].state == "posted"


async def test_refresh_rejects_non_packet_handoffs(posts):
    from brain.systems.briefing.mint import refresh_packet_for_job

    manual = SimpleNamespace(id="hf-manual", source_surface="illo", metadata_={},
                             summary="s", target_tool="codex", source_ref={})
    result = await refresh_packet_for_job(
        FakeSession(), org_id=_ORG, handoff_row=manual, readers=_readers())
    assert result.ok is False
    assert result.reason == "not a packet-minted handoff"


async def test_probe_stage_creates_and_posts_nothing(posts):
    idea = _idea()
    session = FakeSession(ideas=[idea], events=[_event()])
    packet, dossier = await build_packet_for_job(
        session, org_id=_ORG, job_ref=f"idea:{_IDEA_ID}",
        ask="take a pass", owner_user_id=_OWNER, owner_label="Axel",
        target_tool="codex", readers=_readers(), budget=DossierBudget(),
    )
    assert "Launch: {launch_url}" in packet.human_brief  # placeholder intact
    assert session.handoffs == []  # no row
    assert session.deliveries == []  # no obligation
    assert posts == []  # no reply


# --- Actionable-run minting (slack_teammate_run / illo_submit lanes) ---
#
# These lanes have NO pre-run triage idea and their receipts are terminal at
# admission, so the hook must (a) only mint on durable-work evidence, (b)
# create the job-home idea itself, and (c) one-shot via the packet stamp.

_RUN_USER = "8b6f3f7e-0000-0000-0000-000000000002"
_AUTHORITY = "8b6f3f7e-0000-0000-0000-000000000003"
_ISSUE_REF = {
    "kind": "github_issue",
    "id": "uwear-ai/uwear-backend#616",
    "source": "create_github_issue",
}


def _actionable_event(channel_type: str = "channel"):
    event = _event(channel_type)
    event.origin = "slack.channel_message"
    event.connection_id = "conn-1"
    event.authority_user_id = _AUTHORITY
    return event


def _run_row():
    return SimpleNamespace(id=1602, user_id=_RUN_USER)


def _attribution(refs=(_ISSUE_REF,)):
    return {
        "summary": "Illo created github_issue using create_github_issue.",
        "tags": ["created"],
        "tool_names": ["create_github_issue"],
        "target_refs": list(refs),
        "mutated_target_refs": list(refs),
        "run_event_ids": [1],
    }


@pytest.fixture(autouse=True)
def _hermetic_assignment_env(monkeypatch):
    for key in ("ILLO_BUSINESS_OWNER_USER_ID", "ILLO_PRODUCT_OWNER_USER_ID",
                "ILLO_REPO_OWNERS", "ILLO_UNCLAIMED_POOL_USER_ID"):
        monkeypatch.delenv(key, raising=False)


async def test_actionable_run_mints_job_home_silently(posts):
    from brain.systems.briefing.mint import mint_packet_after_actionable_run

    session = FakeSession(events=[_actionable_event()])
    result = await mint_packet_after_actionable_run(
        session, event=_actionable_event(), run_row=_run_row(),
        attribution=_attribution(), readers=_readers(),
    )

    assert result.ok and result.created
    assert result.delivery == "skipped:briefs_retired"
    assert len(session.handoffs) == 1
    row = session.handoffs[0]
    assert row.source_surface == "inbound_triage"
    assert (row.metadata_ or {}).get("job_ref", "").startswith("idea:")

    assert len(session._ideas) == 1
    idea = session._ideas[0]
    assert idea.origin_ref == f"inbound_event:{_EVENT_ID}"
    assert idea.origin == "inbound_signal"
    details = idea.agent_details
    assert details["inbound_triage"]["event_id"] == _EVENT_ID
    assert details["inbound_triage"]["reason"] == "actionable_run_completion"
    assert details["assignment"]["owner_id"] == _RUN_USER  # connection basis
    assert details["task_domain"]
    assert "uwear-ai/uwear-backend#616" in (idea.description or "")
    assert details["packet"]["handoff_id"] == str(row.id)  # stamped

    assert posts == []  # briefs retired: the ticket IS the handoff
    assert session.deliveries == []
    brief = result.human_brief  # still composed for tracker/digest surfaces
    assert "half the batch melted" in brief
    assert "Illo run" not in brief
    assert "task_domain:" not in brief
    assert "basis:" not in brief
    assert _RUN_USER not in brief


async def test_actionable_run_with_existing_reply_composes_compact_follow_up(posts):
    from brain.systems.briefing.mint import mint_packet_after_actionable_run

    issue_title = "[Prod][Batch] POST /batch exceeds 25s while inner task keeps running"
    raw_alert = '<rollbar-link|#2333 New error: {"active_after_504": 1, "route": "/batch"}>'
    event = _actionable_event()
    event.envelope["summary"] = raw_alert

    class RollbarThread:
        async def read_thread(self, *, channel, thread_ts, limit):
            return SlackThreadRead(
                messages=({"ts": thread_ts, "user": "rollbar", "text": raw_alert},),
                total=1,
                channel=channel,
            )

    class FiledIssue:
        async def read_ref(self, *, repo_slug, number):
            assert (repo_slug, number) == ("uwear-ai/uwear-backend", 616)
            return {
                "kind": "github_issue",
                "title": issue_title,
                "body": "The request times out while work continues in the background.",
                "state": "open",
                "body_total_chars": 63,
            }

    attribution = _attribution()
    attribution["tool_names"] = ["post_slack_reply", "create_github_issue"]
    session = FakeSession(
        events=[event],
        users=[SimpleNamespace(id=_RUN_USER, name="Axel", email=None)],
    )
    result = await mint_packet_after_actionable_run(
        session,
        event=event,
        run_row=_run_row(),
        attribution=attribution,
        readers=Readers(slack=RollbarThread(), github=FiledIssue()),
    )

    assert result.ok and result.created
    assert result.delivery == "skipped:briefs_retired"
    assert result.handoff.title == issue_title
    assert issue_title in result.handoff.instructions
    assert raw_alert not in result.handoff.instructions

    # No Slack delivery exists anymore; the compact form survives only as
    # the composed human_brief for tracker/digest surfaces.
    assert session.deliveries == []
    brief = result.human_brief
    assert brief == f"→ Axel · Launch: {result.launch_url}"
    assert "Illo run" not in brief
    assert "task_domain:" not in brief
    assert "basis:" not in brief
    assert _RUN_USER not in brief
    assert raw_alert not in brief


async def test_actionable_run_without_durable_work_skips(posts):
    from brain.systems.briefing.mint import mint_packet_after_actionable_run

    chat_only = {
        "mutated_target_refs": [
            {"kind": "message", "id": "m1", "source": "post_slack_reply"},
        ],
    }
    session = FakeSession(events=[_actionable_event()])
    result = await mint_packet_after_actionable_run(
        session, event=_actionable_event(), run_row=_run_row(),
        attribution=chat_only, readers=_readers(),
    )
    assert result.ok is False
    assert result.reason == "no durable work created by run"
    assert session.handoffs == []
    assert session._ideas == []
    assert session.deliveries == []
    assert posts == []


async def test_actionable_run_mint_is_one_shot_per_event(posts):
    from brain.systems.briefing.mint import mint_packet_after_actionable_run

    session = FakeSession(events=[_actionable_event()])
    first = await mint_packet_after_actionable_run(
        session, event=_actionable_event(), run_row=_run_row(),
        attribution=_attribution(), readers=_readers(),
    )
    assert first.ok and first.created
    second = await mint_packet_after_actionable_run(
        session, event=_actionable_event(), run_row=_run_row(),
        attribution=_attribution(), readers=_readers(),
    )
    assert second.ok is False
    assert second.reason == "packet already minted for event"
    assert len(session.handoffs) == 1
    assert len(session._ideas) == 1
    assert session.deliveries == []


async def test_actionable_run_non_public_provenance_never_records_delivery(posts):
    from brain.systems.briefing.mint import mint_packet_after_actionable_run

    session = FakeSession(events=[_actionable_event("im")])
    result = await mint_packet_after_actionable_run(
        session, event=_actionable_event("im"), run_row=_run_row(),
        attribution=_attribution(), readers=_readers(),
    )
    assert result.ok and result.created
    assert result.delivery == "skipped:briefs_retired"
    assert len(session.handoffs) == 1  # persistence is never suppressed
    assert session.deliveries == []
    assert posts == []


async def test_actionable_run_repo_rule_owns_the_packet(monkeypatch, posts):
    from brain.systems.briefing.mint import mint_packet_after_actionable_run

    ruled_owner = "8b6f3f7e-0000-0000-0000-00000000000e"
    monkeypatch.setenv("ILLO_REPO_OWNERS", f"uwear-backend={ruled_owner}")
    session = FakeSession(events=[_actionable_event()])
    result = await mint_packet_after_actionable_run(
        session, event=_actionable_event(), run_row=_run_row(),
        attribution=_attribution(), readers=_readers(),
    )
    assert result.ok and result.created
    idea = session._ideas[0]
    assert idea.agent_details["assignment"]["owner_id"] == ruled_owner
    assert idea.agent_details["assignment"]["basis"] == "rule"


async def test_actionable_run_unowned_without_pool_skips(posts):
    from brain.systems.briefing.mint import mint_packet_after_actionable_run

    event = _actionable_event()
    event.authority_user_id = None
    session = FakeSession(events=[event])
    result = await mint_packet_after_actionable_run(
        session, event=event, run_row=SimpleNamespace(id=1, user_id=None),
        attribution=_attribution(), readers=_readers(),
    )
    assert result.ok is False
    assert result.reason == "no owner and no unclaimed pool for job home"
    assert session.handoffs == []
    assert session._ideas == []


async def test_actionable_run_unowned_parks_on_unclaimed_pool(monkeypatch, posts):
    from brain.systems.briefing.mint import mint_packet_after_actionable_run

    pool = "8b6f3f7e-0000-0000-0000-00000000000f"
    monkeypatch.setenv("ILLO_UNCLAIMED_POOL_USER_ID", pool)
    event = _actionable_event()
    event.authority_user_id = None
    session = FakeSession(events=[event])
    result = await mint_packet_after_actionable_run(
        session, event=event, run_row=SimpleNamespace(id=1, user_id=None),
        attribution=_attribution(), readers=_readers(),
    )
    assert result.ok and result.created
    idea = session._ideas[0]
    assert idea.agent_details["assignment"]["owner_id"] == pool
    assert idea.agent_details["assignment"]["unclaimed"] is True


async def test_event_mint_lock_taken_on_postgres_only():
    """The per-event advisory lock serializes concurrent actionable mints on
    Postgres (ideas have no origin_ref uniqueness); other dialects skip it."""
    from brain.systems.briefing.mint import _acquire_event_mint_lock

    executed: list[str] = []

    class LockSession:
        def __init__(self, dialect_name):
            self.bind = SimpleNamespace(dialect=SimpleNamespace(name=dialect_name))

        async def execute(self, stmt, params=None):
            executed.append((str(stmt), dict(params or {})))
            return _Result([])

    await _acquire_event_mint_lock(LockSession("postgresql"), org_id=_ORG, event_id=_EVENT_ID)
    assert len(executed) == 1
    assert "pg_advisory_xact_lock" in executed[0][0]
    assert executed[0][1]["key"] == f"packet-mint:{_ORG}:{_EVENT_ID}"

    executed.clear()
    await _acquire_event_mint_lock(LockSession("sqlite"), org_id=_ORG, event_id=_EVENT_ID)
    assert executed == []
    await _acquire_event_mint_lock(FakeSession(), org_id=_ORG, event_id=_EVENT_ID)  # no bind at all


async def test_actionable_job_ref_anchors_on_created_work_not_reads(posts):
    """The first live packet (handoff e827d633) anchored its dossier on the
    playbook doc the run READ because _record_job_ref walks target_refs at
    large. The actionable lane must anchor on the run's own durable refs:
    created record wins; issue-only runs anchor on the job-home idea."""
    from brain.systems.briefing.mint import mint_packet_after_actionable_run

    created_record = {"kind": "domain_record", "id": "1826", "source": "manage_domain"}
    read_doc = {"kind": "domain_record", "id": "1272", "source": "manage_domain"}
    attribution = {
        "target_refs": [read_doc, created_record],  # read-first, like run 1627
        "mutated_target_refs": [created_record],
    }
    session = FakeSession(events=[_actionable_event()])
    result = await mint_packet_after_actionable_run(
        session, event=_actionable_event(), run_row=_run_row(),
        attribution=attribution, readers=_readers(),
    )
    assert result.ok and result.created
    assert (session.handoffs[0].metadata_ or {}).get("job_ref") == "domain_record:1826"

    # Issue-only durable work → the idea is the anchor (its description
    # carries owner/repo#N for gather), never a read doc from target_refs.
    session2 = FakeSession(events=[_actionable_event()])
    result2 = await mint_packet_after_actionable_run(
        session2, event=_actionable_event(), run_row=_run_row(),
        attribution={"target_refs": [read_doc, _ISSUE_REF], "mutated_target_refs": [_ISSUE_REF]},
        readers=_readers(),
    )
    assert result2.ok and result2.created
    idea2 = session2._ideas[0]
    assert (session2.handoffs[0].metadata_ or {}).get("job_ref") == f"idea:{idea2.id}"
