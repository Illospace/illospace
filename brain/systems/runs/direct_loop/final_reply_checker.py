"""Final-reply review runtime for agent and Cortex reply flows."""

from __future__ import annotations

import logging
import json
import re
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

from brain.systems.chantiers import DEFAULT_TRACKER_DOMAIN_ID
from brain.platform.integrations.llm import resolve_llm_client
from brain.platform.integrations.providers import get_provider
from brain.systems.sessions.harvest import _extract_text
from brain.platform.providers.model_policy import (
    get_default_model,
    infer_provider_from_model,
    resolve_default_provider,
)
from brain.systems.runs.direct_loop.final_reply import (
    cache_final_reply_review,
    cached_final_reply_review,
    parse_checker_payload,
)
from brain.systems.runs.direct_loop.final_reply_evidence import (
    DEFAULT_TOOL_FAILURE_THRESHOLD,
    FinalReplyEvidence,
    ToolResultEvidence,
)
from brain.systems.runs.direct_loop.request import build_api_request, normalize_model_name
from brain.systems.runs.status import RunStatus
from brain.systems.sessions import _content_to_dicts

logger = logging.getLogger("agent")

_RESOLVED_STATUSES = {"resolved", "blocked_on_user"}
_CUSTOMER_BUG_FILING_POLICY_REFERENCE = (
    "brain/systems/skills/builtin_skill_bundles/uwear-engineering-triage/"
    "references/creating-work-items.md#customer-bug-filing-policy"
)
_PRODUCT_SURFACE_TERMS = (
    "settings",
    "integrations",
    "oauth",
    "setup screen",
    "setup path",
    "setup ui",
    "admin screen",
    "admin/integration screen",
    "illospace admin",
    "workspace admin",
    "admin approval",
    "deployment/admin",
    "deployment",
    "self-serve ui",
)
_PRODUCT_SURFACE_UNCERTAINTY = (
    "i don't have a confirmed",
    "i do not have a confirmed",
    "i don't see a confirmed",
    "i do not see a confirmed",
    "i shouldn't invent",
    "i should not invent",
    "not confirmed",
    "was not confirmed",
    "i didn't read",
    "i did not read",
)
_PRODUCT_SURFACE_NAV_RE = re.compile(
    r"\b(open|go to|navigate to|click|choose|approve|redirect|select|configure)\b",
    re.IGNORECASE,
)
_REQUESTED_WORK_ARTIFACT_RE = re.compile(
    r"(?:\bgithub\s+(?:issue|ticket)\b|"
    r"\b(?:create|file|open|assign|make|raise|submit|add)\b.{0,40}\b(?:issue|ticket)\b)",
    re.IGNORECASE,
)
_REQUESTED_GITHUB_ARTIFACT_RE = re.compile(
    r"(?:\bgithub\s+(?:issue|ticket)\b|"
    r"\b(?:issue|ticket)\b.{0,20}\b(?:on|in)\s+github\b)",
    re.IGNORECASE,
)
_CUSTOMER_REPORT_ORIGIN_RE = re.compile(
    r"(?:"
    r"\b(?:customer|client|user)\s+(?:reported|reports|said|says|wrote|writes|complained|emailed|contacted)\b|"
    r"\b(?:reported|received|forwarded|escalated)\s+(?:by|from|through|via)\s+(?:a\s+|the\s+)?"
    r"(?:customer|client|user|support|customer\s+success)\b|"
    r"\b(?:support|customer\s+success)\s+(?:reported|reports|said|says|escalated|ticket|case|thread|message)\b"
    r")",
    re.IGNORECASE,
)
_QUOTED_CUSTOMER_REPORT_RE = re.compile(
    r"\b(?:customer|client|user|support)(?:\s+(?:report|quote|message|email|complaint))?"
    r"\s*[:\-]\s*[\"'“‘]",
    re.IGNORECASE,
)
_ARTIFACT_FAILURE_ACK_RE = re.compile(
    r"\b(could not|couldn't|cannot|can't|unable|failed|not created|not opened|"
    r"wasn't created|was not created|didn't create|did not create|blocked)\b",
    re.IGNORECASE,
)
_ARTIFACT_BLOCKER_RE = re.compile(
    r"\b(no_write_token|token|credential|permission|forbidden|unavailable|403|404|not found|"
    r"rate limit|timeout|validation)\b|"
    r"\b(?:because|due to|blocked by|requires?|missing)\b\s+\S+",
    re.IGNORECASE,
)
_GITHUB_ARTIFACT_RE = re.compile(r"\bgithub\s+(?:issue|ticket)\b", re.IGNORECASE)
_TRACKER_ARTIFACT_RE = re.compile(
    r"\b(?:tracker\s+(?:record|mirror)|linked\s+(?:tracker\s+)?record|domain\s+record)\b",
    re.IGNORECASE,
)
_STATUS_IN_PROGRESS_RE = re.compile(
    r"\b(?:in progress|still (?:running|working|underway|ongoing)|"
    r"not (?:done|complete|completed|finished)|not done yet)\b",
    re.IGNORECASE,
)
_UNRESOLVED_RE = re.compile(
    r"\b(?:unresolved|outstanding|pending|missing|not (?:created|opened|filed|done)|"
    r"has not been (?:created|opened|filed|done)|hasn't been (?:created|opened|filed|done)|"
    r"still (?:needed|missing|outstanding|pending))\b",
    re.IGNORECASE,
)
_FAILURE_ACK_RE = re.compile(
    r"\b(?:fail(?:ed|ing|ure)?|error|could not|couldn't|unable|stopped retrying|"
    r"timed out|timeout|blocked)\b",
    re.IGNORECASE,
)
_GITHUB_REF_RE = re.compile(
    r"(?:https?://github\.com/[^\s)]+/(?:issues|pull)/(?P<url_number>\d+)|"
    r"\bgithub\s+(?:issue|ticket)\s+#?(?P<label_number>\d+)\b)",
    re.IGNORECASE,
)
_TICKET_REF_RE = re.compile(
    r"\b(?:ticket|issue)(?:\s+(?:ref(?:erence)?|id))?\s*#?"
    r"(?P<ref>[A-Za-z][A-Za-z0-9_-]*-\d+|\d+)\b",
    re.IGNORECASE,
)
_RECORD_WORD_RE = re.compile(r"\b(?:record|domain|tracker)\b", re.IGNORECASE)
_ASSIGNED_RE = re.compile(r"\b(?:assign(?:ed|ment)?|assignee)\b", re.IGNORECASE)
_SUCCESS_ACTION_RE = re.compile(
    r"\b(?:created|opened|filed|logged|assigned|completed|succeeded|successful)\b",
    re.IGNORECASE,
)
_NEGATION_TAIL_RE = re.compile(
    r"(?:\bnot\b|\bnever\b|\bno\b|\bcould not\b|\bcouldn't\b|\bfailed to\b|"
    r"\bhas not been\b|\bhasn't been\b|\bwas not\b|\bwasn't\b)\s*$",
    re.IGNORECASE,
)


