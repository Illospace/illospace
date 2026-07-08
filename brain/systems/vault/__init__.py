"""Illo Brain — org-owned Secret Vault.

Fernet-encrypted secret storage with DB persistence, org ownership, and
actor audit logging.
"""

import hashlib
import logging
import os
import secrets as stdlib_secrets
from datetime import datetime, timedelta, timezone
from enum import Enum

import bcrypt
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.db.models.org import OrgApiKey, User, UserCodexConnection
from brain.platform.db.models.vault import (
    Secret,
    VaultAccessLog,
    VaultAgentGrant,
    VaultConfig,
    VaultMissingRequest,
    VaultProjectBinding,
    VaultSession,
)
from brain.platform.db.repositories.unit_of_work import UnitOfWork
from brain.platform.vault_crypto import _decrypt, _encrypt, _get_fernet

logger = logging.getLogger(__name__)

USER_OPENAI_API_KEY_SOURCE = "user_openai"
USER_OPENAI_CODEX_SOURCE = "codex_subscription"

VAULT_UNLOCK_HEADER = "X-Vault-Token"
VAULT_SESSION_TTL = timedelta(minutes=15)
VAULT_LOCKOUT_AFTER_FAILURES = 3
VAULT_LOCKOUT_DURATION = timedelta(minutes=5)
VAULT_AGENT_GRANT_TTL = timedelta(minutes=15)
VAULT_ACCESS_ACTORS = {"user", "agent", "api"}
VAULT_ACCESS_ACTOR_ALIASES = {
    "github_connector": "api",
}
VAULT_AGENT_ACCESS_AVAILABLE = "available"
VAULT_AGENT_ACCESS_ASK = "ask"
VAULT_AGENT_ACCESS_MANUAL = "manual"
VAULT_AGENT_ACCESS_LEVELS = {
    VAULT_AGENT_ACCESS_AVAILABLE,
    VAULT_AGENT_ACCESS_ASK,
    VAULT_AGENT_ACCESS_MANUAL,
}


class AgentSecretAccessIntent(str, Enum):
    REFERENCE = "reference"
    READ = "read"

    @property
    def consumes_approved_grant(self) -> bool:
        return self is AgentSecretAccessIntent.READ


DEFAULT_VAULT_AGENT_ACCESS_LEVEL = VAULT_AGENT_ACCESS_ASK

# ---------------------------------------------------------------------------
# Audit logging
# ---------------------------------------------------------------------------

def _normalize_accessed_by(accessed_by: str | None) -> str:
    """Map integration-specific callers onto vault_access_log's actor categories."""
    raw = (accessed_by or "").strip().lower()
    if not raw:
        return "user"
    if raw in VAULT_ACCESS_ACTORS:
        return raw
    return VAULT_ACCESS_ACTOR_ALIASES.get(raw, "api")


def normalize_agent_access_level(value: str | None) -> str:
    """Normalize a secret's agent availability policy."""
    level = (value or DEFAULT_VAULT_AGENT_ACCESS_LEVEL).strip().lower()
    if level not in VAULT_AGENT_ACCESS_LEVELS:
        raise ValueError(
            "agent_access_level must be one of: "
            + ", ".join(sorted(VAULT_AGENT_ACCESS_LEVELS))
        )
    return level


def _normalize_project_slug(value: str | None) -> str:
    return (value or "").strip().lower()


def _normalize_env_name(value: str | None) -> str:
    env_name = (value or "").strip()
    if not env_name:
        raise ValueError("env_name is required")
    first = env_name[0]
    if (
        not env_name.isascii()
        or not (first.isalpha() or first == "_")
        or not all(ch.isalnum() or ch == "_" for ch in env_name)
    ):
        raise ValueError("env_name must be a valid environment variable name")
    return env_name


def _secret_read_dict(
    secret: Secret,
) -> dict:
    return {
        "id": secret.id,
        "key_name": secret.key_name,
        "description": secret.description,
        "category": secret.category,
        "created_at": secret.created_at,
        "updated_at": secret.updated_at,
        "last_accessed_at": secret.last_accessed_at,
        "access_count": secret.access_count,
        "org_id": secret.org_id,
        "created_by_user_id": secret.created_by_user_id,
        "updated_by_user_id": secret.updated_by_user_id,
        "agent_access_level": (
            getattr(secret, "agent_access_level", None)
            or DEFAULT_VAULT_AGENT_ACCESS_LEVEL
        ),
    }


async def _async_secret_by_key(
    uow: UnitOfWork,
    *,
    org_id: str,
    key_name: str,
) -> Secret | None:
    return (
        await uow.session.scalars(
            select(Secret)
            .where(Secret.org_id == org_id, Secret.key_name == key_name)
            .limit(1)
        )
    ).first()


async def _async_secret_by_id_for_org(
    uow: UnitOfWork,
    *,
    secret_id: int,
    org_id: str,
) -> Secret | None:
    secret = await uow.session.get(Secret, secret_id)
    if secret and str(secret.org_id) == str(org_id):
        return secret
    return None


async def _async_log_access(
    *,
    org_id: str,
    actor_user_id: str | None,
    secret_id: int | None,
    key_name: str,
    action: str,
    accessed_by: str = "user",
    uow: UnitOfWork | None = None,
) -> None:
    """Async variant of vault audit logging. Fail-silent."""

    try:
        entry = VaultAccessLog(
            org_id=org_id,
            actor_user_id=actor_user_id,
            secret_id=secret_id,
            key_name=key_name,
            action=action,
            accessed_by=_normalize_accessed_by(accessed_by),
        )
        if uow:
            uow.session.add(entry)
        else:
            async with UnitOfWork() as _uow:
                _uow.session.add(entry)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Org vault CRUD
# ---------------------------------------------------------------------------

def _require_org_id(org_id: str | None) -> str:
    value = (org_id or "").strip()
    if not value:
        raise ValueError("org_id is required for org-owned vault operations")
    return value


def _require_actor_user_id(actor_user_id: str | None) -> str:
    value = (actor_user_id or "").strip()
    if not value or value.startswith("service:"):
        raise ValueError("actor_user_id is required for vault attribution")
    return value


async def get_secret(
    key_name: str,
    actor_user_id: str,
    *,
    org_id: str | None,
    allow_env_fallback: bool = False,
    accessed_by: str = "user",
) -> str | None:
    """Retrieve and decrypt a secret from the caller's org vault."""
    return await async_get_secret(
        key_name,
        actor_user_id,
        org_id=org_id,
        allow_env_fallback=allow_env_fallback,
        accessed_by=accessed_by,
    )


async def async_get_secret(
    key_name: str,
    actor_user_id: str,
    *,
    org_id: str | None,
    allow_env_fallback: bool = False,
    accessed_by: str = "user",
) -> str | None:
    """Read an org-owned secret and record actor attribution."""
    clean_org_id = _require_org_id(org_id)
    clean_actor_user_id = _require_actor_user_id(actor_user_id)
    async with UnitOfWork() as uow:
        secret = await _async_secret_by_key(uow, org_id=clean_org_id, key_name=key_name)
        if secret:
            secret.last_accessed_at = datetime.now(timezone.utc)
            secret.access_count = (secret.access_count or 0) + 1
            await _async_log_access(
                org_id=clean_org_id,
                actor_user_id=clean_actor_user_id,
                secret_id=secret.id,
                key_name=key_name,
                action="read",
                accessed_by=accessed_by,
                uow=uow,
            )
            return _decrypt(bytes(secret.encrypted_value))

    if allow_env_fallback and (env_val := os.environ.get(key_name)):
        return env_val
    await _async_record_missing(key_name, actor_user_id=clean_actor_user_id, org_id=clean_org_id)
    return None


