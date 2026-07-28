"""Illo Brain — Dossier gathering (read-only source adapters).

The single owner of "collect the raw pieces for a job" (spec:
illo-handoff-packets slice 03). Triage minting, notify refresh, and the
on-demand "brief me" flow all gather through :func:`gather_pieces`; no
caller grows its own context collection.

Design stances, each load-bearing (and each cross-family-review hardened):

- **Read-only.** No ``session.add`` / ``flush`` / ``commit`` anywhere, and
  DB reads run under ``no_autoflush`` so a caller's pending ORM state can
  never be flushed by the gather path. One sanctioned exception lives in
  the auth owner: GitHub token-candidate resolution may write vault
  access-audit rows (see ``github_read_ref_for_backend``).
- **Delegation, not duplication.** Adapters are tiny Protocols whose
  default impls delegate to the EXISTING owners of each read path — the
  Slack web client, the handler-owned backend GitHub read (which keeps the
  ordered token-candidate behavior), the domain-record tables. No new HTTP
  clients, no new token paths.
- **Honest degradation.** A source that is down, non-public, partially
  fetched, capped, or upstream-compacted yields an explicit
  ``source_notes`` entry (rendered in both packet audiences) — never a
  silent absence and never a crashed gather. Adapters report TRUE totals.
- **Privacy boundary, fail-closed.** Only Slack surfaces whose envelope
  ``channel_type`` is exactly ``"channel"`` (public) are excerpted;
  private channels, DMs, group DMs, AND empty/unknown types degrade to a
  note BEFORE any Slack call. The referenced inbound event must belong to
  the requesting org.
- **Same-job references only.** GitHub refs come from the inbound event's
  own hints, the tracker record's canonical ``repo``/``pr_number`` (or
  ``pr_url``) identity, and explicit ``owner/repo#N`` text in the job's
  title/description — no fuzzy search.
- **Goal context without fan-out.** A tracker item's stable ``external_id``
  is matched against Domain-1 chantier ``refs``. Sibling states come from
  one bounded tracker-record query; chantier membership never causes a
  per-sibling GitHub read.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from sqlalchemy import select

from brain.kernel.common.coercion import coerce_datetime
from brain.systems.briefing.core import (
    DEPLOY_EVIDENCE_SOURCE,
    DossierBudget,
    SourcePiece,
)
from brain.systems.chantiers import latest_source_movement
from brain.systems.deploy_record_contract import (
    DEPLOY_FIELDS_HIDDEN_FROM_RECORD_PROSE,
)
from brain.systems.deploy_state import (
    DeployState,
    DeployStateBatch,
    render_deploy_state,
)

# Conservative same-job reference pattern: explicit owner/repo#N only.
_GITHUB_REF_RE = re.compile(r"\b([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(\d+)\b")
_MAX_GITHUB_REFS = 4
_MAX_CHANTIER_MEMBER_RECORDS = 100
_SLACK_PAGE_CAP = 50
_SLACK_MAX_PAGES = 3
# The ONLY team-visible surface (ingress envelope vocabulary). Everything
# else — im, mpim, group, empty, unknown — is fail-closed.
PUBLIC_CHANNEL_TYPE = "channel"
_PUBLIC_CHANNEL_TYPE = PUBLIC_CHANNEL_TYPE  # back-compat alias

JOB_REF_IDEA_PREFIX = "idea:"
JOB_REF_RECORD_PREFIX = "domain_record:"
_TRACKER_DOMAIN_SLUG = "github-ticket-tracker"
_CHANTIER_OBJECT_KEY = "chantier"
_TICKET_OBJECT_KEY = "ticket"


@dataclass(frozen=True)
class SlackThreadRead:
    """One thread read: fetched messages + the TRUE total the API reported."""

    messages: tuple[dict[str, Any], ...]  # each: {ts, user, text}
    total: int
    channel: str


@dataclass(frozen=True)
class GatherResult:
    pieces: list[SourcePiece] = field(default_factory=list)
    source_notes: list[str] = field(default_factory=list)


class SlackReader(Protocol):
    async def read_thread(self, *, channel: str, thread_ts: str, limit: int) -> SlackThreadRead: ...


class GithubReader(Protocol):
    async def read_ref(self, *, repo_slug: str, number: int) -> dict[str, Any] | None:
        """Return the FLAT contract {kind, title, body, state,
        body_total_chars, checks?} or None when the ref does not exist."""
        ...

    async def derive_deploy_states(
        self,
        refs: dict[int, tuple[str, str]],
    ) -> Mapping[int, DeployState | None]: ...


def _ts_from_slack(ts: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc)
    except (TypeError, ValueError):
        return None


def _text(value: Any) -> str:
    return str(value or "").strip()


def _data(record: Any) -> dict[str, Any]:
    return dict(getattr(record, "data", None) or {})


def _no_autoflush(session: Any):
    sync_session = getattr(session, "sync_session", session)
    return getattr(sync_session, "no_autoflush", None) or nullcontext()


class DefaultSlackReader:
    """Delegates to the existing Slack web client (read path only).

    Bounded pagination: walks up to ``_SLACK_MAX_PAGES`` cursor pages so the
    tail of a long thread (where decisions live) is reachable; anything
    beyond stays visible through the caller's only-N-of-M note.
    """

    def __init__(self, client: Any | None = None) -> None:
        self._client = client

    async def _resolve_client(self) -> Any:
        if self._client is None:
            from brain.systems.slack.client import slack_web_client_from_runtime

            self._client = await slack_web_client_from_runtime(
                requested_by="packet_gather",
                reason="Read the origin Slack thread for a handoff dossier.",
            )
        return self._client

    async def read_thread(self, *, channel: str, thread_ts: str, limit: int) -> SlackThreadRead:
        client = await self._resolve_client()
        collected: list[dict[str, Any]] = []
        total = 0
        cursor: str | None = None
        for _page in range(_SLACK_MAX_PAGES):
            payload = await client.conversation_replies(
                channel=channel, thread_ts=thread_ts, limit=limit, cursor=cursor
            )
            raw = payload.get("messages") or []
            collected.extend(raw)
            if not total and raw:
                # Slack returns the parent first; reply_count excludes it.
                try:
                    total = int(raw[0].get("reply_count") or 0) + 1
                except (TypeError, ValueError):
                    total = 0
            cursor = _text(((payload.get("response_metadata") or {}).get("next_cursor")))
            if not cursor:
                break
        messages = tuple(
            {"ts": _text(m.get("ts")), "user": _text(m.get("user")), "text": _text(m.get("text"))}
            for m in collected
            if _text(m.get("text"))
        )
        return SlackThreadRead(
            messages=messages, total=max(total, len(collected)), channel=channel
        )


class DefaultGithubReader:
    """Delegates to the handler-owned backend read (the existing owner of
    GitHub auth candidates and retry order)."""

    def __init__(self, *, org_id: str, user_id: str | None = None) -> None:
        self._org_id = str(org_id)
        self._user_id = user_id

    async def read_ref(self, *, repo_slug: str, number: int) -> dict[str, Any] | None:
        from brain.systems.runs.tool_catalog.handlers.github import github_read_ref_for_backend

        return await github_read_ref_for_backend(
            repo_slug=repo_slug, number=number, org_id=self._org_id, user_id=self._user_id
        )

    async def derive_deploy_states(
        self,
        refs: dict[int, tuple[str, str]],
    ) -> DeployStateBatch[int]:
        from brain.systems.runs.tool_catalog.handlers.github import (
            github_deploy_states_for_backend,
        )

        return await github_deploy_states_for_backend(
            refs,
            org_id=self._org_id,
            user_id=self._user_id,
        )


async def _load_job(session: Any, *, org_id: str, job_ref: str) -> tuple[Any | None, Any | None]:
    """Resolve a job_ref to (idea, record) — both queries org-scoped."""
    from brain.platform.db.models.domain import DomainRecord
    from brain.platform.db.models.idea import Idea

    idea = None
    record = None
    with _no_autoflush(session):
        if job_ref.startswith(JOB_REF_IDEA_PREFIX):
            idea = (
                await session.execute(
                    select(Idea).where(
                        Idea.id == job_ref[len(JOB_REF_IDEA_PREFIX):],
                        Idea.org_id == str(org_id),
                    )
                )
            ).scalar_one_or_none()
        elif job_ref.startswith(JOB_REF_RECORD_PREFIX):
            try:
                record_id = int(job_ref[len(JOB_REF_RECORD_PREFIX):])
            except ValueError:
                return None, None
            record = (
                await session.execute(
                    select(DomainRecord).where(
                        DomainRecord.id == record_id,
                        DomainRecord.org_id == str(org_id),
                    )
                )
            ).scalar_one_or_none()
    return idea, record


async def load_inbound_event(session: Any, *, org_id: str, idea: Any) -> Any | None:
    """idea → its inbound event, org-verified. Public: mint (slice 05) and
    the notify refresh (slice 06) reuse this — one owner for provenance."""
    details = dict(getattr(idea, "agent_details", None) or {})
    event_id = str((details.get("inbound_triage") or {}).get("event_id") or "")
    if not event_id:
        return None
    from brain.platform.db.models.inbound import InboundEventRow

    with _no_autoflush(session):
        event = await session.get(InboundEventRow, event_id)
    if event is None or str(getattr(event, "org_id", "") or "") != str(org_id):
        return None
    return event


def slack_provenance(event: Any) -> dict[str, Any] | None:
    """The AUTHORITATIVE origin-thread coordinates: ``envelope["payload"]``
    only (that is what ingress writes — ``channel_id``, ``thread_ts`` /
    ``message_ts``, ``channel_type``). No top-level fallbacks: a shape we
    don't recognize is a note, never a guess."""
    envelope = dict(getattr(event, "envelope", None) or {})
    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        return None
    channel = _text(payload.get("channel_id"))
    thread_ts = _text(payload.get("thread_ts") or payload.get("message_ts"))
    if not channel or not thread_ts:
        return None
    return {
        "channel": channel,
        "thread_ts": thread_ts,
        "channel_type": _text(payload.get("channel_type")).lower(),
        # Illo's own Slack identity: gather MUST filter it out of thread
        # reads, or mint's posted brief feeds the next gather, rotates the
        # revision, and the noise gate collapses into a self-echo repost
        # loop (cross-family review finding, 2026-07-13).
        "bot_user_id": _text(payload.get("bot_user_id")),
    }