class FinalReplyEnforcement(str, Enum):
    """Whether a review is advisory or may temporarily block a reply."""

    ADVISORY = "advisory"
    BLOCK = "block"


@dataclass(frozen=True)
class FinalReplyReview:
    """Typed internal final-reply checker verdict."""

    status: str
    approved: bool
    rationale: str
    missing_requirements: tuple[str, ...] = ()
    raw_output: str = ""
    enforcement: FinalReplyEnforcement = FinalReplyEnforcement.ADVISORY
    confidence: float | None = None
    intent_type: str | None = None
    completion_mode: str | None = None

    @classmethod
    def from_payload(cls, payload: dict, raw_output: str) -> "FinalReplyReview":
        status = str(payload["status"])
        missing = tuple(str(item) for item in (payload.get("missing_requirements") or ()))
        confidence = payload.get("confidence")
        try:
            confidence = float(confidence) if confidence is not None else None
        except (TypeError, ValueError):
            confidence = None
        return cls(
            status=status,
            approved=status in _RESOLVED_STATUSES,
            rationale=str(payload.get("rationale") or ""),
            missing_requirements=missing,
            raw_output=raw_output,
            confidence=confidence,
            intent_type=str(payload.get("intent_type") or "").strip() or None,
            completion_mode=str(payload.get("completion_mode") or "").strip() or None,
        )

    def to_dict(self) -> dict:
        payload = {
            "status": self.status,
            "approved": self.approved,
            "rationale": self.rationale,
            "missing_requirements": list(self.missing_requirements),
            "raw_output": self.raw_output,
            "enforcement": self.enforcement,
        }
        if self.confidence is not None:
            payload["confidence"] = self.confidence
        if self.intent_type:
            payload["intent_type"] = self.intent_type
        if self.completion_mode:
            payload["completion_mode"] = self.completion_mode
        return payload


def _required_openai_auth_mode(model: str) -> str | None:
    normalized = normalize_model_name(model).lower()
    return "chatgpt" if normalized == "gpt-5.5" or normalized.startswith("gpt-5.6") else None


