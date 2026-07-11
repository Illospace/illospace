"""Illo Brain — Packet composer (pure).

One :class:`~brain.systems.briefing.core.Dossier` in, one *packet* out:
(a) a short Slack-mrkdwn human brief and (b) a ready
:class:`~brain.systems.launch_handoffs.LaunchHandoffCreateInput` for the
assignee's own coding agent. Deterministic by design — briefs must be
trusted, and determinism is the trust floor; model-polished phrasing is a
later, eval-gated idea (see specs/illo-handoff-packets/slices/02).

Idempotency contract (cross-family review, findings 1–2 of both passes):
``revision`` hashes EVERYTHING packet-visible that the handoff row persists
or the launch consumes — dossier, ask, acceptance criteria, owner id,
target, repo origin, branch hint, and provenance — because
``create_launch_handoff`` silently returns the existing row on an
idempotency-key hit; any hash gap means a stale row can launch with the
wrong repo/branch/context. Deliberate exclusions, each safe for row reuse:
``org_id`` (the key is already org-scoped by a unique constraint),
``created_by_user_id`` (audit-only — identical content re-minted by a
different actor SHOULD reuse the row), and ``owner_label`` (display-only,
derived fresh from the hashed ``owner_user_id`` at post time). Callers must
pass STABLE provenance in ``source_ref`` (the origin thread, not the
triggering event).

Truncation honesty carries through: brief-level shortening works from the
structured ``DossierItem`` (marker-free excerpt + ``omitted_chars``) and
renders CUMULATIVE totals — a rendered marker is never parsed or re-cut, so
the Slack brief can never under-report what is missing. The brief cap is
enforced by a deterministic tighten cascade (narrative → ask → decisions →
evidence → headline), each step leaving a visible marker; the launch line
and the trimming note are never sacrificed.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

from brain.systems.briefing.core import (
    Dossier,
    DossierItem,
    _plural,
    cut_text,
    render_marker,
)
from brain.systems.launch_handoffs import TARGET_CODEX, LaunchHandoffCreateInput

# Slack chat.postMessage truncates around 4k; a brief should be a glance,
# not a page. Hard cap on the whole rendered brief — the tighten cascade
# guarantees it (floor-sum of all lines is well under the cap).
BRIEF_CHAR_CAP = 1_200
_HASH_HEX_CHARS = 16
_IDEMPOTENCY_KEY_MAX = 120  # LaunchHandoff.idempotency_key column cap
_EVIDENCE_REFS = 5

# Per-line content caps and the floors the tighten cascade may shrink to.
_CAPS = {"headline": 150, "owner": 60, "narrative": 300, "evidence": 300, "decisions": 300, "ask": 300}
_FLOORS = {"headline": 80, "owner": 60, "narrative": 60, "evidence": 90, "decisions": 60, "ask": 80}
_TIGHTEN_ORDER = ("narrative", "ask", "decisions", "evidence", "headline")

# Sections whose refs read as evidence links in the brief.
_EVIDENCE_SOURCES = ("github_issue", "github_pr", "deploy_state", "evidence")
UNCLAIMED_LABEL = "unclaimed"
LAUNCH_URL_PLACEHOLDER = "{launch_url}"
_LAUNCH_LINE = f"Launch: {LAUNCH_URL_PLACEHOLDER}"


@dataclass(frozen=True)
class PacketRender:
    human_brief: str  # Slack mrkdwn; ends with the launch placeholder line
    handoff_input: LaunchHandoffCreateInput
    idempotency_key: str
    revision: str


def fill_launch_url(human_brief: str, launch_url: str) -> str:
    """Replace the placeholder in the FINAL launch line only.

    Interpolated fields (ask, titles, …) may coincidentally contain the
    placeholder text; those occurrences stay literal. The brief's contract
    is that its last line is exactly the launch line — anything else is a
    composer bug, surfaced loudly here.
    """
    head, _, last = human_brief.rpartition("\n")
    if last != _LAUNCH_LINE:
        raise ValueError("brief does not end with the launch placeholder line")
    return f"{head}\nLaunch: {launch_url}"


def _canonical(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _canonical(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    return value


def _compute_revision(dossier: Dossier, packet_fields: Mapping[str, Any]) -> str:
    payload = json.dumps(
        {"dossier": dossier.to_dict(), **_canonical(dict(packet_fields))},
        sort_keys=True,
        ensure_ascii=False,
    )
    return sha256(payload.encode("utf-8")).hexdigest()[:_HASH_HEX_CHARS]


def _idempotency_key(job_ref: str, revision: str) -> str:
    room = _IDEMPOTENCY_KEY_MAX - len(revision) - 1
    return f"{job_ref[:room]}:{revision}"


def _shorten_text(text: str, cap: int) -> str:
    """Cut a plain string with a visible marker; marker included in cap."""
    head, truncated, omitted = cut_text(text, cap)
    return f"{head}{render_marker(omitted)}" if truncated else head


def _shorten_item(item: DossierItem, cap: int) -> str:
    """Shorten an item's excerpt with a CUMULATIVE honest marker.

    Works from the marker-free excerpt + structured ``omitted_chars`` so a
    dossier-level cut and a brief-level cut add up — the marker reports the
    total distance from the raw source, never just the last cut.
    """
    rendered = item.rendered_excerpt
    if len(rendered) <= cap:
        return rendered
    head, _, newly_cut = cut_text(item.excerpt, cap)
    return f"{head}{render_marker(item.omitted_chars + newly_cut)}"


def _narrative_item(dossier: Dossier) -> DossierItem | None:
    for section in dossier.sections:
        for item in section.items:
            if item.excerpt or item.truncated:
                return item
    return None


def _decision_item(dossier: Dossier) -> DossierItem | None:
    for section in dossier.sections:
        if section.source == "decision" and section.items:
            return section.items[0]
    return None


def _evidence_refs(dossier: Dossier) -> list[str]:
    refs: list[str] = []
    for source in _EVIDENCE_SOURCES:
        for section in dossier.sections:
            if section.source != source:
                continue
            refs.extend(item.ref for item in section.items if item.ref)
    return refs[:_EVIDENCE_REFS]


def _evidence_line(refs: Sequence[str], cap: int) -> str:
    if not refs:
        return "none gathered"
    shown = list(refs)

    def line_for(current: Sequence[str]) -> str:
        line = ", ".join(current)
        hidden = len(refs) - len(current)
        return f"{line}, +{hidden} more" if hidden else line

    while len(shown) > 1 and len(line_for(shown)) > cap:
        shown.pop()
    line = line_for(shown)
    return _shorten_text(line, cap) if len(line) > cap else line


def _omissions_note(dossier: Dossier) -> str:
    """Structured trimming totals — dropped items AND shortened excerpts.

    Sums run over ALL sections (including fully-shed empty ones, which the
    core keeps for exactly this accounting), never over rendered strings.
    """
    dropped = sum(section.omitted_count for section in dossier.sections)
    shortened = sum(1 for section in dossier.sections for item in section.items if item.truncated)
    parts: list[str] = []
    if dropped:
        parts.append(f"{_plural(dropped, 'item')} omitted")
    if shortened:
        parts.append(f"{_plural(shortened, 'excerpt')} shortened")
    return f"context trimmed: {', '.join(parts)}" if parts else ""


def _render_brief(
    dossier: Dossier,
    *,
    owner_label: str | None,
    ask: str,
    caps: Mapping[str, int],
) -> str:
    headline = _shorten_text(dossier.headline, caps["headline"])
    owner = _shorten_text(owner_label or UNCLAIMED_LABEL, caps["owner"])
    narrative_item = _narrative_item(dossier)
    narrative = _shorten_item(narrative_item, caps["narrative"]) if narrative_item else "no gathered context"
    decision_item = _decision_item(dossier)
    decisions = _shorten_item(decision_item, caps["decisions"]) if decision_item else "none on record"
    ask_line = f"*Ask:* {_shorten_text(ask, caps['ask'])}"
    note = _omissions_note(dossier)
    if note:
        ask_line = f"{ask_line}   ·   {note}"
    return "\n".join(
        [
            f"*{headline}* → {owner}",
            f"*What happened:* {narrative}",
            f"*Evidence:* {_evidence_line(_evidence_refs(dossier), caps['evidence'])}",
            f"*Prior decisions:* {decisions}",
            ask_line,
            _LAUNCH_LINE,
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
                    "excerpt": item.rendered_excerpt,
                    "truncated": item.truncated,
                    "omitted_chars": item.omitted_chars,
                }
            )
    if dossier.omissions:
        parts.append({"source": "omissions", "notes": list(dossier.omissions)})
    return parts


def _instructions(dossier: Dossier, *, ask: str) -> str:
    refs = _evidence_line(_evidence_refs(dossier), _CAPS["evidence"])
    if refs == "none gathered":
        refs = "see context_parts"
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
    ``LaunchHandoff`` row and fills the brief's launch line via
    :func:`fill_launch_url`.
    """
    clean_ask = " ".join(str(ask or "").split())
    if not clean_ask:
        raise ValueError("compose_packet requires an ask")

    revision = _compute_revision(
        dossier,
        {
            "ask": clean_ask,
            "acceptance_criteria": list(acceptance_criteria),
            "owner_user_id": owner_user_id,
            "target_tool": target_tool,
            "repo_origin_url": repo_origin_url,
            "branch_hint": branch_hint,
            "source_surface": source_surface,
            "source_ref": dict(source_ref or {}),
        },
    )

    caps = dict(_CAPS)
    brief = _render_brief(dossier, owner_label=owner_label, ask=clean_ask, caps=caps)
    for dimension in _TIGHTEN_ORDER:
        if len(brief) <= BRIEF_CHAR_CAP:
            break
        caps[dimension] = _FLOORS[dimension]
        brief = _render_brief(dossier, owner_label=owner_label, ask=clean_ask, caps=caps)

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
