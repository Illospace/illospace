"""Illo Brain — Packet minting (the routing-moment orchestrator).

The single owner of gather → assemble → compose → create → stamp → post for
handoff packets (spec: illo-handoff-packets slice 05). Triage completion
calls :func:`mint_packet_after_triage`; the live actionable lanes
(slack_teammate_run, illo_submit) call
:func:`mint_packet_after_actionable_run` when attribution shows the run
created durable work; the notify refresh (slice 06) and the pre-merge probe
reuse the same stages.

Load-bearing stances:

- **Containment.** A packet failure may never break the thing that worked
  before packets existed: :func:`mint_packet_after_triage` never raises —
  every failure returns a ``MintResult(ok=False)`` and a log line.
- **Noise gate.** ``create_launch_handoff_with_status`` reuse
  (``created=False``) means this content already went out — mint records
  NO Slack delivery on reuse. Re-triage of unchanged truth is silent.
- **Post-commit delivery.** Mint never posts to Slack itself: the write
  phase records a pending ``PacketBriefDelivery`` (one per handoff id)
  inside its savepoint, and ``brain.systems.briefing.deliver`` posts it
  strictly after the enclosing transaction commits — a rolled-back mint
  leaves no Slack message, a crash-retry cannot double-post (the
  deterministic ``packet-brief:<handoff_id>`` identity plus the
  thread-read disambiguation own that).
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

from brain.systems.briefing.compose import (
    UNCLAIMED_LABEL,
    PacketRender,
    compose_packet,
    fill_launch_url,
)
from brain.systems.briefing.core import Dossier, DossierBudget, assemble_dossier
from brain.systems.briefing.deliver import (
    DeliveryTarget,
    record_brief_delivery,
    refresh_pending_delivery_brief,
    schedule_post_commit_delivery,
    transfer_pending_delivery,
)
from brain.systems.briefing.gather import (
    PUBLIC_CHANNEL_TYPE,
    DefaultGithubReader,
    DefaultSlackReader,
    GithubReader,
    SlackReader,
    gather_pieces,
    load_inbound_event,
    slack_provenance,
)
from brain.systems.inbound.attribution import durable_work_refs
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
    # Delivery of the human brief is post-commit (outbox): "recorded" means a
    # pending PacketBriefDelivery was written in the mint's savepoint;
    # "skipped:<why>" means a created mint owes no Slack reply; "none" means
    # nothing new to deliver (reuse, failure, refresh).
    delivery: str = "none"
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
    """Best-effort uuid → display name (the _fill_owner_labels pattern).

    Degrades to a human-safe label — never to a raw internal UUID and never
    to None — so a transient lookup failure can't leak serialization or
    render an assigned item as "unclaimed" in the brief.
    """
    if not owner_user_id or session is None:
        return None
    try:
        from brain.platform.db.models.org import User

        row = (
            await session.execute(select(User.id, User.name, User.email).where(User.id == str(owner_user_id)))
        ).first()
        if row is None:
            return "assigned teammate"
        return row.name or row.email or "assigned teammate"
    except Exception:  # noqa: BLE001 — labels must never break a mint
        return "assigned teammate"


def _preferred_actionable_title(pieces: list[Any]) -> str | None:
    """Choose human-authored work truth over the inbound alert serialization."""
    for source in ("github_issue", "github_pr", "slack_thread"):
        for piece in pieces:
            title = str(getattr(piece, "title", "") or "").strip()
            if getattr(piece, "source", None) == source and title:
                return title
    return None


def _delivery_brief(
    packet: PacketRender,
    *,
    launch_url: str,
    owner_label: str | None,
    source_ref: dict[str, Any] | None,
) -> str:
    if (source_ref or {}).get("brief_mode") == "compact_follow_up":
        return f"→ {owner_label or UNCLAIMED_LABEL} · Launch: {launch_url}"
    return fill_launch_url(packet.human_brief, launch_url)


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
    reader_user_id: str | None = None,
) -> tuple[PacketRender, Dossier]:
    """The shared read-only stage: gather → assemble → compose.

    Used by the live mint AND the pre-merge probe (which stops here).
    ``reader_user_id`` is the identity the GitHub reader resolves tokens
    under — token discovery (project bindings, vault inventory, the App
    mint) requires BOTH org and user context, so backend callers pass the
    inbound connection's ``authority_user_id`` (probe finding: without it,
    private-repo reads 404 as if the refs did not exist).
    """
    active_readers = readers or Readers(
        slack=DefaultSlackReader(),
        github=DefaultGithubReader(org_id=org_id, user_id=reader_user_id),
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
    packet_source_ref = dict(source_ref or {})
    actionable_title = (
        _preferred_actionable_title(gathered.pieces)
        if packet_source_ref.get("origin_lane") == "actionable"
        else None
    )
    dossier = assemble_dossier(
        gathered.pieces,
        job_ref=job_ref,
        budget=active_budget,
        headline=actionable_title,
        source_notes=gathered.source_notes,
    )
    effective_ask = f"Pick up this issue: {actionable_title}" if actionable_title else ask
    packet = compose_packet(
        dossier,
        org_id=org_id,
        ask=effective_ask,
        acceptance_criteria=list(_ACCEPTANCE_CRITERIA_V1),
        owner_user_id=owner_user_id,
        owner_label=owner_label,
        target_tool=target_tool,
        repo_origin_url=_repo_origin_hint(dossier),
        source_surface=source_surface,
        source_ref=packet_source_ref,
    )
    return packet, dossier


async def _lock_idea(session: Any, *, org_id: str, idea: Any) -> Any | None:
    """Re-select the idea FOR UPDATE — the spec-pinned per-job serialization.

    All stamp reads/writes go through the locked row so concurrent minters
    queue instead of cross-superseding.
    """
    if idea is None:
        return None
    from brain.platform.db.models.idea import Idea

    return (
        await session.execute(
            select(Idea)
            .where(Idea.id == str(getattr(idea, "id", "")), Idea.org_id == str(org_id))
            .with_for_update()
        )
    ).scalar_one_or_none()


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
    session: Any, *, org_id: str, prior_handoff_id: str, new_row: LaunchHandoff,
    new_brief: str | None = None,
) -> None:
    from brain.systems.launch_handoffs import get_launch_handoff

    old = await get_launch_handoff(session, prior_handoff_id, org_id=org_id)
    if old is None or str(old.id) == str(new_row.id) or old.status == "archived":
        return
    old.status = "archived"
    old.metadata_ = {**(old.metadata_ or {}), "superseded_by": str(new_row.id)}
    new_row.metadata_ = {**(new_row.metadata_ or {}), "supersedes": str(old.id)}
    # A brief that never went out follows the superseding row (the refresh
    # lane records no delivery of its own, so an unfulfilled triage-moment
    # reply would otherwise die with the archived row — or worse, post its
    # dead launch link).
    await transfer_pending_delivery(
        session,
        org_id=org_id,
        prior_handoff_id=str(old.id),
        new_handoff_id=str(new_row.id),
        new_brief=new_brief,
    )
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
    require_clean_gather: bool = False,
    reader_user_id: str | None = None,
    deliver_to: DeliveryTarget | None = None,
) -> MintResult:
    """Mint one packet. Raises nothing upward except via the caller's choice —
    the triage-facing wrapper is :func:`mint_packet_after_triage`.

    ``require_clean_gather`` (the refresh path): a degraded gather (source
    down, partial fetch) must not supersede a healthy stored packet — a
    transient blip would rotate the revision, archive the good row, and
    rotate back on recovery, burning refresh slots on churn.

    ``deliver_to`` (the triage/actionable lanes): a created mint records ONE
    pending brief delivery for that target inside the write-phase savepoint —
    the Slack post itself happens strictly post-commit (see
    ``brain.systems.briefing.deliver``); a mint must never leave a live Slack
    message pointing at state its transaction later rolled back. Reuse
    records nothing (the noise gate). The refresh lane passes None.
    """
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
        reader_user_id=reader_user_id,
    )
    if require_clean_gather and dossier.source_notes:
        return MintResult(
            ok=False,
            reason="degraded gather; not refreshing",
            source_notes=list(dossier.source_notes),
        )

    # WRITE PHASE — one savepoint around EVERYTHING (create + supersede +
    # repair + stamp), so a DB failure anywhere rolls back to the savepoint
    # and the caller's transaction (the triage receipt!) stays healthy.
    # Containment must hold at commit-time, not just raise-time
    # (cross-family review finding, 2026-07-13).
    #
    # The idea is re-selected WITH a row lock before the stamp is read, so a
    # triage-mint racing a notify-refresh (slice 06) cannot double-supersede
    # or clobber a concurrent agent_details write (spec-pinned serialization).
    delivery = "none"
    try:
        async with session.begin_nested():
            locked_idea = await _lock_idea(session, org_id=org_id, idea=idea)
            prior = (
                dict((dict(getattr(locked_idea, "agent_details", None) or {})).get("packet") or {})
                if locked_idea is not None
                else {}
            )
            row, created = await create_launch_handoff_with_status(session, packet.handoff_input)
            filled_brief = _delivery_brief(
                packet,
                launch_url=launch_handoff_url_for_id(row.id, target_tool=row.target_tool),
                owner_label=owner_label,
                source_ref=source_ref,
            )
            if created:
                if deliver_to is not None:
                    # Recorded BEFORE the supersede so the transfer sees the
                    # new row already owes its own delivery.
                    await record_brief_delivery(
                        session, org_id=org_id, handoff_id=str(row.id),
                        target=deliver_to, brief=filled_brief,
                    )
                    delivery = "recorded"
                if prior.get("handoff_id"):
                    await _supersede_prior(
                        session, org_id=org_id, prior_handoff_id=str(prior["handoff_id"]),
                        new_row=row, new_brief=filled_brief,
                    )
            elif _reused_row_drifted(row, packet):
                _repair_drifted_row(row, packet)
                # Refilled AFTER the repair: it may have rotated target_tool,
                # which changes the launch URL an undelivered brief carries.
                await refresh_pending_delivery_brief(
                    session,
                    handoff_id=str(row.id),
                    brief=_delivery_brief(
                        packet,
                        launch_url=launch_handoff_url_for_id(row.id, target_tool=row.target_tool),
                        owner_label=owner_label,
                        source_ref=source_ref,
                    ),
                )
            if locked_idea is not None:
                _stamp_idea(locked_idea, handoff=row, revision=packet.revision, owner_user_id=owner_user_id)
            await session.flush()
    except IntegrityError:
        # Lost a concurrent race on (org_id, idempotency_key): the winner's
        # row IS this content. The savepoint rolled back; re-select as
        # reused and re-stamp under a fresh savepoint (no supersede, no post).
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
        async with session.begin_nested():
            locked_idea = await _lock_idea(session, org_id=org_id, idea=idea)
            if locked_idea is not None:
                _stamp_idea(locked_idea, handoff=row, revision=packet.revision, owner_user_id=owner_user_id)
            await session.flush()

    launch_url = launch_handoff_url_for_id(row.id, target_tool=row.target_tool)
    return MintResult(
        ok=True,
        created=created,
        delivery=delivery,
        handoff=row,
        human_brief=_delivery_brief(
            packet,
            launch_url=launch_url,
            owner_label=owner_label,
            source_ref=source_ref,
        ),
        launch_url=launch_url,
        reason="minted" if created else "reused",
        source_notes=list(dossier.source_notes),
    )


async def find_packet_handoffs_for_jobs(
    session: Any, *, org_id: str, job_refs: list[str]
) -> dict[str, LaunchHandoff]:
    """job_ref → its current (non-archived) packet handoff, newest wins.

    Packets carry their job identity in ``metadata_["job_ref"]`` — the one
    queryable link between domain events and launch handoffs (slice 06).
    ONE batched query per tick, not one per event (review finding: the
    per-event JSONB scan would not age well as packets accumulate)."""
    if not job_refs:
        return {}
    from brain.platform.db.models.launch_handoff import LaunchHandoff as LaunchHandoffModel

    rows = (
        await session.execute(
            select(LaunchHandoffModel)
            .where(
                LaunchHandoffModel.org_id == str(org_id),
                LaunchHandoffModel.metadata_["job_ref"].astext.in_([str(j) for j in job_refs]),
                LaunchHandoffModel.status != "archived",
            )
            .order_by(LaunchHandoffModel.created_at.asc())
        )
    ).scalars().all()
    newest: dict[str, LaunchHandoff] = {}
    for row in rows:  # ascending order → later rows overwrite = newest wins
        job_ref = str((dict(row.metadata_ or {})).get("job_ref") or "")
        if job_ref:
            newest[job_ref] = row
    return newest


async def _find_idea_by_stamp(session: Any, *, org_id: str, handoff_id: str) -> Any | None:
    from brain.platform.db.models.idea import Idea

    return (
        await session.execute(
            select(Idea).where(
                Idea.org_id == str(org_id),
                Idea.agent_details["packet"]["handoff_id"].astext == str(handoff_id),
            ).limit(1)
        )
    ).scalars().first()


async def refresh_packet_for_job(session: Any, *, org_id: str, handoff_row: Any,
                                 readers: Readers | None = None) -> MintResult:
    """Slice 06: re-render a packet against current truth. Never creates a
    NEW Slack post (nudges and digest lines carry the link); unchanged truth
    reuses the row silently, changed truth supersedes. The one delivery
    nuance: superseding a row whose triage-moment brief is still PENDING
    transfers that unfulfilled obligation to the new row (fresh brief, fresh
    launch URL) — the thread still gets its single reply, never a dead link.
    Contained like the triage hook — a refresh failure degrades to a log
    line, never a dead tick.

    Only packet-minted rows (``source_surface == "inbound_triage"``) are
    refreshable: the ORIGINAL ask/owner/target/provenance are reused from
    the row so the revision reflects TRUTH changes only — a refresh must
    never rotate the key on its own inputs.
    """
    try:
        if str(getattr(handoff_row, "source_surface", "") or "") != "inbound_triage":
            return MintResult(ok=False, reason="not a packet-minted handoff")
        meta = dict(getattr(handoff_row, "metadata_", None) or {})
        job_ref = str(meta.get("job_ref") or "")
        if not job_ref:
            return MintResult(ok=False, reason="handoff has no job_ref")
        owner_user_id = str(meta.get("owner_user_id") or "") or None
        idea = await _find_idea_by_stamp(session, org_id=org_id, handoff_id=str(handoff_row.id))
        reader_user_id = None
        if idea is not None:
            source_event = await load_inbound_event(session, org_id=org_id, idea=idea)
            if source_event is not None:
                reader_user_id = str(getattr(source_event, "authority_user_id", "") or "") or None
        result = await mint_packet_for_job(
            session,
            org_id=org_id,
            idea=idea,
            job_ref=job_ref,
            ask=str(getattr(handoff_row, "summary", "") or "") or "take a pass",
            owner_user_id=owner_user_id,
            owner_label=await _owner_label(session, owner_user_id),
            target_tool=str(getattr(handoff_row, "target_tool", "") or "codex"),
            source_surface=str(getattr(handoff_row, "source_surface", "") or "inbound_triage"),
            source_ref=dict(getattr(handoff_row, "source_ref", None) or {}),
            readers=readers,
            require_clean_gather=True,
            reader_user_id=reader_user_id,
        )
        if result.ok and result.created and idea is None:
            # No idea stamp to find the prior through — the refreshed row IS
            # the prior; supersede it directly, under its own savepoint so a
            # failure here can't leave the tick's transaction poisoned
            # (review finding: this branch skipped the write-phase savepoint).
            async with session.begin_nested():
                await _supersede_prior(
                    session, org_id=org_id, prior_handoff_id=str(handoff_row.id),
                    new_row=result.handoff, new_brief=result.human_brief,
                )
        return result
    except Exception as exc:  # noqa: BLE001 — total containment
        logger.warning("packet refresh failed for handoff %s: %s", getattr(handoff_row, "id", "?"), exc)
        return MintResult(ok=False, reason=f"{type(exc).__name__}: {exc}")


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


def _delivery_target_for_event(event: Any) -> tuple[DeliveryTarget | None, str]:
    """Origin-thread delivery target — same provenance and public-only
    allowlist the deleted in-transaction post used, but resolved at MINT
    time so the outbox row snapshots channel/thread_ts and delivery never
    needs the event again. Returns (target, "") or (None, why)."""
    provenance = slack_provenance(event) if event is not None else None
    if not provenance:
        return None, "no_slack_provenance"
    if provenance["channel_type"] != PUBLIC_CHANNEL_TYPE:
        return None, "non_public"
    return DeliveryTarget(
        channel=provenance["channel"],
        thread_ts=provenance["thread_ts"],
        # Crash disambiguation trusts only Illo-authored thread messages —
        # the same identity gather's echo filter already keys on.
        bot_user_id=str(provenance.get("bot_user_id") or "") or None,
    ), ""


async def _mint_for_idea_and_event(
    session: Any,
    *,
    org_id: str,
    idea: Any,
    event: Any,
    attribution: dict[str, Any] | None,
    readers: Readers | None,
    job_ref: str | None = None,
) -> MintResult:
    """Shared stage for every inbound-completion lane once the job-home idea
    is resolved: owner → target → mint → record-delivery-once. Raises upward;
    the lane-facing wrappers own containment. The Slack reply itself is
    post-commit: the savepoint records the obligation, the after-commit fast
    path (or the notify-cycle sweep) sends it."""
    details = dict(getattr(idea, "agent_details", None) or {})
    assignment = dict(details.get("assignment") or {})
    owner_user_id = str(assignment.get("owner_id") or "") or None
    owner_label = await _owner_label(session, owner_user_id)
    target_tool = agent_target_for_member(owner_user_id, _member_targets())
    deliver_to, delivery_skip = _delivery_target_for_event(event)
    actionable_lane = (
        dict(details.get("inbound_triage") or {}).get("reason")
        == "actionable_run_completion"
    )
    tool_names = {
        str(name) for name in (attribution or {}).get("tool_names") or []
    }
    packet_source_ref: dict[str, Any] = {"inbound_event_id": str(event.id)}
    if actionable_lane:
        packet_source_ref["origin_lane"] = "actionable"
        if "post_slack_reply" in tool_names:
            packet_source_ref["brief_mode"] = "compact_follow_up"

    result = await mint_packet_for_job(
        session,
        org_id=org_id,
        idea=idea,
        job_ref=job_ref or _record_job_ref(attribution, idea),
        ask=_ask_from_event(idea, event),
        owner_user_id=owner_user_id,
        owner_label=owner_label,
        target_tool=target_tool,
        source_surface="inbound_triage",
        source_ref=packet_source_ref,
        readers=readers,
        reader_user_id=str(getattr(event, "authority_user_id", "") or "") or None,
        deliver_to=deliver_to,
    )
    if result.ok and result.created:
        if deliver_to is None:
            result.delivery = f"skipped:{delivery_skip}"
        elif result.delivery == "recorded":
            try:
                schedule_post_commit_delivery(
                    session, org_id=org_id, handoff_ids=[str(result.handoff.id)]
                )
            except Exception as exc:  # noqa: BLE001 — the sweep remains the guaranteed path
                logger.warning("packet %s minted but fast-path dispatch failed to arm: %s",
                               getattr(result.handoff, "id", "?"), exc)
    return result


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

        return await _mint_for_idea_and_event(
            session, org_id=org_id, idea=idea, event=event,
            attribution=attribution, readers=readers,
        )
    except Exception as exc:  # noqa: BLE001 — total containment
        logger.warning("packet mint failed for event %s: %s", getattr(event, "id", "?"), exc)
        return MintResult(ok=False, reason=f"{type(exc).__name__}: {exc}")


async def _acquire_event_mint_lock(session: Any, *, org_id: str, event_id: str) -> None:
    """Postgres advisory xact lock keyed on (org, event) — released at the
    caller's commit/rollback. Non-postgres sessions (unit tests on sqlite,
    fakes) skip it: the races it closes are cross-connection, which those
    environments don't exercise."""
    try:
        dialect = getattr(getattr(session, "bind", None), "dialect", None)
        if getattr(dialect, "name", "") != "postgresql":
            return
    except Exception:  # noqa: BLE001 — no bind info → assume no lock support
        return
    from sqlalchemy import text as sql_text

    await session.execute(
        sql_text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"packet-mint:{org_id}:{event_id}"},
    )