def _init_llm(
    user_id: str | None,
    session_id: str,
    model: str,
    *,
    org_id: str | None = None,
):
    default_provider = resolve_default_provider(user_id=user_id, org_id=org_id)
    requested_provider = infer_provider_from_model(model, default=default_provider)
    llm = resolve_llm_client(
        user_id=user_id,
        org_id=org_id,
        provider=requested_provider,
        auth_mode=_required_openai_auth_mode(model) if requested_provider == "openai" else None,
    )
    provider = get_provider(llm.provider, llm.client)
    extra_headers = llm.build_request_headers(session_id=session_id)
    logger.info(
        "Agent %s: provider=%s, source=%s, auth_mode=%s, token=%s..., oauth=%s",
        session_id,
        llm.provider,
        llm.source,
        getattr(llm, "auth_mode", None),
        llm.token_prefix,
        llm.is_oauth,
    )
    return llm, provider, extra_headers


def _default_checker_model(user_id: str | None) -> str:
    default_provider = resolve_default_provider(user_id=user_id)
    return get_default_model(
        provider=default_provider,
        include_provider_prefix=False,
        user_id=user_id,
    )


def _compact_json(value: Any, *, limit: int = 2500) -> str:
    if value is None:
        return ""
    try:
        text = json.dumps(value, sort_keys=True, ensure_ascii=True, default=str)
    except Exception:
        text = str(value)
    return text[:limit]


def _review_scope(
    *,
    user_request: str,
    execution_context: str | None,
    evidence: FinalReplyEvidence | None,
    intent_profile: dict | None,
) -> dict[str, Any]:
    return {
        "user_request": " ".join((user_request or "").split())[:1200],
        "execution_context": " ".join((execution_context or "").split())[:2500],
        "evidence": evidence.cache_fingerprint() if evidence is not None else "",
        "intent_profile": intent_profile or {},
    }


def _normalize_grounding_text(text: str | None) -> str:
    normalized = str(text or "").lower()
    normalized = normalized.replace("→", "->")
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized


def _supported_by_execution_evidence(candidate: str, evidence: FinalReplyEvidence) -> bool:
    mentioned_terms = [term for term in _PRODUCT_SURFACE_TERMS if term in candidate]
    return evidence.supports_terms(mentioned_terms)


def _ungrounded_product_surface_issue(
    candidate_output: str,
    evidence: FinalReplyEvidence,
) -> str | None:
    """Detect invented product UI/setup surfaces before the LLM checker can bless them."""

    candidate = _normalize_grounding_text(candidate_output)
    if "illospace" not in candidate:
        return None

    has_route_chain = "->" in candidate
    has_product_terms = any(term in candidate for term in _PRODUCT_SURFACE_TERMS)
    has_navigation_step = bool(_PRODUCT_SURFACE_NAV_RE.search(candidate))
    explicitly_uncertain = any(marker in candidate for marker in _PRODUCT_SURFACE_UNCERTAINTY)
    asserts_surface = (
        (has_route_chain and has_product_terms)
        or (has_navigation_step and has_product_terms)
        or "illospace admin" in candidate
        or "workspace admin" in candidate
        or "admin approval" in candidate
        or "deployment/admin" in candidate
        or "admin/integration screen" in candidate
    )
    if not asserts_surface:
        return None
    if explicitly_uncertain and not has_navigation_step and not any(
        marker in candidate for marker in ("deployment/admin", "admin/integration screen")
    ):
        return None
    if _supported_by_execution_evidence(candidate, evidence):
        return None
    return (
        "The candidate asserts an Illospace UI/setup/deployment surface that is not present "
        "in this run's execution evidence."
    )


def _has_customer_report_signal(user_request: str) -> bool:
    request = str(user_request or "")
    return bool(
        _CUSTOMER_REPORT_ORIGIN_RE.search(request)
        or _QUOTED_CUSTOMER_REPORT_RE.search(request)
    )


def _default_tracker_create_attempt(result: ToolResultEvidence) -> bool:
    if result.tool_name != "manage_domain":
        return False
    arguments = result.arguments
    try:
        domain_id = int(arguments.get("domain_id"))
    except (TypeError, ValueError):
        return False
    return (
        str(arguments.get("action") or "").strip() == "create_record"
        and domain_id == DEFAULT_TRACKER_DOMAIN_ID
    )


def _reply_names_failed_artifact(candidate_output: str, artifact_re: re.Pattern[str]) -> bool:
    candidate = str(candidate_output or "")
    return bool(
        artifact_re.search(candidate)
        and _ARTIFACT_FAILURE_ACK_RE.search(candidate)
        and _ARTIFACT_BLOCKER_RE.search(candidate)
    )


