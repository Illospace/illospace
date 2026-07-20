"""Deterministic Slack declaration flow for Domain-1 chantiers.

Language understanding remains deliberately small: a declaration must arrive
as an explicit Slack app mention containing the word ``chantier``.  The parser
extracts the mechanical record contract and leaves the normal Illo run to make
the visible threaded reply and, for engineering work, attempt the GitHub parent
mirror with the existing GitHub tools.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any, Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.db.models.domain import DomainRecord
from brain.systems.chantiers import (
    CHANTIER_OBJECT_KEY,
    MISSING_NEXT_STEP,
    TRACKER_DOMAIN_SLUG,
    is_active_chantier,
    list_all_chantier_records,
    match_active_chantier,
    merge_chantier_refs,
    slugify_chantier,
    title_key,
)
from brain.systems.user_domains.service import AsyncDomainService, DomainNotFound


SLACK_APP_MENTION_ORIGIN = "slack.app_mention"
GITHUB_PARENT_MIRROR_TOOL = "create_github_issue"
GITHUB_SUB_ISSUE_TOOL = "add_github_sub_issue"

_CHANTIER_KEYWORD_RE = re.compile(r"\bchantier\b", re.IGNORECASE)
_PLAIN_DECLARE_PREFIX_RE = re.compile(
    r"(?:\b(?:declare|déclare)(?:\s+(?:a|the|new|un))?"
    r"|\b(?:create|open|start)(?:\s+(?:a|new))?"
    r"|\bkick\s+off(?:\s+(?:a|new))?"
    r"|\b(?:new|nouveau))\s*$",
    re.IGNORECASE,
)
_SLACK_MENTION_RE = re.compile(r"<@[A-Z0-9]+>", re.IGNORECASE)
_URL_RE = re.compile(r"https?://[^\s<>()]+", re.IGNORECASE)
_KIND_RE = re.compile(
    r"\bkind\s*:\s*(feature|incident|quality|gtm|sunset)\b",
    re.IGNORECASE,
)
_LABELED_VALUE_BOUNDARY = (
    r"(?=\s+(?:kind|owner|goal|next[_ ]step)\s*:|\s+https?://|$)"
)
_OWNER_RE = re.compile(
    r"\bowner\s*:\s*(?P<value>.+?)" + _LABELED_VALUE_BOUNDARY,
    re.IGNORECASE,
)
_NEXT_STEP_RE = re.compile(
    r"\bnext[_ ]step\s*:\s*(?P<value>.+?)" + _LABELED_VALUE_BOUNDARY,
    re.IGNORECASE,
)
_GOAL_LABEL_RE = re.compile(
    r"\bgoal\s*:\s*(?P<value>.+?)" + _LABELED_VALUE_BOUNDARY,
    re.IGNORECASE,
)
_DONE_MEANS_RE = re.compile(r"\bdone\s+means\b", re.IGNORECASE)
_GITHUB_ISSUE_URL_RE = re.compile(
    r"^https?://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/"
    r"(?P<repo>[A-Za-z0-9_.-]+)/issues/(?P<number>[1-9][0-9]*)(?:[/?#].*)?$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ChantierDeclaration:
    """Best-effort structured interpretation of one Slack sentence."""

    title: str
    slug: str
    goal: str
    goal_is_explicit: bool
    kind: str
    kind_is_explicit: bool
    owner: str | None
    next_step: str | None
    refs: tuple[dict[str, str], ...]
    mirror_repo_suggestion: str | None


@dataclass(frozen=True)
class ChantierDeclareResult:
    """Persisted result passed to the Slack run for echo/mirror completion."""

    operation: str
    domain_id: int
    record_id: int
    version: int
    data: dict[str, Any]
    goal_was_inferred: bool
    kind_was_guessed: bool
    needs_next_step: bool
    owner_suggestion: str
    mirror_status: str
    mirror_repo_suggestion: str | None

    def as_metadata(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "domain_id": self.domain_id,
            "record_id": self.record_id,
            "version": self.version,
            "data": self.data,
            "goal_was_inferred": self.goal_was_inferred,
            "kind_was_guessed": self.kind_was_guessed,
            "needs_next_step": self.needs_next_step,
            "owner_suggestion": self.owner_suggestion,
            "mirror_status": self.mirror_status,
            "mirror_repo_suggestion": self.mirror_repo_suggestion,
        }


def parse_chantier_declaration(text: str) -> ChantierDeclaration | None:
    """Parse the narrow ``chantier: ...`` declaration contract.

    Merely discussing or querying chantiers is not a declaration.  A colon is
    the clearest door, while imperative/plain forms such as ``declare chantier
    reliability`` are also accepted when they leave a non-question title.
    """

    raw = _SLACK_MENTION_RE.sub(" ", str(text or ""))
    keyword = _CHANTIER_KEYWORD_RE.search(raw)
    if keyword is None:
        return None
    prefix = raw[: keyword.start()].strip().casefold()
    tail = raw[keyword.end() :].strip()
    used_colon = tail.startswith(":")
    tail = tail.lstrip(" \t:-–—")
    if not tail or (not used_colon and _PLAIN_DECLARE_PREFIX_RE.search(prefix) is None):
        return None

    urls = [_trim_url(url) for url in _URL_RE.findall(tail)]
    working = _URL_RE.sub(" ", tail)

    kind_match = _KIND_RE.search(working)
    explicit_kind = kind_match.group(1).lower() if kind_match else None
    if kind_match:
        working = _remove_match(working, kind_match)

    owner, working = _extract_labeled_value(_OWNER_RE, working)
    next_step, working = _extract_labeled_value(_NEXT_STEP_RE, working)
    labeled_goal, working = _extract_labeled_value(_GOAL_LABEL_RE, working)

    working = _compact_spaces(working)
    done_match = _DONE_MEANS_RE.search(working)
    if labeled_goal:
        title = _clean_title(working)
        goal = _done_means_goal(labeled_goal)
        goal_is_explicit = True
    elif done_match:
        title = _clean_title(working[: done_match.start()])
        goal = _done_means_goal(working[done_match.start() :])
        goal_is_explicit = True
    else:
        title = _clean_title(working)
        goal = ""
        goal_is_explicit = False

    if not title:
        return None
    title = title[:500].rstrip()
    slug = _slugify(title)
    if not slug:
        return None
    if not goal:
        goal = f"Done means {title} reaches its stated outcome."
    goal = goal[:4000].rstrip()

    guessed_kind = explicit_kind or _guess_kind(f"{title} {goal}")
    refs = tuple(_refs_from_urls(urls))
    mirror_repo = _first_github_repo(urls)
    return ChantierDeclaration(
        title=title,
        slug=slug,
        goal=goal,
        goal_is_explicit=goal_is_explicit,
        kind=guessed_kind,
        kind_is_explicit=explicit_kind is not None,
        owner=_clean_optional(owner, limit=120),
        next_step=_clean_optional(next_step, limit=500, one_line=True),
        refs=refs,
        mirror_repo_suggestion=mirror_repo,
    )


async def maybe_declare_chantier_from_slack(
    session: AsyncSession,
    *,
    org_id: str,
    actor_user_id: str | None,
    origin: str,
    text: str,
    channel_id: str | None = None,
    thread_ts: str | None = None,
) -> ChantierDeclareResult | None:
    """Create/update a chantier only through the explicit app-mention door."""

    if str(origin or "").strip() != SLACK_APP_MENTION_ORIGIN:
        return None
    if await _thread_belongs_to_chantier(
        session,
        org_id=org_id,
        channel_id=channel_id,
        thread_ts=thread_ts,
    ):
        return None
    declaration = parse_chantier_declaration(text)
    if declaration is None:
        return None
    return await upsert_chantier_declaration(
        session,
        org_id=org_id,
        actor_user_id=actor_user_id,
        declaration=declaration,
    )


async def _thread_belongs_to_chantier(
    session: AsyncSession,
    *,
    org_id: str,
    channel_id: str | None,
    thread_ts: str | None,
) -> bool:
    clean_thread_ts = str(thread_ts or "").strip()
    if not clean_thread_ts:
        return False

    service = AsyncDomainService(session)
    domain = next(
        (item for item in await service.list_domains(org_id) if item.slug == TRACKER_DOMAIN_SLUG),
        None,
    )
    if domain is None:
        return False
    try:
        object_type = await service.get_object_type(domain.id, CHANTIER_OBJECT_KEY)
    except DomainNotFound:
        return False
    records = (
        await session.scalars(
            select(DomainRecord).where(
                DomainRecord.org_id == org_id,
                DomainRecord.domain_id == domain.id,
                DomainRecord.object_type_id == object_type.id,
                DomainRecord.archived_at.is_(None),
            )
        )
    ).all()
    clean_channel_id = str(channel_id or "").strip()
    suffix = (
        f":{clean_channel_id}:{clean_thread_ts}"
        if clean_channel_id
        else f":{clean_thread_ts}"
    )
    for record in records:
        if not is_active_chantier(record):
            continue
        refs = (record.data or {}).get("refs")
        for ref in refs if isinstance(refs, list) else ():
            if not isinstance(ref, Mapping):
                continue
            if str(ref.get("source") or "").casefold() != "slack":
                continue
            if str(ref.get("ref") or "").strip().endswith(suffix):
                return True
    return False


async def upsert_chantier_declaration(
    session: AsyncSession,
    *,
    org_id: str,
    actor_user_id: str | None,
    declaration: ChantierDeclaration,
) -> ChantierDeclareResult:
    """Idempotently persist a declaration by stable slug or obvious title."""

    service = AsyncDomainService(session)
    domain = next(
        (item for item in await service.list_domains(org_id) if item.slug == TRACKER_DOMAIN_SLUG),
        None,
    )
    if domain is None:
        raise DomainNotFound(f"Domain '{TRACKER_DOMAIN_SLUG}' not found")

    # Serialize the application-level slug/title upsert.  The chantier schema
    # predates this flow and intentionally has no new database constraint, so a
    # lock on its object type is the migration-free concurrency boundary.
    await service.get_object_type(domain.id, CHANTIER_OBJECT_KEY, for_update=True)
    records = await list_all_chantier_records(
        service,
        org_id=org_id,
        domain_id=domain.id,
        order="updated_asc",
    )
    match = match_active_chantier(
        records,
        slug=declaration.slug,
        title=declaration.title,
        goal=declaration.goal,
        refs=declaration.refs,
    )
    existing = match.record if match is not None else None
    reason = "Slack chantier declaration"

    if existing is None:
        data: dict[str, Any] = {
            "slug": declaration.slug,
            "title": declaration.title,
            "goal": declaration.goal,
            "kind": declaration.kind,
            "state": "exploring",
            "refs": [dict(item) for item in declaration.refs],
            "next_step": declaration.next_step or MISSING_NEXT_STEP,
        }
        if declaration.owner:
            data["owner"] = declaration.owner
        record = await service.create_record(
            org_id,
            domain.id,
            CHANTIER_OBJECT_KEY,
            data=data,
            actor_id=actor_user_id,
            actor_kind="human",
            reason=reason,
        )
        operation = "created"
    else:
        current = dict(existing.data or {})
        patch: dict[str, Any] = {
            "refs": _merge_refs(current.get("refs"), declaration.refs),
        }
        if declaration.goal_is_explicit or not current.get("goal"):
            patch["goal"] = declaration.goal
        if declaration.kind_is_explicit or not current.get("kind"):
            patch["kind"] = declaration.kind
        if declaration.owner:
            patch["owner"] = declaration.owner
        if declaration.next_step:
            patch["next_step"] = declaration.next_step
        record = await service.update_record(
            org_id,
            domain.id,
            existing.id,
            data_patch=patch,
            expected_version=existing.version,
            actor_id=actor_user_id,
            actor_kind="human",
            reason=reason,
        )
        operation = "updated"

    persisted = dict(record.data or {})
    next_step = str(persisted.get("next_step") or "").strip()
    owner = str(persisted.get("owner") or "").strip()
    parent_issue = str(persisted.get("parent_issue") or "").strip()
    engineering = str(persisted.get("kind") or declaration.kind) != "gtm"
    if parent_issue:
        mirror_status = "linked"
    elif engineering:
        # The #328 native sub-issue interface is registered in the same run
        # tool surface as create_github_issue.  The run completes this external
        # write so token/repo selection stays in the audited GitHub tool lane.
        mirror_status = "ready"
    else:
        mirror_status = "not_required"
    return ChantierDeclareResult(
        operation=operation,
        domain_id=domain.id,
        record_id=record.id,
        version=record.version,
        data=persisted,
        goal_was_inferred=not declaration.goal_is_explicit,
        kind_was_guessed=not declaration.kind_is_explicit,
        needs_next_step=not next_step or next_step == MISSING_NEXT_STEP,
        owner_suggestion=owner or "builder TBD",
        mirror_status=mirror_status,
        mirror_repo_suggestion=declaration.mirror_repo_suggestion,
    )


def apply_chantier_declare_run_contract(
    trigger_payload: dict[str, Any],
    *,
    result: ChantierDeclareResult | None = None,
    error: str | None = None,
) -> None:
    """Make the normal Slack run finish the declaration in the same thread."""

    payload = dict(trigger_payload.get("payload") or {})
    metadata = dict(payload.get("metadata") or {})
    slack_trigger = dict(metadata.get("slack_trigger") or {})
    response_target = dict(slack_trigger.get("response_target") or {})
    response_target["thread_ts"] = str(
        slack_trigger.get("thread_ts") or slack_trigger.get("message_ts") or ""
    ).strip() or None
    slack_trigger["response_target"] = response_target
    metadata["slack_trigger"] = slack_trigger

    base_message = str(payload.get("run_message") or "").strip()
    if result is not None:
        metadata["chantier_declare"] = result.as_metadata()
        run_message = _successful_declare_run_message(base_message, result)
    else:
        clean_error = str(error or "unknown persistence error").strip()
        metadata["chantier_declare"] = {"operation": "failed", "error": clean_error}
        run_message = _failed_declare_run_message(base_message, clean_error)
    trigger_payload["payload"] = {
        **payload,
        "metadata": metadata,
        "run_message": run_message,
    }


def _successful_declare_run_message(
    base_message: str,
    result: ChantierDeclareResult,
) -> str:
    data = result.data
    mirror_lines: list[str]
    if result.mirror_status == "ready":
        repo_hint = result.mirror_repo_suggestion or "none; infer from explicit team/repo context"
        mirror_lines = [
            "This is an engineering chantier with no parent_issue yet.",
            f"Attempt the parent mirror with {GITHUB_PARENT_MIRROR_TOOL}; repo hint: {repo_hint}.",
            (
                "First search open and closed issues in the target repo for the exact chantier slug "
                "or '[Chantier] <title>'; link an existing match instead of creating a duplicate."
            ),
            "Use title '[Chantier] <title>' and include the slug, Done-means goal, and key refs.",
            (
                "On success, update this same Domain record's parent_issue with expected_version, "
                f"then attach pasted GitHub issue refs with {GITHUB_SUB_ISSUE_TOOL}."
            ),
            (
                "If repo/token/tooling is unavailable, do not undo or fail the declaration; say "
                "'mirror pending: <specific reason>' in the Slack confirmation."
            ),
        ]
    elif result.mirror_status == "linked":
        mirror_lines = [
            f"The parent mirror already exists as {data.get('parent_issue')}; never create it again."
        ]
    else:
        mirror_lines = ["This non-engineering chantier does not require a GitHub parent mirror."]

    follow_up = (
        "Ask the teammate for `next_step` because none could be inferred."
        if result.needs_next_step
        else f"Echo next_step: {data.get('next_step')}"
    )
    instructions = [
        "A Slack chantier declaration has already been persisted deterministically.",
        "Do not create another chantier record and do not change its stable slug.",
        (
            f"Persistence result: {result.operation} Domain {result.domain_id} "
            f"record {result.record_id} v{result.version}."
        ),
        f"Slug: {data.get('slug')}",
        f"Title: {data.get('title')}",
        f"Goal: {data.get('goal')}",
        f"Kind {'guess' if result.kind_was_guessed else 'explicit'}: {data.get('kind')}",
        f"Owner suggestion (builder-first): {result.owner_suggestion}",
        (
            "If the owner suggestion is builder TBD, use current explicit issue/PR ownership evidence "
            "to suggest the likely implementation builder; never delay or undo the declare for this."
        ),
        *mirror_lines,
        (
            "Reply with post_slack_reply in the declaration thread. State created/updated, slug, "
            "goal, kind guess/explicit kind, builder-first owner suggestion, mirror outcome, and next_step."
        ),
        follow_up,
        "Never claim a GitHub mirror was opened unless create_github_issue returned its issue number and URL.",
        "",
        "Persisted declaration JSON:",
        json.dumps(result.as_metadata(), default=str, sort_keys=True),
        "",
        base_message,
    ]
    return "\n".join(instructions).strip()


def _failed_declare_run_message(base_message: str, error: str) -> str:
    return "\n".join(
        [
            "A Slack message used the explicit chantier declaration door, but persistence failed.",
            "Do not claim that a chantier or GitHub mirror was created.",
            f"Failure: {error}",
            (
                "Reply with post_slack_reply in the declaration thread and say "
                f"'chantier declare failed: {error}'."
            ),
            "",
            base_message,
        ]
    ).strip()


def _matching_record(
    records: Sequence[DomainRecord],
    declaration: ChantierDeclaration,
) -> DomainRecord | None:
    match = match_active_chantier(
        records,
        slug=declaration.slug,
        title=declaration.title,
        goal=declaration.goal,
        refs=declaration.refs,
    )
    return match.record if match is not None else None


def _merge_refs(existing: Any, incoming: Sequence[Mapping[str, str]]) -> list[dict[str, str]]:
    return merge_chantier_refs(existing, incoming)


def _refs_from_urls(urls: Sequence[str]) -> list[dict[str, str]]:
    refs: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for url in urls:
        github = _GITHUB_ISSUE_URL_RE.match(url)
        if github:
            repo = f"{github.group('owner')}/{github.group('repo')}"
            number = github.group("number")
            item = {
                "source": "github",
                "ref": f"github:{repo}:issue:{number}",
                "title": f"GitHub issue {repo}#{number}",
            }
        elif ".slack.com/archives/" in url.casefold():
            item = {"source": "slack", "ref": url}
        else:
            item = {"source": "url", "ref": url}
        key = (item["source"], item["ref"])
        if key not in seen:
            refs.append(item)
            seen.add(key)
    return refs


def _first_github_repo(urls: Sequence[str]) -> str | None:
    for url in urls:
        match = _GITHUB_ISSUE_URL_RE.match(url)
        if match:
            return f"{match.group('owner')}/{match.group('repo')}"
    return None


def _extract_labeled_value(pattern: re.Pattern[str], text: str) -> tuple[str | None, str]:
    match = pattern.search(text)
    if match is None:
        return None, text
    value = str(match.group("value") or "").strip(" \t,;–—-")
    return value or None, _remove_match(text, match)


def _remove_match(text: str, match: re.Match[str]) -> str:
    return f"{text[:match.start()]} {text[match.end():]}"


def _done_means_goal(value: str) -> str:
    clean = _compact_spaces(value).strip(" \t,;–—-")
    match = _DONE_MEANS_RE.match(clean)
    if match:
        clean = clean[match.end() :].strip()
    return f"Done means {clean}" if clean else ""


def _clean_title(value: str) -> str:
    return _compact_spaces(value).strip(" \t,;:–—-")


def _compact_spaces(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _clean_optional(value: str | None, *, limit: int, one_line: bool = False) -> str | None:
    if value is None:
        return None
    clean = _compact_spaces(value) if one_line else str(value).strip()
    return clean[:limit].rstrip() or None


def _slugify(value: str) -> str:
    return slugify_chantier(value)


def _title_key(value: str) -> str:
    return title_key(value)


def _guess_kind(value: str) -> str:
    words = _title_key(value)
    if any(token in words for token in ("incident", "outage", "rollback", "hotfix", "sev ")):
        return "incident"
    if any(
        token in words
        for token in (
            "hardening",
            "quality",
            "reliability",
            "security",
            "test ",
            "testing",
            "performance",
            "alert",
        )
    ):
        return "quality"
    if any(token in words for token in ("marketing", "campaign", "sales", "seo", "growth")):
        return "gtm"
    return "feature"


def _trim_url(value: str) -> str:
    return str(value or "").rstrip(".,;:!?]}")


__all__ = [
    "CHANTIER_OBJECT_KEY",
    "GITHUB_PARENT_MIRROR_TOOL",
    "GITHUB_SUB_ISSUE_TOOL",
    "MISSING_NEXT_STEP",
    "TRACKER_DOMAIN_SLUG",
    "ChantierDeclaration",
    "ChantierDeclareResult",
    "apply_chantier_declare_run_contract",
    "maybe_declare_chantier_from_slack",
    "parse_chantier_declaration",
    "upsert_chantier_declaration",
]