async def set_secret(
    key_name: str,
    value: str,
    actor_user_id: str,
    *,
    org_id: str | None,
    description: str = "",
    category: str = "general",
    agent_access_level: str | None = None,
) -> None:
    """Encrypt and upsert a secret into the actor's org vault."""
    await async_set_secret(
        key_name,
        value,
        actor_user_id,
        org_id=org_id,
        description=description,
        category=category,
        agent_access_level=agent_access_level,
    )


async def async_set_secret(
    key_name: str,
    value: str,
    actor_user_id: str,
    *,
    org_id: str | None,
    description: str = "",
    category: str = "general",
    agent_access_level: str | None = None,
) -> None:
    """Create or update the org-owned secret identified by ``key_name``."""
    clean_org_id = _require_org_id(org_id)
    clean_actor_user_id = _require_actor_user_id(actor_user_id)
    encrypted = _encrypt(value)
    now = datetime.now(timezone.utc)
    normalized_agent_access = (
        normalize_agent_access_level(agent_access_level)
        if agent_access_level is not None
        else None
    )
    async with UnitOfWork() as uow:
        existing = await _async_secret_by_key(uow, org_id=clean_org_id, key_name=key_name)
        if existing:
            existing.encrypted_value = encrypted
            existing.description = description
            existing.category = category
            existing.updated_by_user_id = clean_actor_user_id
            if normalized_agent_access is not None:
                existing.agent_access_level = normalized_agent_access
            existing.updated_at = now
            secret = existing
        else:
            secret = Secret(
                key_name=key_name,
                encrypted_value=encrypted,
                description=description,
                category=category,
                agent_access_level=normalized_agent_access or DEFAULT_VAULT_AGENT_ACCESS_LEVEL,
                org_id=clean_org_id,
                created_by_user_id=clean_actor_user_id,
                updated_by_user_id=clean_actor_user_id,
                created_at=now,
                updated_at=now,
            )
            uow.session.add(secret)
        await uow.session.flush()
        await _async_log_access(
            org_id=clean_org_id,
            actor_user_id=clean_actor_user_id,
            secret_id=secret.id,
            key_name=key_name,
            action="write",
            uow=uow,
        )
    await async_resolve_missing(key_name, actor_user_id=clean_actor_user_id, org_id=clean_org_id)


async def delete_secret(key_name: str, actor_user_id: str, *, org_id: str | None) -> bool:
    """Delete a secret from an org vault. Returns True when it existed."""
    return await async_delete_secret(key_name, actor_user_id, org_id=org_id)


async def list_secrets(
    actor_user_id: str,
    category: str | None = None,
    *,
    org_id: str | None,
) -> list[dict]:
    """Return org vault metadata. Secret values are never included."""
    return await async_list_secrets(actor_user_id, category, org_id=org_id)


async def async_list_secrets(
    actor_user_id: str,
    category: str | None = None,
    *,
    org_id: str | None,
) -> list[dict]:
    """List the secrets owned by one org vault."""
    del actor_user_id
    clean_org_id = _require_org_id(org_id)
    async with UnitOfWork() as uow:
        stmt = select(Secret).where(Secret.org_id == clean_org_id)
        if category:
            stmt = stmt.where(Secret.category == category)
        stmt = stmt.order_by(Secret.category, Secret.key_name)
        return [_secret_read_dict(secret) for secret in (await uow.session.scalars(stmt)).all()]


async def async_get_secret_record(
    key_name: str,
    actor_user_id: str,
    *,
    org_id: str | None,
) -> Secret | None:
    """Fetch org-owned secret metadata."""
    del actor_user_id
    clean_org_id = _require_org_id(org_id)
    async with UnitOfWork() as uow:
        return await _async_secret_by_key(uow, org_id=clean_org_id, key_name=key_name)


async def reveal_secret(key_name: str, actor_user_id: str, *, org_id: str | None) -> str | None:
    """Reveal a secret for the dashboard and update access stats."""
    return await async_reveal_secret(key_name, actor_user_id=actor_user_id, org_id=org_id)


async def async_reveal_secret(
    key_name: str,
    actor_user_id: str,
    *,
    org_id: str | None,
) -> str | None:
    """Dashboard reveal path for an org-owned secret."""
    return await async_get_secret(key_name, actor_user_id=actor_user_id, org_id=org_id, accessed_by="user")


async def require_secret(
    key_name: str,
    actor_user_id: str,
    *,
    org_id: str | None,
    allow_env_fallback: bool = False,
) -> str:
    """Like get_secret but raises ValueError if not found anywhere."""
    value = await async_get_secret(
        key_name,
        actor_user_id=actor_user_id,
        org_id=org_id,
        allow_env_fallback=allow_env_fallback,
    )
    if value is None:
        raise ValueError(f"Secret '{key_name}' not found in vault or environment")
    return value


# ---------------------------------------------------------------------------
# Project-bound agent token availability
# ---------------------------------------------------------------------------

def _binding_to_dict(binding: VaultProjectBinding, secret: Secret | None = None) -> dict:
    return {
        "id": binding.id,
        "secret_id": binding.secret_id,
        "key_name": secret.key_name if secret else None,
        "agent_access_level": (
            getattr(secret, "agent_access_level", None) or DEFAULT_VAULT_AGENT_ACCESS_LEVEL
            if secret
            else None
        ),
        "org_id": binding.org_id,
        "created_by_user_id": binding.created_by_user_id,
        "target_registry_id": binding.target_registry_id,
        "project_slug": binding.project_slug,
        "env_name": binding.env_name,
        "active": binding.active,
        "created_at": binding.created_at,
        "updated_at": binding.updated_at,
    }


def _binding_project_matches(
    binding: VaultProjectBinding,
    *,
    project_slug: str | None = None,
    project_slugs: list[str] | tuple[str, ...] | set[str] | None = None,
    target_registry_id: int | None = None,
) -> bool:
    if not binding.active:
        return False
    if target_registry_id is not None and binding.target_registry_id is not None:
        if int(binding.target_registry_id) == int(target_registry_id):
            return True
    candidates = {_normalize_project_slug(project_slug)}
    candidates.update(_normalize_project_slug(slug) for slug in (project_slugs or []))
    candidates.discard("")
    return bool(binding.project_slug in candidates)


def _project_binding_rows_stmt(
    *,
    org_id: str,
    secret_id: int | None = None,
):
    stmt = (
        select(VaultProjectBinding, Secret)
        .join(Secret, Secret.id == VaultProjectBinding.secret_id)
        .where(
            VaultProjectBinding.active == True,  # noqa: E712
            VaultProjectBinding.org_id == org_id,
            Secret.org_id == org_id,
        )
    )
    if secret_id is not None:
        stmt = stmt.where(VaultProjectBinding.secret_id == secret_id)
    return stmt.order_by(VaultProjectBinding.project_slug, VaultProjectBinding.env_name)


async def _async_project_binding_rows(
    uow: UnitOfWork,
    *,
    org_id: str,
    secret_id: int | None = None,
) -> list[tuple[VaultProjectBinding, Secret]]:
    result = await uow.session.execute(
        _project_binding_rows_stmt(
            org_id=org_id,
            secret_id=secret_id,
        )
    )
    return list(result.all())


