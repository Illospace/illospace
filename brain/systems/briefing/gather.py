"""Illo Brain — Dossier gathering (read-only source adapters).

The single owner of "collect the raw pieces for a job" (spec:
illo-handoff-packets slice 03). Triage minting, notify refresh, and the
on-demand "brief me" flow all gather through :func:`gather_pieces`; no
caller grows its own context collection.

Design stances, each load-bearing:

- **Read-only.** The gather path never writes — no ``session.add`` /
  ``flush`` / ``commit`` anywhere. A dossier is a view; job truth stays in
  the records (README one-owner invariant).
- **Delegation, not duplication.** Adapters are tiny Protocols whose
  default impls delegate to the EXISTING owners of each read path — the
  Slack web client, the cortex GitHub connector, the domain-record tables.
  No new HTTP clients, no new token paths.
- **Honest degradation.** A source that is down, private, or partially
  fetched yields an explicit ``source_notes`` entry (rendered in both
  packet audiences), never a silent absence and never a crashed gather.
  Adapters fetch within per-source limits AND report true totals so
  omission accounting stays truthful.
- **Privacy boundary.** Only team-visible Slack channels are excerpted; a
  private channel / DM / group surface degrades to a note. ``handoff.get``
  and the handoff API are org-scoped, so anything gathered here becomes
  readable org-wide — the boundary lives at gather time, not render time.
- **Same-job references only.** Related records come from explicit links
  (provenance, tracker fields, ``owner/repo#N`` refs in the job's own
  text) — no fuzzy search; fuzzy relatedness is where briefs start lying.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from sqlalchemy import select

from brain.systems.briefing.core import DossierBudget, SourcePiece

# Conservative same-job reference pattern: explicit owner/repo#N only.
_GITHUB_REF_RE = re.compile(r"\b([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(\d+)\b")
_MAX_GITHUB_REFS = 4
_SLACK_FETCH_CAP = 50
# Slack surfaces that are NOT team-visible (ingress envelope vocabulary).
_PRIVATE_CHANNEL_TYPES = {"im", "mpim", "group"}

JOB_REF_IDEA_PREFIX = "idea:"
JOB_REF_RECORD_PREFIX = "domain_record:"


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
    async def read_ref(self, *, repo_slug: str, number: int) -> dict[str, Any] | None: ...


def _ts_from_slack(ts: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc)
    except (TypeError, ValueError):
        return None


def _text(value: Any) -> str:
    return str(value or "").strip()


class DefaultSlackReader:
    """Delegates to the existing Slack web client (read path only)."""

    def __init__(self, client: Any | None = None) -> None:
        self._client = client

    def _resolve_client(self) -> Any:
        if self._client is None:
            from brain.systems.slack.client import slack_web_client_from_env

            self._client = slack_web_client_from_env()
        return self._client

    async def read_thread(self, *, channel: str, thread_ts: str, limit: int) -> SlackThreadRead:
        client = self._resolve_client()
        payload = await client.conversation_replies(
            channel=channel, thread_ts=thread_ts, limit=limit
        )
        raw = payload.get("messages") or []
        messages = tuple(
            {"ts": _text(m.get("ts")), "user": _text(m.get("user")), "text": _text(m.get("text"))}
            for m in raw
            if _text(m.get("text"))
        )
        # conversations.replies reports the thread size on the parent.
        total = len(raw)
        if raw:
            try:
                total = max(total, int(raw[0].get("reply_count") or 0) + 1)
            except (TypeError, ValueError):
                pass
        return SlackThreadRead(messages=messages, total=total, channel=channel)


class DefaultGithubReader:
    """Delegates to the cortex GitHub connector using the handler-owned
    token resolution (the existing owner of GitHub read auth)."""

    async def _token(self) -> str | None:
        # Reuse the tool handler's candidate resolution rather than growing
        # a second token path; first usable candidate wins.
        from brain.systems.runs.tool_catalog.handlers.github import _github_token_candidates

        try:
            candidates = await _github_token_candidates(None)
        except Exception:  # noqa: BLE001 — degrade, caller renders the note
            return None
        for candidate in candidates or []:
            token = candidate[1] if isinstance(candidate, (tuple, list)) and len(candidate) > 1 else candidate
            if token:
                return str(token)
        return None

    async def read_ref(self, *, repo_slug: str, number: int) -> dict[str, Any] | None:
        from brain.systems.cortex.project_context.github import (
            async_get_pull_request,
            async_list_repo_issues,
        )

        token = await self._token()
        try:
            pr = await async_get_pull_request(repo_slug, number, token=token)
            if pr:
                return {"kind": "github_pr", **pr}
        except Exception:  # noqa: BLE001 — not a PR (or unreadable); try issues
            pass
        issues = await async_list_repo_issues(
            repo_slug, token=token, state="all", labels=[], assignee=None,
            creator=None, mentioned=None, since=None,
            include_pull_requests=False, limit=50,
        )
        for issue in (issues or {}).get("issues", []) if isinstance(issues, dict) else (issues or []):
            if int(issue.get("number") or 0) == number:
                return {"kind": "github_issue", **issue}
        return None


async def _load_job(session: Any, *, org_id: str, job_ref: str) -> tuple[Any | None, Any | None]:
    """Resolve a job_ref to (idea, record); either may be None."""
    from brain.platform.db.models.domain import DomainRecord
    from brain.platform.db.models.idea import Idea

    idea = None
    record = None
    if job_ref.startswith(JOB_REF_IDEA_PREFIX):
        idea = await session.get(Idea, job_ref[len(JOB_REF_IDEA_PREFIX):])
    elif job_ref.startswith(JOB_REF_RECORD_PREFIX):
        record = await session.get(DomainRecord, int(job_ref[len(JOB_REF_RECORD_PREFIX):]))
    if idea is not None and str(getattr(idea, "org_id", "") or "") not in ("", str(org_id)):
        idea = None
    return idea, record


async def _slack_provenance(session: Any, idea: Any) -> dict[str, Any] | None:
    """idea → inbound event → the origin thread's channel/ts + surface type."""
    details = dict(getattr(idea, "agent_details", None) or {})
    event_id = str((details.get("inbound_triage") or {}).get("event_id") or "")
    if not event_id:
        return None
    from brain.platform.db.models.inbound import InboundEventRow

    event = await session.get(InboundEventRow, event_id)
    if event is None:
        return None
    envelope = dict(getattr(event, "envelope", None) or {})
    payload = dict(getattr(event, "normalized_payload", None) or {}) or dict(
        getattr(event, "raw_payload", None) or {}
    )
    channel = _text(payload.get("channel") or envelope.get("channel"))
    thread_ts = _text(
        payload.get("thread_ts") or payload.get("ts") or envelope.get("thread_ts")
    )
    channel_type = _text(payload.get("channel_type") or envelope.get("channel_type")).lower()
    if not channel or not thread_ts:
        return None
    return {"channel": channel, "thread_ts": thread_ts, "channel_type": channel_type}


