"""Shared chantier identity, hygiene, active-set, and retirement machinery."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
import re
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from brain.kernel.common.coercion import coerce_datetime
from brain.platform.db.models.domain import DomainRecord
from brain.systems.user_domains.service import (
    AsyncDomainService,
    DomainError,
    DomainNotFound,
)


TRACKER_DOMAIN_SLUG = "github-ticket-tracker"
CHANTIER_OBJECT_KEY = "chantier"
ACTIVE_CHANTIER_STATES = frozenset({"exploring", "building", "shipping", "verifying"})
MISSING_NEXT_STEP = "Clarify the next most valuable step."

_TITLE_TOKEN_RE = re.compile(r"[a-z0-9]+")
_VERSION_TOKEN_RE = re.compile(r"v[0-9]+")
_TITLE_STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "chantier",
        "for",
        "of",
        "project",
        "the",
        "to",
        "workstream",
    }
)
_GOAL_STOP_WORDS = _TITLE_STOP_WORDS | {
    "done",
    "means",
    "outcome",
    "reaches",
    "stated",
}


class ChantierMatchError(DomainError):
    """A declare identity resolves to more than one active chantier."""


@dataclass(frozen=True)
class ChantierMatch:
    """One high-confidence active chantier match and the evidence used."""

    record: DomainRecord
    evidence: str


@dataclass(frozen=True)
class ChantierMergeResult:
    """Durable result of retiring one duplicate into its canonical chantier."""

    status: str
    domain_id: int
    canonical: DomainRecord
    duplicate: DomainRecord
    active_record_ids: tuple[int, ...]

    @property
    def active_chantier_count(self) -> int:
        return len(self.active_record_ids)


def _data(record_or_data: Any) -> dict[str, Any]:
    if isinstance(record_or_data, Mapping):
        return dict(record_or_data)
    return dict(getattr(record_or_data, "data", None) or {})


def _text(value: Any) -> str:
    return str(value or "").strip()


def slugify_chantier(value: Any) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", _text(value).casefold()).strip("-")
    return re.sub(r"-+", "-", slug)[:80].rstrip("-")


def title_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", _text(value).casefold()).strip()


def is_active_chantier(record_or_data: Any) -> bool:
    """Return whether a chantier belongs in active counts and digest sweeps."""

    data = _data(record_or_data)
    return (
        _text(data.get("state")).casefold() in ACTIVE_CHANTIER_STATES
        and not _text(data.get("superseded_by"))
    )


def is_superseded_chantier(record_or_data: Any) -> bool:
    """Return whether a duplicate has a durable canonical-chantier link."""

    return bool(_text(_data(record_or_data).get("superseded_by")))


def active_chantier_records(records: Sequence[Any]) -> list[Any]:
    """Filter active, non-superseded chantiers while preserving query order."""

    return [record for record in records if is_active_chantier(record)]


async def list_all_chantier_records(
    service: AsyncDomainService,
    *,
    org_id: str,
    domain_id: int,
    order: str = "updated_asc",
) -> list[DomainRecord]:
    """Page through the complete chantier set for identity and exact counts."""

    records: list[DomainRecord] = []
    offset = 0
    while True:
        page = list(
            await service.list_records(
                org_id,
                domain_id,
                object_key=CHANTIER_OBJECT_KEY,
                limit=500,
                order=order,
                offset=offset,
            )
        )
        records.extend(page)
        if len(page) < 500:
            return records
        offset += len(page)


def placeholder_goal(title: Any) -> str:
    return f"Done means {_text(title)} reaches its stated outcome."


def is_placeholder_chantier(data: Mapping[str, Any]) -> bool:
    """Detect the vaporware-shaped placeholder that declare must not create."""

    title = _text(data.get("title"))
    refs = data.get("refs")
    return (
        bool(title)
        and (not isinstance(refs, list) or not refs)
        and not _text(data.get("owner"))
        and _text(data.get("goal")).casefold() == placeholder_goal(title).casefold()
        and _text(data.get("next_step")).casefold() == MISSING_NEXT_STEP.casefold()
    )


def _ref_identities(refs: Sequence[Mapping[str, Any]] | Any) -> set[tuple[str, str]]:
    if not isinstance(refs, (list, tuple)):
        return set()
    return {
        (_text(item.get("source")).casefold(), _text(item.get("ref")))
        for item in refs
        if isinstance(item, Mapping)
        and _text(item.get("source"))
        and _text(item.get("ref"))
    }


def merge_chantier_refs(existing: Any, incoming: Any) -> list[dict[str, str]]:
    """Merge typed refs by source/ref identity without discarding display titles."""

    merged: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    candidates = list(existing) if isinstance(existing, list) else []
    candidates.extend(list(incoming) if isinstance(incoming, (list, tuple)) else [])
    for item in candidates:
        if not isinstance(item, Mapping):
            continue
        source = _text(item.get("source"))
        ref = _text(item.get("ref"))
        identity = (source.casefold(), ref)
        if not source or not ref or identity in seen:
            continue
        clean = {"source": source, "ref": ref}
        item_title = _text(item.get("title"))
        if item_title:
            clean["title"] = item_title
        merged.append(clean)
        seen.add(identity)
    return merged


def _tokens(value: Any, *, goal: bool = False) -> set[str]:
    stop_words = _GOAL_STOP_WORDS if goal else _TITLE_STOP_WORDS
    return {
        token
        for token in _TITLE_TOKEN_RE.findall(_text(value).casefold())
        if len(token) > 1 and token not in stop_words and not _VERSION_TOKEN_RE.fullmatch(token)
    }


def _high_confidence_token_match(left: set[str], right: set[str]) -> bool:
    if not left or not right:
        return False
    overlap = left & right
    union = left | right
    smaller = min(len(left), len(right))
    if len(overlap) >= 2 and len(overlap) / len(union) >= 0.72:
        return True
    # Long generated titles often wrap a stable three-token root cause with
    # planning/version vocabulary. Full containment of that root is strong;
    # one- or two-token containment is too broad to auto-attach.
    return smaller >= 3 and len(overlap) == smaller


def _text_match_evidence(
    record: DomainRecord,
    *,
    slug: str,
    title: str,
    goal: str | None,
) -> str | None:
    data = _data(record)
    stored_title = _text(data.get("title") or record.title)
    stored_slug = _text(data.get("slug"))
    incoming_identity = _tokens(f"{slug} {title}")
    stored_identity = _tokens(f"{stored_slug} {stored_title}")
    if _high_confidence_token_match(incoming_identity, stored_identity):
        return "high_confidence_title_or_slug"

    incoming_goal = _text(goal)
    stored_goal = _text(data.get("goal"))
    if (
        incoming_goal
        and stored_goal
        and incoming_goal.casefold() != placeholder_goal(title).casefold()
        and stored_goal.casefold() != placeholder_goal(stored_title).casefold()
        and _high_confidence_token_match(
            _tokens(incoming_goal, goal=True),
            _tokens(stored_goal, goal=True),
        )
    ):
        return "high_confidence_root_cause"
    return None


def match_active_chantier(
    records: Sequence[DomainRecord],
    *,
    slug: str,
    title: str,
    goal: str | None = None,
    refs: Sequence[Mapping[str, Any]] = (),
) -> ChantierMatch | None:
    """Resolve exact refs/identity first, then one high-confidence text match."""

    active = active_chantier_records(records)
    incoming_slug = slugify_chantier(slug)
    incoming_title = title_key(title)
    incoming_refs = _ref_identities(refs)

    ref_matches = [
        record
        for record in active
        if incoming_refs & _ref_identities(_data(record).get("refs"))
    ]
    exact_matches: list[DomainRecord] = []
    for record in active:
        data = _data(record)
        stored_slug = slugify_chantier(data.get("slug"))
        stored_title = _text(data.get("title") or record.title)
        if (
            stored_slug == incoming_slug
            or title_key(stored_title) == incoming_title
            or slugify_chantier(stored_title) == incoming_slug
            or stored_slug == slugify_chantier(title)
        ):
            exact_matches.append(record)

    primary_ids = {record.id for record in [*ref_matches, *exact_matches]}
    if len(primary_ids) > 1:
        ids = ", ".join(str(record_id) for record_id in sorted(primary_ids))
        raise ChantierMatchError(
            f"Chantier declaration identity resolves to different active records: {ids}"
        )
    if primary_ids:
        record_id = next(iter(primary_ids))
        record = next(record for record in active if record.id == record_id)
        evidence = "overlapping_refs" if record in ref_matches else "exact_slug_or_title"
        return ChantierMatch(record=record, evidence=evidence)

    fuzzy: list[tuple[DomainRecord, str]] = []
    for record in active:
        evidence = _text_match_evidence(record, slug=slug, title=title, goal=goal)
        if evidence:
            fuzzy.append((record, evidence))
    if len(fuzzy) > 1:
        ids = ", ".join(
            str(record.id)
            for record, _ in sorted(fuzzy, key=lambda item: item[0].id)
        )
        raise ChantierMatchError(
            f"Chantier declaration text matches multiple active records: {ids}"
        )
    if fuzzy:
        record, evidence = fuzzy[0]
        return ChantierMatch(record=record, evidence=evidence)
    return None


async def merge_chantier_records(
    session: AsyncSession,
    *,
    org_id: str,
    duplicate_record_id: int,
    canonical_record_id: int,
    expected_duplicate_version: int,
    expected_canonical_version: int,
    reason: str,
    actor_user_id: str | None = None,
    run_id: int | None = None,
) -> ChantierMergeResult:
    """Fold duplicate refs into a canonical chantier, then pause and supersede it."""

    if duplicate_record_id == canonical_record_id:
        raise DomainError("duplicate_record_id and canonical_record_id must be different")
    clean_reason = _text(reason)
    if not clean_reason:
        raise DomainError("merge_chantier requires a non-empty reason")

    service = AsyncDomainService(session)
    domain = next(
        (
            candidate
            for candidate in await service.list_domains(org_id)
            if candidate.slug == TRACKER_DOMAIN_SLUG
        ),
        None,
    )
    if domain is None:
        raise DomainNotFound(f"Domain '{TRACKER_DOMAIN_SLUG}' not found")
    object_type = await service.get_object_type(
        domain.id,
        CHANTIER_OBJECT_KEY,
        for_update=True,
    )
    canonical = await service.get_record(org_id, domain.id, canonical_record_id)
    duplicate = await service.get_record(org_id, domain.id, duplicate_record_id)
    if canonical.object_type_id != object_type.id or duplicate.object_type_id != object_type.id:
        raise DomainError("merge_chantier record ids must both identify chantier records")
    if canonical.archived_at is not None or duplicate.archived_at is not None:
        raise DomainError("merge_chantier cannot operate on archived records")

    canonical_data = _data(canonical)
    duplicate_data = _data(duplicate)
    canonical_slug = _text(canonical_data.get("slug"))
    if not canonical_slug:
        raise DomainError("canonical chantier has no stable slug")
    existing_target = _text(duplicate_data.get("superseded_by"))
    if existing_target:
        if existing_target != canonical_slug:
            raise DomainError(
                f"duplicate chantier is already superseded by {existing_target!r}"
            )
        if _text(duplicate_data.get("state")).casefold() != "paused":
            raise DomainError("superseded duplicate chantier must be paused")
        records = await list_all_chantier_records(
            service,
            org_id=org_id,
            domain_id=domain.id,
        )
        return ChantierMergeResult(
            status="already_merged",
            domain_id=domain.id,
            canonical=canonical,
            duplicate=duplicate,
            active_record_ids=tuple(record.id for record in active_chantier_records(records)),
        )

    if not is_active_chantier(canonical):
        raise DomainError("canonical chantier must be active and not superseded")
    if canonical.version != expected_canonical_version:
        raise DomainError(
            "Canonical record version mismatch: "
            f"expected {expected_canonical_version}, current {canonical.version}"
        )
    if duplicate.version != expected_duplicate_version:
        raise DomainError(
            "Duplicate record version mismatch: "
            f"expected {expected_duplicate_version}, current {duplicate.version}"
        )

    merged_refs = merge_chantier_refs(
        canonical_data.get("refs"),
        duplicate_data.get("refs"),
    )
    if merged_refs != canonical_data.get("refs"):
        canonical = await service.update_record(
            org_id,
            domain.id,
            canonical.id,
            data_patch={"refs": merged_refs},
            expected_version=expected_canonical_version,
            actor_id=actor_user_id,
            actor_kind="human" if actor_user_id else "agent",
            run_id=run_id,
            reason=f"{clean_reason} | absorbed refs from duplicate chantier {duplicate.id}",
        )

    duplicate = await service.update_record(
        org_id,
        domain.id,
        duplicate.id,
        data_patch={"state": "paused", "superseded_by": canonical_slug},
        expected_version=expected_duplicate_version,
        actor_id=actor_user_id,
        actor_kind="human" if actor_user_id else "agent",
        run_id=run_id,
        reason=f"{clean_reason} | superseded by chantier {canonical_slug}",
    )
    records = await list_all_chantier_records(
        service,
        org_id=org_id,
        domain_id=domain.id,
    )
    return ChantierMergeResult(
        status="merged",
        domain_id=domain.id,
        canonical=canonical,
        duplicate=duplicate,
        active_record_ids=tuple(record.id for record in active_chantier_records(records)),
    )


def latest_source_movement(
    chantier: Any,
    *,
    members_by_external_id: Mapping[str, Any],
) -> datetime | None:
    """Return the newest source timestamp among a chantier and its loaded members."""

    chantier_data = _data(chantier)
    timestamps = [
        timestamp
        for timestamp in (
            coerce_datetime(chantier_data.get("updated_at"), utc=True),
            coerce_datetime(chantier_data.get("created_at"), utc=True),
        )
        if timestamp is not None
    ]
    refs = chantier_data.get("refs")
    if isinstance(refs, (list, tuple)):
        for item in refs:
            if not isinstance(item, Mapping):
                continue
            if _text(item.get("source")).casefold() != "github":
                continue
            member = members_by_external_id.get(_text(item.get("ref")))
            member_data = _data(member)
            timestamps.extend(
                timestamp
                for timestamp in (
                    coerce_datetime(member_data.get("updated_at"), utc=True),
                    coerce_datetime(member_data.get("created_at"), utc=True),
                )
                if timestamp is not None
            )
    return max(timestamps, default=None)


__all__ = [
    "ACTIVE_CHANTIER_STATES",
    "CHANTIER_OBJECT_KEY",
    "MISSING_NEXT_STEP",
    "TRACKER_DOMAIN_SLUG",
    "ChantierMatch",
    "ChantierMatchError",
    "ChantierMergeResult",
    "active_chantier_records",
    "is_active_chantier",
    "is_placeholder_chantier",
    "is_superseded_chantier",
    "latest_source_movement",
    "list_all_chantier_records",
    "match_active_chantier",
    "merge_chantier_records",
    "merge_chantier_refs",
    "placeholder_goal",
    "slugify_chantier",
    "title_key",
]