async def _async_upsert_project_binding(
    uow: UnitOfWork,
    secret: Secret,
    *,
    actor_user_id: str,
    org_id: str,
    project_slug: str,
    env_name: str,
    target_registry_id: int | None = None,
) -> VaultProjectBinding:
    clean_project_slug = _normalize_project_slug(project_slug)
    if not clean_project_slug:
        raise ValueError("project_slug is required")
    clean_env_name = _normalize_env_name(env_name)
    now = datetime.now(timezone.utc)
    stmt = select(VaultProjectBinding).where(
        VaultProjectBinding.org_id == org_id,
        VaultProjectBinding.project_slug == clean_project_slug,
        VaultProjectBinding.env_name == clean_env_name,
    )
    existing = (await uow.session.scalars(stmt.limit(1))).first()
    if existing:
        existing.secret_id = secret.id
        existing.target_registry_id = target_registry_id
        existing.active = True
        existing.updated_at = now
        return existing
    binding = VaultProjectBinding(
        secret_id=secret.id,
        org_id=org_id,
        created_by_user_id=actor_user_id,
        target_registry_id=target_registry_id,
        project_slug=clean_project_slug,
        env_name=clean_env_name,
        active=True,
        created_at=now,
        updated_at=now,
    )
    uow.session.add(binding)
    return binding


async def bind_project_secret(
    secret_id: int,
    *,
    actor_user_id: str,
    org_id: str | None,
    project_slug: str,
    env_name: str,
    target_registry_id: int | None = None,
) -> dict | None:
    """Bind an org-owned secret to a project/env name."""
    return await async_bind_project_secret(
        secret_id,
        actor_user_id=actor_user_id,
        org_id=org_id,
        project_slug=project_slug,
        env_name=env_name,
        target_registry_id=target_registry_id,
    )


async def bind_project_secret_by_key(
    key_name: str,
    *,
    actor_user_id: str,
    org_id: str | None,
    project_slug: str,
    env_name: str,
    target_registry_id: int | None = None,
) -> dict | None:
    """Bind an org-owned secret to a project/env name."""
    return await async_bind_project_secret_by_key(
        key_name,
        actor_user_id=actor_user_id,
        org_id=org_id,
        project_slug=project_slug,
        env_name=env_name,
        target_registry_id=target_registry_id,
    )


async def async_bind_project_secret_by_key(
    key_name: str,
    *,
    actor_user_id: str,
    org_id: str | None,
    project_slug: str,
    env_name: str,
    target_registry_id: int | None = None,
) -> dict | None:
    """Bind an org-owned secret to a project/env name."""
    clean_org_id = _require_org_id(org_id)
    clean_actor_user_id = _require_actor_user_id(actor_user_id)
    clean_key_name = (key_name or "").strip()
    if not clean_key_name:
        raise ValueError("key_name is required")

    async with UnitOfWork() as uow:
        secret = await _async_secret_by_key(
            uow,
            org_id=clean_org_id,
            key_name=clean_key_name,
        )
        if secret is None:
            return None

        binding = await _async_upsert_project_binding(
            uow,
            secret,
            actor_user_id=clean_actor_user_id,
            org_id=clean_org_id,
            project_slug=project_slug,
            env_name=env_name,
            target_registry_id=target_registry_id,
        )
        await uow.session.flush()
        return _binding_to_dict(binding, secret)


async def list_project_bindings(
    *,
    actor_user_id: str,
    org_id: str | None,
    secret_id: int | None = None,
) -> list[dict]:
    """List project token bindings in the actor's org vault."""
    return await async_list_project_bindings(actor_user_id, org_id=org_id, secret_id=secret_id)


async def delete_project_binding(
    binding_id: int,
    *,
    actor_user_id: str,
    org_id: str | None,
) -> bool:
    """Deactivate a project binding in the actor's org vault."""
    return await async_delete_project_binding(binding_id, actor_user_id=actor_user_id, org_id=org_id)


async def resolve_project_bound_env_tokens(
    *,
    actor_user_id: str,
    org_id: str | None,
    project_slug: str | None = None,
    project_slugs: list[str] | tuple[str, ...] | set[str] | None = None,
    target_registry_id: int | None = None,
) -> dict[str, str]:
    """Return env-name/token pairs for project-bound secrets.

    Org-owned tokens are eligible for every member in the org. Manual secrets
    are intentionally excluded; ask-level secrets become available only through
    a matching project binding, while available secrets still need a binding to
    define an env name.
    """
    return await async_resolve_project_bound_env_tokens(
        actor_user_id=actor_user_id,
        org_id=org_id,
        project_slug=project_slug,
        project_slugs=project_slugs,
        target_registry_id=target_registry_id,
    )


async def async_resolve_project_bound_env_tokens(
    *,
    actor_user_id: str,
    org_id: str | None,
    project_slug: str | None = None,
    project_slugs: list[str] | tuple[str, ...] | set[str] | None = None,
    target_registry_id: int | None = None,
    github_app_only: bool = False,
) -> dict[str, str]:
    """Return env-name/token pairs for matching org project bindings."""
    if not project_slug and not project_slugs and target_registry_id is None:
        return {}
    clean_org_id = _require_org_id(org_id)
    clean_actor_user_id = _require_actor_user_id(actor_user_id)
    env_assignments: list[tuple[str, str | None, str | None, str | None]] = []
    async with UnitOfWork() as uow:
        for binding, secret in [
            (binding, secret)
            for binding, secret in await _async_project_binding_rows(uow, org_id=clean_org_id)
            if _binding_project_matches(
                binding,
                project_slug=project_slug,
                project_slugs=project_slugs,
                target_registry_id=target_registry_id,
            )
        ]:
            if github_app_only and getattr(secret, "category", None) != "github_app":
                continue
            if getattr(secret, "category", None) == "github_app":
                secret.last_accessed_at = datetime.now(timezone.utc)
                secret.access_count = (secret.access_count or 0) + 1
                await _async_log_access(
                    org_id=clean_org_id,
                    actor_user_id=clean_actor_user_id,
                    secret_id=secret.id,
                    key_name=secret.key_name,
                    action="read",
                    # github_app mint reads audit as agent on the binding lane.
                    accessed_by="agent",
                    uow=uow,
                )
                repo_name = binding.project_slug.split("/")[-1]
                decrypted = _decrypt(bytes(secret.encrypted_value))
                env_assignments.append((binding.env_name, None, repo_name, decrypted))
                decrypted = None
                continue
            if normalize_agent_access_level(getattr(secret, "agent_access_level", None)) == VAULT_AGENT_ACCESS_MANUAL:
                continue
            secret.last_accessed_at = datetime.now(timezone.utc)
            secret.access_count = (secret.access_count or 0) + 1
            await _async_log_access(
                org_id=clean_org_id,
                actor_user_id=clean_actor_user_id,
                secret_id=secret.id,
                key_name=secret.key_name,
                action="read",
                accessed_by="agent",
                uow=uow,
            )
            env_assignments.append((binding.env_name, _decrypt(bytes(secret.encrypted_value)), None, None))

    env: dict[str, str] = {}
    while env_assignments:
        env_name, value, repo_name, decrypted_blob = env_assignments.pop(0)
        if decrypted_blob is None:
            assert value is not None
            env[env_name] = value
            continue
        from brain.systems.vault.github_app_mint import async_mint_installation_token

        assert repo_name is not None
        try:
            env[env_name] = await async_mint_installation_token(
                decrypted_blob,
                repositories=[repo_name],
                permissions={"issues": "write"},
            )
        finally:
            decrypted_blob = None
    return env


# ---------------------------------------------------------------------------
# Runtime provider credentials
# ---------------------------------------------------------------------------

