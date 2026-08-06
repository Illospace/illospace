"""Launch handoff service, URL helpers, and object-reference payloads."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
from typing import Any
from urllib.parse import quote, unquote, urlencode, urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.db.models.launch_handoff import LaunchHandoff
from brain.platform.provider_alerts import parse_rollbar_alert
from brain.systems.cortex.thread_links import public_app_base_url

LAUNCH_HANDOFF_OBJECT_TYPE = "launch_handoff"
TARGET_CODEX = "codex"
TARGET_CLAUDE = "claude"
LAUNCH_HANDOFF_ROUTE_PREFIX = "/api/launch-handoffs"

_HANDOFF_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$")
_URL_CANDIDATE_RE = re.compile(
    r"https?://[^\s<>'\"\]\)]+|/api/launch-handoffs/[^\s<>'\"\]\)]+",
    re.IGNORECASE,
)
_TRAILING_PUNCTUATION = ".,;:!?"
_DERIVED_IDEMPOTENCY_KEY_PREFIX = "derived:launch-handoff:v1:"
_RETIRED_IDEMPOTENCY_KEY_PREFIX = "retired:lh:v1:"
_SYSTEM_METADATA_NAMESPACE = "_illo_system"
_IDEMPOTENCY_METADATA_NAMESPACE = "idempotency"
_DERIVED_KEY_KIND = "rollbar_slack"
_RETIREMENT_REASON = "derived_key_holder_not_actionable"


class LaunchHandoffError(ValueError):
    """Base error for launch handoff service failures."""


class LaunchHandoffNotFound(LaunchHandoffError):
    """Raised when a handoff is missing or outside the caller's org."""


class _ReuseScope(StrEnum):
    STRICT = "strict"
    ACTIONABLE = "actionable"


@dataclass(frozen=True, slots=True)
class _IdempotencyDecision:
    key: str | None
    reuse_scope: _ReuseScope


@dataclass(frozen=True)
class LaunchHandoffCreateInput:
    org_id: str
    created_by_user_id: str | None
    title: str
    instructions: str
    target_tool: str = TARGET_CODEX
    summary: str | None = None
    source_surface: str = "illo"
    source_ref: dict[str, Any] = field(default_factory=dict)
    context_parts: list[dict[str, Any]] = field(default_factory=list)
    acceptance_criteria: list[Any] = field(default_factory=list)
    repo_origin_url: str | None = None
    branch_hint: str | None = None
    idempotency_key: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


def _iso(value: Any) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else (str(value) if value else None)


