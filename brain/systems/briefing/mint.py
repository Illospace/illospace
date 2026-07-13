"""Illo Brain — Packet minting (the routing-moment orchestrator).

The single owner of gather → assemble → compose → create → stamp → post for
handoff packets (spec: illo-handoff-packets slice 05). Triage completion
calls :func:`mint_packet_after_triage`; the notify refresh (slice 06) and
the pre-merge probe reuse the same stages.

Load-bearing stances:

- **Containment.** A packet failure may never break the thing that worked
  before packets existed: :func:`mint_packet_after_triage` never raises —
  every failure returns a ``MintResult(ok=False)`` and a log line.
- **Noise gate.** ``create_launch_handoff_with_status`` reuse
  (``created=False``) means this content already went out — mint posts
  NOTHING to Slack on reuse. Re-triage of unchanged truth is silent.
- **Supersede, existing vocabulary only.** When the job's truth changes,
  the new revision is a NEW row (new idempotency key); the prior row —
  found via the idea's packet stamp — becomes ``archived`` +
  ``metadata_["superseded_by"]`` (the DB CHECK constraint has no
  ``superseded`` status). Reuse-with-drift (same key, different content —
  a hash-gap anomaly the slice-02 revision should make impossible) is
  repaired in place and logged, never duplicated.
- **Stamps live in Illo-owned state**: ``idea.agent_details["packet"]``,
  never in projection-owned record ``data`` (re-projection would clobber
  it and orphan slice 06's lookup).
- **No gates** (Reda, 2026-07-13): once the reconcile hook ships, every
  completed triage of an actionable item mints. The only env var is
  ``ILLO_MEMBER_AGENT_TARGETS`` (config-with-default: unset → codex).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from brain.systems.briefing.compose import PacketRender, compose_packet, fill_launch_url
from brain.systems.briefing.core import Dossier, DossierBudget, assemble_dossier
from brain.systems.briefing.gather import (
    _PUBLIC_CHANNEL_TYPE,
    _load_inbound_event,
    _slack_provenance,
    DefaultGithubReader,
    DefaultSlackReader,
    GithubReader,
    SlackReader,
    gather_pieces,
)
from brain.systems.launch_handoffs import (
    LaunchHandoff,
    agent_target_for_member,
    create_launch_handoff_with_status,
    launch_handoff_url_for_id,
    parse_member_agent_targets,
)

logger = logging.getLogger(__name__)

_ACCEPTANCE_CRITERIA_V1: list[str] = []  # v1: none unless mechanically derivable


@dataclass(frozen=True)
class Readers:
    slack: SlackReader | None = None
    github: GithubReader | None = None


@dataclass
class MintResult:
    ok: bool
    created: bool = False
    posted: bool = False
    handoff: LaunchHandoff | None = None
    human_brief: str = ""
    launch_url: str = ""
    reason: str = ""
    source_notes: list[str] = field(default_factory=list)


def _member_targets() -> dict[str, str]:
    try:
        return parse_member_agent_targets(os.environ.get("ILLO_MEMBER_AGENT_TARGETS"))
    except Exception as exc:  # noqa: BLE001 — bad config degrades to default, loudly
        logger.warning("ILLO_MEMBER_AGENT_TARGETS unparseable (%s); defaulting to codex", exc)
        return {}


async def _owner_label(session: Any, owner_user_id: str | None) -> str | None:
    """Best-effort uuid → display name (the _fill_owner_labels pattern)."""
    if not owner_user_id or session is None:
        return None
    try:
        from brain.platform.db.models.org import User

        row = (
            await session.execute(select(User.id, User.name, User.email).where(User.id == str(owner_user_id)))
        ).first()
        if row is None:
            return None
        return row.name or row.email or str(row.id)
    except Exception:  # noqa: BLE001 — degrade to the raw id, never break a mint
        return None


def _repo_origin_hint(dossier: Dossier) -> str | None:
    for section in dossier.sections:
        if section.source in ("github_pr", "github_issue"):
            for item in section.items:
                slug = item.ref.split("#", 1)[0]
                if slug.count("/") == 1:
                    return f"https://github.com/{slug}.git"
    return None


async def build_packet_for_job(
    session: Any,
    *,
    org_id: str,
    job_ref: str,
    ask: str,
    owner_user_id: str | None,
    owner_label: str | None,
    target_tool: str,
    source_surface: str = "illo",
    source_ref: dict[str, Any] | None = None,
    readers: Readers | None = None,
    budget: DossierBudget | None = None,
) -> tuple[PacketRender, Dossier]:
    """The shared read-only stage: gather → assemble → compose.

    Used by the live mint AND the pre-merge probe (which stops here).
    """
    active_readers = readers or Readers(
        slack=DefaultSlackReader(), github=DefaultGithubReader(org_id=org_id)
    )
    active_budget = budget or DossierBudget()
    gathered = await gather_pieces(
        session,
        org_id=org_id,
        job_ref=job_ref,
        slack=active_readers.slack,
        github=active_readers.github,
        budget=active_budget,
    )
    dossier = assemble_dossier(
        gathered.pieces,
        job_ref=job_ref,
        budget=active_budget,
        source_notes=gathered.source_notes,
    )
    packet = compose_packet(
        dossier,
        org_id=org_id,
        ask=ask,
        acceptance_criteria=list(_ACCEPTANCE_CRITERIA_V1),
        owner_user_id=owner_user_id,
        owner_label=owner_label,
        target_tool=target_tool,
        repo_origin_url=_repo_origin_hint(dossier),
        source_surface=source_surface,
        source_ref=dict(source_ref or {}),
    )
    return packet, dossier


def _stamp_idea(idea: Any, *, handoff: LaunchHandoff, revision: str, owner_user_id: str | None) -> None:
    details = dict(getattr(idea, "agent_details", None) or {})
    details["packet"] = {
        "handoff_id": str(handoff.id),
        "revision": revision,
        "owner_user_id": owner_user_id,
        "minted_at": datetime.now(timezone.utc).isoformat(),
    }
    idea.agent_details = details


async def _supersede_prior(
    session: Any, *, org_id: str, prior_handoff_id: str, new_row: LaunchHandoff
) -> None:
    from brain.systems.launch_handoffs import get_launch_handoff

    old = await get_launch_handoff(session, prior_handoff_id, org_id=org_id)
    if old is None or str(old.id) == str(new_row.id) or old.status == "archived":
        return
    old.status = "archived"
    old.metadata_ = {**(old.metadata_ or {}), "superseded_by": str(new_row.id)}
    new_row.metadata_ = {**(new_row.metadata_ or {}), "supersedes": str(old.id)}
    await session.flush()


def _reused_row_drifted(row: LaunchHandoff, packet: PacketRender) -> bool:
    fresh = packet.handoff_input
    return (
        str(row.instructions or "") != str(fresh.instructions or "")
        or str(row.target_tool or "") != str(fresh.target_tool or "")
        or (row.metadata_ or {}).get("owner_user_id") != (fresh.metadata or {}).get("owner_user_id")
    )


def _repair_drifted_row(row: LaunchHandoff, packet: PacketRender) -> None:
    """Same idempotency key, different content — a revision-hash gap. Repair
    the stored snapshot in place (one row per key) and log; never duplicate."""
    fresh = packet.handoff_input
    row.title = fresh.title
    row.instructions = fresh.instructions
    row.summary = fresh.summary
    row.target_tool = fresh.target_tool
    row.context_parts = list(fresh.context_parts)
    row.acceptance_criteria = list(fresh.acceptance_criteria)
    row.repo_origin_url = fresh.repo_origin_url
    row.branch_hint = fresh.branch_hint
    row.metadata_ = {**(row.metadata_ or {}), **(fresh.metadata or {})}
    logger.warning(
        "handoff %s reused with content drift — repaired in place; revision hashing "
        "should have rotated the key (investigate)", row.id,
    )


async def mint_packet_for_job(
    session: Any,
    *,
    org_id: str,
    idea: Any,
    job_ref: str,
    ask: str,
    owner_user_id: str | None,
    owner_label: str | None,
    target_tool: str,
    source_surface: str = "illo",
    source_ref: dict[str, Any] | None = None,
    readers: Readers | None = None,
    budget: DossierBudget | None = None,
) -> MintResult:
    """Mint one packet. Raises nothing upward except via the caller's choice —
    the triage-facing wrapper is :func:`mint_packet_after_triage`."""
    packet, dossier = await build_packet_for_job(
        session,
        org_id=org_id,
        job_ref=job_ref,
        ask=ask,
        owner_user_id=owner_user_id,
        owner_label=owner_label,
        target_tool=target_tool,
        source_surface=source_surface,
        source_ref=source_ref,
        readers=readers,
        budget=budget,
    )

    prior = dict((dict(getattr(idea, "agent_details", None) or {})).get("packet") or {}) if idea is not None else {}
    try:
        async with session.begin_nested():
            row, created = await create_launch_handoff_with_status(session, packet.handoff_input)
    except IntegrityError:
        # Lost a concurrent race on (org_id, idempotency_key): the winner's
        # row IS this content. Re-select and treat as reused.
        from brain.platform.db.models.launch_handoff import LaunchHandoff as LaunchHandoffModel

        row = (
            await session.execute(
                select(LaunchHandoffModel).where(
                    LaunchHandoffModel.org_id == str(org_id),
                    LaunchHandoffModel.idempotency_key == packet.idempotency_key,
                )
            )
        ).scalar_one()
        created = False

    if created:
        if prior.get("handoff_id"):
            await _supersede_prior(
                session, org_id=org_id, prior_handoff_id=str(prior["handoff_id"]), new_row=row
            )
    elif _reused_row_drifted(row, packet):
        _repair_drifted_row(row, packet)
        await session.flush()

    if idea is not None:
        _stamp_idea(idea, handoff=row, revision=packet.revision, owner_user_id=owner_user_id)
        await session.flush()

    launch_url = launch_handoff_url_for_id(row.id, target_tool=row.target_tool)
    return MintResult(
        ok=True,
        created=created,
        handoff=row,
        human_brief=fill_launch_url(packet.human_brief, launch_url),
        launch_url=launch_url,
        reason="minted" if created else "reused",
        source_notes=list(dossier.source_notes),
    )


def _ask_from_event(idea: Any, event: Any) -> str:
    """Deterministic v1 ask: task_domain + the normalized inbound summary.
    NO run-prose parsing, NO new structured-output channel (see slice 05)."""
    details = dict(getattr(idea, "agent_details", None) or {})
    domain = str(details.get("task_domain") or "other")
    summary = str((dict(getattr(event, "envelope", None) or {})).get("summary") or "").strip()
    if not summary:
        summary = str(getattr(idea, "title", "") or "").strip() or "this item"
    return f"Pick up this {domain} item: {summary}"


def _record_job_ref(attribution: dict[str, Any] | None, idea: Any) -> str:
    for ref in (attribution or {}).get("target_refs") or []:
        if isinstance(ref, dict) and str(ref.get("kind") or "") == "domain_record" and ref.get("id"):
            return f"domain_record:{ref['id']}"
    return f"idea:{getattr(idea, 'id', '')}"


async def _post_brief_to_origin_thread(session: Any, *, org_id: str, idea: Any, brief: str) -> bool:
    """Backend Slack reply into the origin thread — same provenance and
    public-only allowlist as gather; silent skip (False) when not possible."""
    event = await _load_inbound_event(session, org_id=org_id, idea=idea)
    provenance = _slack_provenance(event) if event is not None else None
    if not provenance or provenance["channel_type"] != _PUBLIC_CHANNEL_TYPE:
        return False
    from brain.systems.slack.client import slack_web_client_from_env

    client = slack_web_client_from_env()
    await client.post_message(
        channel=provenance["channel"], text=brief, thread_ts=provenance["thread_ts"]
    )
    return True


async def mint_packet_after_triage(
    session: Any,
    *,
    event: Any,
    run_row: Any,
    attribution: dict[str, Any] | None = None,
    readers: Readers | None = None,
) -> MintResult:
    """The triage-completion hook body. NEVER raises — triage worked before
    packets existed and must keep working without them."""
    try:
        org_id = str(getattr(event, "org_id", "") or "")
        if not org_id:
            return MintResult(ok=False, reason="event has no org")

        from brain.platform.db.models.idea import Idea

        idea = (
            await session.execute(
                select(Idea).where(
                    Idea.origin_ref == f"inbound_event:{event.id}",
                    Idea.org_id == org_id,
                )
            )
        ).scalar_one_or_none()
        if idea is None:
            return MintResult(ok=False, reason="no triage idea for event")

        details = dict(getattr(idea, "agent_details", None) or {})
        assignment = dict(details.get("assignment") or {})
        owner_user_id = str(assignment.get("owner_id") or "") or None
        owner_label = await _owner_label(session, owner_user_id)
        target_tool = agent_target_for_member(owner_user_id, _member_targets())

        result = await mint_packet_for_job(
            session,
            org_id=org_id,
            idea=idea,
            job_ref=_record_job_ref(attribution, idea),
            ask=_ask_from_event(idea, event),
            owner_user_id=owner_user_id,
            owner_label=owner_label,
            target_tool=target_tool,
            source_surface="inbound_triage",
            source_ref={"inbound_event_id": str(event.id)},
            readers=readers,
        )
        if result.ok and result.created:
            try:
                result.posted = await _post_brief_to_origin_thread(
                    session, org_id=org_id, idea=idea, brief=result.human_brief
                )
            except Exception as exc:  # noqa: BLE001 — the packet exists; the reply degraded
                logger.warning("packet %s minted but Slack reply failed: %s",
                               getattr(result.handoff, "id", "?"), exc)
        return result
    except Exception as exc:  # noqa: BLE001 — total containment
        logger.warning("packet mint failed for event %s: %s", getattr(event, "id", "?"), exc)
        return MintResult(ok=False, reason=f"{type(exc).__name__}: {exc}")