def _record_piece(record: Any) -> SourcePiece | None:
    data = _data(record)
    if not data:
        return None
    title = (
        _text(getattr(record, "title", ""))
        or _text(data.get("title") or data.get("name"))
        or f"record {getattr(record, 'id', '?')}"
    )
    skip = {
        "title",
        "name",
        *DEPLOY_FIELDS_HIDDEN_FROM_RECORD_PROSE,
    }
    body = "; ".join(
        f"{key}: {value}" for key, value in sorted(data.items())
        if key not in skip and isinstance(value, (str, int, float, bool)) and _text(value)
    )
    return SourcePiece(
        source="record",
        ref=f"{JOB_REF_RECORD_PREFIX}{getattr(record, 'id', '')}",
        title=title,
        body=body or title,
        ts=getattr(record, "updated_at", None),
        weight=10,
    )


def _chantier_refs(record: Any) -> list[dict[str, str]]:
    """Return the typed-ref contract, defensively normalized for rendering."""
    refs = _data(record).get("refs") or []
    if not isinstance(refs, list):
        return []
    normalized: list[dict[str, str]] = []
    for item in refs:
        if not isinstance(item, dict):
            continue
        source = _text(item.get("source")).lower()
        ref = _text(item.get("ref"))
        if not source or not ref:
            continue
        normalized.append({"source": source, "ref": ref, "title": _text(item.get("title"))})
    return normalized