def _clean_optional_string(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _required_string(value: Any, field_name: str) -> str:
    text = _clean_optional_string(value)
    if not text:
        raise LaunchHandoffError(f"Launch handoff requires {field_name}")
    return text


def _json_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _json_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _json_object_list(value: Any) -> list[dict[str, Any]]:
    return [dict(item) for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _uuid_or_none(value: Any) -> str | None:
    text = _clean_optional_string(value)
    if not text:
        return None
    try:
        return str(uuid.UUID(text))
    except ValueError:
        return None


def derive_launch_handoff_idempotency_key(
    source_ref: dict[str, Any] | None,
    *,
    created_by_user_id: Any = None,
) -> str | None:
    """Derive from Rollbar Slack text and ``created_by_user_id`` or ``unassigned``."""
    slack_trigger = _json_dict(_json_dict(source_ref).get("slack_trigger"))
    text = slack_trigger.get("text")
    if not isinstance(text, str):
        return None
    alert = parse_rollbar_alert(text)
    if alert is None:
        return None
    owner = (_clean_optional_string(created_by_user_id) or "unassigned").casefold()
    digest = sha256(f"{alert.signature}\0{owner}".encode("utf-8")).hexdigest()
    return f"{_DERIVED_IDEMPOTENCY_KEY_PREFIX}{digest}"


def _idempotency_metadata(metadata: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    updated = _json_dict(metadata)
    system = _json_dict(updated.get(_SYSTEM_METADATA_NAMESPACE))
    idempotency = _json_dict(system.get(_IDEMPOTENCY_METADATA_NAMESPACE))
    system[_IDEMPOTENCY_METADATA_NAMESPACE] = idempotency
    updated[_SYSTEM_METADATA_NAMESPACE] = system
    return updated, idempotency


def _record_refire(row: LaunchHandoff, now: datetime) -> None:
    updated, idempotency = _idempotency_metadata(row.metadata_)
    raw_count = idempotency.get("refire_count", 0)
    try:
        current_count = int(raw_count)
    except (TypeError, ValueError):
        current_count = 0
    idempotency["refire_count"] = max(0, current_count) + 1
    idempotency["last_refire_at"] = now.isoformat()
    row.metadata_ = updated
    row.updated_at = now


def _retire_derived_key(row: LaunchHandoff, now: datetime) -> None:
    """Retire one non-actionable derived-key holder without erasing its history."""
    retired_key = _clean_optional_string(row.idempotency_key)
    updated, idempotency = _idempotency_metadata(row.metadata_)
    if (
        not retired_key
        or not retired_key.startswith(_DERIVED_IDEMPOTENCY_KEY_PREFIX)
        or idempotency.get("key_kind") != _DERIVED_KEY_KIND
    ):
        raise LaunchHandoffError("Refusing to retire a caller-supplied idempotency key")

    retired_at = now.isoformat()
    idempotency.update(
        {
            "retired_key": retired_key,
            "retired_at": retired_at,
            "retirement_reason": _RETIREMENT_REASON,
        }
    )
    digest = sha256(f"{retired_key}\0{row.id}\0{retired_at}".encode("utf-8")).hexdigest()
    row.idempotency_key = f"{_RETIRED_IDEMPOTENCY_KEY_PREFIX}{digest}"
    row.metadata_ = updated
    row.updated_at = now


def _resolve_idempotency(
    handoff_input: LaunchHandoffCreateInput,
    source_ref: dict[str, Any],
    *,
    derive_rollbar_idempotency: bool,
) -> _IdempotencyDecision:
    explicit_key = _clean_optional_string(handoff_input.idempotency_key)
    if explicit_key:
        return _IdempotencyDecision(explicit_key, _ReuseScope.STRICT)
    if derive_rollbar_idempotency:
        return _IdempotencyDecision(
            derive_launch_handoff_idempotency_key(
                source_ref,
                created_by_user_id=handoff_input.created_by_user_id,
            ),
            _ReuseScope.ACTIONABLE,
        )
    return _IdempotencyDecision(None, _ReuseScope.STRICT)


def _is_actionable(row: LaunchHandoff, now: datetime) -> bool:
    expires_at = row.expires_at
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return row.status == "open" and (expires_at is None or expires_at > now)


def parse_member_agent_targets(raw: str | None) -> dict[str, str]:
    """Parse ``ILLO_MEMBER_AGENT_TARGETS`` into canonical UUID-keyed targets."""
    targets: dict[str, str] = {}
    for chunk in str(raw or "").split(","):
        entry = chunk.strip()
        if not entry:
            continue
        if "=" not in entry:
            raise LaunchHandoffError(
                "ILLO_MEMBER_AGENT_TARGETS entries must use <user-uuid>=<target>"
            )
        user_id, target = (part.strip() for part in entry.split("=", 1))
        try:
            canonical_user_id = str(uuid.UUID(user_id))
        except ValueError as exc:
            raise LaunchHandoffError(
                f"ILLO_MEMBER_AGENT_TARGETS user id must be a UUID: {user_id or '<empty>'}"
            ) from exc
        if not target:
            raise LaunchHandoffError(
                f"ILLO_MEMBER_AGENT_TARGETS target is empty for user {canonical_user_id}"
            )
        targets[canonical_user_id] = target.lower()
    return targets


def agent_target_for_member(
    user_id: Any,
    targets: dict[str, str],
    *,
    default: str = TARGET_CODEX,
) -> str:
    """Look up a member's configured target, falling back for unknown owners."""
    canonical_user_id = _uuid_or_none(user_id)
    fallback = (_clean_optional_string(default) or TARGET_CODEX).lower()
    if not canonical_user_id:
        return fallback
    return (_clean_optional_string(targets.get(canonical_user_id)) or fallback).lower()


def _strip_candidate(value: Any) -> str:
    cleaned = str(value or "").strip()
    while cleaned and cleaned[-1] in _TRAILING_PUNCTUATION:
        cleaned = cleaned[:-1]
    return cleaned


def _clean_handoff_id(value: Any) -> str | None:
    text = unquote(str(value or "")).strip().rstrip(_TRAILING_PUNCTUATION)
    if not text:
        return None
    return text if _HANDOFF_ID_RE.match(text) else None


def handoff_id_from_reference(value: Any, *, allow_raw_id: bool = False) -> str | None:
    text = _strip_candidate(value)
    if not text:
        return None
    if allow_raw_id and not any(part in text for part in ("/", "?", "#", "://")):
        return _clean_handoff_id(text)

    parts = urlsplit(text)
    path = parts.path or text
    if path.startswith(f"{LAUNCH_HANDOFF_ROUTE_PREFIX}/"):
        tail = path[len(LAUNCH_HANDOFF_ROUTE_PREFIX) + 1:]
        return _clean_handoff_id(tail.split("/", 1)[0])
    return None


def extract_launch_handoff_reference_values(text: str) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for match in _URL_CANDIDATE_RE.finditer(str(text or "")):
        candidate = _strip_candidate(match.group(0))
        if not candidate or candidate in seen:
            continue
        if handoff_id_from_reference(candidate):
            values.append(candidate)
            seen.add(candidate)
    return values


def launch_handoff_route_for_id(handoff_id: Any, *, target_tool: str = TARGET_CODEX) -> str:
    target = str(target_tool or TARGET_CODEX).strip().lower()
    return f"{LAUNCH_HANDOFF_ROUTE_PREFIX}/{quote(str(handoff_id), safe='')}/launch?target={quote(target, safe='')}"


def launch_handoff_url_for_id(handoff_id: Any, *, target_tool: str = TARGET_CODEX) -> str:
    return f"{public_app_base_url()}{launch_handoff_route_for_id(handoff_id, target_tool=target_tool)}"


def codex_prompt_for_handoff(row: LaunchHandoff) -> str:
    title = str(row.title or "Illo launch handoff").strip()
    return (
        f"Pick up Illo launch handoff {row.id}: {title}\n\n"
        "Use the Illo MCP `illo_read` tool with capability `handoff.get` and "
        f"arguments {{\"handoff_id\":\"{row.id}\"}} to fetch the full context, "
        "source references, instructions, and acceptance criteria before changing code."
    )


def claude_prompt_for_handoff(row: LaunchHandoff) -> str:
    title = str(row.title or "Illo launch handoff").strip()
    return (
        f"Pick up Illo launch handoff {row.id} in this Claude Code session: {title}\n\n"
        "Use the Illo MCP `illo_read` tool with capability `handoff.get` and "
        f"arguments {{\"handoff_id\":\"{row.id}\"}} to fetch the full context, "
        "source references, instructions, and acceptance criteria before changing code."
    )


def codex_deep_link_for_handoff(row: LaunchHandoff) -> str:
    params: dict[str, str] = {"prompt": codex_prompt_for_handoff(row)}
    repo_origin_url = _clean_optional_string(row.repo_origin_url)
    if repo_origin_url:
        params["originUrl"] = repo_origin_url
    return f"codex://threads/new?{urlencode(params)}"


async def create_launch_handoff(
    session: AsyncSession,
    handoff_input: LaunchHandoffCreateInput,
    *,
    derive_rollbar_idempotency: bool = False,
) -> LaunchHandoff:
    row, _created = await create_launch_handoff_with_status(
        session,
        handoff_input,
        derive_rollbar_idempotency=derive_rollbar_idempotency,
    )
    return row


async def create_launch_handoff_with_status(
    session: AsyncSession,
    handoff_input: LaunchHandoffCreateInput,
    *,
    derive_rollbar_idempotency: bool = False,
) -> tuple[LaunchHandoff, bool]:
    """Create a handoff, or return the existing row on an idempotency hit.

    The launch tool may opt into actionable Rollbar reuse. All other callers
    get strict reuse only when they supply an idempotency key.

    The boolean is the caller's noise gate: a REUSED row (``False``) means
    this content already went out, so the caller owes no new announcement.
    """
    clean_org_id = _required_string(handoff_input.org_id, "org_id")
    clean_title = _required_string(handoff_input.title, "title")
    clean_instructions = _required_string(handoff_input.instructions, "instructions")
    clean_target_tool = (_clean_optional_string(handoff_input.target_tool) or TARGET_CODEX).lower()
    clean_source_ref = _json_dict(handoff_input.source_ref)
    clean_metadata = _json_dict(handoff_input.metadata)
    clean_metadata.pop(_SYSTEM_METADATA_NAMESPACE, None)
    decision = _resolve_idempotency(
        handoff_input,
        clean_source_ref,
        derive_rollbar_idempotency=derive_rollbar_idempotency,
    )
    if decision.key and decision.reuse_scope is _ReuseScope.ACTIONABLE:
        clean_metadata, idempotency = _idempotency_metadata(clean_metadata)
        idempotency["key_kind"] = _DERIVED_KEY_KIND

    now = datetime.now(timezone.utc)
    existing = None
    if decision.key:
        holder = await session.scalar(
            select(LaunchHandoff).where(
                LaunchHandoff.org_id == clean_org_id,
                LaunchHandoff.idempotency_key == decision.key,
            )
        )
        if holder is not None:
            if decision.reuse_scope is _ReuseScope.STRICT or _is_actionable(holder, now):
                existing = holder
            else:
                _retire_derived_key(holder, now)
                await session.flush()

    if existing is not None:
        _record_refire(existing, now)
        await session.flush()
        return existing, False

    row = LaunchHandoff(
        org_id=clean_org_id,
        created_by_user_id=_uuid_or_none(handoff_input.created_by_user_id),
        source_surface=_clean_optional_string(handoff_input.source_surface) or "illo",
        source_ref=clean_source_ref,
        target_tool=clean_target_tool,
        title=clean_title,
        summary=_clean_optional_string(handoff_input.summary),
        instructions=clean_instructions,
        acceptance_criteria=_json_list(handoff_input.acceptance_criteria),
        context_parts=_json_object_list(handoff_input.context_parts),
        repo_origin_url=_clean_optional_string(handoff_input.repo_origin_url),
        branch_hint=_clean_optional_string(handoff_input.branch_hint),
        idempotency_key=decision.key,
        metadata_=clean_metadata,
    )
    session.add(row)
    await session.flush()
    return row, True


async def get_launch_handoff(
    session: AsyncSession,
    handoff_id: str,
    *,
    org_id: str | None = None,
) -> LaunchHandoff | None:
    row = await session.get(LaunchHandoff, str(handoff_id))
    if row is None:
        return None
    if org_id is not None and str(row.org_id) != str(org_id):
        return None
    return row


async def require_launch_handoff(
    session: AsyncSession,
    handoff_id: str,
    *,
    org_id: str | None = None,
) -> LaunchHandoff:
    row = await get_launch_handoff(session, handoff_id, org_id=org_id)
    if row is None:
        raise LaunchHandoffNotFound("Launch handoff not found")
    return row


async def mark_launch_handoff_launched(
    session: AsyncSession,
    row: LaunchHandoff,
    *,
    launched_by_user_id: str | None = None,
) -> LaunchHandoff:
    row.launch_count = int(row.launch_count or 0) + 1
    row.last_launched_at = datetime.now(timezone.utc)
    launched_by = _uuid_or_none(launched_by_user_id)
    if launched_by:
        row.last_launched_by_user_id = launched_by
    if row.status == "open":
        row.status = "launched"
    await session.flush()
    return row


def serialize_launch_handoff(row: LaunchHandoff, *, include_context: bool = True) -> dict[str, Any]:
    data = {
        "id": str(row.id),
        "org_id": str(row.org_id),
        "created_by_user_id": str(row.created_by_user_id) if row.created_by_user_id else None,
        "source_surface": row.source_surface,
        "source_ref": _json_dict(row.source_ref),
        "target_tool": row.target_tool,
        "title": row.title,
        "summary": row.summary,
        "repo_origin_url": row.repo_origin_url,
        "branch_hint": row.branch_hint,
        "status": row.status,
        "launch_count": int(row.launch_count or 0),
        "last_launched_by_user_id": str(row.last_launched_by_user_id) if row.last_launched_by_user_id else None,
        "last_launched_at": _iso(row.last_launched_at),
        "expires_at": _iso(row.expires_at),
        "idempotency_key": row.idempotency_key,
        "metadata": _json_dict(row.metadata_),
        "route": launch_handoff_route_for_id(row.id, target_tool=row.target_tool),
        "url": launch_handoff_url_for_id(row.id, target_tool=row.target_tool),
        "launch_url": launch_handoff_url_for_id(row.id, target_tool=row.target_tool),
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }
    if include_context:
        data["instructions"] = row.instructions
        data["acceptance_criteria"] = _json_list(row.acceptance_criteria)
        data["context_parts"] = _json_object_list(row.context_parts)
    return data


def launch_handoff_reference_payload(
    row: LaunchHandoff,
    *,
    original_ref: str | None = None,
) -> dict[str, Any]:
    summary = _clean_optional_string(row.summary) or _clean_optional_string(row.instructions)
    return {
        "type": "launch_handoff_reference",
        "object_type": LAUNCH_HANDOFF_OBJECT_TYPE,
        "object_id": str(row.id),
        "launch_handoff_id": str(row.id),
        "target_tool": row.target_tool,
        "handoff_status": row.status,
        "status": "available",
        "title": row.title,
        "preview_summary": summary,
        "source_surface": row.source_surface,
        "repo_origin_url": row.repo_origin_url,
        "branch_hint": row.branch_hint,
        "route": launch_handoff_route_for_id(row.id, target_tool=row.target_tool),
        "url": launch_handoff_url_for_id(row.id, target_tool=row.target_tool),
        "launch_url": launch_handoff_url_for_id(row.id, target_tool=row.target_tool),
        "original_ref": original_ref,
    }


def unavailable_launch_handoff_reference(
    *,
    original_ref: str,
    handoff_id: str | None = None,
) -> dict[str, Any]:
    return {
        "type": "launch_handoff_reference",
        "object_type": LAUNCH_HANDOFF_OBJECT_TYPE,
        "object_id": handoff_id,
        "launch_handoff_id": handoff_id,
        "status": "unavailable",
        "title": None,
        "preview_summary": None,
        "url": None,
        "launch_url": None,
        "original_ref": original_ref,
    }


async def resolve_launch_handoff_reference(
    session: AsyncSession,
    reference: str,
    *,
    org_id: str,
) -> dict[str, Any]:
    handoff_id = handoff_id_from_reference(reference, allow_raw_id=True)
    if not handoff_id:
        return unavailable_launch_handoff_reference(original_ref=str(reference or ""))
    row = await get_launch_handoff(session, handoff_id, org_id=org_id)
    if row is None:
        return unavailable_launch_handoff_reference(original_ref=reference, handoff_id=handoff_id)
    return launch_handoff_reference_payload(row, original_ref=reference)
