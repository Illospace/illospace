"""Section selection helpers for context-pack rendering."""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Any

SECTION_ORDER = (
    "thread_summary",
    "handoffs",
    "user_team_facts",
    "selected_memories",
    "selected_skills",
    "policy_constraints",
    "approvals",
    "budget",
    "output_contract",
    "tool_permissions",
    "uncertainty",
)


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return str(value)


def validate_context_pack(context_pack: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(context_pack, Mapping):
        return {"schema_version": 1, "sections": {}, "render_order": []}
    payload = copy.deepcopy(dict(context_pack))
    payload.setdefault("schema_version", 1)
    payload.setdefault("sections", {})
    payload.setdefault("render_order", [name for name in SECTION_ORDER if name in payload.get("sections", {})])
    return payload

COORDINATOR_SECTION_ORDER = tuple(SECTION_ORDER)
WORKER_SECTION_ORDER = (
    "thread_summary",
    "handoffs",
    "user_team_facts",
    "selected_memories",
    "selected_skills",
    "policy_constraints",
    "uncertainty",
)

DEFAULT_OMISSION_REASONS = {
    "user_team_facts": "worker prompt receives only task-relevant tenant facts from the coordinator boundary",
    "approvals": "approval state is enforced by run/tool policy outside the worker prompt",
    "budget": "budget state remains coordinator-owned for the worker phase",
    "tool_permissions": "worker tool permissions are injected by the worker runtime, not copied from the coordinator pack",
}


def _digest_payload(payload: Mapping[str, Any], *, length: int = 24) -> str:
    raw = json.dumps(_jsonable(payload), sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:length]


def _section_token_estimate(section: Mapping[str, Any]) -> int:
    budget = section.get("token_budget") if isinstance(section.get("token_budget"), Mapping) else {}
    try:
        return int(budget.get("estimated_tokens") or 0)
    except (TypeError, ValueError):
        return 0


def filter_context_pack_sections(
    context_pack: Mapping[str, Any] | None,
    *,
    include_sections: Iterable[str],
    role: str,
    omission_reasons: Mapping[str, str] | None = None,
) -> dict[str, Any] | None:
    """Return a context-pack copy containing only the requested sections.

    The original durable pack digest is preserved as ``source_digest`` while the
    reduced view receives its own deterministic digest. Omitted sections carry a
    compact rationale for audit/evals.
    """
    if not isinstance(context_pack, Mapping):
        return None

    sections = context_pack.get("sections")
    if not isinstance(sections, Mapping):
        return copy.deepcopy(dict(context_pack))

    include = [name for name in include_sections if name in sections]
    included_set = set(include)
    reasons = dict(DEFAULT_OMISSION_REASONS)
    reasons.update(omission_reasons or {})
    source_digest = context_pack.get("digest")

    filtered = copy.deepcopy(dict(context_pack))
    filtered["source_digest"] = source_digest
    filtered["render_role"] = role
    filtered["render_order"] = include
    filtered["sections"] = {
        name: copy.deepcopy(sections[name])
        for name in include
        if name in sections
    }
    if role != "coordinator":
        policy_section = filtered["sections"].get("policy_constraints")
        content = policy_section.get("content") if isinstance(policy_section, Mapping) else None
        if isinstance(content, Mapping) and "coordinator_instructions" in content:
            policy_section["content"] = {
                key: value for key, value in content.items()
                if key != "coordinator_instructions"
            }
    filtered["section_token_budget"] = {
        name: copy.deepcopy((sections[name] or {}).get("token_budget") or {})
        for name in include
        if name in sections
    }

    omitted = []
    for name in context_pack.get("render_order") or SECTION_ORDER:
        section = sections.get(name)
        if name in included_set or not isinstance(section, Mapping):
            continue
        omitted.append({
            "name": name,
            "title": section.get("title") or name.replace("_", " ").title(),
            "source": section.get("source"),
            "estimated_tokens": _section_token_estimate(section),
            "reason": reasons.get(name) or f"omitted from {role} context view",
        })

    filtered["omitted_sections"] = omitted
    filtered["total_estimated_tokens"] = sum(
        _section_token_estimate(section)
        for section in filtered["sections"].values()
        if isinstance(section, Mapping)
    )
    filtered["digest"] = _digest_payload({
        "schema_version": filtered.get("schema_version"),
        "compiler_version": filtered.get("compiler_version"),
        "source_digest": source_digest,
        "render_role": role,
        "render_order": filtered["render_order"],
        "sections": filtered["sections"],
        "omitted_sections": omitted,
    })
    return validate_context_pack(filtered)
