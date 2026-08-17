"""Preservation intent and evidence policy for inbound submissions."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Mapping


PRESERVATION_INTENT_KEYWORDS = frozenset(
    {
        "archive",
        "durable",
        "knowledge trace",
        "methodology",
        "preserve",
        "remember",
        "save",
        "store",
        "storage",
    }
)
PRESERVATION_INTENT_FIELDS = frozenset(
    {
        "preserve_as",
        "storage",
        "store_as",
        "memory",
        "knowledge",
        "durable",
        "retention",
    }
)
PRESERVATION_OUTCOME_VALUES = frozenset(
    {
        "preserve",
        "preserve_knowledge",
        "store",
        "store_knowledge",
        "durable_storage",
        "remember",
        "archive",
    }
)
PRESERVATION_ACCEPTABLE_TOOLS = (
    "memory_ingest_source",
    "memory_supersede",
    "brain_encode",
    "manage_domain",
    "manage_project",
    "create_launch_handoff",
    "post_thread_discussion_reply",
    "post_ai_timeline_message",
    "publish_thread_artifact",
    "manage_workspace_app",
)
PRESERVATION_ACCEPTABLE_TARGET_KINDS = (
    "memory_source",
    "memory_node",
    "memory_assertion",
    "memory_edge",
    "domain_record",
    "domain",
    "project_context",
    "project_context_attachment",
    "thread_message",
    "launch_handoff",
    "workspace_app",
)
PRESERVATION_MISSING_REASON = (
    "Preservation was requested, but the completed Illo run did not produce durable storage evidence."
)


@dataclass(frozen=True)
class PreservationContract:
    requires_durable_evidence: bool
    intent: str
    reason: str | None
    detection_source: str | None
    visibility_hint: str | None
    preserve_as: str | None
    acceptable_tools: tuple[str, ...] = PRESERVATION_ACCEPTABLE_TOOLS
    acceptable_target_kinds: tuple[str, ...] = PRESERVATION_ACCEPTABLE_TARGET_KINDS

    def to_dict(self) -> dict[str, Any]:
        return {
            "requires_durable_evidence": self.requires_durable_evidence,
            "intent": self.intent,
            "reason": self.reason,
            "detection_source": self.detection_source,
            "acceptable_tools": list(self.acceptable_tools),
            "acceptable_target_kinds": list(self.acceptable_target_kinds),
            "visibility_hint": self.visibility_hint,
            "preserve_as": self.preserve_as,
        }


def _clean_optional(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _json_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _intent_text(value: Any) -> str:
    if value in (None, "", [], {}):
        return ""
    if isinstance(value, str):
        return value.lower()
    try:
        return json.dumps(value, ensure_ascii=False, default=str).lower()
    except Exception:
        return str(value).lower()


def _contains_preservation_keyword(text: str) -> bool:
    normalized = re.sub(r"[^a-z0-9_]+", " ", text.lower())
    for keyword in PRESERVATION_INTENT_KEYWORDS:
        pattern = r"\b" + re.escape(keyword).replace(r"\ ", r"\s+") + r"\b"
        if re.search(pattern, normalized):
            return True
    return False


def _has_preservation_field(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    for key, child in value.items():
        key_text = str(key or "").strip().lower()
        if key_text in PRESERVATION_INTENT_FIELDS:
            return True
        if isinstance(child, Mapping) and _has_preservation_field(child):
            return True
    return False


def _explicit_outcome_requests_preservation(value: Any) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return False
    normalized = re.sub(r"[^a-z0-9_]+", "_", text).strip("_")
    return normalized in PRESERVATION_OUTCOME_VALUES or _contains_preservation_keyword(text)


def submission_preservation_contract(normalized: Mapping[str, Any]) -> dict[str, Any]:
    constraints = _json_dict(normalized.get("constraints"))
    response = _json_dict(normalized.get("response"))
    source = _json_dict(normalized.get("source"))
    parts = list(normalized.get("parts") or [])
    part_headers = [
        {"type": part.get("type"), "title": part.get("title")}
        for part in parts
        if isinstance(part, Mapping)
    ]
    desired_outcome = _clean_optional(
        normalized.get("desired_outcome")
        or constraints.get("desired_outcome")
        or response.get("desired_outcome")
        or source.get("desired_outcome")
    )
    preserve_as = _clean_optional(constraints.get("preserve_as") or response.get("preserve_as"))
    visibility_hint = _clean_optional(constraints.get("visibility"))
    scanned = "\n".join(
        [
            _intent_text(normalized.get("message")),
            _intent_text(normalized.get("summary")),
            _intent_text(desired_outcome),
            _intent_text(constraints),
            _intent_text(response),
            _intent_text(source),
            _intent_text(part_headers),
        ]
    )
    explicit_outcome = _explicit_outcome_requests_preservation(desired_outcome)
    explicit_field = any(_has_preservation_field(value) for value in (constraints, response, source))
    language_hint = _contains_preservation_keyword(scanned)
    required = explicit_outcome or explicit_field
    if explicit_outcome:
        reason = "preservation requested by desired_outcome"
        detection_source = "desired_outcome"
    elif explicit_field:
        reason = "preservation field present in submission metadata"
        detection_source = "metadata"
    elif language_hint:
        reason = "preservation language present in submission"
        detection_source = "language_hint"
    else:
        reason = None
        detection_source = None

    if required:
        intent = "preserve_knowledge"
    elif language_hint:
        intent = "possible_preservation"
    else:
        intent = "general_coordination"
    return PreservationContract(
        requires_durable_evidence=required,
        intent=intent,
        reason=reason,
        detection_source=detection_source,
        visibility_hint=visibility_hint,
        preserve_as=preserve_as,
    ).to_dict()


def submission_preservation_prompt_lines(contract: Mapping[str, Any]) -> list[str]:
    if contract.get("requires_durable_evidence") or contract.get("intent") == "possible_preservation":
        return [
            "",
            "Possible preservation workflow:",
            "- The wording may indicate a preservation request. Treat this as a hint, not a storage mandate.",
            "- If durable storage is appropriate, choose an Illo-owned memory, domain, project, handoff, thread, artifact, or workspace-app surface and list the durable handle in the final answer.",
        ]
    return []


def preservation_contract_from_run_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    submission = _json_dict(metadata.get("submission"))
    return _json_dict(submission.get("preservation"))


def preservation_evidence_result(
    contract: Mapping[str, Any] | None,
    *,
    run_status: Any,
    attribution: Mapping[str, Any],
) -> dict[str, Any]:
    contract = _json_dict(contract)
    required = bool(contract.get("requires_durable_evidence"))
    result: dict[str, Any] = {
        "required": required,
        "status": "not_required",
        "acceptable_tools": list(contract.get("acceptable_tools") or PRESERVATION_ACCEPTABLE_TOOLS),
        "acceptable_target_kinds": list(
            contract.get("acceptable_target_kinds") or PRESERVATION_ACCEPTABLE_TARGET_KINDS
        ),
    }
    if not required:
        return result

    run_status_value = str(getattr(run_status, "value", run_status) or "").strip()
    if run_status_value != "completed":
        result["status"] = run_status_value
        return result

    acceptable_kinds = {str(kind) for kind in result["acceptable_target_kinds"]}
    acceptable_tools = {str(tool) for tool in result["acceptable_tools"]}
    mutated_refs = [
        dict(ref)
        for ref in attribution.get("mutated_target_refs", [])
        if str(ref.get("kind") or "") in acceptable_kinds
    ]
    tool_names = [str(tool) for tool in attribution.get("tool_names", [])]
    matching_tools = [tool for tool in tool_names if tool in acceptable_tools]
    if mutated_refs:
        result["status"] = "satisfied"
        result["mutated_target_refs"] = mutated_refs
        result["tool_names"] = matching_tools or tool_names
        return result

    result["status"] = "missing"
    result["reason"] = PRESERVATION_MISSING_REASON
    result["tool_names"] = matching_tools or tool_names
    result["mutated_target_refs"] = []
    return result


__all__ = [
    "PRESERVATION_ACCEPTABLE_TARGET_KINDS",
    "PRESERVATION_ACCEPTABLE_TOOLS",
    "PRESERVATION_MISSING_REASON",
    "submission_preservation_contract",
    "submission_preservation_prompt_lines",
    "preservation_contract_from_run_metadata",
    "preservation_evidence_result",
]