def _openai_user_connection_source(credential_payload: str) -> str | None:
    """Classify a stored OpenAI connection without treating ChatGPT as an API key."""
    payload = (credential_payload or "").strip()
    if not payload:
        return None
    if payload.startswith("sk-"):
        return USER_OPENAI_API_KEY_SOURCE

    try:
        from brain.platform.integrations.openai_codex_auth import parse_codex_auth_payload

        credential = parse_codex_auth_payload(payload, source="vault")
    except Exception:
        return None

    if credential.auth_mode == "api_key" and credential.access_token:
        return USER_OPENAI_API_KEY_SOURCE
    if credential.auth_mode == "chatgpt":
        return USER_OPENAI_CODEX_SOURCE
    return None


async def resolve_api_key(
    user_id: str | None = None,
    org_id: str | None = None,
    provider: str = "anthropic",
    auth_mode: str | None = None,
) -> tuple[str | None, str]:
    """Resolve a runtime provider credential.

    Provider API keys are org-owned except for a user's own OpenAI model
    connection, which may be either a Codex/ChatGPT session or an API key.

    Returns (key, source) where source describes which level resolved.
    """
    return await async_resolve_api_key(
        user_id=user_id,
        org_id=org_id,
        provider=provider,
        auth_mode=auth_mode,
    )


async def async_resolve_api_key(
    user_id: str | None = None,
    org_id: str | None = None,
    provider: str = "anthropic",
    auth_mode: str | None = None,
    *,
    session: AsyncSession | None = None,
) -> tuple[str | None, str]:
    """Resolve a provider credential using the async DB path."""

    async def _resolve(active_session: AsyncSession) -> tuple[str | None, str]:
        normalized_provider = provider.strip().lower()
        if normalized_provider == "openai" and user_id:
            stmt = (
                select(UserCodexConnection)
                .where(
                    UserCodexConnection.user_id == user_id,
                    UserCodexConnection.is_active == True,  # noqa: E712
                )
                .limit(1)
            )
            codex_connection = (await active_session.scalars(stmt)).first()
            if codex_connection:
                credential = _decrypt(bytes(codex_connection.encrypted_credential))
                user_source = _openai_user_connection_source(credential)
                if auth_mode == "api_key":
                    if user_source == USER_OPENAI_API_KEY_SOURCE:
                        return credential, user_source
                elif auth_mode == "chatgpt":
                    if user_source == USER_OPENAI_CODEX_SOURCE:
                        return credential, user_source
                elif user_source:
                    return credential, user_source

        effective_org_id = org_id
        if not effective_org_id and user_id:
            user = await active_session.get(User, user_id)
            if user:
                effective_org_id = str(user.org_id)

        if effective_org_id:
            stmt = select(OrgApiKey).where(
                OrgApiKey.org_id == effective_org_id,
                OrgApiKey.provider == normalized_provider,
            )
            org_key = (await active_session.scalars(stmt)).first()
            if org_key:
                return _decrypt(bytes(org_key.encrypted_key)), "org_main"

        env_key = os.environ.get(f"{normalized_provider.upper()}_API_KEY")
        if env_key:
            return env_key, "env"
        return None, "none"

    if session is not None:
        return await _resolve(session)
    async with UnitOfWork() as uow:
        return await _resolve(uow.session)  # type: ignore[arg-type]


async def update_resolved_api_key(
    *,
    user_id: str | None = None,
    org_id: str | None = None,
    provider: str = "anthropic",
    source: str,
    api_key: str,
) -> bool:
    """Update the DB credential row that ``resolve_api_key`` selected.

    This is used by OAuth-style providers when a refresh returns a rotated
    token bundle. Environment-backed keys are intentionally not writable.
    """
    return await async_update_resolved_api_key(
        user_id=user_id,
        org_id=org_id,
        provider=provider,
        source=source,
        api_key=api_key,
    )


async def async_update_resolved_api_key(
    *,
    user_id: str | None = None,
    org_id: str | None = None,
    provider: str = "anthropic",
    source: str,
    api_key: str,
    session: AsyncSession | None = None,
) -> bool:
    """Update the credential row selected by ``async_resolve_api_key``."""
    if source not in {USER_OPENAI_CODEX_SOURCE, USER_OPENAI_API_KEY_SOURCE, "org_main"}:
        return False

    encrypted = _encrypt(api_key)

    async def _update(active_session: AsyncSession) -> bool:
        normalized_provider = provider.strip().lower()
        if (
            source in {USER_OPENAI_CODEX_SOURCE, USER_OPENAI_API_KEY_SOURCE}
            and user_id
            and normalized_provider == "openai"
        ):
            stmt = (
                select(UserCodexConnection)
                .where(
                    UserCodexConnection.user_id == user_id,
                    UserCodexConnection.is_active == True,  # noqa: E712
                )
                .limit(1)
            )
            connection = (await active_session.scalars(stmt)).first()
            if connection:
                connection.encrypted_credential = encrypted
                connection.is_active = True
                await active_session.flush()
                return True

        if source == "org_main":
            effective_org_id = org_id
            if not effective_org_id and user_id:
                user = await active_session.get(User, user_id)
                if user:
                    effective_org_id = str(user.org_id)

            if effective_org_id:
                stmt = select(OrgApiKey).where(
                    OrgApiKey.org_id == effective_org_id,
                    OrgApiKey.provider == normalized_provider,
                )
                org_key = (await active_session.scalars(stmt)).first()
                if org_key:
                    org_key.encrypted_key = encrypted
                    await active_session.flush()
                    return True

        return False

    if session is not None:
        return await _update(session)
    async with UnitOfWork() as uow:
        return await _update(uow.session)  # type: ignore[arg-type]


async def set_org_api_key(
    org_id: str,
    api_key: str,
    provider: str = "anthropic",
    label: str = "main",
) -> int:
    """Store an encrypted provider API key for an org. Returns the key ID."""
    return await async_set_org_api_key(org_id, api_key, provider=provider, label=label)


async def async_set_org_api_key(
    org_id: str,
    api_key: str,
    provider: str = "anthropic",
    label: str = "main",
    *,
    session: AsyncSession | None = None,
) -> int:
    """Store an encrypted org provider API key using the async DB path."""
    encrypted = _encrypt(api_key)

    async def _set(active_session: AsyncSession) -> int:
        clean_org_id = _require_org_id(org_id)
        normalized_provider = provider.strip().lower()
        stmt = select(OrgApiKey).where(
            OrgApiKey.org_id == clean_org_id,
            OrgApiKey.provider == normalized_provider,
        )
        existing = (await active_session.scalars(stmt)).first()
        if existing:
            existing.encrypted_key = encrypted
            existing.label = label
            await active_session.flush()
            return int(existing.id)

        key_obj = OrgApiKey(
            org_id=clean_org_id,
            provider=normalized_provider,
            encrypted_key=encrypted,
            label=label,
        )
        active_session.add(key_obj)
        await active_session.flush()
        return int(key_obj.id)

    if session is not None:
        return await _set(session)
    async with UnitOfWork() as uow:
        return await _set(uow.session)  # type: ignore[arg-type]


async def set_user_codex_connection(
    user_id: str,
    credential_payload: str,
    *,
    label: str = "Codex / ChatGPT",
) -> int:
    """Store a user's personal OpenAI runtime credential."""
    return await async_set_user_codex_connection(user_id, credential_payload, label=label)


