"""LLM-first memory harvest extraction.

This module owns the deterministic contract around memory extraction:
models decide semantics, while Python validates schema, length, safety,
scope, and persistence metadata. If the extraction provider is unavailable
or returns invalid structured output, we capture only a low-confidence raw
episode.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, datetime
import json
import logging
import os
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from brain.platform.async_io import http_post
from brain.platform.integrations.llm import resolve_llm_client
from brain.platform.integrations.providers import LLMRequest, get_provider
from brain.platform.providers.model_policy import infer_provider_from_model, resolve_default_provider

logger = logging.getLogger(__name__)

MEMORY_EXTRACTION_SCHEMA_VERSION = 1
HARVEST_MAX_ITEMS = 12
HARVEST_MAX_CONTENT_CHARS = 600
HARVEST_MAX_TRANSCRIPT_CHARS = 4000
RAW_EPISODE_CONFIDENCE = 0.15

MemoryKind = Literal[
    "fact",
    "preference",
    "decision",
    "commitment",
    "lesson",
    "procedure",
    "correction",
]
Sensitivity = Literal["low", "medium", "high", "restricted"]
ExtractionScope = Literal["personal", "project", "team", "org", "global"]

MEMORY_KIND_TO_TYPE: dict[str, str] = {
    "fact": "fact",
    "preference": "preference",
    "decision": "decision",
    "commitment": "decision",
    "lesson": "lesson",
    "procedure": "procedure",
    "correction": "fact",
    "raw_episode": "episode",
}

SENSITIVITY_ORDER = {"low": 0, "medium": 1, "high": 2, "restricted": 3}

HARVEST_SYSTEM_PROMPT = (
    "You are Illo's memory extraction engine. Extract durable memories from "
    "a conversation. You must make semantic decisions yourself and return "
    "strict JSON only. Do not output chain-of-thought."
)

HARVEST_PROMPT = """\
Extract memory candidates from this conversation.

Return a JSON object that satisfies the provided schema:
- schema_version must be 1.
- memories is an array. Use an empty array when nothing durable should be remembered.
- kind must be one of: fact, preference, decision, commitment, lesson, procedure, correction.
- confidence is 0.0-1.0.
- sensitivity is one of: low, medium, high, restricted.
- scope is one of: personal, project, team, org, global.
- expiry is an ISO 8601 date/datetime string or null.
- evidence must cite short direct evidence from the conversation.
- topic_tags should be short normalized topic labels.

Guidance:
- Facts are stable claims about the user, product, team, system, or environment.
- Preferences are user/team preferences.
- Decisions are choices already made.
- Commitments are explicit future obligations or promised follow-ups.
- Lessons are reusable learnings from outcomes or corrections.
- Procedures are reusable ordered ways to do something.
- Corrections capture user corrections to previous understanding.
- If team/private scope is ambiguous, choose the narrower scope and higher sensitivity.
- Never include secrets in content or evidence.