def _github_repo_from_refs(work_refs: list[dict[str, str]]) -> str | None:
    for ref in work_refs:
        if str(ref.get("kind") or "") in ("github_issue", "github_pull_request"):
            slug = str(ref.get("id") or "").split("#", 1)[0]
            if slug.count("/") == 1:
                return slug
    return None


async def _create_job_home_idea(
    session: Any,
    *,
    org_id: str,
    event: Any,
    run_row: Any,
    attribution: dict[str, Any] | None,
    work_refs: list[dict[str, str]],
) -> Any | None:
    """Create the job-home idea the actionable lanes lack.

    Slack-teammate and submission runs are admitted without a triage idea
    (only ``_queue_illo_triage`` creates one), so when such a run completes
    with durable work there is no stamp target, no gather anchor, and no
    provenance bridge. This mirrors the triage idea's exact shape —
    ``agent_details.inbound_triage.event_id`` is what ``load_inbound_event``
    and gather's provenance walk — but deliberately admits NO new run: the
    work already happened; the idea documents where it was routed.

    The description embeds each work ref (``owner/repo#N`` included) so
    gather's same-job reference discovery reaches the created artifacts.
    Returns None (mint skips, logged by the hook) when no owner resolves
    and no unclaimed pool is configured — the same parking rule as triage.
    """
    from brain.platform.db.models.idea import Idea
    from brain.systems.inbound.assignment import default_rules, resolve_owner
    from brain.systems.inbound.service import _unclaimed_pool_user_id
    from brain.systems.task_domain import classify_task_domain

    envelope = dict(getattr(event, "envelope", None) or {})
    summary = str(envelope.get("summary") or "").strip()
    origin = str(getattr(event, "origin", "") or "")
    ref_ids = [str(ref.get("id") or "") for ref in work_refs if ref.get("id")]
    repo = _github_repo_from_refs(work_refs)
    task_domain = classify_task_domain(summary)
    run_user_id = str(getattr(run_row, "user_id", "") or "") or None
    authority_user_id = str(getattr(event, "authority_user_id", "") or "") or None
    # Same rule order as _queue_illo_triage: domain rules only when a repo
    # anchors the signal (here: the repo the run actually filed into), so a
    # stray keyword in a Slack message can't yank ownership on its own.
    decision = resolve_owner(
        task_domain=task_domain if repo else None,
        repo=repo,
        connection_owner_id=run_user_id or authority_user_id,
        rules=default_rules(),
    )
    owner_user_id = decision.user_id
    unclaimed = False
    if not owner_user_id:
        pool_user_id = _unclaimed_pool_user_id()
        if not pool_user_id:
            return None
        owner_user_id = pool_user_id
        unclaimed = True

    title = summary[:180] or f"Inbound {origin} signal routed by Illo"[:180]
    description_lines = [
        f"Illo run {getattr(run_row, 'id', '?')} handled this {origin or 'inbound'} "
        "signal and created durable work.",
        f"Work refs: {', '.join(ref_ids) or 'unknown'}",
    ]
    idea = Idea(
        title=title,
        description="\n".join(description_lines)[:500],
        status="emerged",
        origin="inbound_signal",
        origin_ref=f"inbound_event:{event.id}",
        user_id=owner_user_id,
        org_id=org_id,
        agent_details={
            "inbound_triage": {
                "event_id": str(event.id),
                "origin": origin,
                "reason": "actionable_run_completion",
                "connection_id": str(getattr(event, "connection_id", "") or "") or None,
                "policy_id": None,
            },
            "task_domain": task_domain.value,
            "assignment": {
                "owner_id": owner_user_id,
                "basis": decision.basis.value,
                "authority_user_id": authority_user_id,
                "unclaimed": unclaimed,
            },
            # Gather's evidence piece renders scalar values from this trail.
            "attribution": {
                "summary": str((attribution or {}).get("summary") or ""),
                "tools": ", ".join(
                    str(tool) for tool in (attribution or {}).get("tool_names") or []
                ),
                "run_id": str(getattr(run_row, "id", "") or ""),
            },
        },
    )
    # Own savepoint: an INSERT failure must roll back cleanly instead of
    # poisoning the caller's transaction (which holds the run's terminal
    # status write) — the same commit-time containment mint_packet_for_job
    # keeps for its write phase.
    async with session.begin_nested():
        session.add(idea)
        await session.flush()
    return idea