async def async_set_user_codex_connection(
    user_id: str,
    credential_payload: str,
    *,
    label: str = "Codex / ChatGPT",
    session: AsyncSession | None = None,
) -> int:
    """Store a user's personal OpenAI runtime credential using async DB access."""
    clean_user_id = _require_actor_user_id(user_id)
    encrypted = _encrypt(credential_payload)

    async def _set(active_session: AsyncSession) -> int:
        stmt = select(UserCodexConnection).where(
            UserCodexConnection.user_id == clean_user_id,
        )
        existing = (await active_session.scalars(stmt)).first()
        if existing:
            existing.encrypted_credential = encrypted
            existing.label = label
            existing.is_active = True
            await active_session.flush()
            return int(existing.id)

        connection = UserCodexConnection(
            user_id=clean_user_id,
            encrypted_credential=encrypted,
            label=label,
        )
        active_session.add(connection)
        await active_session.flush()
        return int(connection.id)

    if session is not None:
        return await _set(session)
    async with UnitOfWork() as uow:
        return await _set(uow.session)  # type: ignore[arg-type]


async def record_api_key_usage(
    api_key_id: int,
    tokens_used: int,
    cost_usd: float,
) -> None:
    """Record token usage for spend tracking."""
    await async_record_api_key_usage(api_key_id, tokens_used, cost_usd)


async def async_record_api_key_usage(
    api_key_id: int,
    tokens_used: int,
    cost_usd: float,
) -> None:
    """Record org provider-key usage for spend tracking using async DB access."""
    async with UnitOfWork() as uow:
        key_obj = await uow.session.get(OrgApiKey, api_key_id)
        if key_obj:
            key_obj.last_used_at = datetime.now(timezone.utc)
            key_obj.total_tokens_used = (key_obj.total_tokens_used or 0) + tokens_used
            key_obj.estimated_cost_usd = float(key_obj.estimated_cost_usd or 0) + cost_usd


async def get_vault_access_log(
    actor_user_id: str,
    *,
    org_id: str | None,
    limit: int = 50,
) -> list[dict]:
    """Return access log scoped to the caller's org vault."""
    return await async_get_vault_access_log(actor_user_id, org_id=org_id, limit=limit)


# ---------------------------------------------------------------------------
# Missing secret tracking
# ---------------------------------------------------------------------------

def _record_missing(
    key_name: str,
    *,
    actor_user_id: str | None = None,
    org_id: str | None = None,
) -> None:
    """Record that a secret was requested but not found."""
    raise RuntimeError("Use _async_record_missing()")


async def _async_record_missing(
    key_name: str,
    *,
    actor_user_id: str | None = None,
    org_id: str | None = None,
) -> None:
    """Record a missing org vault secret. Fail-silent."""
    try:
        clean_org_id = _require_org_id(org_id)
    except ValueError:
        return
    now = datetime.now(timezone.utc)
    try:
        async with UnitOfWork() as uow:
            stmt = select(VaultMissingRequest).where(
                VaultMissingRequest.org_id == clean_org_id,
                VaultMissingRequest.key_name == key_name,
                VaultMissingRequest.resolved == False,  # noqa: E712
            )
            existing = (await uow.session.scalars(stmt.limit(1))).first()
            if existing:
                existing.actor_user_id = actor_user_id or existing.actor_user_id
                existing.request_count = (existing.request_count or 0) + 1
                existing.last_requested = now
                existing.resolved = False
            else:
                uow.session.add(
                    VaultMissingRequest(
                        key_name=key_name,
                        actor_user_id=actor_user_id,
                        org_id=clean_org_id,
                        request_count=1,
                        first_requested=now,
                        last_requested=now,
                        resolved=False,
                    )
                )
    except Exception:
        pass


async def record_missing_request(
    key_name: str,
    *,
    actor_user_id: str | None = None,
    org_id: str | None = None,
) -> None:
    """Record a missing secret request from a non-read workflow."""
    await async_record_missing_request(key_name, actor_user_id=actor_user_id, org_id=org_id)


async def async_record_missing_request(
    key_name: str,
    *,
    actor_user_id: str | None = None,
    org_id: str | None = None,
) -> None:
    """Record a missing secret request from an async workflow."""
    await _async_record_missing(key_name, actor_user_id=actor_user_id, org_id=org_id)


async def get_missing_requests(
    *,
    actor_user_id: str | None = None,
    org_id: str | None = None,
) -> list[dict]:
    return await async_get_missing_requests(actor_user_id=actor_user_id, org_id=org_id)


async def resolve_missing(
    key_name: str,
    *,
    actor_user_id: str | None = None,
    org_id: str | None = None,
) -> None:
    await async_resolve_missing(key_name, actor_user_id=actor_user_id, org_id=org_id)


async def async_resolve_missing(
    key_name: str,
    *,
    actor_user_id: str | None = None,
    org_id: str | None = None,
) -> None:
    del actor_user_id
    try:
        clean_org_id = _require_org_id(org_id)
        async with UnitOfWork() as uow:
            stmt = select(VaultMissingRequest).where(
                VaultMissingRequest.org_id == clean_org_id,
                VaultMissingRequest.key_name == key_name,
            )
            rows = (await uow.session.scalars(stmt)).all()
            for existing in rows:
                existing.resolved = True
    except Exception:
        logger.debug("vault_resolve_missing_failed", exc_info=True)


async def async_get_missing_requests(
    actor_user_id: str | None = None,
    *,
    org_id: str | None = None,
    include_resolved: bool = False,
) -> list[dict]:
    del actor_user_id
    clean_org_id = _require_org_id(org_id)
    async with UnitOfWork() as uow:
        stmt = select(VaultMissingRequest).where(VaultMissingRequest.org_id == clean_org_id)
        if not include_resolved:
            stmt = stmt.where(VaultMissingRequest.resolved == False)  # noqa: E712
        stmt = stmt.order_by(VaultMissingRequest.last_requested.desc()).limit(20)
        rows = (await uow.session.scalars(stmt)).all()
        return [
            {
                "key_name": r.key_name,
                "request_count": r.request_count,
                "first_requested": r.first_requested,
                "last_requested": r.last_requested,
                "actor_user_id": r.actor_user_id,
                "org_id": r.org_id,
            }
            for r in rows
        ]


# ---------------------------------------------------------------------------
# Agent grants
# ---------------------------------------------------------------------------

def _grant_to_dict(grant: VaultAgentGrant) -> dict:
    return {
        "id": grant.id,
        "key_name": grant.key_name,
        "org_id": grant.org_id,
        "requested_by_user_id": grant.requested_by_user_id,
        "run_id": grant.run_id,
        "requested_by": grant.requested_by,
        "reason": grant.reason,
        "status": grant.status,
        "approved_by_user_id": grant.approved_by_user_id,
        "requested_at": grant.requested_at,
        "decided_at": grant.decided_at,
        "expires_at": grant.expires_at,
        "last_used_at": grant.last_used_at,
        "read_count": grant.read_count,
        "max_reads": grant.max_reads,
    }


def _pending_grant_is_actionable(grant: VaultAgentGrant, secret: Secret | None) -> bool:
    if grant.status != "pending":
        return True
    if secret is None:
        return False
    return normalize_agent_access_level(getattr(secret, "agent_access_level", None)) == VAULT_AGENT_ACCESS_ASK


async def list_agent_grants(
    actor_user_id: str,
    *,
    org_id: str | None,
    statuses: list[str] | tuple[str, ...] | None = None,
    limit: int = 50,
) -> list[dict]:
    """List org vault access grants."""
    return await async_list_agent_grants(actor_user_id, org_id=org_id, statuses=list(statuses or []), limit=limit)


async def approve_agent_grant(
    grant_id: int,
    *,
    approved_by_user_id: str,
    org_id: str | None,
    ttl_minutes: int = 15,
    max_reads: int | None = None,
) -> dict | None:
    """Approve a pending org vault grant."""
    return await async_approve_agent_grant(
        grant_id,
        approved_by_user_id=approved_by_user_id,
        org_id=org_id,
        ttl_minutes=ttl_minutes,
        max_reads=max_reads or 1,
    )


