"""Slice 03 (illo-handoff-packets): read-only gather wiring.

Contract under test: one gatherer collects raw pieces from the existing
read seams via tiny reader protocols; every degradation (source down,
private channel, partial fetch, missing job) is an explicit source note;
the gather path never writes.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from brain.systems.briefing import DossierBudget, assemble_dossier, gather_pieces
from brain.systems.briefing.compose import compose_packet
from brain.systems.briefing.gather import SlackThreadRead

_T0 = datetime(2026, 7, 9, 9, 0, tzinfo=timezone.utc)
_IDEA_ID = "0f6f3f7e-0000-0000-0000-00000000aaaa"
_EVENT_ID = "0f6f3f7e-0000-0000-0000-00000000bbbb"


def _idea(**overrides):
    base = dict(
        id=_IDEA_ID,
        org_id="org-1",
        title="Maison L. melted hands uwear/uwear-backend#346",
        description="customer batch regression, see uwear/uwear-backend#347",
        updated_at=_T0,
        agent_details={
            "inbound_triage": {"event_id": _EVENT_ID},
            "task_domain": "engineering",
            "assignment": {"owner_id": "u-axel", "basis": "rule", "unclaimed": False},
        },
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _event(channel_type: str = "channel"):
    return SimpleNamespace(
        id=_EVENT_ID,
        envelope={"channel_type": channel_type},
        normalized_payload={"channel": "C0PROD", "thread_ts": "1751964840.0", "channel_type": channel_type},
        raw_payload={},
    )


class WriteForbiddenSession:
    """Fake AsyncSession: canned reads, loud failure on ANY write."""

    def __init__(self, objects: dict):
        self._objects = objects

    async def get(self, model, key):
        return self._objects.get((model.__name__, str(key)))

    def add(self, *_args, **_kwargs):  # pragma: no cover - the assertion IS the test
        raise AssertionError("gather path must never write (session.add called)")

    async def flush(self, *_args, **_kwargs):  # pragma: no cover
        raise AssertionError("gather path must never write (session.flush called)")

    async def commit(self, *_args, **_kwargs):  # pragma: no cover
        raise AssertionError("gather path must never write (session.commit called)")


class FakeSlack:
    def __init__(self, messages=None, total=None, error: Exception | None = None):
        self._messages = messages or []
        self._total = total
        self._error = error
        self.calls: list[dict] = []

    async def read_thread(self, *, channel, thread_ts, limit):
        self.calls.append({"channel": channel, "thread_ts": thread_ts, "limit": limit})
        if self._error:
            raise self._error
        fetched = tuple(self._messages[:limit])
        return SlackThreadRead(
            messages=fetched,
            total=self._total if self._total is not None else len(self._messages),
            channel=channel,
        )


class FakeGithub:
    def __init__(self, refs=None, error: Exception | None = None):
        self._refs = refs or {}
        self._error = error

    async def read_ref(self, *, repo_slug, number):
        if self._error:
            raise self._error
        return self._refs.get((repo_slug, number))


def _session(idea=None, event=None):
    objects = {}
    if idea is not None:
        objects[("Idea", _IDEA_ID)] = idea
    if event is not None:
        objects[("InboundEventRow", _EVENT_ID)] = event
    return WriteForbiddenSession(objects)


def _messages(n):
    return [
        {"ts": f"175196484{i}.0", "user": f"u{i}", "text": f"message number {i}"}
        for i in range(n)
    ]


async def test_happy_path_gathers_all_sources_read_only():
    session = _session(_idea(), _event())
    slack = FakeSlack(messages=_messages(3))
    github = FakeGithub(
        refs={
            ("uwear/uwear-backend", 346): {"kind": "github_issue", "title": "Backfill default_model", "body": "41/96 affected"},
            ("uwear/uwear-backend", 347): {"kind": "github_pr", "title": "Restore backfill", "body": "adds regression test"},
        }
    )
    result = await gather_pieces(
        session, org_id="org-1", job_ref=f"idea:{_IDEA_ID}",
        slack=slack, github=github, budget=DossierBudget(),
    )
    sources = sorted({p.source for p in result.pieces})
    assert sources == ["github_issue", "github_pr", "record", "slack_thread"]
    assert result.source_notes == []
    # The gather feeds the pure layer cleanly, end to end.
    dossier = assemble_dossier(
        result.pieces, job_ref=f"idea:{_IDEA_ID}", budget=DossierBudget(),
        source_notes=result.source_notes,
    )
    packet = compose_packet(dossier, org_id="org-1", ask="take a pass")
    assert "uwear/uwear-backend#346" in packet.human_brief


async def test_missing_job_yields_note_not_crash():
    result = await gather_pieces(
        _session(), org_id="org-1", job_ref=f"idea:{_IDEA_ID}",
        slack=FakeSlack(), github=FakeGithub(), budget=DossierBudget(),
    )
    assert result.pieces == []
    assert result.source_notes == ["record: job not found"]


async def test_private_channel_is_omitted_with_note_and_never_fetched():
    slack = FakeSlack(messages=_messages(3))
    result = await gather_pieces(
        _session(_idea(), _event(channel_type="im")), org_id="org-1",
        job_ref=f"idea:{_IDEA_ID}", slack=slack, github=None, budget=DossierBudget(),
    )
    assert slack.calls == []  # boundary enforced BEFORE the read, not after
    assert "slack: private source omitted" in result.source_notes
    assert all(p.source != "slack_thread" for p in result.pieces)


async def test_slack_failure_degrades_to_note():
    result = await gather_pieces(
        _session(_idea(), _event()), org_id="org-1", job_ref=f"idea:{_IDEA_ID}",
        slack=FakeSlack(error=RuntimeError("boom")), github=None, budget=DossierBudget(),
    )
    assert any(note.startswith("slack: unavailable") for note in result.source_notes)
    assert any(p.source == "record" for p in result.pieces)  # gather still returns


async def test_github_failure_and_miss_note_per_ref():
    result = await gather_pieces(
        _session(_idea(), _event()), org_id="org-1", job_ref=f"idea:{_IDEA_ID}",
        slack=None, github=FakeGithub(error=RuntimeError("rate limited")),
        budget=DossierBudget(),
    )
    github_notes = [n for n in result.source_notes if n.startswith("github:")]
    assert len(github_notes) == 2  # one per discovered ref (#346, #347)

    miss = await gather_pieces(
        _session(_idea(), _event()), org_id="org-1", job_ref=f"idea:{_IDEA_ID}",
        slack=None, github=FakeGithub(refs={}), budget=DossierBudget(),
    )
    assert any("not found in readable window" in n for n in miss.source_notes)


async def test_partial_slack_fetch_reports_true_total():
    budget = DossierBudget(max_items_per_source=3)
    slack = FakeSlack(messages=_messages(10), total=40)
    result = await gather_pieces(
        _session(_idea(), _event()), org_id="org-1", job_ref=f"idea:{_IDEA_ID}",
        slack=slack, github=None, budget=budget,
    )
    assert slack.calls[0]["limit"] == 10  # 2x items cap, floor 10
    fetched = sum(1 for p in result.pieces if p.source == "slack_thread")
    assert any(
        note == f"slack_thread: only {fetched} of 40 messages fetched"
        for note in result.source_notes
    )


async def test_org_scope_mismatch_treated_as_not_found():
    result = await gather_pieces(
        _session(_idea(org_id="org-OTHER"), _event()), org_id="org-1",
        job_ref=f"idea:{_IDEA_ID}", slack=None, github=None, budget=DossierBudget(),
    )
    assert result.pieces == []
    assert result.source_notes == ["record: job not found"]


async def test_source_notes_flow_into_dossier_and_brief():
    dossier = assemble_dossier(
        [], job_ref="idea:x", budget=DossierBudget(),
        source_notes=["slack: private source omitted", "github: unavailable — Boom"],
    )
    assert dossier.source_notes == ("slack: private source omitted", "github: unavailable — Boom")
    assert "private source omitted" in dossier.render_text()
    packet = compose_packet(dossier, org_id="org-1", ask="take a pass")
    assert "2 sources degraded" in packet.human_brief
    omissions_part = [p for p in packet.handoff_input.context_parts if p["source"] == "omissions"]
    assert "slack: private source omitted" in omissions_part[0]["notes"]


async def test_ref_discovery_is_conservative_and_deduped():
    idea = _idea(
        title="see uwear/uwear-backend#346 and uwear/uwear-backend#346 again",
        description="also uwear/x#1 uwear/x#2 uwear/x#3 uwear/x#4 uwear/x#5",
    )
    seen = []

    class CountingGithub:
        async def read_ref(self, *, repo_slug, number):
            seen.append((repo_slug, number))
            return None

    await gather_pieces(
        _session(idea, _event()), org_id="org-1", job_ref=f"idea:{_IDEA_ID}",
        slack=None, github=CountingGithub(), budget=DossierBudget(),
    )
    assert len(seen) == len(set(seen)) == 4  # deduped + capped at _MAX_GITHUB_REFS