def _tracker_state(record: Any) -> str:
    data = _data(record)
    return _text(data.get("status") or data.get("state")) or "unavailable"


def _chantier_piece(
    chantier: Any,
    *,
    subject_external_id: str,
    members_by_external_id: dict[str, Any],
    member_lookup_capped: int = 0,
) -> SourcePiece:
    """Render one chantier's goal, state, siblings, and artifact refs.

    The body stays marker-free here. Dossier assembly owns every excerpt and
    section cut, so oversized goal context receives the same cumulative
    ``omitted_chars`` markers and floors as every other source.
    """
    data = _data(chantier)
    movement_at = latest_source_movement(
        chantier,
        members_by_external_id=members_by_external_id,
    )
    movement_observation = (
        f"last source movement: {movement_at.date().isoformat()}"
        if movement_at is not None
        else "last source movement: unknown"
    )
    if movement_at is None:
        row_written_at = coerce_datetime(
            getattr(chantier, "updated_at", None),
            utc=True,
        ) or coerce_datetime(getattr(chantier, "created_at", None), utc=True)
        if row_written_at is not None:
            movement_observation += (
                f"; tracker row last written {row_written_at.date().isoformat()}"
            )
    if member_lookup_capped:
        movement_observation += (
            f"; source movement observation partial: {member_lookup_capped} "
            "additional member records not gathered (cap)"
        )
    sibling_bits: list[str] = []
    artifact_bits: list[str] = []
    for item in _chantier_refs(chantier):
        source = item["source"]
        ref = item["ref"]
        title = item["title"]
        if source == "github":
            if ref == subject_external_id:
                continue
            member = members_by_external_id.get(ref)
            label = title or _text(getattr(member, "title", "")) or ref
            sibling_bits.append(f"{label} ({ref}, state: {_tracker_state(member)})")
            continue
        label = title or source
        artifact_bits.append(f"{label} ({source}: {ref})")

    if member_lookup_capped:
        sibling_bits.append(f"{member_lookup_capped} additional member states not gathered (cap)")

    body_bits = [
        movement_observation,
        f"goal: {_text(data.get('goal')) or 'not recorded'}",
        f"state: {_text(data.get('state')) or 'not recorded'}",
        f"kind: {_text(data.get('kind')) or 'not recorded'}",
        f"owner: {_text(data.get('owner')) or 'unclaimed'}",
        f"next_step: {_text(data.get('next_step')) or 'not recorded'}",
        f"siblings: {', '.join(sibling_bits) if sibling_bits else 'none'}",
        f"artifacts: {', '.join(artifact_bits) if artifact_bits else 'none'}",
    ]
    slug = _text(data.get("slug"))
    title = (
        _text(getattr(chantier, "title", ""))
        or _text(data.get("title"))
        or slug
        or f"chantier {getattr(chantier, 'id', '?')}"
    )
    return SourcePiece(
        source="chantier",
        ref=f"{JOB_REF_RECORD_PREFIX}{getattr(chantier, 'id', '')}",
        title=title,
        body="; ".join(body_bits),
        ts=movement_at,
        weight=10,
    )