async def deny_agent_grant(
    grant_id: int,
    *,
    denied_by_user_id: str,
    org_id: str | None,
) -> dict | None:
    """Deny/revoke an org vault grant."""
    return await async_deny_agent_grant(grant_id, denied_by_user_id=denied_by_user_id, org_id=org_id)


async def authorize_agent_secret_read(
    key_name: str,
    *,
    actor_user_id: str,
    org_id: str | None,
    run_id: int | None,
    reason: str | None,
    requested_by: str = "agent",
    project_slug: str | None = None,
    project_slugs: list[str] | tuple[str, ...] | set[str] | None = None,
    target_registry_id: int | None = None,
) -> dict:
    """Allow policy-based org vault reads, consume a grant, or request one."""
    return await async_authorize_agent_secret_read(
        key_name,
        actor_user_id=actor_user_id,
        org_id=org_id,
        run_id=run_id,
        reason=reason,
        requested_by=requested_by,
        project_slug=project_slug,
        project_slugs=project_slugs,
        target_registry_id=target_registry_id,
    )


async def authorize_agent_secret_reference(
    key_name: str,
    *,
    actor_user_id: str,
    org_id: str | None,
    run_id: int | None,
    reason: str | None,
    requested_by: str = "agent",
    project_slug: str | None = None,
    project_slugs: list[str] | tuple[str, ...] | set[str] | None = None,
    target_registry_id: int | None = None,
) -> dict:
    """Allow policy-based org vault references without consuming a read grant."""
    return await async_authorize_agent_secret_reference(
        key_name,
        actor_user_id=actor_user_id,
        org_id=org_id,
        run_id=run_id,
        reason=reason,
        requested_by=requested_by,
        project_slug=project_slug,
        project_slugs=project_slugs,
        target_registry_id=target_registry_id,
    )


async def async_authorize_agent_secret_read(
    key_name: str,
    *,
    actor_user_id: str,
    org_id: str | None,
    run_id: int | None,
    reason: str | None,
    requested_by: str = "agent",
    project_slug: str | None = None,
    project_slugs: list[str] | tuple[str, ...] | set[str] | None = None,
    target_registry_id: int | None = None,
) -> dict:
    """Policy check/approval flow for org-owned agent secret reads."""
    return await _async_authorize_agent_secret_access(
        key_name,
        actor_user_id=actor_user_id,
        org_id=org_id,
        run_id=run_id,
        reason=reason,
        requested_by=requested_by,
        project_slug=project_slug,
        project_slugs=project_slugs,
        target_registry_id=target_registry_id,
        intent=AgentSecretAccessIntent.READ,
    )


async def async_authorize_agent_secret_reference(
    key_name: str,
    *,
    actor_user_id: str,
    org_id: str | None,
    run_id: int | None,
    reason: str | None,
    requested_by: str = "agent",
    project_slug: str | None = None,
    project_slugs: list[str] | tuple[str, ...] | set[str] | None = None,
    target_registry_id: int | None = None,
) -> dict:
    """Policy check/approval flow for references that must not consume grants."""
    return await _async_authorize_agent_secret_access(
        key_name,
        actor_user_id=actor_user_id,
        org_id=org_id,
        run_id=run_id,
        reason=reason,
        requested_by=requested_by,
        project_slug=project_slug,
        project_slugs=project_slugs,
        target_registry_id=target_registry_id,
        intent=AgentSecretAccessIntent.REFERENCE,
    )


async def _async_authorize_agent_secret_access(
    key_name: str,
    *,
    actor_user_id: str,
    org_id: str | None,
    run_id: int | None,
    reason: str | None,
    requested_by: str = "agent",
    project_slug: str | None = None,
    project_slugs: list[str] | tuple[str, ...] | set[str] | None = None,
    target_registry_id: int | None = None,
    intent: AgentSecretAccessIntent,
) -> dict:
    """Policy check/approval flow for org-owned agent secret access."""
    try:
        clean_org_id = _require_org_id(org_id)
        clean_actor_user_id = _require_actor_user_id(actor_user_id)
    except ValueError as exc:
        return {"allowed": False, "status": "denied", "reason": str(exc)}

    async with UnitOfWork() as uow:
        secret = await _async_secret_by_key(uow, org_id=clean_org_id, key_name=key_name)
        if secret:
            access_level = normalize_agent_access_level(getattr(secret, "agent_access_level", None))
            if access_level == VAULT_AGENT_ACCESS_AVAILABLE:
                return {"allowed": True, "status": "available", "policy": {"agent_access_level": access_level}}
            if access_level == VAULT_AGENT_ACCESS_MANUAL:
                return {
                    "allowed": False,
                    "status": "denied",
                    "reason": "secret is marked manual and cannot be auto-read by agents",
                    "policy": {"agent_access_level": access_level},
                }
            rows = await _async_project_binding_rows(uow, org_id=clean_org_id, secret_id=secret.id)
            binding = next(
                (
                    binding
                    for binding, _ in rows
                    if _binding_project_matches(
                        binding,
                        project_slug=project_slug,
                        project_slugs=project_slugs,
                        target_registry_id=target_registry_id,
                    )
                ),
                None,
            )
            if binding:
                return {
                    "allowed": True,
                    "status": "project_bound",
                    "policy": {"agent_access_level": access_level},
                    "binding": _binding_to_dict(binding, secret),
                }
        else:
            await _async_record_missing(key_name, actor_user_id=clean_actor_user_id, org_id=clean_org_id)
            return {
                "allowed": False,
                "status": "missing",
                "reason": f"Secret '{key_name}' not found in Vault",
            }

    if not run_id:
        return {"allowed": False, "status": "denied", "reason": "run-scoped grant required"}
    clean_reason = (reason or "").strip()
    if len(clean_reason) < 8:
        return {"allowed": False, "status": "denied", "reason": "agent must provide a specific access reason"}

    now = datetime.now(timezone.utc)
    async with UnitOfWork() as uow:
        base = (
            VaultAgentGrant.org_id == clean_org_id,
            VaultAgentGrant.key_name == key_name,
            VaultAgentGrant.run_id == run_id,
        )
        approved_stmt = select(VaultAgentGrant).where(
            *base,
            VaultAgentGrant.status == "approved",
            VaultAgentGrant.read_count < VaultAgentGrant.max_reads,
            or_(VaultAgentGrant.expires_at.is_(None), VaultAgentGrant.expires_at > now),
        )
        grant = (
            await uow.session.scalars(
                approved_stmt.order_by(VaultAgentGrant.expires_at.desc()).limit(1).with_for_update()
            )
        ).first()
        if grant:
            if intent.consumes_approved_grant:
                grant.read_count = int(grant.read_count or 0) + 1
                grant.last_used_at = now
                if grant.read_count >= int(grant.max_reads or 1):
                    grant.status = "used"
                await uow.session.flush()
            return {"allowed": True, "status": "approved", "grant": _grant_to_dict(grant)}

        pending_stmt = select(VaultAgentGrant).where(*base, VaultAgentGrant.status == "pending")
        pending = (
            await uow.session.scalars(
                pending_stmt.order_by(VaultAgentGrant.requested_at.desc()).limit(1).with_for_update()
            )
        ).first()
        if pending:
            pending.reason = clean_reason
            pending.requested_by = requested_by
            pending.requested_by_user_id = clean_actor_user_id
            pending.requested_at = now
            await uow.session.flush()
            return {"allowed": False, "status": "pending", "grant": _grant_to_dict(pending)}

        pending = VaultAgentGrant(
            key_name=key_name,
            org_id=clean_org_id,
            requested_by_user_id=clean_actor_user_id,
            run_id=run_id,
            requested_by=requested_by,
            reason=clean_reason,
            status="pending",
            requested_at=now,
            max_reads=1,
            read_count=0,
        )
        uow.session.add(pending)
        await uow.session.flush()
        return {"allowed": False, "status": "pending", "grant": _grant_to_dict(pending)}