async def mint_packet_after_actionable_run(
    session: Any,
    *,
    event: Any,
    run_row: Any,
    attribution: dict[str, Any] | None,
    readers: Readers | None = None,
) -> MintResult:
    """Completion hook for the live actionable lanes (``slack_teammate_run``,
    ``illo_submit``): mint iff attribution proves the run created or routed
    durable work. NEVER raises — these runs worked before packets existed.

    Differences from the triage hook, both deliberate:

    - **Durable-work predicate.** Triage completion IS the routing moment,
      so it mints unconditionally. These runs answer questions and stay
      silent far more often than they route work — only
      :func:`durable_work_refs` evidence mints, so a Slack reply can never
      spawn a packet.
    - **One-shot stamp guard.** The slack lane's receipt is terminal at
      admission time, so the reconcile transition can't serve as the
      once-per-lifecycle gate the triage lane uses. The idea's packet stamp
      is the gate instead: stamped → skip (refresh, slice 06, owns truth
      drift after that). A failed mint leaves no stamp, so a crash-retry
      of the same terminal transition retries the mint.
    """
    try:
        org_id = str(getattr(event, "org_id", "") or "")
        if not org_id:
            return MintResult(ok=False, reason="event has no org")
        work_refs = durable_work_refs(attribution)
        if not work_refs:
            return MintResult(ok=False, reason="no durable work created by run")

        # Serialize per event BEFORE the idea lookup: ideas have no unique
        # constraint on origin_ref, so an unlocked check-then-insert lets two
        # concurrent reconciles (illo_get_result polls racing on a still-open
        # submit receipt) create two job homes → two packets (cross-family
        # review finding, 2026-07-16). The xact-scoped advisory lock makes
        # lookup → stamp check → create → mint one critical section per
        # event; the loser blocks, then sees the winner's committed stamp.
        await _acquire_event_mint_lock(session, org_id=org_id, event_id=str(event.id))

        from brain.platform.db.models.idea import Idea

        idea = (
            (
                await session.execute(
                    select(Idea)
                    .where(
                        Idea.origin_ref == f"inbound_event:{event.id}",
                        Idea.org_id == org_id,
                    )
                    .order_by(Idea.created_at.asc())
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )
        if idea is not None and dict(dict(getattr(idea, "agent_details", None) or {}).get("packet") or {}):
            return MintResult(ok=False, reason="packet already minted for event")
        if idea is None:
            idea = await _create_job_home_idea(
                session,
                org_id=org_id,
                event=event,
                run_row=run_row,
                attribution=attribution,
                work_refs=work_refs,
            )
        if idea is None:
            return MintResult(ok=False, reason="no owner and no unclaimed pool for job home")

        # Anchor the job on the run's OWN durable work, never on attribution
        # target_refs at large — those include read-only tool results, and
        # the first live packet anchored its dossier on the triage playbook
        # doc the run had READ instead of the tracker item it CREATED
        # (illo-dev E2E, handoff e827d633).
        job_ref = next(
            (
                f"domain_record:{ref['id']}"
                for ref in work_refs
                if str(ref.get("kind") or "") == "domain_record" and ref.get("id")
            ),
            f"idea:{getattr(idea, 'id', '')}",
        )
        return await _mint_for_idea_and_event(
            session, org_id=org_id, idea=idea, event=event,
            attribution=attribution, readers=readers, job_ref=job_ref,
        )
    except Exception as exc:  # noqa: BLE001 — total containment
        logger.warning("packet mint failed for event %s: %s", getattr(event, "id", "?"), exc)
        return MintResult(ok=False, reason=f"{type(exc).__name__}: {exc}")