async def _chantier_pieces_for_record(
    session: Any, *, org_id: str, record: Any
) -> list[SourcePiece]:
    """Find the subject's chantier(s) and batch-load their tracker members.

    Membership is the v1 contract's exact typed ref match. The second query
    is one batch DB read for all sibling external ids across all matching
    chantiers — deliberately no GitHub fan-out inside packet minting.
    """
    subject_data = _data(record)
    subject_external_id = _text(subject_data.get("external_id"))
    if not subject_external_id:
        return []

    from brain.platform.db.models.domain import Domain, DomainObjectType, DomainRecord
    from brain.systems.chantiers import is_superseded_chantier

    with _no_autoflush(session):
        chantiers = (
            await session.execute(
                select(DomainRecord)
                .join(DomainObjectType, DomainObjectType.id == DomainRecord.object_type_id)
                .join(Domain, Domain.id == DomainRecord.domain_id)
                .where(
                    DomainRecord.org_id == str(org_id),
                    Domain.slug == _TRACKER_DOMAIN_SLUG,
                    Domain.archived_at.is_(None),
                    DomainObjectType.key == _CHANTIER_OBJECT_KEY,
                    DomainObjectType.archived_at.is_(None),
                    DomainRecord.archived_at.is_(None),
                    DomainRecord.data["refs"].contains([{"ref": subject_external_id}]),
                )
                .order_by(DomainRecord.id)
            )
        ).scalars().all()
    # The JSONB predicate is authoritative in production. Keep this exact
    # defensive filter as well so malformed legacy rows cannot leak a false
    # chantier association through a permissive test/alternate dialect.
    chantiers = [
        chantier
        for chantier in chantiers
        if not is_superseded_chantier(chantier)
        and subject_external_id in {item["ref"] for item in _chantier_refs(chantier)}
    ]
    if not chantiers:
        return []

    member_external_ids = sorted({
        item["ref"]
        for chantier in chantiers
        for item in _chantier_refs(chantier)
        if item["source"] == "github" and item["ref"] != subject_external_id
    })
    capped_count = max(0, len(member_external_ids) - _MAX_CHANTIER_MEMBER_RECORDS)
    member_external_ids = member_external_ids[:_MAX_CHANTIER_MEMBER_RECORDS]

    members_by_external_id: dict[str, Any] = {subject_external_id: record}
    if member_external_ids:
        with _no_autoflush(session):
            members = (
                await session.execute(
                    select(DomainRecord)
                    .join(DomainObjectType, DomainObjectType.id == DomainRecord.object_type_id)
                    .where(
                        DomainRecord.org_id == str(org_id),
                        DomainRecord.domain_id.in_({
                            int(getattr(chantier, "domain_id")) for chantier in chantiers
                        }),
                        DomainObjectType.key == _TICKET_OBJECT_KEY,
                        DomainObjectType.archived_at.is_(None),
                        DomainRecord.archived_at.is_(None),
                        DomainRecord.data["external_id"].astext.in_(member_external_ids),
                    )
                    .order_by(DomainRecord.id)
                    .limit(_MAX_CHANTIER_MEMBER_RECORDS)
                )
            ).scalars().all()
        for member in members:
            external_id = _text(_data(member).get("external_id"))
            if external_id and external_id not in members_by_external_id:
                members_by_external_id[external_id] = member

    return [
        _chantier_piece(
            chantier,
            subject_external_id=subject_external_id,
            members_by_external_id=members_by_external_id,
            member_lookup_capped=capped_count,
        )
        for chantier in chantiers
    ]