async def async_delete_secret(key_name: str, actor_user_id: str, *, org_id: str | None) -> bool:
    """Delete a secret through the org vault path."""
    clean_org_id = _require_org_id(org_id)
    clean_actor_user_id = _require_actor_user_id(actor_user_id)
    async with UnitOfWork() as uow:
        secret = await _async_secret_by_key(uow, org_id=clean_org_id, key_name=key_name)
        if not secret:
            return False
        await _async_log_access(
            org_id=clean_org_id,
            actor_user_id=clean_actor_user_id,
            secret_id=secret.id,
            key_name=key_name,
            action="delete",
            accessed_by="user",
            uow=uow,
        )
        await uow.session.delete(secret)
        return True


async def async_get_vault_access_log(
    actor_user_id: str,
    *,
    org_id: str | None,
    limit: int = 100,
) -> list[dict]:
    del actor_user_id
    clean_org_id = _require_org_id(org_id)
    async with UnitOfWork() as uow:
        stmt = (
            select(VaultAccessLog, User.name.label("actor_name"))
            .outerjoin(User, User.id == VaultAccessLog.actor_user_id)
            .where(VaultAccessLog.org_id == clean_org_id)
            .order_by(VaultAccessLog.accessed_at.desc())
            .limit(limit)
        )
        rows = (await uow.session.execute(stmt)).all()
        return [
            {
                "id": log.id,
                "key_name": log.key_name,
                "action": log.action,
                "accessed_by": log.accessed_by,
                "accessed_at": log.accessed_at,
                "actor_user_id": log.actor_user_id,
                "actor_name": actor_name,
            }
            for log, actor_name in rows
        ]


async def async_list_agent_grants(
    actor_user_id: str,
    *,
    org_id: str | None,
    statuses: list[str] | None = None,
    limit: int = 50,
) -> list[dict]:
    """List vault access grants through the org vault path."""
    del actor_user_id
    if limit <= 0:
        return []
    clean_org_id = _require_org_id(org_id)
    async with UnitOfWork() as uow:
        base_stmt = select(VaultAgentGrant).where(VaultAgentGrant.org_id == clean_org_id)
        if statuses:
            base_stmt = base_stmt.where(VaultAgentGrant.status.in_(tuple(statuses)))

        grants = list(
            (
                await uow.session.scalars(
                    base_stmt.order_by(VaultAgentGrant.requested_at.desc()).limit(limit)
                )
            ).all()
        )
        pending_keys = sorted({grant.key_name for grant in grants if grant.status == "pending"})
        secrets_by_key: dict[str, Secret] = {}
        if pending_keys:
            secrets_stmt = select(Secret).where(
                Secret.org_id == clean_org_id,
                Secret.key_name.in_(pending_keys),
            )
            for secret in (await uow.session.scalars(secrets_stmt)).all():
                secrets_by_key.setdefault(secret.key_name, secret)
        return [
            _grant_to_dict(grant)
            for grant in grants
            if _pending_grant_is_actionable(grant, secrets_by_key.get(grant.key_name))
        ]


async def async_approve_agent_grant(
    grant_id: int,
    *,
    approved_by_user_id: str,
    org_id: str | None,
    ttl_minutes: int = 15,
    max_reads: int = 1,
) -> dict | None:
    """Approve a pending org vault grant."""
    clean_org_id = _require_org_id(org_id)
    _require_actor_user_id(approved_by_user_id)
    now = datetime.now(timezone.utc)
    async with UnitOfWork() as uow:
        grant = await uow.session.get(VaultAgentGrant, grant_id)
        if not grant or str(grant.org_id) != str(clean_org_id):
            return None
        if grant.status != "pending":
            return None
        secret = await _async_secret_by_key(uow, org_id=clean_org_id, key_name=grant.key_name)
        if not _pending_grant_is_actionable(grant, secret):
            return None
        grant.status = "approved"
        grant.approved_by_user_id = approved_by_user_id
        grant.decided_at = now
        grant.expires_at = now + timedelta(minutes=max(1, min(int(ttl_minutes or 15), 60)))
        grant.max_reads = max(1, min(int(max_reads or grant.max_reads or 1), 25))
        grant.read_count = 0
        grant.last_used_at = None
        await uow.session.flush()
        return _grant_to_dict(grant)


async def async_deny_agent_grant(
    grant_id: int,
    *,
    denied_by_user_id: str,
    org_id: str | None,
) -> dict | None:
    """Deny/revoke an org vault grant."""
    clean_org_id = _require_org_id(org_id)
    _require_actor_user_id(denied_by_user_id)
    now = datetime.now(timezone.utc)
    async with UnitOfWork() as uow:
        grant = await uow.session.get(VaultAgentGrant, grant_id)
        if not grant or str(grant.org_id) != str(clean_org_id):
            return None
        grant.status = "denied"
        grant.approved_by_user_id = denied_by_user_id
        grant.decided_at = now
        grant.expires_at = now
        await uow.session.flush()
        return _grant_to_dict(grant)


async def async_list_project_bindings(
    actor_user_id: str,
    *,
    org_id: str | None,
    secret_id: int | None = None,
) -> list[dict]:
    """List project token bindings through the org vault path."""
    del actor_user_id
    clean_org_id = _require_org_id(org_id)
    async with UnitOfWork() as uow:
        return [
            _binding_to_dict(binding, secret)
            for binding, secret in await _async_project_binding_rows(
                uow,
                org_id=clean_org_id,
                secret_id=secret_id,
            )
        ]


async def async_bind_project_secret(
    secret_id: int,
    *,
    actor_user_id: str,
    org_id: str | None,
    project_slug: str,
    env_name: str,
    target_registry_id: int | None = None,
) -> dict | None:
    """Bind an org-owned secret to a project/env name."""
    clean_org_id = _require_org_id(org_id)
    clean_actor_user_id = _require_actor_user_id(actor_user_id)
    async with UnitOfWork() as uow:
        secret = await _async_secret_by_id_for_org(
            uow,
            secret_id=secret_id,
            org_id=clean_org_id,
        )
        if not secret:
            return None
        binding = await _async_upsert_project_binding(
            uow,
            secret,
            actor_user_id=clean_actor_user_id,
            org_id=clean_org_id,
            project_slug=project_slug,
            env_name=env_name,
            target_registry_id=target_registry_id,
        )
        await uow.session.flush()
        return _binding_to_dict(binding, secret)


async def async_delete_project_binding(
    binding_id: int,
    *,
    actor_user_id: str,
    org_id: str | None,
) -> bool:
    """Deactivate a project binding through the org vault path."""
    del actor_user_id
    clean_org_id = _require_org_id(org_id)
    async with UnitOfWork() as uow:
        binding = await uow.session.get(VaultProjectBinding, binding_id)
        if not binding or str(binding.org_id) != str(clean_org_id):
            return False
        binding.active = False
        binding.updated_at = datetime.now(timezone.utc)
        await uow.session.flush()
        return True


# ---------------------------------------------------------------------------
# Per-user org vault PIN protection
# ---------------------------------------------------------------------------

def _pin_scope_key(org_id: str, actor_user_id: str) -> str:
    clean_org_id = _require_org_id(org_id)
    clean_actor_user_id = _require_actor_user_id(actor_user_id)
    return f"pin:org:{clean_org_id}:user:{clean_actor_user_id}"


