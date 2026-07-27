"""Slice 03 (illo-handoff-packets): read-only gather wiring.

Cross-family-review-hardened contract: fixtures mirror the REAL seam shapes
(ingress writes provenance into ``envelope["payload"].channel_id``; the
backend GitHub read returns a flat {kind,title,body,state,body_total_chars,
checks?} contract), org scoping is enforced on ideas, records, AND events,
the privacy boundary is fail-closed on unknown surface types, every
degradation and cap is an explicit source note, and the gather path never
writes.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from brain.systems.briefing import DossierBudget, assemble_dossier, gather_pieces
from brain.systems.briefing.compose import compose_packet
from brain.systems.briefing.gather import DefaultSlackReader, SlackThreadRead
from brain.systems.chantiers import latest_source_movement
from brain.systems.deploy_state import (
    DeployState,
    DeployStateBatch,
    DeployStateObservation,
)
from brain.systems.deploy_state_github import AncestryObservation

_T0 = datetime(2026, 7, 9, 9, 0, tzinfo=timezone.utc)
_IDEA_ID = "0f6f3f7e-0000-0000-0000-00000000aaaa"
_EVENT_ID = "0f6f3f7e-0000-0000-0000-00000000bbbb"
_ORG = "org-1"


def _idea(**overrides):
    base = dict(
        id=_IDEA_ID,
        org_id=_ORG,
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


def _event(channel_type: str = "channel", *, org_id: str = _ORG, hints: dict | None = None,
           payload_overrides: dict | None = None):
    """REAL shape: ingress puts provenance in envelope['payload'] with
    channel_id/thread_ts/channel_type (see slack/ingress.py + inbound/service.py)."""
    payload = {
        "channel_id": "C0PROD",
        "channel_name": "prod-support",
        "channel_type": channel_type,
        "thread_ts": "1751964840.0",
        "message_ts": "1751964840.0",
    }
    payload.update(payload_overrides or {})
    return SimpleNamespace(
        id=_EVENT_ID,
        org_id=org_id,
        envelope={"kind": "slack.message", "payload": payload, "hints": hints or {}},
        normalized_payload={},
        raw_payload={},
    )


class WriteForbiddenSession:
    """Fake AsyncSession: canned org-filtered reads, loud failure on writes.

    ``execute`` honors the statement's bound parameter VALUES (id/org for
    point lookups; the in-list for the related-records query), so org-scope
    regressions fail here instead of only in production.
    """

    def __init__(self, ideas=(), records=(), events=()):
        self._ideas = list(ideas)
        self._records = list(records)
        self._events = {str(e.id): e for e in events}

    async def get(self, model, key):
        if model.__name__ == "InboundEventRow":
            return self._events.get(str(key))
        raise AssertionError(f"unexpected session.get({model.__name__})")

    async def execute(self, stmt):
        params = stmt.compile().params
        scalars = {str(v) for v in params.values() if not isinstance(v, (list, tuple, set))}
        in_lists = [
            set(map(str, v)) for v in params.values() if isinstance(v, (list, tuple, set))
        ]
        chantier_needle = next(
            (
                str(item["ref"])
                for value in params.values()
                if isinstance(value, list)
                for item in value
                if isinstance(item, dict) and item.get("ref")
            ),
            None,
        )
        entity = stmt.column_descriptions[0]["entity"].__name__

        if entity == "Idea":
            rows = [i for i in self._ideas
                    if str(i.id) in scalars and str(i.org_id) in scalars]
        elif entity == "DomainRecord" and chantier_needle is not None:
            rows = [
                r for r in self._records
                if str(r.org_id) in scalars
                and getattr(r, "object_key", None) == "chantier"
                and chantier_needle
                in {
                    str(item.get("ref"))
                    for item in dict(r.data or {}).get("refs") or []
                    if isinstance(item, dict)
                }
            ]
        elif entity == "DomainRecord" and in_lists:
            external_ids = next(
                (
                    values
                    for values in in_lists
                    if any(
                        str(dict(r.data or {}).get("external_id")) in values
                        for r in self._records
                        if dict(r.data or {}).get("external_id")
                    )
                ),
                None,
            )
            if external_ids is not None:
                rows = [
                    r for r in self._records
                    if str(r.org_id) in scalars
                    and getattr(r, "object_key", None) == "ticket"
                    and str(dict(r.data or {}).get("external_id")) in external_ids
                ]
            else:
                in_list = in_lists[0]
                rows = [r for r in self._records
                        if str(r.org_id) in scalars
                        and str(dict(r.data or {}).get("pr_number")) in in_list]
        elif entity == "DomainRecord":
            rows = [r for r in self._records
                    if str(r.id) in scalars and str(r.org_id) in scalars]
        else:  # pragma: no cover
            raise AssertionError(f"unexpected select entity {entity}")

        class _Result:
            def scalar_one_or_none(self):
                return rows[0] if rows else None

            def scalars(self):
                return self

            def all(self):
                return rows

        return _Result()

    def add(self, *_a, **_k):  # pragma: no cover - the assertion IS the test
        raise AssertionError("gather path must never write (session.add called)")

    async def flush(self, *_a, **_k):  # pragma: no cover
        raise AssertionError("gather path must never write (session.flush called)")

    async def commit(self, *_a, **_k):  # pragma: no cover
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
    """Speaks the FLAT backend-read contract."""

    def __init__(
        self,
        refs=None,
        error: Exception | None = None,
        deploy_states=None,
        deploy_result=None,
    ):
        self._refs = refs or {}
        self._error = error
        self._deploy_states = deploy_states or {}
        self._deploy_result = deploy_result
        self.calls: list[tuple[str, int]] = []
        self.deploy_calls: list[dict[int, tuple[str, str]]] = []

    async def read_ref(self, *, repo_slug, number):
        self.calls.append((repo_slug, number))
        if self._error:
            raise self._error
        return self._refs.get((repo_slug, number))

    async def derive_deploy_states(self, refs):
        self.deploy_calls.append(dict(refs))
        if self._error:
            raise self._error
        if self._deploy_result is not None:
            return self._deploy_result
        return {
            key: self._deploy_states.get(ref)
            for key, ref in refs.items()
        }


def _session(idea=None, event=None, records=()):
    return WriteForbiddenSession(
        ideas=[idea] if idea else [], events=[event] if event else [], records=records
    )


def _messages(n):
    return [
        {"ts": f"175196484{i}.0", "user": f"u{i}", "text": f"message number {i}"}
        for i in range(n)
    ]


def _flat_pr(title="Restore backfill", body="adds regression test", total=None, checks=None):
    payload = {
        "kind": "github_pr", "title": title, "body": body, "state": "open",
        "body_total_chars": total if total is not None else len(body),
    }
    if checks is not None:
        payload["checks"] = checks
    return payload


def _movement_chantier(
    *,
    data_updated_at,
    row_updated_at,
    refs=(),
    created_at=None,
    row_created_at=None,
):
    data = {
        "slug": "shopify-app-sunset",
        "title": "Shopify app sunset",
        "goal": "Done means the legacy app is retired.",
        "kind": "sunset",
        "state": "exploring",
        "owner": "Reda",
        "refs": list(refs),
        "next_step": "Wait for the owner to reply lock.",
        "updated_at": data_updated_at,
    }
    if created_at is not None:
        data["created_at"] = created_at
    return SimpleNamespace(
        id=1995,
        org_id=_ORG,
        domain_id=1,
        object_key="chantier",
        title="Shopify app sunset",
        data=data,
        created_at=row_created_at,
        updated_at=row_updated_at,
    )


async def test_happy_path_gathers_all_sources_read_only():
    session = _session(_idea(), _event())
    slack = FakeSlack(messages=_messages(3))
    github = FakeGithub(
        refs={
            ("uwear/uwear-backend", 346): {"kind": "github_issue", "title": "Backfill default_model",
                                           "body": "41/96 affected", "state": "open",
                                           "body_total_chars": len("41/96 affected")},
            ("uwear/uwear-backend", 347): _flat_pr(),
        }
    )
    result = await gather_pieces(
        session, org_id=_ORG, job_ref=f"idea:{_IDEA_ID}",
        slack=slack, github=github, budget=DossierBudget(),
    )
    sources = sorted({p.source for p in result.pieces})
    assert sources == ["github_issue", "github_pr", "record", "slack_thread"]
    assert result.source_notes == []
    dossier = assemble_dossier(
        result.pieces, job_ref=f"idea:{_IDEA_ID}", budget=DossierBudget(),
        source_notes=result.source_notes,
    )
    packet = compose_packet(dossier, org_id=_ORG, ask="take a pass")
    assert "uwear/uwear-backend#346" in packet.human_brief


async def test_missing_job_yields_note_not_crash():
    result = await gather_pieces(
        _session(), org_id=_ORG, job_ref=f"idea:{_IDEA_ID}",
        slack=FakeSlack(), github=FakeGithub(), budget=DossierBudget(),
    )
    assert result.pieces == []
    assert result.source_notes == ["record: job not found"]


async def test_idea_org_scope_enforced():
    result = await gather_pieces(
        _session(_idea(org_id="org-OTHER"), _event()), org_id=_ORG,
        job_ref=f"idea:{_IDEA_ID}", slack=None, github=None, budget=DossierBudget(),
    )
    assert result.pieces == []
    assert result.source_notes == ["record: job not found"]


async def test_record_org_scope_enforced():
    foreign = SimpleNamespace(id=1238, org_id="org-OTHER", title="t",
                              data={}, updated_at=_T0)
    result = await gather_pieces(
        WriteForbiddenSession(records=[foreign]), org_id=_ORG,
        job_ref="domain_record:1238", slack=None, github=None, budget=DossierBudget(),
    )
    assert result.pieces == []
    assert result.source_notes == ["record: job not found"]


async def test_event_org_mismatch_yields_provenance_note():
    result = await gather_pieces(
        _session(_idea(), _event(org_id="org-OTHER")), org_id=_ORG,
        job_ref=f"idea:{_IDEA_ID}", slack=FakeSlack(), github=None, budget=DossierBudget(),
    )
    assert "slack: provenance unavailable" in result.source_notes


@pytest.mark.parametrize("channel_type", ["im", "mpim", "group", "", "weird_new_type"])
async def test_non_public_surfaces_fail_closed_before_any_read(channel_type):
    slack = FakeSlack(messages=_messages(3))
    result = await gather_pieces(
        _session(_idea(), _event(channel_type=channel_type)), org_id=_ORG,
        job_ref=f"idea:{_IDEA_ID}", slack=slack, github=None, budget=DossierBudget(),
    )
    assert slack.calls == []  # boundary enforced BEFORE the read
    assert "slack: non-public or unknown-visibility source omitted" in result.source_notes
    assert all(p.source != "slack_thread" for p in result.pieces)


async def test_malformed_provenance_yields_note():
    event = _event(payload_overrides={"channel_id": ""})
    result = await gather_pieces(
        _session(_idea(), event), org_id=_ORG, job_ref=f"idea:{_IDEA_ID}",
        slack=FakeSlack(), github=None, budget=DossierBudget(),
    )
    assert "slack: provenance malformed or missing" in result.source_notes


async def test_no_reader_configured_notes():
    result = await gather_pieces(
        _session(_idea(), _event()), org_id=_ORG, job_ref=f"idea:{_IDEA_ID}",
        slack=None, github=None, budget=DossierBudget(),
    )
    assert "slack: no reader configured" in result.source_notes
    assert "github: no reader configured" in result.source_notes  # refs were discovered


async def test_slack_failure_degrades_to_note():
    result = await gather_pieces(
        _session(_idea(), _event()), org_id=_ORG, job_ref=f"idea:{_IDEA_ID}",
        slack=FakeSlack(error=RuntimeError("boom")), github=None, budget=DossierBudget(),
    )
    assert any(note.startswith("slack: unavailable") for note in result.source_notes)
    assert any(p.source == "record" for p in result.pieces)  # gather still returns


async def test_partial_slack_fetch_reports_true_total():
    budget = DossierBudget(max_items_per_source=3)
    slack = FakeSlack(messages=_messages(10), total=40)
    result = await gather_pieces(
        _session(_idea(), _event()), org_id=_ORG, job_ref=f"idea:{_IDEA_ID}",
        slack=slack, github=None, budget=budget,
    )
    assert slack.calls[0]["limit"] == 10  # 2x items cap, floor 10
    fetched = sum(1 for p in result.pieces if p.source == "slack_thread")
    assert f"slack_thread: only {fetched} of 40 messages fetched" in result.source_notes


async def test_refs_from_event_hints_and_tracker_identity_and_text():
    idea = _idea(title="plain title", description="no refs in text")
    event = _event(hints={"provider": "github", "repo": "uwear/uwear-app", "number": 12})
    record = SimpleNamespace(
        id=1238, org_id=_ORG, title="tracker",
        data={"repo": "uwear/uwear-backend", "pr_number": "347"},
        updated_at=_T0,
    )
    seen = []

    class CountingGithub:
        async def read_ref(self, *, repo_slug, number):
            seen.append((repo_slug, number))
            return None

    # Record path: job_ref is the record; idea won't resolve. Hints ride the
    # idea's event, so check them via the idea path separately below.
    await gather_pieces(
        WriteForbiddenSession(records=[record]), org_id=_ORG,
        job_ref="domain_record:1238", slack=None, github=CountingGithub(),
        budget=DossierBudget(),
    )
    assert ("uwear/uwear-backend", 347) in seen  # tracker repo+pr_number identity

    seen.clear()
    await gather_pieces(
        _session(idea, event), org_id=_ORG, job_ref=f"idea:{_IDEA_ID}",
        slack=None, github=CountingGithub(), budget=DossierBudget(),
    )
    assert ("uwear/uwear-app", 12) in seen  # event hints identity


async def test_pr_url_fallback_for_tracker_identity():
    record = SimpleNamespace(
        id=1238, org_id=_ORG, title="tracker",
        data={"pr_url": "https://github.com/uwear/uwear-backend/pull/293"},
        updated_at=_T0,
    )
    seen = []

    class CountingGithub:
        async def read_ref(self, *, repo_slug, number):
            seen.append((repo_slug, number))
            return None

    await gather_pieces(
        WriteForbiddenSession(records=[record]), org_id=_ORG,
        job_ref="domain_record:1238", slack=None, github=CountingGithub(),
        budget=DossierBudget(),
    )
    assert ("uwear/uwear-backend", 293) in seen


async def test_ref_cap_is_visible_not_silent():
    idea = _idea(description="uwear/x#1 uwear/x#2 uwear/x#3 uwear/x#4 uwear/x#5 uwear/x#6")
    result = await gather_pieces(
        _session(idea, _event()), org_id=_ORG, job_ref=f"idea:{_IDEA_ID}",
        slack=None, github=FakeGithub(refs={}), budget=DossierBudget(),
    )
    cap_notes = [n for n in result.source_notes if "additional refs not gathered (cap)" in n]
    assert cap_notes  # dropped refs are announced, never silently capped


async def test_github_flat_payload_compaction_note_and_checks_evidence():
    checks = {"check_runs": [
        {"name": "ci", "status": "completed", "conclusion": "success"},
        {"name": "lint", "status": "completed", "conclusion": "failure"},
    ]}
    body = "compacted body..."
    github = FakeGithub(refs={
        ("uwear/uwear-backend", 346): _flat_pr(body=body, total=5000, checks=checks),
    })
    idea = _idea(title="see uwear/uwear-backend#346", description="")
    result = await gather_pieces(
        _session(idea, _event()), org_id=_ORG, job_ref=f"idea:{_IDEA_ID}",
        slack=None, github=github, budget=DossierBudget(),
    )
    assert any(
        n == f"github: uwear/uwear-backend#346 body pre-compacted upstream (+{5000 - len(body)} chars)"
        for n in result.source_notes
    )
    checks_pieces = [p for p in result.pieces if p.ref.endswith(":checks")]
    assert checks_pieces and checks_pieces[0].source == "evidence"
    assert "1 failure" in checks_pieces[0].body and "1 success" in checks_pieces[0].body


async def test_github_failure_and_miss_note_per_ref():
    result = await gather_pieces(
        _session(_idea(), _event()), org_id=_ORG, job_ref=f"idea:{_IDEA_ID}",
        slack=None, github=FakeGithub(error=RuntimeError("rate limited")),
        budget=DossierBudget(),
    )
    github_notes = [n for n in result.source_notes if "unavailable" in n and n.startswith("github:")]
    assert len(github_notes) == 2  # one per discovered ref (#346, #347)

    miss = await gather_pieces(
        _session(_idea(), _event()), org_id=_ORG, job_ref=f"idea:{_IDEA_ID}",
        slack=None, github=FakeGithub(refs={}), budget=DossierBudget(),
    )
    assert any(n.endswith("not found") for n in miss.source_notes)


async def test_related_tracker_records_are_gathered_and_self_excluded():
    record = SimpleNamespace(
        id=1238, org_id=_ORG, title="app ticket",
        data={"repo": "uwear/uwear-backend", "pr_number": "347"}, updated_at=_T0,
    )
    related = SimpleNamespace(
        id=1300, org_id=_ORG, title="backend ticket",
        data={"repo": "uwear/uwear-backend", "pr_number": "347"},
        updated_at=_T0,
    )
    foreign = SimpleNamespace(
        id=1301, org_id="org-OTHER", title="foreign",
        data={"repo": "uwear/uwear-backend", "pr_number": "347"}, updated_at=_T0,
    )
    result = await gather_pieces(
        WriteForbiddenSession(records=[record, related, foreign]), org_id=_ORG,
        job_ref="domain_record:1238", slack=None, github=FakeGithub(refs={}),
        budget=DossierBudget(),
    )
    refs = [p.ref for p in result.pieces if p.source == "record"]
    assert "domain_record:1300" in refs  # related found, org-scoped
    assert "domain_record:1301" not in refs  # foreign org never leaks
    assert refs.count("domain_record:1238") == 1  # self not duplicated


@pytest.mark.parametrize(
    ("derived", "expected"),
    [
        (DeployState.DEPLOYED, "state: deployed"),
        (None, "state: unknown"),
    ],
)
async def test_deploy_piece_uses_batch_deriver_and_verified_overlay(
    derived,
    expected,
):
    fix_ref = ("uwear-ai/uwear-backend", "a" * 40)
    record = SimpleNamespace(
        id=2474,
        org_id=_ORG,
        title="Production alert",
        data={
            "fix_pr": "uwear-ai/uwear-backend#1264",
            "fix_merge_sha": fix_ref[1],
            "verified": True,
            "verified_at": "2026-07-27T12:00:00+00:00",
        },
        updated_at=_T0,
    )
    github = FakeGithub(deploy_states={fix_ref: derived})

    result = await gather_pieces(
        WriteForbiddenSession(records=[record]),
        org_id=_ORG,
        job_ref="domain_record:2474",
        slack=None,
        github=github,
        budget=DossierBudget(),
    )

    deploy = next(piece for piece in result.pieces if piece.source == "deploy_state")
    assert expected in deploy.body
    assert "verified: yes" in deploy.body
    assert "verified_at: 2026-07-27T12:00:00+00:00" in deploy.body
    assert github.deploy_calls == [{2474: fix_ref}]


async def test_record_prose_hides_conflicting_stored_deploy_state():
    fix_ref = ("uwear-ai/uwear-backend", "a" * 40)
    record = SimpleNamespace(
        id=2474,
        org_id=_ORG,
        title="Conflicting legacy state",
        data={
            "summary": "Production alert",
            "fix_pr": "uwear-ai/uwear-backend#1264",
            "fix_merge_sha": fix_ref[1],
            "deploy_state": "staging",
            "deployed_at": "2026-07-01T12:00:00+00:00",
            "verified": False,
        },
        updated_at=_T0,
    )

    result = await gather_pieces(
        WriteForbiddenSession(records=[record]),
        org_id=_ORG,
        job_ref="domain_record:2474",
        slack=None,
        github=FakeGithub(
            deploy_states={fix_ref: DeployState.DEPLOYED}
        ),
        budget=DossierBudget(),
    )

    rendered = "\n".join(piece.body for piece in result.pieces)
    assert "state: deployed" in rendered
    assert rendered.count("state: deployed") == 1
    assert "staging" not in rendered
    assert "deploy_state:" not in rendered
    record_piece = next(
        piece for piece in result.pieces
        if piece.source == "record"
    )
    assert record_piece.body == "summary: Production alert"


async def test_deploy_degradation_note_summarizes_per_ref_compare_failures():
    fix_ref = ("uwear-ai/uwear-backend", "a" * 40)
    failure = DeployStateObservation(
        state=None,
        in_staging=None,
        in_main=None,
        comparisons=(
            AncestryObservation(
                branch="staging",
                is_ancestor=None,
                error_category="github_http_503",
                status_code=503,
            ),
            AncestryObservation(
                branch="main",
                is_ancestor=None,
                error_category="github_http_503",
                status_code=503,
            ),
        ),
    )
    batch = DeployStateBatch(
        {2474: None},
        observations_by_key={2474: failure},
        observations_by_ref={fix_ref: failure},
    )
    record = SimpleNamespace(
        id=2474,
        org_id=_ORG,
        title="Unavailable ancestry",
        data={
            "fix_pr": "uwear-ai/uwear-backend#1264",
            "fix_merge_sha": fix_ref[1],
        },
        updated_at=_T0,
    )

    result = await gather_pieces(
        WriteForbiddenSession(records=[record]),
        org_id=_ORG,
        job_ref="domain_record:2474",
        slack=None,
        github=FakeGithub(deploy_result=batch),
        budget=DossierBudget(),
    )

    assert (
        "deploy: ancestry unavailable for 1/1 fixes "
        "(github_http_503×2)"
    ) in result.source_notes
    deploy = next(
        piece for piece in result.pieces
        if piece.source == "deploy_state"
    )
    assert "state: unknown" in deploy.body


@pytest.mark.parametrize(
    ("chantier_times", "member_times", "refs", "expected"),
    [
        (
            {"created_at": "2026-07-17T19:10:01Z"},
            {},
            (),
            datetime(2026, 7, 17, 19, 10, 1, tzinfo=timezone.utc),
        ),
        (
            {"updated_at": "2026-07-17T19:10:01Z"},
            {"issue:438": {"updated_at": datetime(2026, 7, 22, 10, 30)}},
            ("issue:438",),
            datetime(2026, 7, 22, 10, 30, tzinfo=timezone.utc),
        ),
        (
            {"updated_at": "2026-07-22T11:00:00+00:00"},
            {"issue:438": {"created_at": "2026-07-20T08:00:00Z"}},
            ("issue:438",),
            datetime(2026, 7, 22, 11, 0, tzinfo=timezone.utc),
        ),
        ({}, {}, (), None),
        (
            {"updated_at": "2026-07-17T19:10:01Z"},
            {"issue:loaded": {"updated_at": "2026-07-21T14:00:00Z"}},
            ("issue:loaded", "issue:not-loaded-after-cap"),
            datetime(2026, 7, 21, 14, 0, tzinfo=timezone.utc),
        ),
    ],
    ids=("chantier-only", "member-newer", "chantier-newer", "none-available", "capped-coverage"),
)
def test_latest_source_movement_selects_from_loaded_source_data(
    chantier_times,
    member_times,
    refs,
    expected,
):
    chantier = _movement_chantier(
        data_updated_at=chantier_times.get("updated_at"),
        created_at=chantier_times.get("created_at"),
        row_updated_at=datetime(2026, 7, 23, tzinfo=timezone.utc),
        refs=[{"source": "github", "ref": ref} for ref in refs],
    )
    members = {
        ref: SimpleNamespace(data={"external_id": ref, **times})
        for ref, times in member_times.items()
    }

    assert latest_source_movement(
        chantier,
        members_by_external_id=members,
    ) == expected


def test_bookkeeping_only_row_write_does_not_change_reported_source_movement():
    source_time = "2026-07-17T19:10:01Z"
    before_refresh = _movement_chantier(
        data_updated_at=source_time,
        row_updated_at=datetime(2026, 7, 22, tzinfo=timezone.utc),
    )
    after_refresh = _movement_chantier(
        data_updated_at=source_time,
        row_updated_at=datetime(2026, 7, 23, tzinfo=timezone.utc),
    )

    before = latest_source_movement(before_refresh, members_by_external_id={})
    after = latest_source_movement(after_refresh, members_by_external_id={})

    assert before == after == datetime(2026, 7, 17, 19, 10, 1, tzinfo=timezone.utc)


async def test_item_in_chantier_gathers_goal_sibling_states_and_artifact_refs():
    subject_external_id = "github:Illospace/illospace:issue:330"
    capped_refs = [
        {
            "source": "github",
            "ref": f"github:Illospace/illospace:issue:{5000 + index}",
            "title": f"Capped member {index}",
        }
        for index in range(99)
    ]
    capped_members = [
        SimpleNamespace(
            id=5000 + index,
            org_id=_ORG,
            domain_id=1,
            object_key="ticket",
            title=f"Capped member {index}",
            data={"external_id": item["ref"], "status": "Todo"},
            updated_at=_T0,
        )
        for index, item in enumerate(capped_refs)
    ]
    subject = SimpleNamespace(
        id=1238,
        org_id=_ORG,
        domain_id=1,
        object_key="ticket",
        title="Handoff dossiers inherit chantier context",
        data={
            "external_id": subject_external_id,
            "status": "In Progress",
            "updated_at": "2026-07-18T12:00:00Z",
        },
        updated_at=_T0,
    )
    sibling_a = SimpleNamespace(
        id=1237,
        org_id=_ORG,
        domain_id=1,
        object_key="ticket",
        title="Chantier record contract",
        data={
            "external_id": "github:Illospace/illospace:issue:327",
            "status": "Done",
            "created_at": "2026-07-19T08:00:00Z",
        },
        updated_at=_T0,
    )
    sibling_b = SimpleNamespace(
        id=1239,
        org_id=_ORG,
        domain_id=1,
        object_key="ticket",
        title="Chantier-aware check-ins",
        data={
            "external_id": "github:Illospace/illospace:issue:331",
            "status": "Todo",
            # Deliberately naive: source movement comparison must normalize it.
            "updated_at": datetime(2026, 7, 20, 14, 30),
        },
        updated_at=_T0,
    )
    chantier = SimpleNamespace(
        id=1400,
        org_id=_ORG,
        domain_id=1,
        object_key="chantier",
        title="Agent runtime chantier layer",
        data={
            "slug": "agent-runtime-chantier-layer",
            "title": "Agent runtime chantier layer",
            "goal": "Done means no work arrives cold at an item boundary.",
            "kind": "feature",
            "state": "building",
            "owner": "Reda",
            "updated_at": "2026-07-17T19:10:01Z",
            "refs": [
                {"source": "github", "ref": subject_external_id, "title": "Handoff dossier"},
                {
                    "source": "github",
                    "ref": "github:Illospace/illospace:issue:327",
                    "title": "Chantier record contract",
                },
                {
                    "source": "github",
                    "ref": "github:Illospace/illospace:issue:331",
                    "title": "Chantier-aware check-ins",
                },
                *capped_refs,
                {"source": "doc", "ref": "specs/chantier.md", "title": "PRD"},
                {"source": "url", "ref": "https://figma.example/chantier", "title": "Mockups"},
            ],
            "next_step": "Ship chantier context in handoff packets.",
        },
        updated_at=_T0,
    )
    session = WriteForbiddenSession(
        records=[subject, chantier, sibling_a, sibling_b, *capped_members]
    )
    github = FakeGithub()

    result = await gather_pieces(
        session,
        org_id=_ORG,
        job_ref="domain_record:1238",
        slack=None,
        github=github,
        budget=DossierBudget(),
    )

    chantier_pieces = [piece for piece in result.pieces if piece.source == "chantier"]
    assert len(chantier_pieces) == 1
    piece = chantier_pieces[0]
    body = piece.body
    assert piece.ts == datetime(2026, 7, 20, 14, 30, tzinfo=timezone.utc)
    assert "last source movement: 2026-07-20" in body
    assert (
        "source movement observation partial: "
        "1 additional member records not gathered (cap)"
    ) in body
    assert "1 additional member states not gathered (cap)" in body
    assert "goal: Done means no work arrives cold at an item boundary." in body
    assert "state: building" in body
    assert "kind: feature" in body
    assert "owner: Reda" in body
    assert "next_step: Ship chantier context in handoff packets." in body
    assert "Chantier record contract (github:Illospace/illospace:issue:327, state: Done)" in body
    assert "Chantier-aware check-ins (github:Illospace/illospace:issue:331, state: Todo)" in body
    assert "PRD (doc: specs/chantier.md)" in body
    assert "Mockups (url: https://figma.example/chantier)" in body
    assert github.calls == []  # sibling state is DB context, never a GitHub fan-out
    assert result.source_notes == []
    dossier = assemble_dossier(
        result.pieces,
        job_ref="domain_record:1238",
        budget=DossierBudget(),
        source_notes=result.source_notes,
    )
    chantier_section = next(section for section in dossier.sections if section.source == "chantier")
    assert chantier_section.items[0].ref == "domain_record:1400"


async def test_superseded_chantier_is_excluded_from_context_digest_sweep():
    subject_external_id = "github:Illospace/illospace:issue:386"
    subject = SimpleNamespace(
        id=1386,
        org_id=_ORG,
        domain_id=1,
        object_key="ticket",
        title="Deduplicate chantier declarations",
        data={"external_id": subject_external_id, "status": "In Progress"},
        updated_at=_T0,
    )
    superseded = SimpleNamespace(
        id=2096,
        org_id=_ORG,
        domain_id=1,
        object_key="chantier",
        title="Duplicate placeholder",
        data={
            "slug": "duplicate-placeholder",
            "title": "Duplicate placeholder",
            "goal": "Done means duplicate placeholder reaches its stated outcome.",
            "kind": "feature",
            # The superseded marker wins even if malformed legacy state was not paused.
            "state": "exploring",
            "superseded_by": "agent-mcp-repositioning",
            "refs": [{"source": "github", "ref": subject_external_id}],
            "next_step": "Clarify the next most valuable step.",
        },
        updated_at=_T0,
    )

    result = await gather_pieces(
        WriteForbiddenSession(records=[subject, superseded]),
        org_id=_ORG,
        job_ref="domain_record:1386",
        slack=None,
        github=FakeGithub(),
        budget=DossierBudget(),
    )

    assert [piece for piece in result.pieces if piece.source == "chantier"] == []


async def test_item_not_in_chantier_keeps_previous_bytes():
    subject = SimpleNamespace(
        id=1238,
        org_id=_ORG,
        domain_id=1,
        object_key="ticket",
        title="Ship handoff context",
        data={
            "external_id": "github:Illospace/illospace:issue:330",
            "status": "ready-for-agent",
        },
        updated_at=datetime(2026, 7, 16, tzinfo=timezone.utc),
    )
    result = await gather_pieces(
        WriteForbiddenSession(records=[subject]),
        org_id=_ORG,
        job_ref="domain_record:1238",
        slack=None,
        github=FakeGithub(),
        budget=DossierBudget(),
    )
    dossier = assemble_dossier(
        result.pieces,
        job_ref="domain_record:1238",
        budget=DossierBudget(),
        source_notes=result.source_notes,
    )
    packet = compose_packet(dossier, org_id=_ORG, ask="Implement the ticket")

    assert dossier.render_text() == (
        "# Ship handoff context\n\n"
        "job: domain_record:1238\n\n"
        "## record (1 item)\n"
        "- [domain_record:1238] Ship handoff context: "
        "external_id: github:Illospace/illospace:issue:330; status: ready-for-agent"
    )
    assert packet.human_brief == (
        "*Ship handoff context* → unclaimed\n"
        "*What happened:* external_id: github:Illospace/illospace:issue:330; "
        "status: ready-for-agent\n"
        "*Evidence:* none gathered\n"
        "*Prior decisions:* none on record\n"
        "*Ask:* Implement the ticket\n"
        "Launch: {launch_url}"
    )


async def test_evidence_piece_from_idea_attribution():
    idea = _idea(agent_details={
        "inbound_triage": {"event_id": _EVENT_ID},
        "attribution": {"record_id": 1238, "summary": "created ticket"},
    })
    result = await gather_pieces(
        _session(idea, _event()), org_id=_ORG, job_ref=f"idea:{_IDEA_ID}",
        slack=None, github=None, budget=DossierBudget(),
    )
    assert any(p.source == "evidence" and "record_id: 1238" in p.body for p in result.pieces)


async def test_source_notes_rotate_revision_and_flow_to_both_audiences():
    base = assemble_dossier([], job_ref="idea:x", budget=DossierBudget())
    degraded = assemble_dossier(
        [], job_ref="idea:x", budget=DossierBudget(),
        source_notes=["slack: private source omitted", "github: unavailable — Boom"],
    )
    assert degraded.total_chars > base.total_chars  # notes render in total
    packet_a = compose_packet(base, org_id=_ORG, ask="take a pass")
    packet_b = compose_packet(degraded, org_id=_ORG, ask="take a pass")
    assert packet_a.idempotency_key != packet_b.idempotency_key  # notes are hashed
    assert "2 sources degraded" in packet_b.human_brief
    omissions_part = [p for p in packet_b.handoff_input.context_parts if p["source"] == "omissions"]
    assert "slack: private source omitted" in omissions_part[0]["notes"]


async def test_default_slack_reader_walks_cursor_pages_with_true_totals():
    class FakeClient:
        def __init__(self):
            self.pages = [
                {"messages": [{"ts": "1.0", "user": "u", "text": "root", "reply_count": 5}],
                 "response_metadata": {"next_cursor": "c2"}},
                {"messages": [{"ts": "2.0", "user": "u", "text": "mid"}],
                 "response_metadata": {"next_cursor": "c3"}},
                {"messages": [{"ts": "3.0", "user": "u", "text": "tail"}],
                 "response_metadata": {"next_cursor": ""}},
            ]
            self.calls = []

        async def conversation_replies(self, *, channel, thread_ts, limit, cursor=None):
            self.calls.append(cursor)
            return self.pages[len(self.calls) - 1]

    client = FakeClient()
    reader = DefaultSlackReader(client)
    thread = await reader.read_thread(channel="C1", thread_ts="1.0", limit=10)
    assert client.calls == [None, "c2", "c3"]
    assert [m["text"] for m in thread.messages] == ["root", "mid", "tail"]
    assert thread.total == 6  # parent's reply_count + 1, not just fetched


async def test_backend_github_read_flattens_pr_and_falls_back_to_exact_issue(monkeypatch):
    from brain.systems.cortex.project_context.github import GitHubConnectorError
    from brain.systems.runs.tool_catalog.handlers import github as handler

    async def fake_candidates(**kwargs):
        assert kwargs["org_id"] == _ORG  # explicit backend context, not run context
        return [{"key_name": "k", "token": "tok-1", "source": "test"}]

    monkeypatch.setattr(handler, "_github_token_candidates", fake_candidates)

    async def fake_pr(slug, number, *, token=None):
        return {"repo": slug, "pull_request": {"title": "PR title", "body": "pr body", "state": "open"},
                "checks": {"check_runs": []}, "body_total_chars": 7}

    import brain.systems.cortex.project_context.github as connector
    monkeypatch.setattr(connector, "async_get_pull_request", fake_pr)
    flat = await handler.github_read_ref_for_backend(
        repo_slug="uwear/x", number=1, org_id=_ORG
    )
    assert flat == {"kind": "github_pr", "title": "PR title", "body": "pr body",
                    "state": "open", "body_total_chars": 7, "checks": {"check_runs": []}}

    async def pr_404(slug, number, *, token=None):
        raise GitHubConnectorError(status_code=404, message="no such PR")

    async def fake_issue(slug, number, *, token=None):
        return {"repo": slug, "issue": {"title": "Issue", "body": "ib", "state": "open",
                                        "body_total_chars": 2}}

    monkeypatch.setattr(connector, "async_get_pull_request", pr_404)
    monkeypatch.setattr(connector, "async_get_issue", fake_issue)
    flat = await handler.github_read_ref_for_backend(
        repo_slug="uwear/x", number=2, org_id=_ORG
    )
    assert flat["kind"] == "github_issue" and flat["title"] == "Issue"

    async def issue_404(slug, number, *, token=None):
        raise GitHubConnectorError(status_code=404, message="no such issue")

    monkeypatch.setattr(connector, "async_get_issue", issue_404)
    assert await handler.github_read_ref_for_backend(
        repo_slug="uwear/x", number=3, org_id=_ORG
    ) is None


def _github_event(*, org_id=_ORG, summary="GitHub issue #81 opened: SEO landing pages"):
    """Non-Slack origin: kind is github.*, payload has no channel provenance."""
    return SimpleNamespace(
        id=_EVENT_ID,
        org_id=org_id,
        created_at=_T0,
        envelope={
            "kind": "github.subject",
            "summary": summary,
            "payload": {"action": "opened"},
            "hints": {"provider": "github", "repo": "uwear-ai/uwear-website",
                      "number": 81, "url": "https://github.com/uwear-ai/uwear-website/issues/81"},
        },
        normalized_payload={},
        raw_payload={},
    )


async def test_github_origin_events_get_no_bogus_slack_note():
    """Probe finding (2026-07-13): a GitHub webhook has no Slack thread —
    its absence is not a degradation."""
    result = await gather_pieces(
        _session(_idea(), _github_event()), org_id=_ORG, job_ref=f"idea:{_IDEA_ID}",
        slack=FakeSlack(), github=None, budget=DossierBudget(),
    )
    assert not any(note.startswith("slack:") for note in result.source_notes)


async def test_github_origin_event_summary_becomes_the_leading_piece():
    """Probe finding: the envelope summary reads far better than the generic
    triage idea description — it should lead the record section."""
    idea = _idea(title="Inbound signal needs Illo triage: github:uwear-ai/uwear-website",
                 description="generic")
    result = await gather_pieces(
        _session(idea, _github_event()), org_id=_ORG, job_ref=f"idea:{_IDEA_ID}",
        slack=None, github=None, budget=DossierBudget(),
    )
    from brain.systems.briefing import assemble_dossier as _assemble

    dossier = _assemble(result.pieces, job_ref=f"idea:{_IDEA_ID}", budget=DossierBudget(),
                        source_notes=result.source_notes)
    assert dossier.headline.startswith("GitHub issue #81 opened")
    record_section = next(s for s in dossier.sections if s.source == "record")
    assert "url: https://github.com/uwear-ai/uwear-website/issues/81" in record_section.items[0].excerpt