def _idea_piece(idea: Any) -> SourcePiece:
    details = dict(getattr(idea, "agent_details", None) or {})
    assignment = details.get("assignment") or {}
    body_bits = [_text(getattr(idea, "description", ""))]
    if details.get("task_domain"):
        body_bits.append(f"task_domain: {details['task_domain']}")
    if assignment:
        body_bits.append(
            f"owner: {assignment.get('owner_id') or 'unclaimed'} (basis: {assignment.get('basis', '?')})"
        )
    return SourcePiece(
        source="record",
        ref=f"{JOB_REF_IDEA_PREFIX}{getattr(idea, 'id', '')}",
        title=_text(getattr(idea, "title", "")) or "triaged item",
        body="; ".join(bit for bit in body_bits if bit),
        ts=getattr(idea, "updated_at", None),
        weight=8,  # below the event-summary piece: the raw signal reads better
    )


def _evidence_piece(idea: Any) -> SourcePiece | None:
    details = dict(getattr(idea, "agent_details", None) or {})
    trail = details.get("attribution") or details.get("preservation")
    if not trail:
        return None
    body = "; ".join(
        f"{key}: {value}" for key, value in sorted(dict(trail).items())
        if isinstance(value, (str, int, float, bool)) and _text(value)
    ) or str(trail)
    return SourcePiece(
        source="evidence",
        ref=f"{JOB_REF_IDEA_PREFIX}{getattr(idea, 'id', '')}:attribution",
        title="Triage-run attribution",
        body=body,
        ts=getattr(idea, "updated_at", None),
    )


def _github_refs(idea: Any, record: Any, event: Any) -> tuple[list[tuple[str, int]], list[str]]:
    """Explicit same-job refs, priority-ordered: event hints → tracker
    identity → owner/repo#N text. Deduped; caps produce a visible note."""
    notes: list[str] = []
    refs: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()

    def add(repo: Any, number: Any) -> None:
        slug = _text(repo).rstrip("/")
        try:
            n = int(str(number).lstrip("#"))
        except (TypeError, ValueError):
            return
        if slug.count("/") != 1 or n < 1:
            return
        ref = (slug, n)
        if ref not in seen:
            seen.add(ref)
            refs.append(ref)

    # (a) GitHub-origin inbound events carry exact identity in hints.
    hints = dict((dict(getattr(event, "envelope", None) or {})).get("hints") or {}) if event else {}
    if _text(hints.get("provider")).lower() == "github":
        add(hints.get("repo"), hints.get("number"))

    # (b) Canonical tracker identity via the existing normalizers.
    data = _data(record)
    if data:
        from brain.systems.user_domains.service import (
            _github_pr_key_from_url,
            _normalize_pr_number,
            _normalize_repo_identity,
        )

        repo = _normalize_repo_identity(data.get("repo"))
        pr_number = _normalize_pr_number(data.get("pr_number"))
        if repo and pr_number:
            add(repo, pr_number)
        elif data.get("pr_url"):
            key = _github_pr_key_from_url(data.get("pr_url"))
            if key:
                add(key[0], key[1])

    # (c) Explicit owner/repo#N text in the job's own words.
    texts = []
    if idea is not None:
        texts.extend([_text(getattr(idea, "title", "")), _text(getattr(idea, "description", ""))])
    for key in ("fix_pr", "pr", "ticket", "issue", "url"):
        texts.append(_text(data.get(key)))
    for text in texts:
        for match in _GITHUB_REF_RE.finditer(text):
            add(match.group(1), match.group(2))

    if len(refs) > _MAX_GITHUB_REFS:
        notes.append(f"github: {len(refs) - _MAX_GITHUB_REFS} additional refs not gathered (cap)")
        refs = refs[:_MAX_GITHUB_REFS]
    return refs, notes