def _pin_config_keys(org_id: str, actor_user_id: str) -> tuple[str, str, str]:
    scope = _pin_scope_key(org_id, actor_user_id)
    return f"{scope}:hash", f"{scope}:failures", f"{scope}:lockout"


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _utcnow() -> datetime:
    """Return a timezone-aware UTC timestamp for asyncpg timestamptz columns."""
    return datetime.now(timezone.utc)


def _as_db_utc(value: datetime) -> datetime:
    """Normalize DB-loaded timestamp values to timezone-aware UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


async def get_pin_status(org_id: str, actor_user_id: str) -> dict:
    return await async_get_pin_status(org_id, actor_user_id)


async def async_get_pin_status(org_id: str, actor_user_id: str) -> dict:
    hash_key, failures_key, lockout_key = _pin_config_keys(org_id, actor_user_id)
    lockout = await async_get_config(lockout_key)
    locked_until = None
    if lockout:
        try:
            lockout_time = datetime.fromisoformat(lockout)
            if datetime.now(timezone.utc) < lockout_time:
                locked_until = lockout_time
        except ValueError:
            await async_delete_config(lockout_key)
    return {
        "has_pin": await async_get_config(hash_key) is not None,
        "failed_attempts": int(await async_get_config(failures_key) or "0"),
        "locked_until": locked_until,
    }


async def set_pin(org_id: str, actor_user_id: str, new_pin: str, current_pin: str | None = None) -> bool:
    return await async_set_pin(org_id, actor_user_id, new_pin, current_pin)


async def async_set_pin(
    org_id: str,
    actor_user_id: str,
    new_pin: str,
    current_pin: str | None = None,
) -> bool:
    hash_key, failures_key, lockout_key = _pin_config_keys(org_id, actor_user_id)
    if await async_get_config(hash_key) is not None and not await async_verify_pin(
        org_id,
        actor_user_id,
        current_pin or "",
    ):
        return False
    pin_hash = bcrypt.hashpw(new_pin.encode(), bcrypt.gensalt()).decode()
    await async_set_config(hash_key, pin_hash)
    await async_set_config(failures_key, "0")
    await async_delete_config(lockout_key)
    return True


async def has_pin(org_id: str, actor_user_id: str) -> bool:
    return await async_has_pin(org_id, actor_user_id)


async def async_has_pin(org_id: str, actor_user_id: str) -> bool:
    hash_key, _, _ = _pin_config_keys(org_id, actor_user_id)
    return await async_get_config(hash_key) is not None


async def verify_pin(org_id: str, actor_user_id: str, pin: str) -> bool:
    return await async_verify_pin(org_id, actor_user_id, pin)


async def async_verify_pin(org_id: str, actor_user_id: str, pin: str) -> bool:
    hash_key, failures_key, lockout_key = _pin_config_keys(org_id, actor_user_id)
    lockout = await async_get_config(lockout_key)
    if lockout:
        lockout_time = datetime.fromisoformat(lockout)
        if datetime.now(timezone.utc) < lockout_time:
            return False
        await async_delete_config(lockout_key)
        await async_set_config(failures_key, "0")

    stored_hash = await async_get_config(hash_key)
    if not stored_hash:
        return False

    if bcrypt.checkpw(pin.encode(), stored_hash.encode()):
        await async_set_config(failures_key, "0")
        return True

    attempts = int(await async_get_config(failures_key) or "0") + 1
    await async_set_config(failures_key, str(attempts))
    if attempts >= VAULT_LOCKOUT_AFTER_FAILURES:
        lockout_until = datetime.now(timezone.utc) + VAULT_LOCKOUT_DURATION
        await async_set_config(lockout_key, lockout_until.isoformat())
    return False


async def generate_vault_token(org_id: str, actor_user_id: str) -> tuple[str, datetime]:
    return await async_generate_vault_token(org_id, actor_user_id)


async def async_generate_vault_token(org_id: str, actor_user_id: str) -> tuple[str, datetime]:
    clean_org_id = _require_org_id(org_id)
    clean_actor_user_id = _require_actor_user_id(actor_user_id)
    token = stdlib_secrets.token_urlsafe(32)
    now = _utcnow()
    expires = now + VAULT_SESSION_TTL
    async with UnitOfWork() as uow:
        uow.session.add(VaultSession(
            token_hash=_token_hash(token),
            org_id=clean_org_id,
            actor_user_id=clean_actor_user_id,
            created_at=now,
            expires_at=expires,
        ))
    return token, _as_utc(expires)


async def unlock_vault(org_id: str, actor_user_id: str, pin: str) -> tuple[str, datetime] | None:
    return await async_unlock_vault(org_id, actor_user_id, pin)


async def async_unlock_vault(org_id: str, actor_user_id: str, pin: str) -> tuple[str, datetime] | None:
    if not await async_verify_pin(org_id, actor_user_id, pin):
        return None
    return await async_generate_vault_token(org_id, actor_user_id)


async def validate_vault_token(org_id: str, actor_user_id: str, token: str | None) -> bool:
    return await async_validate_vault_token(org_id, actor_user_id, token)


async def async_validate_vault_token(org_id: str, actor_user_id: str, token: str | None) -> bool:
    if not token:
        return False
    clean_org_id = _require_org_id(org_id)
    clean_actor_user_id = _require_actor_user_id(actor_user_id)
    now = _utcnow()
    async with UnitOfWork() as uow:
        session = await uow.session.get(VaultSession, _token_hash(token))
        if (
            not session
            or str(session.org_id) != str(clean_org_id)
            or str(session.actor_user_id) != str(clean_actor_user_id)
        ):
            return False
        if session.revoked_at is not None:
            return False
        if now > _as_db_utc(session.expires_at):
            session.revoked_at = now
            return False
        session.last_seen_at = now
        return True


async def revoke_vault_token(org_id: str, actor_user_id: str, token: str | None) -> None:
    await async_revoke_vault_token(org_id, actor_user_id, token)


async def async_revoke_vault_token(org_id: str, actor_user_id: str, token: str | None) -> None:
    if not token:
        return
    clean_org_id = _require_org_id(org_id)
    clean_actor_user_id = _require_actor_user_id(actor_user_id)
    async with UnitOfWork() as uow:
        session = await uow.session.get(VaultSession, _token_hash(token))
        if (
            session
            and str(session.org_id) == str(clean_org_id)
            and str(session.actor_user_id) == str(clean_actor_user_id)
            and session.revoked_at is None
        ):
            session.revoked_at = _utcnow()


async def _get_config(key: str) -> str | None:
    return await async_get_config(key)


async def async_get_config(key: str) -> str | None:
    async with UnitOfWork() as uow:
        result = await uow.session.scalars(
            select(VaultConfig).where(VaultConfig.key == key)
        )
        config = result.first()
        return config.value if config else None


async def _set_config(key: str, value: str) -> None:
    await async_set_config(key, value)


async def async_set_config(key: str, value: str) -> None:
    now = datetime.now(timezone.utc)
    async with UnitOfWork() as uow:
        result = await uow.session.scalars(
            select(VaultConfig).where(VaultConfig.key == key)
        )
        config = result.first()
        if config:
            config.value = value
            config.updated_at = now
        else:
            uow.session.add(VaultConfig(key=key, value=value, updated_at=now))


async def _delete_config(key: str) -> None:
    await async_delete_config(key)


async def async_delete_config(key: str) -> None:
    async with UnitOfWork() as uow:
        result = await uow.session.scalars(
            select(VaultConfig).where(VaultConfig.key == key)
        )
        config = result.first()
        if config:
            await uow.session.delete(config)
