"""Illo Brain — Packet composer (pure).

One :class:`~brain.systems.briefing.core.Dossier` in, one *packet* out:
(a) a short Slack-mrkdwn human brief and (b) a ready
:class:`~brain.systems.launch_handoffs.LaunchHandoffCreateInput` for the
assignee's own coding agent. Deterministic by design — briefs must be
trusted, and determinism is the trust floor; model-polished phrasing is a
later, eval-gated idea (see specs/illo-handoff-packets/slices/02).

Idempotency contract (spec review finding): ``revision`` hashes the COMPOSE
OUTPUT — dossier + ask + acceptance criteria + owner + target — because
``create_launch_handoff`` silently returns the existing row on an
idempotency-key hit. Any input that changes the packet MUST change the key,
or the stored handoff diverges from the posted brief. The key stays within
the model's 120-char column by truncating the job_ref side, never the hash.

Truncation honesty carries through: the brief's length cap is enforced by
tightening the narrative excerpt (with a visible marker), never by dropping
the omissions note or the launch link.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

from brain.systems.briefing.core import Dossier, _cut, _plural
from brain.systems.launch_handoffs import TARGET_CODEX, LaunchHandoffCreateInput

# Slack chat.postMessage truncates around 4k; a brief should be a glance,
# not a page. Content cap for the narrative; structural lines are exempt
# bounded overhead (same stance as the dossier core).
BRIEF_CHAR_CAP = 1_200
_NARRATIVE_CAP = 300
_MIN_NARRATIVE = 60
_EVIDENCE_REFS = 5
_HASH_HEX_CHARS = 16
_IDEMPOTENCY_KEY_MAX = 120  # LaunchHandoff.idempotency_key column cap

# Sections whose refs read as evidence links in the brief.
_EVIDENCE_SOURCES = ("github_issue", "github_pr", "deploy_state", "evidence")
UNCLAIMED_LABEL = "unclaimed"
LAUNCH_URL_PLACEHOLDER = "{launch_url}"


@dataclass(frozen=True)
class PacketRender:
    human_brief: str  # Slack mrkdwn; contains LAUNCH_URL_PLACEHOLDER for mint to fill
    handoff_input: LaunchHandoffCreateInput
    idempotency_key: str
    revision: str


def _compute_revision(
    dossier: Dossier,
    *,
    ask: str,
    acceptance_criteria: Sequence[Any],
    owner_user_id: str | None,
    target_tool: str,
) -> str:
    payload = json.dumps(
        {
            "dossier": dossier.to_dict(),
            "ask": ask,
            "acceptance_criteria": list(acceptance_criteria),
            "owner_user_id": owner_user_id,
            "target_tool": target_tool,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return sha256(payload.encode("utf-8")).hexdigest()[:_HASH_HEX_CHARS]


def _idempotency_key(job_ref: str, revision: str) -> str:
    room = _IDEMPOTENCY_KEY_MAX - len(revision) - 1
    return f"{job_ref[:room]}:{revision}"


def _narrative(dossier: Dossier) -> str:
    for section in dossier.sections:
        for item in section.items:
            if item.excerpt:
                excerpt, _, _ = _cut(item.excerpt, _NARRATIVE_CAP)
                return excerpt
    return "no gathered context"


def _evidence_refs(dossier: Dossier) -> list[str]:
    refs: list[str] = []
    for source in _EVIDENCE_SOURCES:
        for section in dossier.sections:
            if section.source != source:
                continue
            refs.extend(item.ref for item in section.items if item.ref)
    return refs[:_EVIDENCE_REFS]


def _prior_decisions(dossier: Dossier) -> str:
    for section in dossier.sections:
        if section.source == "decision" and section.items:
            decision = section.items[0]
            excerpt, _, _ = _cut(decision.excerpt, _NARRATIVE_CAP)
            return excerpt
    return "none on record"


def _omissions_note(dossier: Dossier) -> str:
    if not dossier.omissions:
        return ""
    total = sum(section.omitted_count for section in dossier.sections) or len(dossier.omissions)
    return f"context trimmed: {_plural(total, 'item')} omitted"


def _render_brief(
    dossier: Dossier,
    *,
    owner_label: str | None,
    ask: str,
    narrative_cap: int,
) -> str:
    narrative, _, _ = _cut(_narrative(dossier), narrative_cap)
    evidence = _evidence_refs(dossier)
    evidence_line = ", ".join(evidence) if evidence else "none gathered"
    ask_line = f"*Ask:* {ask}"
    note = _omissions_note(dossier)
    if note:
        ask_line = f"{ask_line}   ·   {note}"
    return "\n".join(
        [
            f"*{dossier.headline}* → {owner_label or UNCLAIMED_LABEL}",
            f"*What happened:* {narrative}",
            f"*Evidence:* {evidence_line}",
            f"*Prior decisions:* {_prior_decisions(dossier)}",
            ask_line,
            f"Launch: {LAUNCH_URL_PLACEHOLDER}",
        ]
    )


def _context_parts(dossier: Dossier) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    for section in dossier.sections:
        for item in section.items:
            parts.append(
                {
                    "source": section.source,
                    "ref": item.ref,
                    "title": item.title,
                    "excerpt": item.excerpt,
                    "truncated": item.truncated,
                    "omitted_chars": item.omitted_chars,
                }
            )
    if dossier.omissions:
        parts.append({"source": "omissions", "notes": list(dossier.omissions)})
    return parts


def _instructions(dossier: Dossier, *, ask: str) -> str:
    refs = ", ".join(_evidence_refs(dossier)) or "see context_parts"
    return (
        f"{ask}\n\n"
        f"Job: {dossier.job_ref} — {dossier.headline}. The gathered context "
        "is in this handoff's context_parts (fetch the full packet via the "
        "Illo MCP `illo_read` capability `handoff.get` before starting). "
        f"Key refs: {refs}."
    )


def compose_packet(
    dossier: Dossier,
    *,
    org_id: str,
    ask: str,
    acceptance_criteria: Sequence[Any] = (),
    owner_user_id: str | None = None,
    owner_label: str | None = None,
    target_tool: str = TARGET_CODEX,
    repo_origin_url: str | None = None,
    branch_hint: str | None = None,
    source_surface: str = "illo",
    source_ref: Mapping[str, Any] | None = None,
    created_by_user_id: str | None = None,
) -> PacketRender:
    """Render one dossier into the dual-audience packet.

    Pure: no I/O, no clock. The caller (mint, slice 05) creates the
    ``LaunchHandoff`` row, fills :data:`LAUNCH_URL_PLACEHOLDER` in the brief
    with the real launch URL, and posts.
    """
    clean_ask = " ".join(str(ask or "").split())
    if not clean_ask:
        raise ValueError("compose_packet requires an ask")

    revision = _compute_revision(
        dossier,
        ask=clean_ask,
        acceptance_criteria=acceptance_criteria,
        owner_user_id=owner_user_id,
        target_tool=target_tool,
    )

    brief = _render_brief(dossier, owner_label=owner_label, ask=clean_ask, narrative_cap=_NARRATIVE_CAP)
    if len(brief) > BRIEF_CHAR_CAP:
        # Tighten the narrative, never the omissions note or the launch line.
        overage = len(brief) - BRIEF_CHAR_CAP
        tightened = max(_MIN_NARRATIVE, _NARRATIVE_CAP - overage)
        brief = _render_brief(dossier, owner_label=owner_label, ask=clean_ask, narrative_cap=tightened)

    metadata: dict[str, Any] = {"revision": revision, "job_ref": dossier.job_ref}
    if owner_user_id:
        metadata["owner_user_id"] = owner_user_id

    handoff_input = LaunchHandoffCreateInput(
        org_id=org_id,
        created_by_user_id=created_by_user_id,
        title=dossier.headline,
        instructions=_instructions(dossier, ask=clean_ask),
        target_tool=target_tool,
        summary=clean_ask,
        source_surface=source_surface,
        source_ref=dict(source_ref or {}),
        context_parts=_context_parts(dossier),
        acceptance_criteria=list(acceptance_criteria),
        repo_origin_url=repo_origin_url,
        branch_hint=branch_hint,
        idempotency_key=_idempotency_key(dossier.job_ref, revision),
        metadata=metadata,
    )
    return PacketRender(
        human_brief=brief,
        handoff_input=handoff_input,
        idempotency_key=handoff_input.idempotency_key or "",
        revision=revision,
    )
