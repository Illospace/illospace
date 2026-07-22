"""Final-reply review runtime for agent and Cortex reply flows."""

from __future__ import annotations

import logging
import json
import re
import uuid
from dataclasses import dataclass
from typing import Any, Callable

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
from brain.systems.runs.direct_loop.request import build_api_request, normalize_model_name
from brain.systems.sessions import _content_to_dicts

logger = logging.getLogger("agent")

_RESOLVED_STATUSES = {"resolved", "blocked_on_user"}
_PRODUCT_SURFACE_EVIDENCE_MARKERS = (
    "recent tool result",
    "artifact",
    "worker",
    "evidence",
    "source",
    "runtime",
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
_CUSTOMER_REPORT_RE = re.compile(
    r"\b(customer|client|support|complaint|email|credits?|profile\s+id|user\s+report)\b",
    re.IGNORECASE,
)
_ARTIFACT_FAILURE_ACK_RE = re.compile(
    r"\b(could not|couldn't|cannot|can't|unable|failed|not created|not opened|blocked)\b",
    re.IGNORECASE,
)
_ARTIFACT_BLOCKER_RE = re.compile(
    r"\b(no_write_token|token|credential|permission|forbidden|unavailable|403|404|not found|rate limit|timeout|validation)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class FinalReplyReview:
    """Typed internal final-reply checker verdict."""

    status: str
    approved: bool
    rationale: str
    missing_requirements: tuple[str, ...] = ()
    raw_output: str = ""
    override: str | None = None
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
        }
        if self.override:
            payload["override"] = self.override
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
    intent_profile: dict | None,
) -> dict[str, Any]:
    return {
        "user_request": " ".join((user_request or "").split())[:1200],
        "execution_context": " ".join((execution_context or "").split())[:2500],
        "intent_profile": intent_profile or {},
    }


def _normalize_grounding_text(text: str | None) -> str:
    normalized = str(text or "").lower()
    normalized = normalized.replace("→", "->")
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized


def _supported_by_execution_evidence(candidate: str, execution_context: str | None) -> bool:
    evidence = _normalize_grounding_text(execution_context)
    if not evidence:
        return False
    if not any(marker in evidence for marker in _PRODUCT_SURFACE_EVIDENCE_MARKERS):
        return False
    mentioned_terms = [term for term in _PRODUCT_SURFACE_TERMS if term in candidate]
    if not mentioned_terms:
        return False
    return all(term in evidence for term in mentioned_terms)


def _ungrounded_product_surface_issue(
    candidate_output: str,
    execution_context: str | None,
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
    if _supported_by_execution_evidence(candidate, execution_context):
        return None
    return (
        "The candidate asserts an Illospace UI/setup/deployment surface that is not present "
        "in this run's execution evidence."
    )


def _recent_tool_result_entries(execution_context: str | None, tool_name: str) -> tuple[str, ...]:
    """Return compact recent-result lines for one tool, when still in evidence."""

    matches: list[str] = []
    expected = str(tool_name or "").strip().lower()
    for line in str(execution_context or "").splitlines():
        normalized = _normalize_grounding_text(line)
        if "recent tool result" in normalized and expected in normalized:
            matches.append(normalized)
    return tuple(matches)


def _recent_tool_result_entry(execution_context: str | None, tool_name: str) -> str | None:
    entries = _recent_tool_result_entries(execution_context, tool_name)
    return entries[-1] if entries else None


def _tool_result_failed(entry: str | None) -> bool:
    if not entry:
        return False
    return bool(re.search(r"[\"']?is_error[\"']?\s*:\s*true\b", entry))


def _tool_result_succeeded(entry: str | None) -> bool:
    if not entry:
        return False
    return bool(re.search(r"[\"']?is_error[\"']?\s*:\s*false\b", entry))


def _customer_bug_missing_tracker_mirror(
    user_request: str,
    execution_context: str | None,
) -> bool:
    request = str(user_request or "")
    if not (_CUSTOMER_REPORT_RE.search(request) and _REQUESTED_WORK_ARTIFACT_RE.search(request)):
        return False
    github_entry = _recent_tool_result_entry(execution_context, "create_github_issue")
    if not _tool_result_succeeded(github_entry):
        return False
    domain_entries = _recent_tool_result_entries(execution_context, "manage_domain")
    return not any(
        _tool_result_succeeded(entry)
        and "create_record" in entry
        and bool(re.search(r'domain_id\\?"\s*:\s*1\b', entry))
        for entry in domain_entries
    )


def _requested_artifact_failure_omitted(
    user_request: str,
    candidate_output: str,
    execution_context: str | None,
) -> bool:
    if not _REQUESTED_WORK_ARTIFACT_RE.search(str(user_request or "")):
        return False
    github_entry = _recent_tool_result_entry(execution_context, "create_github_issue")
    if not _tool_result_failed(github_entry):
        return False
    candidate = str(candidate_output or "")
    names_artifact = bool(re.search(r"\bgithub\s+(?:issue|ticket)\b", candidate, re.IGNORECASE))
    names_failure = bool(_ARTIFACT_FAILURE_ACK_RE.search(candidate))
    names_blocker = bool(_ARTIFACT_BLOCKER_RE.search(candidate))
    return not (names_artifact and names_failure and names_blocker)


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

    grounding_issue = _ungrounded_product_surface_issue(candidate_output, execution_context)
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

    if _customer_bug_missing_tracker_mirror(user_request, execution_context):
        return FinalReplyReview(
            status="continue",
            approved=False,
            rationale=(
                "The customer-bug GitHub issue succeeded, but execution evidence has no successful "
                "linked tracker-record mirror."
            ),
            missing_requirements=(
                "Create the linked record in an existing tracker (Domain 1 by default); do not create a Domain.",
            ),
            raw_output="deterministic_customer_bug_mirror_contract",
            override="artifact_contract",
        ).to_dict()

    if _requested_artifact_failure_omitted(user_request, candidate_output, execution_context):
        return FinalReplyReview(
            status="continue",
            approved=False,
            rationale=(
                "The requested GitHub artifact failed, but the reply does not explicitly name both "
                "the GitHub issue and its concrete blocker."
            ),
            missing_requirements=(
                "Say that the requested GitHub issue was not created and name the exact blocker; "
                "describe any tracker record only as retention or handoff.",
            ),
            raw_output="deterministic_requested_artifact_contract",
            override="artifact_contract",
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
