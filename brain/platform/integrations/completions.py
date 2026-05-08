"""Provider-neutral text completion helpers."""

from __future__ import annotations

import logging

from brain.platform.integrations.llm import resolve_llm_client
from brain.platform.integrations.providers import LLMRequest, get_provider
from brain.platform.providers.model_policy import (
    MODEL_TIERS,
    get_default_model,
    get_model_for_tier,
    infer_provider_from_model,
    normalize_model_tier,
    resolve_default_provider,
)

logger = logging.getLogger("brain.platform.integrations.completions")

DEFAULT_SIMPLE_COMPLETION_SYSTEM_PROMPT = (
    "You are a precise assistant. Follow the user's request exactly and return only the requested content."
)


def _strip_provider_prefix(model: str) -> str:
    for prefix in ("anthropic/", "openai/", "anthropic:", "openai:"):
        if model.startswith(prefix):
            return model[len(prefix):]
    return model


def simple_text_completion(
    prompt: str,
    *,
    model: str | None = None,
    max_tokens: int = 512,
    user_id: str | None = None,
    org_id: str | None = None,
    system_prompt: str | None = None,
    reasoning_effort: str | None = None,
    operation_type: str | None = None,
) -> str | None:
    """Run a simple single-turn completion and return the text content."""
    default_provider = resolve_default_provider(user_id=user_id, org_id=org_id)
    tier = normalize_model_tier(model, default=None)
    if tier in MODEL_TIERS:
        model = get_model_for_tier(
            tier,
            provider=default_provider,
            include_provider_prefix=True,
            user_id=user_id,
            org_id=org_id,
        )
    resolved_model = _strip_provider_prefix(
        model
        or get_default_model(
            provider=default_provider,
            include_provider_prefix=False,
            user_id=user_id,
            org_id=org_id,
        )
    )
    requested_provider = infer_provider_from_model(model or resolved_model, default=default_provider)
    llm = resolve_llm_client(user_id=user_id, org_id=org_id, provider=requested_provider)
    provider = get_provider(llm.provider, llm.client)
    effective_system_prompt = system_prompt or DEFAULT_SIMPLE_COMPLETION_SYSTEM_PROMPT

    response = provider.create(LLMRequest(
        model=resolved_model,
        max_output_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
        system=effective_system_prompt,
        reasoning_effort=reasoning_effort,
        extra_headers=llm.build_request_headers() or None,
        operation_type=operation_type,
    ))
    if not getattr(response, "content", None):
        return None

    text_parts: list[str] = []
    for block in response.content:
        if getattr(block, "type", None) == "text" and getattr(block, "text", None):
            text_parts.append(block.text)

    text = "\n".join(part.strip() for part in text_parts if part and part.strip()).strip()
    return text or None