def _checks_summary(checks: Any) -> str:
    runs = (checks or {}).get("check_runs") if isinstance(checks, dict) else None
    if not isinstance(runs, list) or not runs:
        return ""
    conclusions: dict[str, int] = {}
    for run in runs:
        conclusion = _text((run or {}).get("conclusion")) or _text((run or {}).get("status")) or "unknown"
        conclusions[conclusion] = conclusions.get(conclusion, 0) + 1
    summary = ", ".join(f"{count} {name}" for name, count in sorted(conclusions.items()))
    return f"{len(runs)} check runs: {summary}"


def _deploy_ref(record: Any) -> tuple[str, str] | None:
    data = _data(record)
    match = _GITHUB_REF_RE.fullmatch(_text(data.get("fix_pr")))
    sha = _text(data.get("fix_merge_sha"))
    if match is None or not sha:
        return None
    return match.group(1), sha


def _deploy_piece(
    record: Any,
    state: DeployState | None,
) -> SourcePiece | None:
    data = _data(record)
    fix_pr = _text(data.get("fix_pr"))
    sha = _text(data.get("fix_merge_sha"))
    verified = data.get("verified") is True
    if not fix_pr and not sha and not verified and not data.get("verified_at"):
        return None
    bits = [f"state: {render_deploy_state(state)}"]
    for key in ("fix_pr", "fix_merge_sha"):
        if _text(data.get(key)):
            bits.append(f"{key}: {data[key]}")
    bits.append(f"verified: {'yes' if verified else 'no'}")
    if _text(data.get("verified_at")):
        bits.append(f"verified_at: {data['verified_at']}")
    return SourcePiece(
        source=DEPLOY_EVIDENCE_SOURCE,
        ref=f"deploy:{fix_pr or getattr(record, 'id', '')}",
        title="Deploy state",
        body="; ".join(bits),
        ts=getattr(record, "updated_at", None),
    )


async def _deploy_pieces(
    records: list[Any],
    *,
    github: GithubReader | None,
    notes: list[str],
) -> list[SourcePiece]:
    refs = {
        int(getattr(record, "id")): ref
        for record in records
        if (ref := _deploy_ref(record)) is not None
    }
    states: dict[int, DeployState | None] = {}
    derive_many = getattr(github, "derive_deploy_states", None)
    if refs and derive_many is None:
        notes.append("deploy: ancestry reader unavailable")
    elif refs:
        try:
            result = await derive_many(refs)
            states = dict(result)
            unavailable = getattr(result, "unavailable_refs", {})
            if unavailable:
                labels_by_ref = {
                    ref: (
                        _text(_data(record).get("fix_pr"))
                        or f"{ref[0]}@{ref[1][:12]}"
                    )
                    for record in records
                    if (ref := _deploy_ref(record)) is not None
                }
                affected_labels = [
                    labels_by_ref.get(
                        ref,
                        f"{ref[0]}@{ref[1][:12]}",
                    )
                    for ref in unavailable
                ]
                visible_labels = affected_labels[:4]
                affected_summary = ", ".join(visible_labels)
                if len(affected_labels) > len(visible_labels):
                    affected_summary += (
                        f", +{len(affected_labels) - len(visible_labels)} more"
                    )
                categories = Counter(
                    str(failure.error_category)
                    for observation in unavailable.values()
                    for failure in observation.failures
                )
                category_summary = ", ".join(
                    (
                        f"{category}×{count}"
                        if count > 1
                        else category
                    )
                    for category, count in sorted(categories.items())
                )
                total = len(
                    getattr(result, "observations_by_ref", {})
                ) or len(set(refs.values()))
                note = (
                    "deploy: ancestry unavailable for "
                    f"{len(unavailable)}/{total} fixes"
                )
                if affected_summary:
                    note += f": {affected_summary}"
                if category_summary:
                    note += f" ({category_summary})"
                notes.append(note)
        except Exception as exc:  # noqa: BLE001
            notes.append(f"deploy: ancestry unavailable — {type(exc).__name__}")
    pieces: list[SourcePiece] = []
    for record in records:
        piece = _deploy_piece(
            record,
            states.get(int(getattr(record, "id"))),
        )
        if piece is not None:
            pieces.append(piece)
    return pieces


