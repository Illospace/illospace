"""Cortex display-title generation."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from brain.platform.async_io import run_blocking

logger = logging.getLogger(__name__)

TITLE_GENERATION_SYSTEM_PROMPT = (
    "You generate concise display titles for raw thoughts and idea drafts. "
    "Return only the title text. Keep it specific, descriptive, and compact. "
    "Target 3 to 6 words when possible. Do not add explanations, quotes, bullets, or prefixes."
)
TITLE_PREFIX_RE = re.compile(
    r"^(?:title|suggested title|headline|summary title)\s*:\s*",
    re.IGNORECASE,
)
TITLE_MODEL_TIER = "low"
TITLE_REASONING_EFFORT = "low"
TITLE_INPUT_CHAR_LIMIT = 500
TITLE_MAX_TOKENS = 20


@dataclass(frozen=True)
class StoredDisplayTitle:
    idea_id: str
    title: str | None = None
    updated: bool = False
    skipped_reason: str | None = None


def _title_generation_local_prompt(raw_text: str) -> str:
    thought = (raw_text or "").strip()[:TITLE_INPUT_CHAR_LIMIT]
    return (
        f"{TITLE_GENERATION_SYSTEM_PROMPT}\n\n"
        "Raw thought or idea:\n"
        f"{thought}\n\n"
        "Title:"
    )


def _title_generation_user_prompt(raw_text: str) -> str:
    thought = (raw_text or "").strip()[:TITLE_INPUT_CHAR_LIMIT]
    return (
        "Raw thought or idea:\n"
        f"{thought}\n\n"
        "Return the best concise display title."
    )


def normalize_generated_title(title: str | None) -> str | None:
    title = (title or "").strip()
    if not title:
        return None

    first_line = next((line.strip() for line in title.splitlines() if line.strip()), "")
    candidate = TITLE_PREFIX_RE.sub("", first_line).strip()
    candidate = candidate.strip("`\"'\u201c\u201d\u2018\u2019[](){} ")
    candidate = re.sub(r"\s+", " ", candidate).strip()

    if candidate.endswith((".", "!", "?", ";")) and len(candidate) > 4:
        candidate = candidate[:-1].strip()

    if candidate and 2 < len(candidate) < 60:
        return candidate
    return None


def _strip_provider_prefix(model: str | None) -> str:
    value = (model or "").strip()
    for prefix in ("anthropic/", "openai/", "anthropic:", "openai:"):
        if value.startswith(prefix):
            return value[len(prefix):]
    return value


def is_local_title_model(model: str | None) -> bool:
    normalized = _strip_provider_prefix(model).strip().lower()
    return (
        normalized == "local"
        or normalized == "gpu_server"
        or normalized.startswith("local/")
        or normalized.startswith("gpu_server/")
        or normalized.startswith("brain.platform.gpu/")
    )


def _provider_model_spec(provider: str, model: str) -> str:
    value = (model or "").strip()
    if value.startswith(("anthropic/", "openai/")):
        return value
    if value.startswith("anthropic:"):
        return f"anthropic/{value[len('anthropic:'):]}"
    if value.startswith("openai:"):
        return f"openai/{value[len('openai:'):]}"
    return f"{provider}/{value}"


def _local_title_runtime_ready(client) -> bool:
    try:
        return client.is_ready("llm")
    except Exception:
        # If health probing fails, still attempt generation once.
        return True


def _generate_with_local_title_model(raw_text: str) -> str | None:
    try:
        from brain.platform.gpu_client import get_client

        client = get_client()
        if not _local_title_runtime_ready(client):
            logger.info("Local title generation skipped because the configured low-tier llm worker is not ready")
            return None

        return normalize_generated_title(
            client.generate(
                prompt=_title_generation_local_prompt(raw_text),
                max_tokens=TITLE_MAX_TOKENS,
                temperature=0.3,
                think=False,
                fallback_policy="local-only",
            )
        )
    except Exception as exc:
        logger.warning("Local title generation failed: %s", exc)
        return None


def _generate_with_provider_title_model(
    raw_text: str,
    *,
    model: str,
    user_id: str | None,
    org_id: str | None,
) -> str | None:
    try:
        from brain.platform.integrations.completions import simple_text_completion

        return normalize_generated_title(
            simple_text_completion(
                _title_generation_user_prompt(raw_text),
                model=model,
                max_tokens=TITLE_MAX_TOKENS,
                user_id=user_id,
                org_id=org_id,
                system_prompt=TITLE_GENERATION_SYSTEM_PROMPT,
                reasoning_effort=TITLE_REASONING_EFFORT,
                operation_type="title_generation",
            )
        )
    except Exception as exc:
        logger.warning("Provider title generation failed: %s", exc)
        return None


async def _async_generate_with_provider_title_model(
    raw_text: str,
    *,
    provider: str,
    model: str,
    user_id: str | None,
    org_id: str | None,
) -> str | None:
    try:
        from brain.platform.integrations.llm import async_resolve_llm_client
        from brain.platform.integrations.providers import LLMRequest, get_provider

        llm = await async_resolve_llm_client(
            user_id=user_id,
            org_id=org_id,
            provider=provider,
        )
        provider_client = get_provider(llm.provider, llm.client)
        response = await run_blocking(
            provider_client.create,
            LLMRequest(
                model=_strip_provider_prefix(model),
                max_output_tokens=TITLE_MAX_TOKENS,
                messages=[{"role": "user", "content": _title_generation_user_prompt(raw_text)}],
                system=TITLE_GENERATION_SYSTEM_PROMPT,
                reasoning_effort=TITLE_REASONING_EFFORT,
                extra_headers=llm.build_request_headers() or None,
                operation_type="title_generation",
            ),
        )
        text_parts = [
            block.text
            for block in getattr(response, "content", None) or []
            if getattr(block, "type", None) == "text" and getattr(block, "text", None)
        ]
        return normalize_generated_title("\n".join(text_parts))
    except Exception as exc:
        logger.warning("Provider title generation failed: %s", exc)
        return None


def generate_display_title(
    raw_text: str,
    *,
    user_id: str | None = None,
    org_id: str | None = None,
) -> str | None:
    """Generate a display title using the configured low-intelligence model."""
    if not (raw_text or "").strip():
        return None

    try:
        from brain.platform.providers.model_policy import get_model_for_tier, resolve_default_provider

        provider = resolve_default_provider(user_id=user_id, org_id=org_id)
        model = get_model_for_tier(
            TITLE_MODEL_TIER,
            provider=provider,
            include_provider_prefix=False,
            user_id=user_id,
            org_id=org_id,
        )
    except Exception as exc:
        logger.warning("Title model resolution failed: %s", exc)
        return None

    if is_local_title_model(model):
        return _generate_with_local_title_model(raw_text)

    return _generate_with_provider_title_model(
        raw_text,
        model=_provider_model_spec(provider, model),
        user_id=user_id,
        org_id=org_id,
    )


async def async_generate_display_title(
    raw_text: str,
    *,
    user_id: str | None = None,
    org_id: str | None = None,
) -> str | None:
    """Async display-title generation that can resolve DB-backed provider auth."""
    if not (raw_text or "").strip():
        return None

    try:
        from brain.platform.db.repositories.unit_of_work import UnitOfWork
        from brain.platform.providers.model_policy import async_get_model_for_tier, async_resolve_default_provider

        async with UnitOfWork() as uow:
            provider = await async_resolve_default_provider(uow.session, user_id=user_id, org_id=org_id)
            model = await async_get_model_for_tier(
                uow.session,
                TITLE_MODEL_TIER,
                provider=provider,
                include_provider_prefix=False,
                user_id=user_id,
                org_id=org_id,
            )
    except Exception as exc:
        logger.warning("Title model resolution failed: %s", exc)
        return None

    if is_local_title_model(model):
        return await run_blocking(_generate_with_local_title_model, raw_text)

    return await _async_generate_with_provider_title_model(
        raw_text,
        provider=provider,
        model=_provider_model_spec(provider, model),
        user_id=user_id,
        org_id=org_id,
    )


def _blank(value: str | None) -> bool:
    return not str(value or "").strip()


async def _idea_title_source(
    idea_id: str,
    *,
    user_id: str | None,
    org_id: str | None,
    raw_title: str | None,
) -> tuple[str | None, str | None]:
    from brain.platform.db.models.idea import Idea
    from brain.platform.db.repositories.unit_of_work import UnitOfWork

    async with UnitOfWork() as uow:
        idea = await uow.session.get(Idea, str(idea_id))
        if idea is None:
            return None, "missing"
        if getattr(idea, "archived_at", None) is not None:
            return None, "archived"
        if not _blank(getattr(idea, "display_title", None)):
            return None, "already_titled"
        if org_id and str(getattr(idea, "org_id", "") or "") != str(org_id):
            return None, "scope_mismatch"
        if not org_id and user_id and str(getattr(idea, "user_id", "") or "") != str(user_id):
            return None, "scope_mismatch"

        title = str(getattr(idea, "title", "") or "").strip()
        if not title and raw_title:
            title = str(raw_title).strip()
        if not title:
            return None, "empty_title"
        return title, None


async def _store_generated_display_title(
    idea_id: str,
    *,
    source_title: str,
    display_title: str,
    user_id: str | None,
    org_id: str | None,
) -> bool:
    from sqlalchemy import func, or_, update

    from brain.platform.db.models.idea import Idea
    from brain.platform.db.repositories.unit_of_work import UnitOfWork

    async with UnitOfWork() as uow:
        stmt = (
            update(Idea)
            .where(
                Idea.id == str(idea_id),
                Idea.title == source_title,
                Idea.archived_at.is_(None),
                or_(Idea.display_title.is_(None), func.trim(Idea.display_title) == ""),
            )
            .values(display_title=display_title)
        )
        if org_id:
            stmt = stmt.where(Idea.org_id == str(org_id))
        elif user_id:
            stmt = stmt.where(Idea.user_id == str(user_id))

        result = await uow.session.execute(stmt)
        return int(getattr(result, "rowcount", 0) or 0) == 1


def _publish_generated_display_title(
    idea_id: str,
    title: str,
    *,
    org_id: str | None,
) -> None:
    from brain.systems.cortex.events import publish

    payload: dict[str, str] = {"idea_id": str(idea_id), "title": title}
    if org_id:
        payload["org_id"] = str(org_id)
    publish("title_generated", payload)


async def generate_and_store_idea_display_title(
    idea_id: str,
    *,
    user_id: str | None = None,
    org_id: str | None = None,
    raw_title: str | None = None,
    publish_update: bool = True,
) -> StoredDisplayTitle:
    """Generate, store, and publish a display title for an idea if it still needs one."""
    source_title, skipped_reason = await _idea_title_source(
        idea_id,
        user_id=user_id,
        org_id=org_id,
        raw_title=raw_title,
    )
    if skipped_reason or not source_title:
        return StoredDisplayTitle(idea_id=str(idea_id), skipped_reason=skipped_reason or "empty_title")

    title = await async_generate_display_title(source_title, user_id=user_id, org_id=org_id)
    if not title:
        return StoredDisplayTitle(idea_id=str(idea_id), skipped_reason="generation_failed")

    updated = await _store_generated_display_title(
        idea_id,
        source_title=source_title,
        display_title=title,
        user_id=user_id,
        org_id=org_id,
    )
    if not updated:
        return StoredDisplayTitle(idea_id=str(idea_id), title=title, skipped_reason="stale")

    if publish_update:
        try:
            _publish_generated_display_title(idea_id, title, org_id=org_id)
        except Exception:
            logger.exception("Failed to publish generated title for idea %s", idea_id)

    return StoredDisplayTitle(idea_id=str(idea_id), title=title, updated=True)
