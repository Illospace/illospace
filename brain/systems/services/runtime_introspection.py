"""Shared runtime/auth introspection for API and agent tools."""

from __future__ import annotations

import os
from typing import Any

from sqlalchemy import select

from brain.platform.db.models.org import OrgApiKey, User, UserApiKey
from brain.platform.db.repositories.unit_of_work import UnitOfWork, open_unit_of_work
from brain.platform.integrations.llm import resolve_llm_client
from brain.systems.learning.budget import LearningBudgetPolicy
from brain.systems.learning.policy import build_learning_policy_from_env
from brain.platform.providers.model_policy import (
    get_provider_model_map,
    normalize_default_provider,
    normalize_runtime_provider,
    resolve_default_provider,
)
from brain.platform.provider_health import provider_health_snapshot
from brain.systems.routing import get_routing_marketplace_snapshot
from brain.systems.runs.predict_rlm_backend import get_agent_worker_backend_settings


def _env_bool(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _resolve_effective_org_id(user_id: str | None, org_id: str | None) -> str | None:
    if org_id or not user_id:
        return org_id
    try:
        with open_unit_of_work(UnitOfWork) as uow:
            user = uow.session.get(User, user_id)
            return user.org_id if user else None
    except Exception:
        return org_id


def get_provider_auth_status(
    *,
    user_id: str | None,
    org_id: str | None,
    provider: str | None = None,
) -> dict[str, Any]:
    """Return provider-specific auth/runtime status for a user/org."""
    org_id = _resolve_effective_org_id(user_id, org_id)
    effective_provider = normalize_default_provider(resolve_default_provider(user_id=user_id, org_id=org_id))
    provider = normalize_runtime_provider(provider or effective_provider)
    if provider not in {"anthropic", "openai"}:
        raise ValueError(f"Unsupported provider: {provider}")

    has_personal_key = False
    has_org_key = False
    personal_default_key_id = None
    runtime_credential_id = None
    runtime_credential_name = None

    if user_id:
        try:
            with open_unit_of_work(UnitOfWork) as uow:
                db_user = uow.session.get(User, user_id)
                if db_user:
                    personal_default_key_id = db_user.default_api_key_id
                stmt = (
                    select(UserApiKey.id)
                    .where(
                        UserApiKey.user_id == user_id,
                        UserApiKey.provider == provider,
                        UserApiKey.is_active == True,  # noqa: E712
                    )
                    .limit(1)
                )
                if uow.session.scalars(stmt).first():
                    has_personal_key = True
                if org_id:
                    stmt = (
                        select(OrgApiKey.id)
                        .where(
                            OrgApiKey.org_id == org_id,
                            OrgApiKey.provider == provider,
                        )
                        .limit(1)
                    )
                    if uow.session.scalars(stmt).first():
                        has_org_key = True
        except Exception:
            pass

    llm = None
    runtime_available = False
    runtime_source = "none"
    runtime_auth_mode = None
    runtime_uses_external_auth = False
    if user_id:
        try:
            llm = resolve_llm_client(
                user_id=user_id,
                org_id=org_id,
                provider=provider,
            )
            runtime_available = True
            runtime_source = llm.source
            runtime_auth_mode = getattr(llm, "auth_mode", None)
            runtime_uses_external_auth = runtime_source == "codex_cache"
        except Exception:
            pass

    authenticated = runtime_available
    method = "none"
    if provider == "anthropic" and runtime_available:
        method = "setup_token" if bool(getattr(llm, "is_oauth", False)) else "api_key"
    elif provider == "openai" and runtime_available:
        method = "chatgpt" if runtime_auth_mode == "chatgpt" else "api_key"

    connect_instructions = (
        "Run claude setup-token in your terminal, then paste the token here."
        if provider == "anthropic"
        else "Connect ChatGPT / Codex below, or paste an OpenAI API key."
    )
    connect_placeholder = "sk-ant-oat01-..." if provider == "anthropic" else "sk-..."

    is_selected_provider = provider == effective_provider
    runtime_scope = (
        "personal" if runtime_source == "user_default"
        else "org" if runtime_source == "org_main"
        else "external" if runtime_source == "codex_cache"
        else "env" if runtime_source in {"env", "dotenv"}
        else "none"
    )

    if user_id and runtime_scope in {"personal", "org"}:
        try:
            with open_unit_of_work(UnitOfWork) as uow:
                if runtime_scope == "personal" and personal_default_key_id is not None:
                    key_row = uow.session.get(UserApiKey, personal_default_key_id)
                    if key_row and key_row.provider == provider and key_row.is_active:
                        runtime_credential_id = key_row.id
                        runtime_credential_name = key_row.label or "default"
                elif runtime_scope == "org" and org_id:
                    stmt = (
                        select(OrgApiKey)
                        .where(
                            OrgApiKey.org_id == org_id,
                            OrgApiKey.provider == provider,
                        )
                        .limit(1)
                    )
                    org_key_row = uow.session.scalars(stmt).first()
                    if org_key_row:
                        runtime_credential_id = org_key_row.id
                        runtime_credential_name = org_key_row.label or "org default"
        except Exception:
            pass

    if is_selected_provider and runtime_available:
        status = "in_use"
    elif has_personal_key or has_org_key or runtime_uses_external_auth:
        status = "available"
    else:
        status = "not_configured"

    runtime_label = (
        "your personal credential" if runtime_scope == "personal"
        else "the org default credential" if runtime_scope == "org"
        else "a local Codex session fallback" if runtime_scope == "external"
        else "an environment credential" if runtime_scope == "env"
        else "no credential"
    )

    return {
        "provider": provider,
        "effective_provider": effective_provider,
        "is_selected_provider": is_selected_provider,
        "status": status,
        "authenticated": authenticated,
        "method": method,
        "auth_mode": runtime_auth_mode,
        "has_personal_db_key": has_personal_key,
        "has_org_db_key": has_org_key,
        "has_db_keys": has_personal_key or has_org_key,
        "runtime_key_available": authenticated,
        "runtime_key_source": runtime_source,
        "runtime_key_scope": runtime_scope,
        "runtime_key_label": runtime_label,
        "runtime_credential_id": runtime_credential_id,
        "runtime_credential_name": runtime_credential_name,
        "runtime_uses_db_key": runtime_source in ("user_default", "org_main"),
        "runtime_uses_external_auth": runtime_uses_external_auth,
        "personal_default_key_id": personal_default_key_id,
        "setup_required": not authenticated and not (has_personal_key or has_org_key),
        "supports_db_api_keys": provider in {"anthropic", "openai"},
        "supports_external_auth": provider == "openai",
        "connect_instructions": connect_instructions,
        "connect_placeholder": connect_placeholder,
    }


def get_learning_budget_runtime_settings() -> dict[str, Any]:
    """Return environment-derived learning budget defaults for introspection."""
    return LearningBudgetPolicy.from_env().to_payload()


def get_learning_policy_runtime_settings() -> dict[str, Any]:
    """Return environment-derived tenant learning policy defaults."""
    return build_learning_policy_from_env().to_payload()


def get_runtime_settings_snapshot(
    *,
    user_id: str | None,
    org_id: str | None,
    provider: str | None = None,
) -> dict[str, Any]:
    """Return a compact runtime/settings snapshot for agent introspection."""
    effective_provider = normalize_default_provider(resolve_default_provider(user_id=user_id, org_id=org_id))
    selected_provider = normalize_runtime_provider(provider or effective_provider)
    provider_status = get_provider_auth_status(
        user_id=user_id,
        org_id=org_id,
        provider=selected_provider,
    )
    return {
        "selected_provider": selected_provider,
        "effective_provider": effective_provider,
        "user_id": user_id,
        "org_id": org_id,
        "learning_budget": get_learning_budget_runtime_settings(),
        "learning_policy": get_learning_policy_runtime_settings(),
        "provider_health": provider_health_snapshot(),
        "providers": {
            "anthropic": get_provider_auth_status(user_id=user_id, org_id=org_id, provider="anthropic"),
            "openai": get_provider_auth_status(user_id=user_id, org_id=org_id, provider="openai"),
        },
        "provider_model_mappings": {
            "anthropic": get_provider_model_map("anthropic", user_id=user_id, org_id=org_id),
            "openai": get_provider_model_map("openai", user_id=user_id, org_id=org_id),
        },
        "worker_backend": get_agent_worker_backend_settings(user_id=user_id, org_id=org_id).to_dict(),
        "active": provider_status,
        "routing_marketplace": get_routing_marketplace_snapshot(
            user_id=user_id,
            org_id=org_id,
            provider=selected_provider,
        ),
    }