async def _related_tracker_records(
    session: Any, *, org_id: str, refs: list[tuple[str, int]], exclude_id: Any
) -> list[Any]:
    """Org-scoped tracker records matching a discovered repo#number identity."""
    if not refs:
        return []
    from brain.platform.db.models.domain import DomainRecord

    numbers = {str(n) for _, n in refs}
    with _no_autoflush(session):
        rows = (
            await session.execute(
                select(DomainRecord).where(
                    DomainRecord.org_id == str(org_id),
                    DomainRecord.data["pr_number"].astext.in_(numbers),
                ).limit(8)
            )
        ).scalars().all()
    repo_names = {slug.split("/", 1)[1].lower() for slug, _ in refs}
    related = []
    for row in rows:
        if exclude_id is not None and getattr(row, "id", None) == exclude_id:
            continue
        row_repo = _text(_data(row).get("repo")).lower().rstrip("/")
        if not row_repo or any(row_repo.endswith(name) for name in repo_names):
            related.append(row)
    return related


async def gather_pieces(
    session: Any,
    *,
    org_id: str,
    job_ref: str,
    slack: SlackReader | None,
    github: GithubReader | None,
    budget: DossierBudget,
) -> GatherResult:
    """Collect raw pieces + degradation notes for one job. Read-only."""
    result = GatherResult()
    notes = result.source_notes

    try:
        idea, record = await _load_job(session, org_id=org_id, job_ref=str(job_ref or ""))
    except Exception as exc:  # noqa: BLE001
        notes.append(f"record: unavailable — {type(exc).__name__}")
        return result
    if idea is None and record is None:
        notes.append("record: job not found")
        return result

    event = None
    provenance_note_added = False
    deploy_records: list[Any] = []
    if idea is not None:
        try:
            event = await load_inbound_event(session, org_id=org_id, idea=idea)
        except Exception as exc:  # noqa: BLE001
            notes.append(f"slack: provenance unavailable — {type(exc).__name__}")
            provenance_note_added = True

    if record is not None:
        deploy_records.append(record)
        piece = _record_piece(record)
        if piece:
            result.pieces.append(piece)
        try:
            result.pieces.extend(
                await _chantier_pieces_for_record(session, org_id=org_id, record=record)
            )
        except Exception as exc:  # noqa: BLE001
            notes.append(f"chantier: lookup unavailable — {type(exc).__name__}")
    if idea is not None:
        result.pieces.append(_idea_piece(idea))
        evidence = _evidence_piece(idea)
        if evidence:
            result.pieces.append(evidence)

    # An inbound event's own normalized summary is often the best statement
    # of "what happened" for NON-Slack origins (GitHub webhooks etc.), where
    # no thread exists to gather. Slack origins skip this — the thread
    # itself carries the message (probe finding, 2026-07-13).
    envelope = dict(getattr(event, "envelope", None) or {}) if event is not None else {}
    is_slack_event = str(envelope.get("kind") or "").startswith("slack")
    if event is not None and not is_slack_event:
        summary = _text(envelope.get("summary"))
        if summary:
            hints = dict(envelope.get("hints") or {})
            bits = [summary] + [
                f"{key}: {hints[key]}" for key in ("url", "action", "state") if _text(hints.get(key))
            ]
            result.pieces.append(
                SourcePiece(
                    source="record",
                    ref=f"inbound_event:{getattr(event, 'id', '')}",
                    title=summary[:90],
                    body="; ".join(bits),
                    ts=getattr(event, "created_at", None),
                    weight=9,
                )
            )

    # Origin Slack thread — the fail-closed privacy boundary lives HERE.
    # Only SLACK-origin events are expected to carry thread provenance; a
    # GitHub-origin event has no thread, so its absence is not a degradation
    # (probe finding: bogus "malformed" notes on every GitHub item).
    if idea is not None:
        details = dict(getattr(idea, "agent_details", None) or {})
        event_expected = bool((details.get("inbound_triage") or {}).get("event_id"))
        provenance = slack_provenance(event) if event is not None else None
        if event_expected and event is None:
            # Missing row OR org mismatch — either way, say so, don't guess.
            if not provenance_note_added:
                notes.append("slack: provenance unavailable")
        elif event is not None and not is_slack_event:
            pass  # non-Slack origin: nothing omitted, nothing to note
        elif event is not None and provenance is None:
            notes.append("slack: provenance malformed or missing")
        elif provenance is not None and slack is None:
            notes.append("slack: no reader configured")
        elif provenance is not None:
            if provenance["channel_type"] != _PUBLIC_CHANNEL_TYPE:
                notes.append("slack: non-public or unknown-visibility source omitted")
            else:
                fetch_limit = min(max(budget.max_items_per_source * 2, 10), _SLACK_PAGE_CAP)
                try:
                    thread = await slack.read_thread(
                        channel=provenance["channel"],
                        thread_ts=provenance["thread_ts"],
                        limit=fetch_limit,
                    )
                    bot_user_id = provenance.get("bot_user_id") or ""
                    for message in thread.messages:
                        if bot_user_id and message.get("user") == bot_user_id:
                            continue  # Illo's own replies are output, not context
                        result.pieces.append(
                            SourcePiece(
                                source="slack_thread",
                                ref=f"slack:{thread.channel}/p{str(message['ts']).replace('.', '')}",
                                title=f"{message['user'] or 'someone'} in {thread.channel}",
                                body=message["text"],
                                ts=_ts_from_slack(message["ts"]),
                            )
                        )
                    if thread.total > len(thread.messages):
                        notes.append(
                            f"slack_thread: only {len(thread.messages)} of {thread.total} messages fetched"
                        )
                except Exception as exc:  # noqa: BLE001
                    notes.append(f"slack: unavailable — {type(exc).__name__}")

    # Linked GitHub refs — explicit same-job references only.
    refs, ref_notes = _github_refs(idea, record, event)
    notes.extend(ref_notes)
    if refs and github is None:
        notes.append("github: no reader configured")
    elif github is not None:
        for repo_slug, number in refs:
            ref_label = f"{repo_slug}#{number}"
            try:
                payload = await github.read_ref(repo_slug=repo_slug, number=number)
            except Exception as exc:  # noqa: BLE001
                notes.append(f"github: {ref_label} unavailable — {type(exc).__name__}")
                continue
            if payload is None:
                notes.append(f"github: {ref_label} not found")
                continue
            kind = _text(payload.get("kind"))
            body = _text(payload.get("body")) or _text(payload.get("state"))
            total_chars = int(payload.get("body_total_chars") or 0)
            if total_chars > len(_text(payload.get("body"))):
                notes.append(
                    f"github: {ref_label} body pre-compacted upstream "
                    f"(+{total_chars - len(_text(payload.get('body')))} chars)"
                )
            result.pieces.append(
                SourcePiece(
                    source=kind if kind in ("github_issue", "github_pr") else "github_issue",
                    ref=ref_label,
                    title=_text(payload.get("title")) or ref_label,
                    body=body,
                    ts=None,
                    weight=3,
                )
            )
            checks = _checks_summary(payload.get("checks"))
            if checks:
                result.pieces.append(
                    SourcePiece(
                        source="evidence",
                        ref=f"{ref_label}:checks",
                        title=f"CI checks for {ref_label}",
                        body=checks,
                        ts=None,
                    )
                )

    # Related tracker records reached through the discovered identity.
    if refs:
        try:
            related = await _related_tracker_records(
                session, org_id=org_id, refs=refs,
                exclude_id=getattr(record, "id", None) if record is not None else None,
            )
            for row in related:
                deploy_records.append(row)
                piece = _record_piece(row)
                if piece:
                    result.pieces.append(
                        SourcePiece(
                            source="record", ref=piece.ref, title=piece.title,
                            body=piece.body, ts=piece.ts, weight=8,
                        )
                    )
        except Exception as exc:  # noqa: BLE001
            notes.append(f"record: related lookup unavailable — {type(exc).__name__}")

    result.pieces.extend(
        await _deploy_pieces(
            deploy_records,
            github=github,
            notes=notes,
        )
    )
    return result