def _customer_bug_missing_tracker_mirror(
    user_request: str,
    candidate_output: str,
    evidence: FinalReplyEvidence,
) -> bool:
    request = str(user_request or "")
    if not (_has_customer_report_signal(request) and _REQUESTED_WORK_ARTIFACT_RE.search(request)):
        return False
    github_results = evidence.results_for("create_github_issue")
    if not github_results or not github_results[-1].succeeded:
        return False
    mirror_attempts = tuple(item for item in evidence.tool_results if _default_tracker_create_attempt(item))
    if any(item.succeeded for item in mirror_attempts):
        return False
    if any(item.failed for item in mirror_attempts) and _reply_names_failed_artifact(
        candidate_output,
        _TRACKER_ARTIFACT_RE,
    ):
        return False
    return True


def _requested_github_artifact_contract_violated(
    user_request: str,
    candidate_output: str,
    evidence: FinalReplyEvidence,
) -> bool:
    if not _REQUESTED_GITHUB_ARTIFACT_RE.search(str(user_request or "")):
        return False
    github_results = evidence.results_for("create_github_issue")
    if not github_results:
        return True
    if github_results[-1].succeeded:
        return False
    return not _reply_names_failed_artifact(candidate_output, _GITHUB_ARTIFACT_RE)


def _claims_request_complete(candidate_output: str) -> bool:
    """Detect a top-level completion claim without treating partial progress as done."""

    candidate = " ".join(str(candidate_output or "").split())
    if re.match(r"^(?:yes\b|done\b)", candidate, re.IGNORECASE):
        return True
    completion_re = re.compile(
        r"\b(?:(?:it|that|this|the\s+(?:request|work|task)|everything|all\s+of\s+it)\s+"
        r"(?:is|was|has been)\s+)?(?:done|complete|completed|finished)\b",
        re.IGNORECASE,
    )
    for match in completion_re.finditer(candidate):
        prefix = candidate[max(0, match.start() - 24):match.start()]
        if not _NEGATION_TAIL_RE.search(prefix):
            return True
    return False


def _asserts_success(candidate_output: str) -> bool:
    if _claims_request_complete(candidate_output):
        return True
    candidate = str(candidate_output or "")
    for match in _SUCCESS_ACTION_RE.finditer(candidate):
        prefix = candidate[max(0, match.start() - 32):match.start()]
        if not _NEGATION_TAIL_RE.search(prefix):
            return True
    return False


def _reply_names_tool_failures(
    candidate_output: str,
    evidence: FinalReplyEvidence,
) -> bool:
    candidate = str(candidate_output or "").lower()
    if not _FAILURE_ACK_RE.search(candidate):
        return False
    for tool_name in evidence.failed_tool_names:
        variants = {
            tool_name.lower(),
            tool_name.lower().replace("_", " "),
            tool_name.lower().replace("-", " "),
        }
        if not any(variant and variant in candidate for variant in variants):
            return False
    return True


def _tool_failure_success_issue(
    candidate_output: str,
    evidence: FinalReplyEvidence,
) -> str | None:
    if not evidence.failure_threshold_reached:
        return None
    if not _asserts_success(candidate_output):
        return None
    if _reply_names_tool_failures(candidate_output, evidence):
        return None
    state = evidence.tool_failure_state
    failure_count = max(
        DEFAULT_TOOL_FAILURE_THRESHOLD,
        int(getattr(state, "consecutive_failures", 0) or 0),
        int(getattr(state, "total_failures", 0) or 0),
        sum(1 for item in evidence.tool_results if item.failed),
    )
    tools = ", ".join(f"`{name}`" for name in evidence.failed_tool_names) or "the failing tool"
    return (
        f"The run reached the tool-failure threshold after {failure_count} failures involving "
        f"{tools}, but the candidate asserts success without naming those failures."
    )


def _github_refs(value: str) -> tuple[str, ...]:
    refs: list[str] = []
    for match in _GITHUB_REF_RE.finditer(str(value or "")):
        ref = match.group("url_number") or match.group("label_number")
        if ref and ref not in refs:
            refs.append(ref)
    return tuple(refs)


def _ticket_refs(value: str) -> tuple[str, ...]:
    refs: list[str] = []
    for match in _TICKET_REF_RE.finditer(str(value or "")):
        ref = match.group("ref")
        if ref and ref not in refs:
            refs.append(ref)
    return tuple(refs)


