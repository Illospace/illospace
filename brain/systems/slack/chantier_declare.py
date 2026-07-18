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

from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.db.models.domain import DomainRecord
from brain.systems.user_domains.service import AsyncDomainService, DomainNotFound


TRACKER_DOMAIN_SLUG = "github-ticket-tracker"
CHANTIER_OBJECT_KEY = "chantier"
SLACK_APP_MENTION_ORIGIN = "slack.app_mention"
MISSING_NEXT_STEP = "Clarify the next most valuable step."
GITHUB_PARENT_MIRROR_TOOL = "create_github_issue"
GITHUB_SUB_ISSUE_TOOL = "add_github_sub_issue"

_CHANTIER_KEYWORD_RE = re.compile(r"\bchantier\b", re.IGNORECASE)
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
_QUESTION_PREFIXES = (
    "are there",
    "list",
    "show",
    "status",
    "what",
    "which",
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
    if not tail or (
        not used_colon
        and (
            prefix.startswith(_QUESTION_PREFIXES)
            or tail.casefold().startswith(_QUESTION_PREFIXES)
        )
    ):
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
) -> ChantierDeclareResult | None:
    """Create/update a chantier only through the explicit app-mention door."""

    if str(origin or "").strip() != SLACK_APP_MENTION_ORIGIN:
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
    records = await service.list_records(
        org_id,
        domain.id,
        object_key=CHANTIER_OBJECT_KEY,
        limit=500,
        order="updated_asc",
    )
    existing = _matching_record(records, declaration)
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
            "title": declaration.title,
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
    incoming_title = _title_key(declaration.title)
    for record in sorted(records, key=lambda item: item.id):
        data = dict(record.data or {})
        if str(data.get("slug") or "").casefold() == declaration.slug.casefold():
            return record
        stored_title = str(data.get("title") or record.title or "")
        if _title_key(stored_title) == incoming_title:
            return record
        if _slugify(stored_title) == declaration.slug:
            return record
    return None


def _merge_refs(existing: Any, incoming: Sequence[Mapping[str, str]]) -> list[dict[str, str]]:
    merged: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    candidates = list(existing) if isinstance(existing, list) else []
    candidates.extend(incoming)
    for item in candidates:
        if not isinstance(item, Mapping):
            continue
        source = str(item.get("source") or "").strip()
        ref = str(item.get("ref") or "").strip()
        key = (source, ref)
        if not source or not ref or key in seen:
            continue
        clean = {"source": source, "ref": ref}
        title = str(item.get("title") or "").strip()
        if title:
            clean["title"] = title
        merged.append(clean)
        seen.add(key)
    return merged


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
    slug = re.sub(r"[^a-z0-9]+", "-", str(value or "").casefold()).strip("-")
    return re.sub(r"-+", "-", slug)[:80].rstrip("-")


def _title_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


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
