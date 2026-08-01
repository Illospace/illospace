"""Provider-neutral text completion helpers."""

from __future__ import annotations

import logging

from brain.platform.async_io import run_blocking
from brain.platform.integrations.llm import (
    async_resolve_llm_client,
    resolve_llm_client,
)
from brain.platform.integrations.providers import LLMRequest, get_provider
from brain.platform.providers.model_policy import (
    get_default_model,
    infer_provider_from_model,
    required_openai_auth_mode,
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


def _completion_text(response) -> str | None:
    if not getattr(response, "content", None):
        return None

    text_parts: list[str] = []
    for block in response.content:
        if getattr(block, "type", None) == "text" and getattr(block, "text", None):
            text_parts.append(block.text)

    text = "\n".join(part.strip() for part in text_parts if part and part.strip()).strip()
    return text or None


def _completion_request(
    prompt: str,
    *,
    model: str,
    max_tokens: int,
    system_prompt: str | None,
    reasoning_effort: str | None,
    operation_type: str | None,
    extra_headers: dict[str, str] | None,
) -> LLMRequest:
    return LLMRequest(
        model=model,
        max_output_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
        system=system_prompt or DEFAULT_SIMPLE_COMPLETION_SYSTEM_PROMPT,
        reasoning_effort=reasoning_effort,
        extra_headers=extra_headers,
        operation_type=operation_type,
    )


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
    response = provider.create(
        _completion_request(
            prompt,
            model=resolved_model,
            max_tokens=max_tokens,
            system_prompt=system_prompt,
            reasoning_effort=reasoning_effort,
            operation_type=operation_type,
            extra_headers=llm.build_request_headers() or None,
        )
    )
    return _completion_text(response)


async def async_simple_text_completion(
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
    """Run a simple completion with DB-backed user and org authentication."""
    default_provider = resolve_default_provider(user_id=user_id, org_id=org_id)
    resolved_model = _strip_provider_prefix(
        model
        or get_default_model(
            provider=default_provider,
            include_provider_prefix=False,
            user_id=user_id,
            org_id=org_id,
        )
    )
    requested_provider = infer_provider_from_model(
        model or resolved_model,
        default=default_provider,
    )
    llm = await async_resolve_llm_client(
        user_id=user_id,
        org_id=org_id,
        provider=requested_provider,
        auth_mode=(
            required_openai_auth_mode(model or resolved_model)
            if requested_provider == "openai"
            else None
        ),
    )
    provider = get_provider(llm.provider, llm.client)
    response = await run_blocking(
        provider.create,
        _completion_request(
            prompt,
            model=resolved_model,
            max_tokens=max_tokens,
            system_prompt=system_prompt,
            reasoning_effort=reasoning_effort,
            operation_type=operation_type,
            extra_headers=llm.build_request_headers() or None,
        ),
    )
    return _completion_text(response)