def _record_ids(value: Any, *, in_record: bool = False) -> tuple[str, ...]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            normalized_key = str(key or "").strip().lower()
            nested_record = in_record or normalized_key in {
                "record",
                "records",
                "domain_record",
                "tracker_record",
            }
            if normalized_key in {"record_id", "domain_record_id", "tracker_record_id"}:
                candidate = str(item or "").strip()
                if candidate and candidate not in found:
                    found.append(candidate)
            elif normalized_key == "id" and in_record:
                candidate = str(item or "").strip()
                if candidate and candidate not in found:
                    found.append(candidate)
            for candidate in _record_ids(item, in_record=nested_record):
                if candidate not in found:
                    found.append(candidate)
    elif isinstance(value, (list, tuple)):
        for item in value:
            for candidate in _record_ids(item, in_record=in_record):
                if candidate not in found:
                    found.append(candidate)
    return tuple(found)


def _successful_record_ids(evidence: FinalReplyEvidence) -> tuple[str, ...]:
    found: list[str] = []
    for item in evidence.tool_results:
        if not item.succeeded:
            continue
        for record_id in _record_ids(item.result):
            if record_id not in found:
                found.append(record_id)
    return tuple(found)


def _mapping_values_for_keys(value: Any, keys: frozenset[str]) -> tuple[str, ...]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key or "").strip().lower() in keys:
                candidate = str(item or "").strip()
                if candidate and candidate not in found:
                    found.append(candidate)
            for candidate in _mapping_values_for_keys(item, keys):
                if candidate not in found:
                    found.append(candidate)
    elif isinstance(value, (list, tuple)):
        for item in value:
            for candidate in _mapping_values_for_keys(item, keys):
                if candidate not in found:
                    found.append(candidate)
    return tuple(found)


def _successful_assignees(evidence: FinalReplyEvidence) -> tuple[str, ...]:
    found: list[str] = []
    for item in evidence.tool_results:
        if not item.succeeded:
            continue
        for assignee in _mapping_values_for_keys(
            item.result,
            frozenset({"assignee", "assigned_to", "assignee_name"}),
        ):
            if assignee not in found:
                found.append(assignee)
    return tuple(found)


