"""Illo Brain — Dossier assembly core (pure).

A *dossier* is the rendered answer to "what does the assignee need to know
about this job right now?", assembled from already-fetched fragments
(:class:`SourcePiece`) into an ordered, budgeted, deduplicated view. It is a
VIEW, never a store: job truth stays in the owning record/idea (see
``specs/illo-handoff-packets/README.md`` — one owner per concept), and a
dossier is recomputed from truth at mint time.

This module is the single owner of assembly policy, and it is pure — no
I/O, no DB, no clock — so the policy is unit-testable and deterministic:
the same pieces produce the same dossier regardless of input order.

Truncation honesty (the run-1057 lesson): every cut this module makes is
visible in the output — a per-item ``… (+N chars)`` marker when an excerpt
is shortened, a per-section ``omitted_count`` when items are dropped, and a
human-readable ``omissions`` list on the dossier. Budgets govern *content*;
the markers, section headers, and omissions footer are exempt bounded
overhead — trimming the evidence of a cut to fit a budget would defeat the
point. Duplicates collapsed by ``(source, ref)`` are NOT omissions: the
dossier still points at that object once.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# Fixed section order, highest priority first. Unknown sources render after
# these, alphabetically, and shed first under total-budget pressure.
SOURCE_PRIORITY: tuple[str, ...] = (
    "record",
    "slack_thread",
    "github_issue",
    "github_pr",
    "deploy_state",
    "decision",
    "evidence",
)

# Reserved headroom so an appended truncation marker never busts the cap it
# is reporting. Wide enough for "… (+NNNNNNN chars)".
_MARKER_RESERVE = 24
# Floor for a re-cut excerpt when a single item must fit a tiny source
# budget — below this the excerpt stops being information, so the floor wins
# over the cap (honesty over amputation) and the section may run slightly
# hot.
_MIN_EXCERPT_CHARS = 40

_OLDEST = datetime.min.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class SourcePiece:
    """One already-fetched fragment of job truth (input to assembly)."""

    source: str
    ref: str
    title: str
    body: str
    ts: datetime | None = None
    weight: int = 0  # caller-supplied salience hint; higher sorts first


@dataclass(frozen=True)
class DossierItem:
    ref: str
    title: str
    excerpt: str  # marker-FREE kept text; render via rendered_excerpt
    truncated: bool  # body was cut
    omitted_chars: int  # raw chars removed by the cut (0 when not truncated)

    @property
    def rendered_excerpt(self) -> str:
        """The excerpt with its visible truncation marker, for text audiences.

        Structured consumers (and honest re-cutters — see compose) work from
        the marker-free ``excerpt`` + ``omitted_chars`` so markers are never
        parsed back or double-counted.
        """
        if not self.truncated:
            return self.excerpt
        return f"{self.excerpt}{render_marker(self.omitted_chars)}"


@dataclass(frozen=True)
class DossierSection:
    source: str
    items: tuple[DossierItem, ...]
    omitted_count: int  # pieces dropped from this source by budget pressure


@dataclass(frozen=True)
class DossierBudget:
    """Explicit caps for one assembly. All values are hard content caps."""

    total_chars: int = 12_000
    source_chars: int = 2_400
    source_overrides: Mapping[str, int] = field(default_factory=dict)
    max_items_per_source: int = 6
    excerpt_chars: int = 600

    def __post_init__(self) -> None:
        for name in ("total_chars", "source_chars", "max_items_per_source"):
            if int(getattr(self, name)) <= 0:
                raise ValueError(f"DossierBudget.{name} must be positive")
        # Below the floor a cap cannot hold both content and an honest
        # marker, so tiny caps are a config error, not a silent overshoot.
        if int(self.excerpt_chars) < _MIN_EXCERPT_CHARS:
            raise ValueError(f"DossierBudget.excerpt_chars must be >= {_MIN_EXCERPT_CHARS}")
        for source, cap in dict(self.source_overrides).items():
            if int(cap) <= 0:
                raise ValueError(f"DossierBudget.source_overrides[{source!r}] must be positive")

    def chars_for(self, source: str) -> int:
        return int(self.source_overrides.get(source, self.source_chars))

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_chars": self.total_chars,
            "source_chars": self.source_chars,
            "source_overrides": dict(self.source_overrides),
            "max_items_per_source": self.max_items_per_source,
            "excerpt_chars": self.excerpt_chars,
        }


@dataclass(frozen=True)
class Dossier:
    job_ref: str
    headline: str
    sections: tuple[DossierSection, ...]
    total_chars: int  # len(render_text()) — the real artifact, markers included
    budget: DossierBudget
    omissions: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_ref": self.job_ref,
            "headline": self.headline,
            "sections": [
                {
                    "source": section.source,
                    "omitted_count": section.omitted_count,
                    "items": [
                        {
                            "ref": item.ref,
                            "title": item.title,
                            "excerpt": item.excerpt,
                            "truncated": item.truncated,
                            "omitted_chars": item.omitted_chars,
                        }
                        for item in section.items
                    ],
                }
                for section in self.sections
            ],
            "total_chars": self.total_chars,
            "budget": self.budget.to_dict(),
            "omissions": list(self.omissions),
        }

    def render_text(self) -> str:
        return _render(self.job_ref, self.headline, self.sections, self.omissions)


def _normalize_text(value: Any) -> str:
    """Collapse whitespace runs to single spaces; the dossier is a compact view."""
    return " ".join(str(value or "").split())


def _source_rank(source: str) -> tuple[int, str]:
    try:
        return (SOURCE_PRIORITY.index(source), source)
    except ValueError:
        return (len(SOURCE_PRIORITY), source)


def _ts_utc(piece: SourcePiece) -> datetime:
    ts = piece.ts if piece.ts is not None else _OLDEST
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)


def _ordered_pieces(pieces: Sequence[SourcePiece]) -> list[SourcePiece]:
    """weight desc, then newest first, then ref/title/body asc — via stable sorts.

    The normalized body participates in the final tiebreak so that two
    same-ref pieces with identical metadata but different bodies pick the
    same dedupe winner regardless of input order.
    """
    ordered = sorted(
        pieces,
        key=lambda p: (_normalize_text(p.ref), _normalize_text(p.title), _normalize_text(p.body)),
    )
    ordered.sort(key=_ts_utc, reverse=True)
    ordered.sort(key=lambda p: p.weight, reverse=True)
    return ordered


def render_marker(omitted_chars: int) -> str:
    """The one visible truncation-marker format. Render-only — never parsed."""
    return f" … (+{omitted_chars} chars)"


def cut_text(text: str, cap: int) -> tuple[str, bool, int]:
    """Cut ``text`` so that head + marker fit within ``cap`` chars.

    Returns ``(head, truncated, omitted_chars)`` with the head MARKER-FREE —
    callers render via :func:`render_marker` / ``rendered_excerpt`` so
    re-cutters can accumulate honest totals instead of re-cutting rendered
    markers. Cuts land on a word boundary; a single token longer than the
    cap is hard-cut (no boundary to respect), and the marker still reports
    the loss. ``cap`` below ``_MIN_EXCERPT_CHARS`` is a caller bug.
    """
    if len(text) <= cap:
        return text, False, 0
    keep = max(1, cap - _MARKER_RESERVE)
    head = text[:keep]
    boundary = head.rfind(" ")
    if boundary > 0:
        head = head[:boundary]
    head = head.rstrip()
    return head, True, len(text) - len(head)


def _item_from_piece(piece: SourcePiece, *, excerpt_cap: int) -> DossierItem:
    excerpt, truncated, omitted = cut_text(_normalize_text(piece.body), excerpt_cap)
    return DossierItem(
        ref=_normalize_text(piece.ref),
        title=_normalize_text(piece.title),
        excerpt=excerpt,
        truncated=truncated,
        omitted_chars=omitted,
    )


def _render_item(item: DossierItem) -> str:
    return f"- [{item.ref}] {item.title}: {item.rendered_excerpt}"


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _render_section(section: DossierSection) -> str:
    suffix = f", {section.omitted_count} omitted" if section.omitted_count else ""
    lines = [f"## {section.source} ({_plural(len(section.items), 'item')}{suffix})"]
    lines.extend(_render_item(item) for item in section.items)
    return "\n".join(lines)


def _render(
    job_ref: str,
    headline: str,
    sections: Sequence[DossierSection],
    omissions: Sequence[str],
) -> str:
    # Empty sections stay on the Dossier for structured accounting (their
    # omitted_count must survive for downstream honest totals) but render
    # only through the omissions footer.
    parts = [f"# {headline}", f"job: {job_ref}"]
    parts.extend(_render_section(section) for section in sections if section.items)
    if omissions:
        parts.append("Omitted: " + "; ".join(omissions))
    return "\n\n".join(parts)


def _section_size(section: DossierSection) -> int:
    return len(_render_section(section))


def _shed_tail(section: DossierSection) -> DossierSection:
    return DossierSection(
        source=section.source,
        items=section.items[:-1],
        omitted_count=section.omitted_count + 1,
    )


def _fit_section_to_source_budget(
    section: DossierSection, *, source_piece: SourcePiece | None, cap: int
) -> DossierSection:
    while _section_size(section) > cap and len(section.items) > 1:
        section = _shed_tail(section)
    if _section_size(section) > cap and section.items and source_piece is not None:
        # One item left and still over: re-cut its excerpt against the real
        # rendered overhead (header + item line skeleton, empty excerpt).
        probe = DossierItem(
            ref=_normalize_text(source_piece.ref),
            title=_normalize_text(source_piece.title),
            excerpt="",
            truncated=False,
            omitted_chars=0,
        )
        overhead = _section_size(
            DossierSection(source=section.source, items=(probe,), omitted_count=section.omitted_count)
        )
        excerpt_cap = max(_MIN_EXCERPT_CHARS, cap - overhead)
        section = DossierSection(
            source=section.source,
            items=(_item_from_piece(source_piece, excerpt_cap=excerpt_cap),),
            omitted_count=section.omitted_count,
        )
    return section


def assemble_dossier(
    pieces: Sequence[SourcePiece],
    *,
    job_ref: str,
    budget: DossierBudget,
    headline: str | None = None,
) -> Dossier:
    """Assemble a deterministic, budgeted dossier from raw pieces.

    Policy, in order: dedupe by ``(source, ref)`` (first by ordering wins;
    empty refs are never deduped), rank within source by weight desc / ts
    desc / ref asc, cap items per source, cut excerpts at word boundaries
    with visible markers, fit each section to its source budget (dropping
    tail items, always keeping at least one), then fit the whole dossier to
    the total budget by shedding items from the lowest-priority section
    upward — never the highest-priority section's first item. Every drop is
    counted in ``omitted_count`` and surfaced in ``omissions``.
    """
    clean_job_ref = _normalize_text(job_ref)
    if not clean_job_ref:
        raise ValueError("assemble_dossier requires a job_ref")

    by_source: dict[str, list[SourcePiece]] = {}
    for piece in pieces:
        source = _normalize_text(piece.source) or "other"
        by_source.setdefault(source, []).append(piece)

    sections: list[DossierSection] = []
    lead_piece_by_source: dict[str, SourcePiece] = {}
    for source in sorted(by_source, key=_source_rank):
        seen_refs: set[str] = set()
        deduped: list[SourcePiece] = []
        for piece in _ordered_pieces(by_source[source]):
            ref = _normalize_text(piece.ref)
            if ref and ref in seen_refs:
                continue  # same object, already cited — not an omission
            if ref:
                seen_refs.add(ref)
            deduped.append(piece)

        kept = deduped[: budget.max_items_per_source]
        if kept:
            lead_piece_by_source[source] = kept[0]
        section = DossierSection(
            source=source,
            items=tuple(_item_from_piece(piece, excerpt_cap=budget.excerpt_chars) for piece in kept),
            omitted_count=len(deduped) - len(kept),
        )
        sections.append(
            _fit_section_to_source_budget(
                section, source_piece=lead_piece_by_source.get(source), cap=budget.chars_for(source)
            )
        )

    clean_headline = _normalize_text(headline)
    if not clean_headline:
        for section in sections:
            if section.items:
                clean_headline = section.items[0].title or section.items[0].ref
                break
    if not clean_headline:
        clean_headline = clean_job_ref

    # Total budget: shed from the lowest-priority section upward. The
    # highest-priority section always keeps its first item. Only sections
    # that still render (have items) count toward the total; empty sections
    # surface as omission lines, which are exempt footer overhead.
    header_size = len(f"# {clean_headline}") + len(f"job: {clean_job_ref}") + 2

    def content_size(current: Sequence[DossierSection]) -> int:
        return header_size + sum(_section_size(s) + 2 for s in current if s.items)

    fitted: list[DossierSection] = list(sections)
    while content_size(fitted) > budget.total_chars:
        victim_index = None
        for index in range(len(fitted) - 1, -1, -1):
            if not fitted[index].items:
                continue
            if index == 0 and len(fitted[index].items) == 1:
                continue  # never shed the top section's first item
            victim_index = index
            break
        if victim_index is None:
            break  # nothing shed-able remains; bounded overhead stands
        fitted[victim_index] = _shed_tail(fitted[victim_index])

    omissions: list[str] = []
    final_sections: list[DossierSection] = []
    for section in fitted:
        if not section.items and not section.omitted_count:
            continue  # nothing kept, nothing lost — no accounting to carry
        final_sections.append(section)
        if section.items and section.omitted_count:
            omissions.append(f"{section.source}: {_plural(section.omitted_count, 'item')} omitted (budget)")
        elif section.omitted_count:
            omissions.append(f"{section.source}: all {_plural(section.omitted_count, 'item')} omitted (budget)")

    rendered = _render(clean_job_ref, clean_headline, final_sections, omissions)
    return Dossier(
        job_ref=clean_job_ref,
        headline=clean_headline,
        sections=tuple(final_sections),
        total_chars=len(rendered),
        budget=budget,
        omissions=tuple(omissions),
    )