def _record_piece(record: Any) -> SourcePiece | None:
    data = dict(getattr(record, "data", None) or {})
    if not data:
        return None
    title = _text(data.get("title") or data.get("name")) or f"record {getattr(record, 'id', '?')}"
    skip = {"title", "name"}
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
        weight=9,
    )


def _github_refs(idea: Any, record: Any) -> list[tuple[str, int]]:
    texts: list[str] = []
    if idea is not None:
        texts.extend([_text(getattr(idea, "title", "")), _text(getattr(idea, "description", ""))])
    data = dict(getattr(record, "data", None) or {}) if record is not None else {}
    for key in ("fix_pr", "pr", "ticket", "issue", "url"):
        texts.append(_text(data.get(key)))
    refs: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()
    for text in texts:
        for match in _GITHUB_REF_RE.finditer(text):
            ref = (match.group(1), int(match.group(2)))
            if ref not in seen:
                seen.add(ref)
                refs.append(ref)
    return refs[:_MAX_GITHUB_REFS]


def _deploy_piece(record: Any) -> SourcePiece | None:
    data = dict(getattr(record, "data", None) or {}) if record is not None else {}
    state = _text(data.get("deploy_state"))
    if not state:
        return None
    bits = [f"deploy_state: {state}"]
    for key in ("fix_pr", "repo", "deployed_at", "verified_at"):
        if _text(data.get(key)):
            bits.append(f"{key}: {data[key]}")
    return SourcePiece(
        source="deploy_state",
        ref=f"deploy:{data.get('fix_pr') or getattr(record, 'id', '')}",
        title="Deploy ladder",
        body="; ".join(bits),
        ts=getattr(record, "updated_at", None),
    )


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

    if record is not None:
        piece = _record_piece(record)
        if piece:
            result.pieces.append(piece)
        deploy = _deploy_piece(record)
        if deploy:
            result.pieces.append(deploy)
    if idea is not None:
        result.pieces.append(_idea_piece(idea))

    # Origin Slack thread — the privacy boundary lives HERE.
    if slack is not None and idea is not None:
        try:
            provenance = await _slack_provenance(session, idea)
        except Exception as exc:  # noqa: BLE001
            provenance = None
            notes.append(f"slack: provenance unavailable — {type(exc).__name__}")
        if provenance:
            if provenance["channel_type"] in _PRIVATE_CHANNEL_TYPES:
                notes.append("slack: private source omitted")
            else:
                fetch_limit = min(max(budget.max_items_per_source * 2, 10), _SLACK_FETCH_CAP)
                try:
                    thread = await slack.read_thread(
                        channel=provenance["channel"],
                        thread_ts=provenance["thread_ts"],
                        limit=fetch_limit,
                    )
                    for message in thread.messages:
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
    if github is not None:
        for repo_slug, number in _github_refs(idea, record):
            try:
                payload = await github.read_ref(repo_slug=repo_slug, number=number)
            except Exception as exc:  # noqa: BLE001
                notes.append(f"github: {repo_slug}#{number} unavailable — {type(exc).__name__}")
                continue
            if payload is None:
                notes.append(f"github: {repo_slug}#{number} not found in readable window")
                continue
            kind = _text(payload.get("kind")) or "github_issue"
            result.pieces.append(
                SourcePiece(
                    source=kind if kind in ("github_issue", "github_pr") else "github_issue",
                    ref=f"{repo_slug}#{number}",
                    title=_text(payload.get("title")) or f"{repo_slug}#{number}",
                    body=_text(payload.get("body")) or _text(payload.get("state")),
                    ts=None,
                    weight=3,
                )
            )

    return result