def _status_question_contract_issue(
    candidate_output: str,
    evidence: FinalReplyEvidence,
) -> str | None:
    status = evidence.status_question
    if status is None:
        return None
    candidate = str(candidate_output or "")
    claims_complete = _claims_request_complete(candidate)
    origin = status.originating_run

    if status.lookup_status != "verified":
        if claims_complete:
            return (
                "The same-thread run lookup did not verify an originating outcome, so the "
                "status reply cannot assert completion."
            )
        return None
    if origin is None:
        if claims_complete:
            return (
                "No originating run outcome was found on this thread, so the status reply "
                "cannot assert completion from an incidental artifact."
            )
        return None

    if status.has_live_sibling:
        live = status.live_sibling_runs[0]
        if claims_complete:
            return (
                f"Sibling run {live.run_id} is still {live.status}; the status reply must say "
                "the request is in progress and cannot say yes/done."
            )
        if not _STATUS_IN_PROGRESS_RE.search(candidate):
            return (
                f"Sibling run {live.run_id} is still {live.status}, but the reply does not "
                "explicitly say the request is in progress."
            )

    record_ids = _successful_record_ids(evidence)
    if status.has_live_sibling and record_ids:
        names_a_record = bool(_RECORD_WORD_RE.search(candidate))
        names_known_ref = any(record_id in candidate for record_id in record_ids)
        if not (names_a_record and names_known_ref):
            return (
                "The reply omits the concrete partial record already present in execution "
                f"evidence (record {record_ids[0]})."
            )

    source_output = (
        str(origin.final_output or "")
        if origin.status == RunStatus.COMPLETED
        else ""
    )
    github_refs = list(_github_refs(source_output))
    for result in evidence.results_for("create_github_issue"):
        if not result.succeeded:
            continue
        result_refs = _github_refs(_compact_json(result.result, limit=5000))
        for ref in result_refs:
            if ref not in github_refs:
                github_refs.append(ref)
        if isinstance(result.result, dict):
            number = result.result.get("number") or result.result.get("issue_number")
            if number not in (None, "") and str(number) not in github_refs:
                github_refs.append(str(number))
    ticket_refs = list(_ticket_refs(source_output))
    for result in evidence.tool_results:
        if not result.succeeded:
            continue
        for ref in _mapping_values_for_keys(
            result.result,
            frozenset(
                {
                    "issue_id",
                    "issue_number",
                    "ticket_id",
                    "ticket_number",
                }
            ),
        ):
            if ref not in ticket_refs:
                ticket_refs.append(ref)

    for deliverable in status.deliverables:
        if deliverable.kind == "github_issue":
            if github_refs:
                if claims_complete and not any(ref in candidate for ref in github_refs):
                    return (
                        "The completed status claim omits the verified GitHub issue ref "
                        f"({github_refs[0]})."
                    )
            else:
                if claims_complete:
                    return (
                        "The originating ask requires a GitHub ticket, but no verified "
                        "GitHub ref exists; the reply cannot report the request as done."
                    )
                if not (
                    _GITHUB_ARTIFACT_RE.search(candidate)
                    and _UNRESOLVED_RE.search(candidate)
                ):
                    return (
                        "The originating ask requires a GitHub ticket, but no verified GitHub "
                        "ref exists; the reply must name that ticket as unresolved."
                    )
        elif deliverable.kind == "assignment":
            assignees = list(_successful_assignees(evidence))
            if source_output and _ASSIGNED_RE.search(source_output):
                source_assignees = re.findall(
                    r"\bassigned(?:\s+it)?\s+to\s+@?([\w.-]+)",
                    source_output,
                    re.IGNORECASE,
                )
                for assignee in source_assignees:
                    if assignee not in assignees:
                        assignees.append(assignee)
            if assignees:
                if not (
                    _ASSIGNED_RE.search(candidate)
                    and any(assignee.lower() in candidate.lower() for assignee in assignees)
                ):
                    return (
                        "The reply omits the verified ticket assignment "
                        f"to {assignees[0]}."
                    )
            else:
                if claims_complete:
                    return (
                        "The originating ask includes ticket assignment, but no verified "
                        "assignment exists; the reply cannot report the request as done."
                    )
                if not (
                    _ASSIGNED_RE.search(candidate)
                    and _UNRESOLVED_RE.search(candidate)
                ):
                    return (
                        "The originating ask includes ticket assignment, but no verified "
                        "assignment exists; the reply must state that it is unresolved."
                    )
        elif deliverable.kind == "ticket":
            if ticket_refs:
                if claims_complete and not any(
                    ref in candidate for ref in ticket_refs
                ):
                    return (
                        "The completed status claim omits the verified ticket ref "
                        f"({ticket_refs[0]})."
                    )
            else:
                if claims_complete:
                    return (
                        f"The {deliverable.label} has no verified ref; the reply cannot "
                        "report the request as done."
                    )
                if not (
                    deliverable.label.lower() in candidate.lower()
                    and _UNRESOLVED_RE.search(candidate)
                ):
                    return (
                        f"The {deliverable.label} has no verified ref; the reply must "
                        "state that it is unresolved."
                    )
        elif deliverable.kind == "request":
            if not source_output:
                if claims_complete:
                    return (
                        "The originating request has no verified final outcome; the reply "
                        "cannot report it as done."
                    )
                if not (
                    _STATUS_IN_PROGRESS_RE.search(candidate)
                    or _UNRESOLVED_RE.search(candidate)
                ):
                    return (
                        "The originating request has no verified final outcome; the reply "
                        "must state that it is unresolved or in progress."
                    )
        else:
            if claims_complete:
                return (
                    f"The {deliverable.label} has no verified resolution for deliverable "
                    f"kind {deliverable.kind!r}; the reply cannot report the request as done."
                )
            if not (
                deliverable.label.lower() in candidate.lower()
                and _UNRESOLVED_RE.search(candidate)
            ):
                return (
                    f"The {deliverable.label} has no verified resolution for deliverable "
                    f"kind {deliverable.kind!r}; the reply must state that it is unresolved."
                )

    if claims_complete and origin.status != RunStatus.COMPLETED:
        return (
            f"Originating run {origin.run_id} is {origin.status}, not completed; "
            "the status reply cannot assert completion."
        )
    if claims_complete and not source_output:
        return (
            f"Originating run {origin.run_id} has no verified final outcome with refs, "
            "so the status reply cannot assert completion."
        )
    return None