Conversation:
{conversation}
""".strip()

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")

_CODE_FENCE_RE = re.compile(r"```(?:json)?\s*\n?(.*?)\n?\s*```", re.DOTALL)
_SECRET_TOKEN_RE = re.compile(
    r"(?i)\b("
    r"sk-[A-Za-z0-9_-]{12,}|"
    r"ghp_[A-Za-z0-9_]{12,}|"
    r"github_pat_[A-Za-z0-9_]{16,}|"
    r"xox[baprs]-[A-Za-z0-9-]{10,}|"
    r"bearer\s+[A-Za-z0-9._~+/=-]{12,}|"
    r"api[_-]?key\s*[:=]\s*[A-Za-z0-9._~+/=-]{12,}"
    r")\b"
)
_PII_RE = re.compile(
    r"(?i)("
    r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b|"
    r"\b\d{1,3}(?:\.\d{1,3}){3}\b|"
    r"\b(?:\+?1[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?)\d{3}[-.\s]?\d{4}\b"
    r")"
)


class HarvestExtractionEvidence(BaseModel):
    """Evidence for one extracted memory."""

    message_index: int | None = Field(default=None, ge=0, le=999)
    role: Literal["user", "assistant", "system", "tool", "unknown"] = "unknown"
    quote: str = Field(min_length=1, max_length=500)

    model_config = ConfigDict(extra="forbid")

    @field_validator("quote")
    @classmethod
    def _strip_quote(cls, value: str) -> str:
        cleaned = " ".join(str(value).split())
        if not cleaned:
            raise ValueError("quote cannot be empty")
        return cleaned[:500]


class HarvestExtractionMemory(BaseModel):
    """Strict model-facing contract for a semantic memory candidate."""

    content: str = Field(min_length=12, max_length=HARVEST_MAX_CONTENT_CHARS)
    kind: MemoryKind
    confidence: float = Field(ge=0.0, le=1.0)
    sensitivity: Sensitivity
    scope: ExtractionScope
    expiry: str | None = None
    evidence: list[HarvestExtractionEvidence] = Field(min_length=1, max_length=3)
    topic_tags: list[str] = Field(default_factory=list, max_length=6)

    model_config = ConfigDict(extra="forbid")

    @field_validator("content")
    @classmethod
    def _strip_content(cls, value: str) -> str:
        cleaned = " ".join(str(value).split())
        if len(cleaned) < 12:
            raise ValueError("content is too short")
        return cleaned[:HARVEST_MAX_CONTENT_CHARS]

    @field_validator("expiry")
    @classmethod
    def _validate_expiry(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = str(value).strip()
        if not cleaned:
            return None
        try:
            if "T" in cleaned:
                datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
            else:
                date.fromisoformat(cleaned)
        except ValueError as exc:
            raise ValueError("expiry must be ISO 8601 date/datetime or null") from exc
        return cleaned

    @field_validator("topic_tags")
    @classmethod
    def _normalize_tags(cls, value: list[Any]) -> list[str]:
        tags: list[str] = []
        seen: set[str] = set()
        for tag in value or []:
            normalized = str(tag).strip().lower()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            tags.append(normalized[:40])
            if len(tags) >= 6:
                break
        return tags


class HarvestExtractionPayload(BaseModel):
    """Top-level provider response contract."""

    schema_version: Literal[MEMORY_EXTRACTION_SCHEMA_VERSION]
    memories: list[HarvestExtractionMemory] = Field(default_factory=list, max_length=HARVEST_MAX_ITEMS)

    model_config = ConfigDict(extra="forbid")


@dataclass
class HarvestItem:
    """A validated memory candidate ready for scoped persistence."""

    content: str
    harvest_type: str
    confidence: float
    topic_tags: list[str] = field(default_factory=list)
    sensitivity: str = "low"
    scope: str = "personal"
    expiry: str | None = None
    evidence: list[dict[str, Any]] = field(default_factory=list)
    raw_episode: bool = False

    @property
    def memory_type(self) -> str:
        return MEMORY_KIND_TO_TYPE.get(self.harvest_type, "episode")

    @property
    def memory_tier(self) -> str:
        return "episodic"

    @property
    def salience(self) -> float:
        if self.raw_episode:
            return 3.0
        return 5.0 + (self.confidence * 2.0)

    def visibility_for(self, org_id: str | None) -> str:
        """Return the safest existing visibility for this item."""
        if self.sensitivity in {"high", "restricted"}:
            return "private"
        if self.scope == "personal":
            return "private"
        if self.scope == "team" and org_id:
            return "team"
        if self.scope in {"project", "org", "global"} and org_id:
            return "org"
        return "private"

    def storage_scope(self, org_id: str | None) -> str:
        """Normalize schema scope into the existing memory scope column."""
        if self.scope == "global":
            return "project" if org_id else "personal"
        if self.scope in {"project", "team", "org"} and org_id:
            return "project"
        return "personal"

    def evidence_payload(self) -> dict[str, Any]:
        return {
            "schema_version": MEMORY_EXTRACTION_SCHEMA_VERSION,
            "harvest_type": self.harvest_type,
            "sensitivity": self.sensitivity,
            "scope": self.scope,
            "expiry": self.expiry,
            "topic_tags": list(self.topic_tags),
            "evidence": list(self.evidence),
            "raw_episode": self.raw_episode,
        }


def extract_harvest_items(
    messages: list[dict],
    model: str | None = None,
    *,
    user_id: str | None = None,
    org_id: str | None = None,
) -> list[HarvestItem]:
    """Extract validated memory candidates from conversation messages.

    Provider-unavailable or invalid structured output degrades to exactly one
    low-confidence raw episode. A valid empty extraction returns an empty list.
    """
    if not messages:
        return []
    if model is None:
        from brain.platform.providers.model_policy import get_model_for_tier

        model = get_model_for_tier(
            "low",
            include_provider_prefix=True,
            user_id=user_id,
            org_id=org_id,
        )

    conversation_text = _format_messages(messages)
    prompt = HARVEST_PROMPT.format(conversation=conversation_text[:HARVEST_MAX_TRANSCRIPT_CHARS])

    raw_response: str | None = None
    try:
        raw_response = _call_extraction_provider(
            prompt,
            model,
            user_id=user_id,
            org_id=org_id,
        )
    except Exception:
        logger.exception("Model call failed for harvest extraction")

    if raw_response:
        items, valid = _parse_response_with_status(raw_response)
        if valid:
            return items
        logger.warning("Harvest model returned invalid structured output; storing raw episode only")
    else:
        logger.info("Harvest extraction provider unavailable; storing raw episode only")

    return _fallback_raw_episode(
        conversation_text,
        reason="provider_unavailable" if not raw_response else "invalid_structured_output",
    )


def _format_messages(messages: list[dict]) -> str:
    """Format message dicts into a simple indexed conversation transcript."""
    lines: list[str] = []
    for index, msg in enumerate(messages):
        role = str(msg.get("role", "unknown") or "unknown")
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                str(block.get("text", ""))
                for block in content
                if isinstance(block, dict)
            )
        content_text = str(content).strip()
        if content_text:
            lines.append(f"[{index}] {role}: {content_text}")
    return "\n".join(lines)


def _call_extraction_provider(
    prompt: str,
    model: str,
    *,
    user_id: str | None = None,
    org_id: str | None = None,
) -> str | None:
    """Route extraction through local Ollama or provider-neutral LLMRequest."""
    if model.startswith("ollama:"):
        return _call_ollama(prompt, model.removeprefix("ollama:"))

    requested_model = _normalize_model_name(model)
    requested_provider = infer_provider_from_model(
        requested_model,
        default=resolve_default_provider(user_id=user_id, org_id=org_id),
    )
    llm = resolve_llm_client(user_id=user_id, org_id=org_id, provider=requested_provider)
    provider = get_provider(llm.provider, llm.client)
    response_format = _response_format() if llm.provider == "openai" else None

    response = provider.create(
        LLMRequest(
            model=requested_model,
            max_output_tokens=1200,
            messages=[{"role": "user", "content": prompt}],
            system=HARVEST_SYSTEM_PROMPT,
            extra_headers=llm.build_request_headers() or None,
            response_format=response_format,
            operation_type="memory_extraction",
        )
    )
    return _response_text(response)


def _normalize_model_name(model: str) -> str:
    if model.startswith("claude:"):
        return f"anthropic:{model.removeprefix('claude:')}"
    return model


def _call_ollama(prompt: str, model: str) -> str | None:
    """Call Ollama using its schema-format hint when supported."""
    try:
        payload = {
            "model": model,
            "prompt": f"{HARVEST_SYSTEM_PROMPT}\n\n{prompt}",
            "stream": False,
            "format": _json_schema_for_provider(),
            "options": {"temperature": 0.0, "num_predict": 1200},
            "think": False,
            "keep_alive": "5m",
        }
        resp = http_post(
            f"{OLLAMA_URL}/api/generate",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        resp.raise_for_status()
        result = resp.json()
        return str(result.get("response", "")).strip() or None
    except Exception as e:
        logger.warning("Ollama harvest call failed: %s", e)
        return None


def _response_text(response: Any) -> str | None:
    text_parts: list[str] = []
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", None) == "text" and getattr(block, "text", None):
            text_parts.append(str(block.text))
    text = "\n".join(part.strip() for part in text_parts if part and part.strip()).strip()
    return text or None


def _parse_response(raw: str) -> list[HarvestItem]:
    """Parse a provider response and return items only when it is valid."""
    items, valid = _parse_response_with_status(raw)
    return items if valid else []


def _parse_response_with_status(raw: str) -> tuple[list[HarvestItem], bool]:
    payload_text = _extract_json_text(raw)
    try:
        data = json.loads(payload_text)
        payload = HarvestExtractionPayload.model_validate(data)
    except (json.JSONDecodeError, ValidationError, TypeError, ValueError) as exc:
        logger.warning("Failed to validate harvest structured output: %s", exc)
        return [], False

    items: list[HarvestItem] = []
    for memory in payload.memories:
        item = _to_harvest_item(memory)
        if item is not None:
            items.append(item)
    return items, True


def _extract_json_text(raw: str) -> str:
    text = str(raw or "").strip()
    fence_match = _CODE_FENCE_RE.search(text)
    if fence_match:
        return fence_match.group(1).strip()
    return text


def _to_harvest_item(memory: HarvestExtractionMemory) -> HarvestItem | None:
    raw_evidence_quotes = [ev.quote for ev in memory.evidence]
    sensitivity = _enforced_sensitivity(
        " ".join([memory.content, *raw_evidence_quotes]),
        memory.sensitivity,
    )
    content = _redact_sensitive_text(memory.content)
    evidence = [
        {
            "message_index": ev.message_index,
            "role": ev.role,
            "quote": _redact_sensitive_text(ev.quote),
        }
        for ev in memory.evidence
    ]
    if len(content.strip()) < 12:
        return None
    return HarvestItem(
        content=content[:HARVEST_MAX_CONTENT_CHARS],
        harvest_type=memory.kind,
        confidence=max(0.0, min(1.0, float(memory.confidence))),
        topic_tags=list(memory.topic_tags),
        sensitivity=sensitivity,
        scope=memory.scope,
        expiry=memory.expiry,
        evidence=evidence,
    )


def _enforced_sensitivity(text: str, model_sensitivity: str) -> str:
    sensitivity = model_sensitivity if model_sensitivity in SENSITIVITY_ORDER else "low"
    if _SECRET_TOKEN_RE.search(text):
        return "restricted"
    if _PII_RE.search(text) and SENSITIVITY_ORDER[sensitivity] < SENSITIVITY_ORDER["high"]:
        return "high"
    return sensitivity


def _redact_sensitive_text(text: str) -> str:
    return _SECRET_TOKEN_RE.sub("[REDACTED_SECRET]", str(text))


def _fallback_raw_episode(conversation_text: str, *, reason: str) -> list[HarvestItem]:
    """Capture a raw episode without semantic classification."""
    content = _redact_sensitive_text(conversation_text).strip()
    if not content:
        return []
    if len(content) > 1200:
        content = content[:1190].rstrip() + "..."
    return [
        HarvestItem(
            content=f"Raw conversation episode captured because memory extraction {reason}: {content}",
            harvest_type="raw_episode",
            confidence=RAW_EPISODE_CONFIDENCE,
            topic_tags=["raw-episode"],
            sensitivity=_enforced_sensitivity(content, "high" if _PII_RE.search(content) else "medium"),
            scope="personal",
            evidence=[{
                "message_index": None,
                "role": "unknown",
                "quote": "Provider unavailable or invalid structured output; semantic extraction skipped.",
            }],
            raw_episode=True,
        )
    ]


def _response_format() -> dict[str, Any]:
    return {
        "type": "json_schema",
        "name": "MemoryExtractionPayload",
        "strict": True,
        "schema": _json_schema_for_provider(),
    }


def _json_schema_for_provider() -> dict[str, Any]:
    return _strict_json_schema(HarvestExtractionPayload.model_json_schema())


def _strict_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Normalize JSON Schema for providers with strict structured outputs."""
    normalized = deepcopy(schema)

    def _walk(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                _walk(item)
            return
        if not isinstance(node, dict):
            return

        for key in ("$defs", "definitions", "properties"):
            child = node.get(key)
            if isinstance(child, dict):
                for value in child.values():
                    _walk(value)

        if "items" in node:
            _walk(node["items"])
        for key in ("anyOf", "allOf", "oneOf", "prefixItems"):
            if key in node:
                _walk(node[key])

        if node.get("type") == "object" or "properties" in node:
            properties = node.get("properties")
            node["required"] = list(properties.keys()) if isinstance(properties, dict) else []
            node["additionalProperties"] = False

    _walk(normalized)
    return normalized