def _token_resolution_verdict(
    provider,
    llm,
    model: str,
    session_id: str,
    user_request: str,
    candidate_output: str,
    *,
    build_request: Callable = build_api_request,
    extract_text: Callable = _extract_text,
    content_to_dicts: Callable = _content_to_dicts,
) -> bool:
    """Return True only when the model judges the candidate output as resolved."""

    system = [{
        "type": "text",
        "text": (
            "You are a strict completion gate for an agent harness. "
            "Decide whether the candidate final assistant message means the user's request is reasonably done. "
            "Reply with exactly one token: RESOLVED or UNRESOLVED. "
            "Return UNRESOLVED for partial progress, offers to continue later, permission-seeking, "
            "or cases where the assistant says more work remains. "
            "Return RESOLVED when the request has been completed, fully answered, or clearly blocked by "
            "specific missing user input that truly prevents completion."
        ),
    }]
    messages = [{
        "role": "user",
        "content": (
            "User request:\n"
            f"{user_request.strip() or '(empty request)'}\n\n"
            "Candidate final assistant message:\n"
            f"{candidate_output.strip() or '(empty output)'}"
        ),
    }]
    request = build_request(
        model=model,
        messages=messages,
        max_tokens=32,
        system=system,
        tools=None,
        reasoning_effort=None,
        extra_headers=llm.build_request_headers(session_id=f"{session_id}:reply-check"),
        provider_name=llm.provider,
        session_id=f"{session_id}:reply-check",
        persist_session=False,
        cache_tools=False,
        operation_type="verifier",
    )
    response = provider.create(request)
    verdict = extract_text([{"role": "assistant", "content": content_to_dicts(response.content)}]).strip().upper()
    return verdict == "RESOLVED"


def review_candidate_final_reply(
    *,
    user_request: str,
    candidate_output: str,
    execution_context: str | None = None,
    evidence: FinalReplyEvidence | None = None,
    intent_profile: dict | None = None,
    user_id: str | None = None,
    provider=None,
    llm=None,
    model: str | None = None,
    session_id: str | None = None,
    normalize_model: Callable[[str], str] = normalize_model_name,
    init_llm: Callable = _init_llm,
    build_request: Callable = build_api_request,
    extract_text: Callable = _extract_text,
    content_to_dicts: Callable = _content_to_dicts,
) -> dict:
    """Run the final-reply checker and return a dict-compatible review payload."""

    structured_evidence = evidence or FinalReplyEvidence()
    tool_failure_issue = _tool_failure_success_issue(
        candidate_output,
        structured_evidence,
    )
    if tool_failure_issue:
        return FinalReplyReview(
            status="continue",
            approved=False,
            rationale=tool_failure_issue,
            missing_requirements=(
                "Name the failing tool(s), the repeated failures, and the incomplete "
                "tool-dependent work before ending the run.",
            ),
            raw_output="deterministic_tool_failure_honesty_contract",
            enforcement=FinalReplyEnforcement.BLOCK,
        ).to_dict()

    status_question_issue = _status_question_contract_issue(
        candidate_output,
        structured_evidence,
    )
    if status_question_issue:
        return FinalReplyReview(
            status="continue",
            approved=False,
            rationale=status_question_issue,
            missing_requirements=(
                "Report a live sibling as in progress; enumerate every originating "
                "deliverable with its verified ref or unresolved state.",
            ),
            raw_output="deterministic_status_question_contract",
            enforcement=FinalReplyEnforcement.BLOCK,
        ).to_dict()

    grounding_issue = _ungrounded_product_surface_issue(candidate_output, structured_evidence)
    if grounding_issue:
        return FinalReplyReview(
            status="continue",
            approved=False,
            rationale=grounding_issue,
            missing_requirements=(
                "Answer only with setup/product surfaces supported by current tool results or source context.",
            ),
            raw_output="deterministic_product_surface_grounding",
        ).to_dict()

    if _customer_bug_missing_tracker_mirror(user_request, candidate_output, structured_evidence):
        return FinalReplyReview(
            status="continue",
            approved=False,
            rationale=(
                "Execution evidence does not satisfy the canonical customer-bug filing policy "
                f"({_CUSTOMER_BUG_FILING_POLICY_REFERENCE})."
            ),
            missing_requirements=(
                "Follow the canonical customer-bug filing policy before sending the final reply.",
            ),
            raw_output="deterministic_customer_bug_mirror_contract",
            enforcement=FinalReplyEnforcement.BLOCK,
        ).to_dict()

    if _requested_github_artifact_contract_violated(
        user_request,
        candidate_output,
        structured_evidence,
    ):
        return FinalReplyReview(
            status="continue",
            approved=False,
            rationale=(
                "The requested GitHub artifact was not successfully created, but the reply does not "
                "have structured failure evidence plus an explicit GitHub-issue blocker."
            ),
            missing_requirements=(
                "Say that the requested GitHub issue was not created and name the exact blocker; "
                "describe any tracker record only as retention or handoff.",
            ),
            raw_output="deterministic_requested_artifact_contract",
            enforcement=FinalReplyEnforcement.BLOCK,
        ).to_dict()

    checker_model = normalize_model(model) if model else None
    checker_llm = llm
    checker_provider = provider
    checker_session_id = session_id or f"final-reply-checker-{uuid.uuid4().hex[:12]}"
    request_session_id = f"{checker_session_id}:final-reply-checker"

    if checker_llm is None or checker_provider is None:
        if not checker_model:
            checker_model = _default_checker_model(user_id)
        checker_llm, checker_provider, checker_headers = init_llm(
            user_id=user_id,
            session_id=request_session_id,
            model=checker_model,
        )
    else:
        checker_headers = checker_llm.build_request_headers(session_id=request_session_id)

    system = [{
        "type": "text",
        "text": (
            "You are the adaptive final run checker for an agent harness. "
            "Decide whether the proposed final user-facing message should be allowed to reach the user, "
            "given the user's intent profile and execution evidence. "
            "Return JSON only with keys: status, rationale, missing_requirements, confidence, intent_type, completion_mode. "
            "status must be one of: resolved, blocked_on_user, continue. "
            "Use continue only when there is a concrete unmet requirement or the evidence shows the work is only partial. "
            "Use blocked_on_user when a specific missing user input, approval, credential, unavailable service, "
            "or concrete backend/tool dependency truly prevents completion. "
            "Use resolved only when the user's goal is completed. "
            "For light quick-answer intents, do not demand extra work when the answer directly satisfies the question. "
            "For analysis intents, caveats are acceptable when the requested analysis or judgment is delivered. "
            "For strict_contract intents, reject partial progress, first-slice completions, unsupported side-effect claims, "
            "or 'there may be more' endings unless a concrete blocker makes further work impossible. "
            "Reject candidate replies that invent Illospace UI screens, settings paths, setup flows, admin roles, "
            "deployment paths, or external OAuth flows not present in execution evidence."
        ),
    }]
    message_parts = [
        "Original user request:",
        user_request.strip() or "(empty request)",
        "",
        "Proposed final user-facing message:",
        candidate_output.strip() or "(empty output)",
    ]
    if intent_profile:
        message_parts.extend(["", "Intent satisfaction profile:", _compact_json(intent_profile, limit=2500)])
    if execution_context:
        message_parts.extend(["", "Execution context:", execution_context.strip()[:2500]])
    messages = [{
        "role": "user",
        "content": "\n".join(message_parts),
    }]
    request = build_request(
        model=checker_model or _default_checker_model(user_id),
        messages=messages,
        max_tokens=360,
        system=system,
        tools=None,
        reasoning_effort="low",
        extra_headers=checker_headers,
        provider_name=checker_llm.provider,
        session_id=request_session_id,
        persist_session=False,
        cache_tools=False,
        operation_type="verifier",
    )
    response = checker_provider.create(request)
    raw_output = extract_text([{"role": "assistant", "content": content_to_dicts(response.content)}]).strip()
    parsed = parse_checker_payload(raw_output)
    if parsed:
        return FinalReplyReview.from_payload(parsed, raw_output).to_dict()

    resolved = _token_resolution_verdict(
        checker_provider,
        checker_llm,
        checker_model or _default_checker_model(user_id),
        checker_session_id,
        user_request,
        candidate_output,
        build_request=build_request,
        extract_text=extract_text,
        content_to_dicts=content_to_dicts,
    )
    return FinalReplyReview(
        status="resolved" if resolved else "continue",
        approved=resolved,
        rationale="Fallback checker used the compact resolved/unresolved verdict.",
        raw_output=raw_output,
    ).to_dict()


def review_final_reply_once(
    *,
    user_request: str,
    candidate_output: str,
    execution_context: str | None = None,
    evidence: FinalReplyEvidence | None = None,
    intent_profile: dict | None = None,
    user_id: str | None = None,
    provider=None,
    llm=None,
    model: str | None = None,
    session_id: str | None = None,
    agent_context=None,
    review_candidate: Callable | None = None,
) -> dict:
    """Review a candidate final reply once per unique candidate text."""

    scope = _review_scope(
        user_request=user_request,
        execution_context=execution_context,
        evidence=evidence,
        intent_profile=intent_profile,
    )
    if agent_context is not None:
        cached = cached_final_reply_review(agent_context, candidate_output, scope)
        if cached is not None:
            return cached

    reviewer = review_candidate or review_candidate_final_reply
    review = reviewer(
        user_request=user_request,
        candidate_output=candidate_output,
        execution_context=execution_context,
        evidence=evidence,
        intent_profile=intent_profile,
        user_id=user_id,
        provider=provider,
        llm=llm,
        model=model,
        session_id=session_id,
    )
    if agent_context is not None:
        return cache_final_reply_review(agent_context, candidate_output, review, scope)
    return review
