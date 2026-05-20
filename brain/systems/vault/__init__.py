"""Illo Brain — Secret Vault.

Fernet-encrypted secret storage with DB persistence, per-user scoping,
selective sharing, and audit logging.
"""

import hashlib
import logging
import os
import secrets as stdlib_secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

import bcrypt
from cryptography.fernet import Fernet
from sqlalchemy import and_, case, inspect, or_, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from brain.kernel import config
from brain.platform.db.models.org import OrgApiKey, User, UserApiKey, ApiKeyShare
from brain.platform.db.models.vault import (
    Secret,
    VaultAccessLog,
    VaultAgentGrant,
    VaultConfig,
    VaultMissingRequest,
    VaultProjectBinding,
    VaultSession,
    VaultShare,
)
from brain.platform.db.repositories.unit_of_work import UnitOfWork

logger = logging.getLogger(__name__)

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
DEFAULT_VAULT_AGENT_ACCESS_LEVEL = VAULT_AGENT_ACCESS_ASK

# ---------------------------------------------------------------------------
# Master key management
# ---------------------------------------------------------------------------

def _read_key_from_env_file(env_path: Path) -> str:
    """Try to read VAULT_MASTER_KEY from a .env file."""
    if not env_path.exists():
        return ""
    try:
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line.startswith("VAULT_MASTER_KEY="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


def _get_fernet() -> Fernet:
    """Return a Fernet instance using VAULT_MASTER_KEY."""
    key = os.environ.get("VAULT_MASTER_KEY", "")
    if not key:
        brain_env = Path(config.BRAIN_DIR) / "brain" / ".env"
        key = _read_key_from_env_file(brain_env)
    if not key:
        core_env = Path(config.BRAIN_DIR) / "core" / ".env"
        key = _read_key_from_env_file(core_env)
        if key:
            new_env = Path(config.BRAIN_DIR) / "brain" / ".env"
            try:
                with open(new_env, "a") as f:
                    f.write(f"VAULT_MASTER_KEY={key}\n")
                logger.info("Migrated VAULT_MASTER_KEY from core/.env → brain/.env")
            except OSError:
                pass
    if not key:
        root_env = Path(config.BRAIN_DIR) / ".env"
        key = _read_key_from_env_file(root_env)
    if key:
        os.environ["VAULT_MASTER_KEY"] = key
    if not key:
        raise RuntimeError(
            "VAULT_MASTER_KEY is required. Refusing to auto-generate a vault "
            "key because that can silently strand existing secrets."
        )
    return Fernet(key.encode() if isinstance(key, str) else key)


def _encrypt(value: str) -> bytes:
    return _get_fernet().encrypt(value.encode())


def _decrypt(token: bytes) -> str:
    return _get_fernet().decrypt(token).decode()


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


def _table_exists(uow: UnitOfWork, table_name: str) -> bool:
    try:
        if hasattr(uow.session, "table_exists"):
            return bool(uow.session.table_exists(table_name))
        return inspect(uow.session.connection()).has_table(table_name)
    except Exception:
        return True


async def _async_table_exists(uow: UnitOfWork, table_name: str) -> bool:
    try:
        if hasattr(uow.session, "table_exists"):
            result = uow.session.table_exists(table_name)
            if hasattr(result, "__await__"):
                result = await result
            return bool(result)
        bind = getattr(uow.session, "bind", None)
        dialect_name = getattr(getattr(bind, "dialect", None), "name", "")
        if dialect_name == "sqlite":
            result = await uow.session.execute(
                text("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = :table_name"),
                {"table_name": table_name},
            )
            return result.first() is not None
        result = await uow.session.scalar(text("SELECT to_regclass(:table_name) IS NOT NULL"), {"table_name": table_name})
        return bool(result)
    except Exception:
        return True


def _secret_org_id(secret: Secret) -> str | None:
    value = getattr(secret, "org_id", None)
    if value is None or not isinstance(value, (str, int)):
        return None
    return str(value) if value else None


def _secret_read_dict(
    secret: Secret,
    *,
    is_shared: bool = False,
    shared_by_name: str | None = None,
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
        "user_id": secret.user_id,
        "org_id": getattr(secret, "org_id", None),
        "agent_access_level": (
            getattr(secret, "agent_access_level", None)
            or DEFAULT_VAULT_AGENT_ACCESS_LEVEL
        ),
        "shared_by_name": shared_by_name,
        "is_shared": is_shared,
    }


async def _async_secret_by_key_for_write(
    uow: UnitOfWork,
    *,
    user_id: str,
    key_name: str,
    org_id: str | None = None,
) -> Secret | None:
    if org_id:
        secret = (
            await uow.session.scalars(
                select(Secret)
                .where(Secret.org_id == org_id, Secret.key_name == key_name)
                .order_by(Secret.id.desc())
                .limit(1)
            )
        ).first()
        if secret:
            return secret

    repo = getattr(uow, "vault", None)
    if repo is not None:
        try:
            return await repo.get_by_key(user_id, key_name)
        except (AttributeError, TypeError):
            pass

    legacy_stmt = select(Secret).where(
        Secret.user_id == user_id,
        Secret.key_name == key_name,
    )
    if org_id:
        legacy_stmt = legacy_stmt.where(Secret.org_id.is_(None))
    return (await uow.session.scalars(legacy_stmt.limit(1))).first()


def _secret_bindable_by_actor(secret: Secret, *, user_id: str, org_id: str | None) -> bool:
    secret_org_id = _secret_org_id(secret)
    if org_id and secret_org_id == str(org_id):
        return True
    secret_user_id = getattr(secret, "user_id", user_id)
    if not isinstance(secret_user_id, (str, int)):
        secret_user_id = user_id
    return secret_org_id is None and str(secret_user_id) == str(user_id)


async def _async_secret_by_id_for_actor(
    uow: UnitOfWork,
    *,
    secret_id: int,
    user_id: str,
    org_id: str | None = None,
) -> Secret | None:
    secret = await uow.session.get(Secret, secret_id)
    if secret and _secret_bindable_by_actor(secret, user_id=user_id, org_id=org_id):
        return secret
    return None


async def _async_log_access(
    user_id: str,
    secret_id: int | None,
    key_name: str,
    action: str,
    accessed_by: str = "user",
    org_id: str | None = None,
    uow: UnitOfWork | None = None,
) -> None:
    """Async variant of vault audit logging. Fail-silent."""

    try:
        entry = VaultAccessLog(
            user_id=user_id,
            secret_id=secret_id,
            key_name=key_name,
            action=action,
            accessed_by=_normalize_accessed_by(accessed_by),
            org_id=org_id,
        )
        if uow:
            uow.session.add(entry)
        else:
            async with UnitOfWork() as _uow:
                _uow.session.add(entry)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# CRUD (user-scoped)
# ---------------------------------------------------------------------------

async def get_secret(
    key_name: str,
    user_id: str,
    *,
    org_id: str | None = None,
    allow_shared: bool = True,
    allow_env_fallback: bool = False,
    accessed_by: str = "user",
) -> str | None:
    """Retrieve and decrypt a user-scoped secret using async DB access."""
    return await async_get_secret(
        key_name,
        user_id,
        org_id=org_id,
        allow_shared=allow_shared,
        allow_env_fallback=allow_env_fallback,
        accessed_by=accessed_by,
    )


async def async_get_secret(
    key_name: str,
    user_id: str,
    *,
    org_id: str | None = None,
    allow_shared: bool = True,
    allow_env_fallback: bool = False,
    accessed_by: str = "user",
) -> str | None:
    """Async retrieve/decrypt path for org-scoped secrets with legacy fallback."""

    if not user_id:
        raise ValueError("user_id is required to read a vault secret")
    async with UnitOfWork() as uow:
        secret = await _visible_secret_by_key(
            uow,
            user_id=user_id,
            key_name=key_name,
            org_id=org_id,
            include_shared=allow_shared,
        )

        if secret:
            secret.last_accessed_at = datetime.now(timezone.utc)
            secret.access_count = (secret.access_count or 0) + 1
            await _async_log_access(
                user_id,
                secret.id,
                key_name,
                "read",
                accessed_by,
                org_id=_secret_org_id(secret) or org_id,
                uow=uow,
            )
            return _decrypt(bytes(secret.encrypted_value))

    if allow_env_fallback and (env_val := os.environ.get(key_name)):
        return env_val
    await _async_record_missing(key_name, user_id=user_id, org_id=org_id)
    return None


async def set_secret(
    key_name: str,
    value: str,
    user_id: str,
    *,
    org_id: str | None = None,
    description: str = "",
    category: str = "general",
    agent_access_level: str | None = None,
) -> None:
    """Encrypt and upsert a secret, scoped to user, using async DB access."""
    await async_set_secret(
        key_name,
        value,
        user_id,
        org_id=org_id,
        description=description,
        category=category,
        agent_access_level=agent_access_level,
    )


async def async_set_secret(
    key_name: str,
    value: str,
    user_id: str,
    *,
    org_id: str | None = None,
    description: str = "",
    category: str = "general",
    agent_access_level: str | None = None,
) -> None:
    """Async encrypt/upsert path for org-scoped secrets with legacy fallback."""

    encrypted = _encrypt(value)
    now = datetime.now(timezone.utc)
    if not user_id:
        raise ValueError("user_id is required to set a secret (user_id is NOT NULL)")
    normalized_agent_access = (
        normalize_agent_access_level(agent_access_level)
        if agent_access_level is not None
        else None
    )
    async with UnitOfWork() as uow:
        existing = await _async_secret_by_key_for_write(
            uow,
            user_id=user_id,
            key_name=key_name,
            org_id=org_id,
        )
        if existing:
            existing.encrypted_value = encrypted
            existing.description = description
            existing.category = category
            if org_id and not getattr(existing, "org_id", None):
                existing.org_id = org_id
            if normalized_agent_access is not None:
                existing.agent_access_level = normalized_agent_access
            existing.updated_at = now
            await uow.session.flush()
            await _async_log_access(
                user_id,
                existing.id,
                key_name,
                "write",
                org_id=_secret_org_id(existing) or org_id,
                uow=uow,
            )
        else:
            secret = Secret(
                key_name=key_name,
                encrypted_value=encrypted,
                description=description,
                category=category,
                agent_access_level=normalized_agent_access or DEFAULT_VAULT_AGENT_ACCESS_LEVEL,
                user_id=user_id,
                org_id=org_id,
                created_at=now,
                updated_at=now,
            )
            uow.session.add(secret)
            await uow.session.flush()
            await _async_log_access(user_id, secret.id, key_name, "write", org_id=org_id, uow=uow)
    await async_resolve_missing(key_name, user_id=user_id, org_id=org_id)


async def delete_secret(key_name: str, user_id: str, *, org_id: str | None = None) -> bool:
    """Delete a secret. Returns True if it existed."""
    return await async_delete_secret(key_name, user_id, org_id=org_id)


async def list_secrets(
    user_id: str,
    category: str | None = None,
    *,
    org_id: str | None = None,
) -> list[dict]:
    """Return metadata for user's own + shared secrets (never includes encrypted_value)."""
    return await async_list_secrets(user_id, category, org_id=org_id)


async def async_list_secrets(
    user_id: str,
    category: str | None = None,
    *,
    org_id: str | None = None,
) -> list[dict]:
    """Async metadata listing for org vault secrets plus legacy personal/shared rows."""

    if not user_id:
        raise ValueError("user_id is required to list vault secrets")
    async with UnitOfWork() as uow:
        results = []
        seen: set[int] = set()

        if org_id:
            stmt = select(Secret).where(
                or_(
                    Secret.org_id == org_id,
                    and_(Secret.org_id.is_(None), Secret.user_id == user_id),
                )
            )
        else:
            stmt = select(Secret).where(Secret.user_id == user_id)
        if category:
            stmt = stmt.where(Secret.category == category)
        own_result = await uow.session.scalars(stmt)
        for s in own_result.all():
            seen.add(s.id)
            results.append(_secret_read_dict(s))

        if await _async_table_exists(uow, "vault_shares"):
            shared_stmt = (
                select(Secret, User.name.label("shared_by_name"))
                .join(VaultShare, VaultShare.secret_id == Secret.id)
                .join(User, User.id == VaultShare.shared_by_user_id)
                .where(
                    VaultShare.shared_with_user_id == user_id,
                    VaultShare.revoked_at.is_(None),
                )
            )
            if category:
                shared_stmt = shared_stmt.where(Secret.category == category)
            if org_id:
                shared_stmt = shared_stmt.where(User.org_id == org_id)
            shared_result = await uow.session.execute(shared_stmt)
            for s, shared_by_name in shared_result.all():
                if s.id in seen:
                    continue
                seen.add(s.id)
                results.append(_secret_read_dict(s, is_shared=True, shared_by_name=shared_by_name))
        results.sort(key=lambda r: (r["category"], r["key_name"], r["is_shared"]))
        return results


async def async_get_secret_record(
    key_name: str,
    user_id: str,
    *,
    org_id: str | None = None,
) -> Secret | None:
    """Async fetch of an owned secret metadata row."""

    if not user_id:
        raise ValueError("user_id is required to read a vault secret")
    async with UnitOfWork() as uow:
        return await _async_secret_by_key_for_write(
            uow,
            user_id=user_id,
            key_name=key_name,
            org_id=org_id,
        )


async def reveal_secret(key_name: str, user_id: str, *, org_id: str | None = None) -> str | None:
    """Reveal a secret for the dashboard (updates access stats)."""
    return await async_reveal_secret(key_name, user_id=user_id, org_id=org_id)


async def async_reveal_secret(key_name: str, user_id: str, *, org_id: str | None = None) -> str | None:
    """Async reveal path for the dashboard."""

    return await async_get_secret(key_name, user_id=user_id, org_id=org_id, accessed_by="user")


async def require_secret(
    key_name: str,
    user_id: str,
    *,
    org_id: str | None = None,
    allow_env_fallback: bool = False,
) -> str:
    """Like get_secret but raises ValueError if not found anywhere."""
    value = await async_get_secret(
        key_name,
        user_id=user_id,
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
        "user_id": binding.user_id,
        "org_id": binding.org_id,
        "target_registry_id": binding.target_registry_id,
        "project_slug": binding.project_slug,
        "env_name": binding.env_name,
        "active": binding.active,
        "created_at": binding.created_at,
        "updated_at": binding.updated_at,
    }


def _binding_org_matches(binding: VaultProjectBinding, org_id: str | None) -> bool:
    if binding.org_id is None:
        return True
    return bool(org_id) and str(binding.org_id) == str(org_id)


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
    user_id: str,
    org_id: str | None = None,
    secret_id: int | None = None,
):
    stmt = (
        select(VaultProjectBinding, Secret)
        .join(Secret, Secret.id == VaultProjectBinding.secret_id)
        .where(
            VaultProjectBinding.active == True,  # noqa: E712
        )
    )
    if org_id:
        stmt = stmt.where(
            or_(
                and_(
                    VaultProjectBinding.org_id == org_id,
                    Secret.org_id == org_id,
                ),
                and_(
                    VaultProjectBinding.org_id == org_id,
                    VaultProjectBinding.user_id == user_id,
                    Secret.org_id.is_(None),
                    Secret.user_id == user_id,
                ),
                and_(
                    VaultProjectBinding.org_id.is_(None),
                    VaultProjectBinding.user_id == user_id,
                    Secret.org_id.is_(None),
                    Secret.user_id == user_id,
                ),
            )
        )
    else:
        stmt = stmt.where(
            VaultProjectBinding.org_id.is_(None),
            VaultProjectBinding.user_id == user_id,
            Secret.org_id.is_(None),
            Secret.user_id == user_id,
        )
    if secret_id is not None:
        stmt = stmt.where(VaultProjectBinding.secret_id == secret_id)
    return stmt.order_by(VaultProjectBinding.project_slug, VaultProjectBinding.env_name)




async def _async_project_binding_rows(
    uow: UnitOfWork,
    *,
    user_id: str,
    org_id: str | None = None,
    secret_id: int | None = None,
) -> list[tuple[VaultProjectBinding, Secret]]:
    if not await _async_table_exists(uow, "vault_project_bindings"):
        return []
    try:
        result = await uow.session.execute(
            _project_binding_rows_stmt(
                user_id=user_id,
                org_id=org_id,
                secret_id=secret_id,
            )
        )
        return list(result.all())
    except SQLAlchemyError:
        await uow.session.rollback()
        return []










async def _async_upsert_project_binding(
    uow: UnitOfWork,
    secret: Secret,
    *,
    user_id: str,
    org_id: str | None = None,
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
        VaultProjectBinding.project_slug == clean_project_slug,
        VaultProjectBinding.env_name == clean_env_name,
    )
    if org_id:
        stmt = stmt.where(VaultProjectBinding.org_id == org_id)
    else:
        stmt = stmt.where(
            VaultProjectBinding.org_id.is_(None),
            VaultProjectBinding.user_id == user_id,
        )
    existing = (await uow.session.scalars(stmt.limit(1))).first()
    if existing:
        existing.secret_id = secret.id
        existing.org_id = org_id
        existing.target_registry_id = target_registry_id
        existing.active = True
        existing.updated_at = now
        return existing
    binding = VaultProjectBinding(
        secret_id=secret.id,
        user_id=user_id,
        org_id=org_id,
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
    user_id: str,
    org_id: str | None = None,
    project_slug: str,
    env_name: str,
    target_registry_id: int | None = None,
) -> dict | None:
    """Bind an org-visible or legacy personal secret to a project/env name."""
    return await async_bind_project_secret(
        secret_id,
        user_id=user_id,
        org_id=org_id,
        project_slug=project_slug,
        env_name=env_name,
        target_registry_id=target_registry_id,
    )


async def bind_project_secret_by_key(
    key_name: str,
    *,
    user_id: str,
    org_id: str | None = None,
    project_slug: str,
    env_name: str,
    target_registry_id: int | None = None,
) -> dict | None:
    """Bind an org-visible or legacy personal secret to a project/env name."""
    return await async_bind_project_secret_by_key(
        key_name,
        user_id=user_id,
        org_id=org_id,
        project_slug=project_slug,
        env_name=env_name,
        target_registry_id=target_registry_id,
    )


async def async_bind_project_secret_by_key(
    key_name: str,
    *,
    user_id: str,
    org_id: str | None = None,
    project_slug: str,
    env_name: str,
    target_registry_id: int | None = None,
) -> dict | None:
    """Async bind an org-visible or legacy personal secret to a project/env name."""
    if not user_id:
        raise ValueError("user_id is required to bind a vault secret")
    clean_key_name = (key_name or "").strip()
    if not clean_key_name:
        raise ValueError("key_name is required")

    async with UnitOfWork() as uow:
        try:
            secret = await _async_secret_by_key_for_write(
                uow,
                user_id=user_id,
                key_name=clean_key_name,
                org_id=org_id,
            )
        except SQLAlchemyError:
            await uow.session.rollback()
            return None
        if secret is None or not _secret_bindable_by_actor(secret, user_id=user_id, org_id=org_id):
            return None

        clean_project_slug = _normalize_project_slug(project_slug)
        if not clean_project_slug:
            raise ValueError("project_slug is required")
        clean_env_name = _normalize_env_name(env_name)
        now = datetime.now(timezone.utc)
        existing_stmt = select(VaultProjectBinding).where(
            VaultProjectBinding.project_slug == clean_project_slug,
            VaultProjectBinding.env_name == clean_env_name,
        )
        if org_id:
            existing_stmt = existing_stmt.where(VaultProjectBinding.org_id == org_id)
        else:
            existing_stmt = existing_stmt.where(
                VaultProjectBinding.org_id.is_(None),
                VaultProjectBinding.user_id == user_id,
            )
        existing = (await uow.session.scalars(existing_stmt.limit(1))).first()
        if existing:
            existing.secret_id = secret.id
            existing.org_id = org_id
            existing.target_registry_id = target_registry_id
            existing.active = True
            existing.updated_at = now
            binding = existing
        else:
            binding = VaultProjectBinding(
                secret_id=secret.id,
                user_id=user_id,
                org_id=org_id,
                target_registry_id=target_registry_id,
                project_slug=clean_project_slug,
                env_name=clean_env_name,
                active=True,
                created_at=now,
                updated_at=now,
            )
            uow.session.add(binding)
        await uow.session.flush()
        return _binding_to_dict(binding, secret)


async def list_project_bindings(
    *,
    user_id: str,
    org_id: str | None = None,
    secret_id: int | None = None,
) -> list[dict]:
    """List project token bindings owned by the current user."""
    return await async_list_project_bindings(user_id, org_id=org_id, secret_id=secret_id)


async def delete_project_binding(
    binding_id: int,
    *,
    user_id: str,
    org_id: str | None = None,
) -> bool:
    """Deactivate a project binding owned by the current user."""
    return await async_delete_project_binding(binding_id, user_id=user_id, org_id=org_id)


async def resolve_project_bound_env_tokens(
    *,
    user_id: str,
    org_id: str | None = None,
    project_slug: str | None = None,
    project_slugs: list[str] | tuple[str, ...] | set[str] | None = None,
    target_registry_id: int | None = None,
) -> dict[str, str]:
    """Return env-name/token pairs for project-bound secrets.

    Org-owned tokens are eligible for every member in the org. Legacy personal
    tokens remain scoped to the owning user. Manual secrets are intentionally
    excluded; ask-level secrets become available only through a matching project
    binding, while available secrets still need a binding to define an env name.
    """
    return await async_resolve_project_bound_env_tokens(
        user_id=user_id,
        org_id=org_id,
        project_slug=project_slug,
        project_slugs=project_slugs,
        target_registry_id=target_registry_id,
    )


async def async_resolve_project_bound_env_tokens(
    *,
    user_id: str,
    org_id: str | None = None,
    project_slug: str | None = None,
    project_slugs: list[str] | tuple[str, ...] | set[str] | None = None,
    target_registry_id: int | None = None,
) -> dict[str, str]:
    """Return env-name/token pairs for project-bound secrets using async DB access."""
    if not user_id or (not project_slug and not project_slugs and target_registry_id is None):
        return {}
    async with UnitOfWork() as uow:
        env: dict[str, str] = {}
        for binding, secret in [
            (binding, secret)
            for binding, secret in await _async_project_binding_rows(uow, user_id=user_id, org_id=org_id)
            if _binding_project_matches(
                binding,
                project_slug=project_slug,
                project_slugs=project_slugs,
                target_registry_id=target_registry_id,
            )
        ]:
            if normalize_agent_access_level(getattr(secret, "agent_access_level", None)) == VAULT_AGENT_ACCESS_MANUAL:
                continue
            secret.last_accessed_at = datetime.now(timezone.utc)
            secret.access_count = (secret.access_count or 0) + 1
            await _async_log_access(
                user_id,
                secret.id,
                secret.key_name,
                "read",
                "agent",
                org_id=_secret_org_id(secret) or org_id,
                uow=uow,
            )
            env[binding.env_name] = _decrypt(bytes(secret.encrypted_value))
        return env


# ---------------------------------------------------------------------------
# Sharing
# ---------------------------------------------------------------------------

async def share_secret(
    secret_id: int,
    shared_with_user_id: str,
    shared_by_user_id: str,
    *,
    org_id: str | None = None,
) -> dict | None:
    """Share a secret with another user. Returns share record or None if secret not found."""
    return await async_share_secret(
        secret_id,
        shared_with_user_id,
        shared_by_user_id,
        org_id=org_id,
    )


async def revoke_share(share_id: int, user_id: str) -> bool:
    """Revoke a vault share. Only the original sharer can revoke."""
    return await async_revoke_share(share_id, user_id)


# ---------------------------------------------------------------------------
# Per-user API key management
# ---------------------------------------------------------------------------

async def resolve_api_key(
    user_id: str | None = None,
    org_id: str | None = None,
    provider: str = "anthropic",
) -> tuple[str | None, str]:
    """Resolve API key with fallback chain:

    1. User's chosen default key (set via Settings UI)
    2. Org main key (set by org owner)
    3. Environment variable (dev convenience)

    Returns (key, source) where source describes which level resolved.
    """
    return await async_resolve_api_key(user_id=user_id, org_id=org_id, provider=provider)


async def async_resolve_api_key(
    user_id: str | None = None,
    org_id: str | None = None,
    provider: str = "anthropic",
    *,
    session: AsyncSession | None = None,
) -> tuple[str | None, str]:
    """Resolve API key using the async DB path."""

    async def _resolve(active_session: AsyncSession) -> tuple[str | None, str]:
        if user_id:
            stmt = (
                select(UserApiKey)
                .join(User, User.default_api_key_id == UserApiKey.id)
                .where(
                    User.id == user_id,
                    UserApiKey.provider == provider,
                    UserApiKey.is_active == True,  # noqa: E712
                )
            )
            key_row = (await active_session.scalars(stmt)).first()
            if key_row:
                return _decrypt(bytes(key_row.encrypted_key)), "user_default"

            stmt = (
                select(UserApiKey)
                .where(
                    UserApiKey.user_id == user_id,
                    UserApiKey.provider == provider,
                    UserApiKey.is_active == True,  # noqa: E712
                )
                .order_by(
                    case((UserApiKey.label == "default", 0), else_=1),
                    UserApiKey.id.desc(),
                )
                .limit(1)
            )
            key_row = (await active_session.scalars(stmt)).first()
            if key_row:
                return _decrypt(bytes(key_row.encrypted_key)), "user_default"

        effective_org_id = org_id
        if not effective_org_id and user_id:
            user = await active_session.get(User, user_id)
            if user:
                effective_org_id = str(user.org_id)

        if effective_org_id:
            stmt = select(OrgApiKey).where(
                OrgApiKey.org_id == effective_org_id,
                OrgApiKey.provider == provider,
            )
            org_key = (await active_session.scalars(stmt)).first()
            if org_key:
                return _decrypt(bytes(org_key.encrypted_key)), "org_main"

        env_key = os.environ.get(f"{provider.upper()}_API_KEY")
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
    """Update the DB key row that ``resolve_api_key`` would select.

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
    """Update the key row selected by ``async_resolve_api_key``."""
    if source not in {"user_default", "org_main"}:
        return False

    encrypted = _encrypt(api_key)

    async def _update(active_session: AsyncSession) -> bool:
        if source == "user_default" and user_id:
            stmt = (
                select(UserApiKey)
                .join(User, User.default_api_key_id == UserApiKey.id)
                .where(
                    User.id == user_id,
                    UserApiKey.provider == provider,
                    UserApiKey.is_active == True,  # noqa: E712
                )
            )
            key_row = (await active_session.scalars(stmt)).first()

            if not key_row:
                stmt = (
                    select(UserApiKey)
                    .where(
                        UserApiKey.user_id == user_id,
                        UserApiKey.provider == provider,
                        UserApiKey.is_active == True,  # noqa: E712
                    )
                    .order_by(
                        case((UserApiKey.label == "default", 0), else_=1),
                        UserApiKey.id.desc(),
                    )
                    .limit(1)
                )
                key_row = (await active_session.scalars(stmt)).first()

            if key_row:
                key_row.encrypted_key = encrypted
                key_row.is_active = True
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
                    OrgApiKey.provider == provider,
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


async def set_api_key(
    user_id: str,
    api_key: str,
    provider: str = "anthropic",
    label: str = "default",
) -> int:
    """Store an encrypted API key for a user. Returns the key ID."""
    return await async_set_api_key(user_id, api_key, provider=provider, label=label)


async def async_set_api_key(
    user_id: str,
    api_key: str,
    provider: str = "anthropic",
    label: str = "default",
    *,
    session: AsyncSession | None = None,
) -> int:
    """Store an encrypted user API key using the async DB path."""
    encrypted = _encrypt(api_key)

    async def _set(active_session: AsyncSession) -> int:
        stmt = select(UserApiKey).where(
            UserApiKey.user_id == user_id,
            UserApiKey.provider == provider,
            UserApiKey.label == label,
        )
        existing = (await active_session.scalars(stmt)).first()
        if existing:
            existing.encrypted_key = encrypted
            existing.is_active = True
            await active_session.flush()
            return int(existing.id)

        key_obj = UserApiKey(
            user_id=user_id,
            provider=provider,
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


async def share_api_key(
    api_key_id: int,
    shared_with_user_id: str,
    shared_by_user_id: str,
) -> int:
    """Share an API key with another user. Returns the share ID."""
    return await async_share_api_key(api_key_id, shared_with_user_id, shared_by_user_id)


async def async_share_api_key(
    api_key_id: int,
    shared_with_user_id: str,
    shared_by_user_id: str,
) -> int:
    """Share an API key with another user using async DB access."""
    async with UnitOfWork() as uow:
        stmt = select(UserApiKey).where(
            UserApiKey.id == api_key_id,
            UserApiKey.user_id == shared_by_user_id,
        )
        key_row = (await uow.session.scalars(stmt)).first()
        if not key_row:
            raise ValueError(f"API key {api_key_id} not found or not owned by {shared_by_user_id}")

        sharer = await uow.session.get(User, shared_by_user_id)
        recipient = await uow.session.get(User, shared_with_user_id)
        if (
            not sharer
            or not recipient
            or not getattr(sharer, "org_id", None)
            or str(sharer.org_id) != str(recipient.org_id)
        ):
            raise ValueError("API keys can only be shared with users in the same org")

        stmt = select(ApiKeyShare).where(
            ApiKeyShare.api_key_id == api_key_id,
            ApiKeyShare.shared_with_user_id == shared_with_user_id,
        )
        existing = (await uow.session.scalars(stmt)).first()
        now = datetime.now(timezone.utc)
        if existing:
            existing.revoked_at = None
            existing.shared_at = now
            await uow.session.flush()
            return existing.id
        share = ApiKeyShare(
            api_key_id=api_key_id,
            shared_with_user_id=shared_with_user_id,
            shared_by_user_id=shared_by_user_id,
            shared_at=now,
        )
        uow.session.add(share)
        await uow.session.flush()
        return share.id


async def revoke_api_key_share(share_id: int, user_id: str) -> bool:
    """Revoke a shared API key. Only the original sharer can revoke."""
    return await async_revoke_api_key_share(share_id, user_id)


async def async_revoke_api_key_share(share_id: int, user_id: str) -> bool:
    """Revoke a shared API key using async DB access."""
    async with UnitOfWork() as uow:
        stmt = select(ApiKeyShare).where(
            ApiKeyShare.id == share_id,
            ApiKeyShare.shared_by_user_id == user_id,
            ApiKeyShare.revoked_at.is_(None),
        )
        share = (await uow.session.scalars(stmt)).first()
        if share:
            share.revoked_at = datetime.now(timezone.utc)
            return True
        return False


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
    """Record token usage for spend tracking using async DB access."""
    async with UnitOfWork() as uow:
        key_obj = await uow.session.get(UserApiKey, api_key_id)
        if key_obj:
            key_obj.last_used_at = datetime.now(timezone.utc)
            key_obj.total_tokens_used = (key_obj.total_tokens_used or 0) + tokens_used
            key_obj.estimated_cost_usd = float(key_obj.estimated_cost_usd or 0) + cost_usd


async def get_vault_access_log(
    user_id: str,
    *,
    org_id: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Return access log scoped to the caller's org, or own rows without org."""
    return await async_get_vault_access_log(user_id, org_id=org_id, limit=limit)


async def get_org_users(org_id: str) -> list[dict]:
    """Return users in the org (for share picker). Delegates to auth module."""
    from brain.systems.auth.users import async_get_org_users as _get_org_users
    return await _get_org_users(org_id)


# ---------------------------------------------------------------------------
# Missing secret tracking
# ---------------------------------------------------------------------------

def _record_missing(
    key_name: str,
    *,
    user_id: str | None = None,
    org_id: str | None = None,
) -> None:
    """Record that a secret was requested but not found."""
    raise RuntimeError("Use _async_record_missing()")


async def _async_record_missing(
    key_name: str,
    *,
    user_id: str | None = None,
    org_id: str | None = None,
) -> None:
    """Async variant of missing-secret tracking. Fail-silent."""
    now = datetime.now(timezone.utc)
    try:
        async with UnitOfWork() as uow:
            stmt = select(VaultMissingRequest).where(VaultMissingRequest.key_name == key_name)
            if org_id:
                stmt = stmt.where(VaultMissingRequest.org_id == org_id)
            elif user_id:
                stmt = stmt.where(VaultMissingRequest.user_id == user_id)
            else:
                return
            existing = (await uow.session.scalars(stmt)).first()
            if existing:
                existing.request_count = (existing.request_count or 0) + 1
                existing.last_requested = now
                existing.resolved = False
            else:
                uow.session.add(VaultMissingRequest(
                    key_name=key_name,
                    user_id=user_id,
                    org_id=org_id,
                    request_count=1,
                    first_requested=now,
                    last_requested=now,
                    resolved=False,
                ))
    except Exception:
        pass


async def record_missing_request(
    key_name: str,
    *,
    user_id: str | None = None,
    org_id: str | None = None,
) -> None:
    """Record a missing secret request from a non-read workflow."""
    await async_record_missing_request(key_name, user_id=user_id, org_id=org_id)


async def async_record_missing_request(
    key_name: str,
    *,
    user_id: str | None = None,
    org_id: str | None = None,
) -> None:
    """Record a missing secret request from an async workflow."""
    await _async_record_missing(key_name, user_id=user_id, org_id=org_id)


async def get_missing_requests(
    *,
    user_id: str | None = None,
    org_id: str | None = None,
) -> list[dict]:
    return await async_get_missing_requests(user_id, org_id=org_id)


async def resolve_missing(
    key_name: str,
    *,
    user_id: str | None = None,
    org_id: str | None = None,
) -> None:
    await async_resolve_missing(key_name, user_id=user_id, org_id=org_id)


async def async_resolve_missing(
    key_name: str,
    *,
    user_id: str | None = None,
    org_id: str | None = None,
) -> None:
    try:
        async with UnitOfWork() as uow:
            stmt = select(VaultMissingRequest).where(VaultMissingRequest.key_name == key_name)
            if org_id:
                stmt = stmt.where(VaultMissingRequest.org_id == org_id)
            elif user_id:
                stmt = stmt.where(VaultMissingRequest.user_id == user_id)
            rows = (await uow.session.scalars(stmt)).all()
            for existing in rows:
                existing.resolved = True
    except Exception:
        logger.debug("vault_resolve_missing_failed", exc_info=True)


# ---------------------------------------------------------------------------
# Agent grants
# ---------------------------------------------------------------------------

def _grant_to_dict(grant: VaultAgentGrant) -> dict:
    return {
        "id": grant.id,
        "key_name": grant.key_name,
        "user_id": grant.user_id,
        "org_id": grant.org_id,
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


async def _visible_secret_stmt(
    uow: UnitOfWork,
    user_id: str,
    *,
    org_id: str | None = None,
    include_shared: bool = True,
):
    visibility = []
    if org_id:
        visibility.append(Secret.org_id == org_id)
        visibility.append(and_(Secret.org_id.is_(None), Secret.user_id == user_id))
    else:
        visibility.append(Secret.user_id == user_id)

    if include_shared and await _async_table_exists(uow, "vault_shares"):
        shared_secret_ids = select(VaultShare.secret_id).where(
            VaultShare.shared_with_user_id == user_id,
            VaultShare.revoked_at.is_(None),
        )
        if org_id:
            shared_secret_ids = shared_secret_ids.join(
                User,
                User.id == VaultShare.shared_by_user_id,
            ).where(User.org_id == org_id)
        visibility.append(Secret.id.in_(shared_secret_ids))
    return select(Secret).where(or_(*visibility))


async def _visible_secret_by_key(
    uow: UnitOfWork,
    *,
    user_id: str,
    key_name: str,
    org_id: str | None = None,
    include_shared: bool = True,
) -> Secret | None:
    repo = getattr(uow, "vault", None)
    if repo is not None:
        try:
            secret = (
                await repo.get_by_key(user_id, key_name, org_id=org_id)
                if org_id
                else await repo.get_by_key(user_id, key_name)
            )
        except (AttributeError, TypeError):
            secret = None
        if secret:
            return secret

    stmt = (
        (await _visible_secret_stmt(uow, user_id, org_id=org_id, include_shared=include_shared))
        .where(Secret.key_name == key_name)
        .order_by(
            case((Secret.org_id == org_id, 0), (Secret.user_id == user_id, 1), else_=2),
            Secret.id.desc(),
        )
        .limit(1)
    )
    return (await uow.session.scalars(stmt)).first()


async def list_agent_grants(
    user_id: str,
    *,
    org_id: str | None = None,
    statuses: list[str] | tuple[str, ...] | None = None,
    limit: int = 50,
) -> list[dict]:
    """List vault access grants visible to the vault owner."""
    return await async_list_agent_grants(user_id, org_id=org_id, statuses=list(statuses or []), limit=limit)


async def approve_agent_grant(
    grant_id: int,
    *,
    approved_by_user_id: str,
    org_id: str | None = None,
    ttl_minutes: int = 15,
    max_reads: int | None = None,
) -> dict | None:
    """Approve a pending agent grant for this user's vault."""
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
    org_id: str | None = None,
) -> dict | None:
    """Deny/revoke an agent grant for this user's vault."""
    return await async_deny_agent_grant(grant_id, denied_by_user_id=denied_by_user_id, org_id=org_id)


async def authorize_agent_secret_read(
    key_name: str,
    *,
    user_id: str,
    org_id: str | None,
    run_id: int | None,
    reason: str | None,
    requested_by: str = "agent",
    project_slug: str | None = None,
    project_slugs: list[str] | tuple[str, ...] | set[str] | None = None,
    target_registry_id: int | None = None,
) -> dict:
    """Allow policy-based reads, consume a grant, or create a pending request.

    Agents should never be allowed to read arbitrary user secrets simply because
    they are running with that user's context. Available secrets may be read by
    any agent for the user, ask-level secrets need either a matching project
    binding or a live run-scoped grant, and manual secrets are never auto-read.
    """
    return await async_authorize_agent_secret_read(
        key_name,
        user_id=user_id,
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
    user_id: str,
    org_id: str | None,
    run_id: int | None,
    reason: str | None,
    requested_by: str = "agent",
    project_slug: str | None = None,
    project_slugs: list[str] | tuple[str, ...] | set[str] | None = None,
    target_registry_id: int | None = None,
) -> dict:
    """Async policy check/approval flow for agent secret reads."""
    if not user_id:
        return {"allowed": False, "status": "denied", "reason": "authenticated user context required"}
    clean_org_id = org_id or None
    secret_found = False

    async with UnitOfWork() as uow:
        secret = (
            await _visible_secret_by_key(
                uow,
                user_id=user_id,
                key_name=key_name,
                org_id=clean_org_id,
            )
        )
        if secret:
            secret_found = True
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
            rows = await _async_project_binding_rows(uow, user_id=user_id, org_id=clean_org_id, secret_id=secret.id)
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

    if not secret_found:
        await _async_record_missing(key_name, user_id=user_id, org_id=clean_org_id)
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
            VaultAgentGrant.key_name == key_name,
            VaultAgentGrant.user_id == user_id,
            VaultAgentGrant.run_id == run_id,
        )
        approved_stmt = select(VaultAgentGrant).where(
            *base,
            VaultAgentGrant.status == "approved",
            VaultAgentGrant.read_count < VaultAgentGrant.max_reads,
            or_(VaultAgentGrant.expires_at.is_(None), VaultAgentGrant.expires_at > now),
        )
        if clean_org_id:
            approved_stmt = approved_stmt.where(VaultAgentGrant.org_id == clean_org_id)
        else:
            approved_stmt = approved_stmt.where(VaultAgentGrant.org_id.is_(None))
        grant = (
            await uow.session.scalars(
                approved_stmt.order_by(VaultAgentGrant.expires_at.desc()).limit(1).with_for_update()
            )
        ).first()
        if grant:
            grant.read_count = int(grant.read_count or 0) + 1
            grant.last_used_at = now
            if grant.read_count >= int(grant.max_reads or 1):
                grant.status = "used"
            await uow.session.flush()
            return {"allowed": True, "status": "approved", "grant": _grant_to_dict(grant)}

        pending_stmt = select(VaultAgentGrant).where(*base, VaultAgentGrant.status == "pending")
        if clean_org_id:
            pending_stmt = pending_stmt.where(VaultAgentGrant.org_id == clean_org_id)
        else:
            pending_stmt = pending_stmt.where(VaultAgentGrant.org_id.is_(None))
        pending = (
            await uow.session.scalars(
                pending_stmt.order_by(VaultAgentGrant.requested_at.desc()).limit(1).with_for_update()
            )
        ).first()
        if pending:
            pending.reason = clean_reason
            pending.requested_by = requested_by
            pending.requested_at = now
            await uow.session.flush()
            return {"allowed": False, "status": "pending", "grant": _grant_to_dict(pending)}

        pending = VaultAgentGrant(
            key_name=key_name,
            user_id=user_id,
            org_id=clean_org_id,
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


async def async_delete_secret(key_name: str, user_id: str, *, org_id: str | None = None) -> bool:
    """Delete a secret through the native async DB path."""
    if not user_id:
        raise ValueError("user_id is required to delete a vault secret")
    async with UnitOfWork() as uow:
        secret = await _async_secret_by_key_for_write(
            uow,
            user_id=user_id,
            key_name=key_name,
            org_id=org_id,
        )
        if not secret:
            return False
        if not _secret_bindable_by_actor(secret, user_id=user_id, org_id=org_id):
            return False
        await _async_log_access(
            user_id,
            secret.id,
            key_name,
            "delete",
            "user",
            org_id=_secret_org_id(secret) or org_id,
            uow=uow,
        )
        await uow.session.delete(secret)
        return True


async def async_revoke_share(share_id: int, user_id: str) -> bool:
    """Revoke a share through the native async DB path."""
    async with UnitOfWork() as uow:
        stmt = select(VaultShare).where(
            VaultShare.id == share_id,
            VaultShare.shared_by_user_id == user_id,
            VaultShare.revoked_at.is_(None),
        )
        share = (await uow.session.scalars(stmt)).first()
        if share:
            share.revoked_at = datetime.now(timezone.utc)
            await _async_log_access(user_id, share.secret_id, "", "revoke", "user", uow=uow)
            return True
        return False


async def async_get_missing_requests(
    user_id: str | None = None,
    *,
    org_id: str | None = None,
    include_resolved: bool = False,
) -> list[dict]:
    if not user_id and not org_id:
        return []
    async with UnitOfWork() as uow:
        stmt = select(VaultMissingRequest)
        if not include_resolved:
            stmt = stmt.where(VaultMissingRequest.resolved == False)  # noqa: E712
        if org_id:
            stmt = stmt.where(VaultMissingRequest.org_id == org_id)
        elif user_id:
            stmt = stmt.where(VaultMissingRequest.user_id == user_id)
        stmt = stmt.order_by(VaultMissingRequest.last_requested.desc()).limit(20)
        rows = (await uow.session.scalars(stmt)).all()
        return [
            {
                "key_name": r.key_name,
                "request_count": r.request_count,
                "first_requested": r.first_requested,
                "last_requested": r.last_requested,
                "user_id": r.user_id,
                "org_id": r.org_id,
            }
            for r in rows
        ]


async def async_get_vault_access_log(user_id: str, *, org_id: str | None = None, limit: int = 100) -> list[dict]:
    async with UnitOfWork() as uow:
        stmt = select(VaultAccessLog, User.name.label("actor_name")).join(
            User,
            User.id == VaultAccessLog.user_id,
        )
        if org_id:
            stmt = stmt.where(User.org_id == org_id)
        else:
            subq = select(Secret.id).where(Secret.user_id == user_id).scalar_subquery()
            stmt = stmt.where(
                (VaultAccessLog.secret_id.in_(subq)) | (VaultAccessLog.user_id == user_id)
            )
        stmt = stmt.order_by(VaultAccessLog.accessed_at.desc()).limit(limit)
        rows = (await uow.session.execute(stmt)).all()
        return [
            {
                "id": log.id,
                "key_name": log.key_name,
                "action": log.action,
                "accessed_by": log.accessed_by,
                "accessed_at": log.accessed_at,
                "actor_name": actor_name,
            }
            for log, actor_name in rows
        ]


async def async_get_org_users(org_id: str) -> list[dict]:
    """Return users in the org for share pickers using native async DB access."""
    async with UnitOfWork() as uow:
        users = await uow.team.list_by_org(org_id)
        return [{"id": str(u.id), "name": u.name, "email": u.email, "color": u.color} for u in users]


async def async_list_agent_grants(
    user_id: str,
    *,
    org_id: str | None = None,
    statuses: list[str] | None = None,
    limit: int = 50,
) -> list[dict]:
    """List vault access grants through the native async DB path."""
    if limit <= 0:
        return []
    async with UnitOfWork() as uow:
        base_stmt = select(VaultAgentGrant).where(VaultAgentGrant.user_id == user_id)
        if org_id:
            base_stmt = base_stmt.where(VaultAgentGrant.org_id == org_id)
        if statuses:
            base_stmt = base_stmt.where(VaultAgentGrant.status.in_(tuple(statuses)))

        visible_grants: list[VaultAgentGrant] = []
        batch_size = max(limit * 2, 50)
        offset = 0
        while len(visible_grants) < limit:
            grants = list(
                (
                    await uow.session.scalars(
                        base_stmt.order_by(VaultAgentGrant.requested_at.desc())
                        .offset(offset)
                        .limit(batch_size)
                    )
                ).all()
            )
            if not grants:
                break
            offset += len(grants)

            pending_keys = sorted({grant.key_name for grant in grants if grant.status == "pending"})
            secrets_by_key: dict[str, Secret] = {}
            if pending_keys:
                secrets_stmt = (
                    (await _visible_secret_stmt(uow, user_id, org_id=org_id))
                    .where(Secret.key_name.in_(pending_keys))
                    .order_by(case((Secret.user_id == user_id, 0), else_=1), Secret.id.desc())
                )
                for secret in (await uow.session.scalars(secrets_stmt)).all():
                    secrets_by_key.setdefault(secret.key_name, secret)
            visible_grants.extend(
                grant
                for grant in grants
                if _pending_grant_is_actionable(grant, secrets_by_key.get(grant.key_name))
            )
            if len(grants) < batch_size:
                break
        return [_grant_to_dict(grant) for grant in visible_grants[:limit]]


async def async_approve_agent_grant(
    grant_id: int,
    *,
    approved_by_user_id: str,
    org_id: str | None = None,
    ttl_minutes: int = 15,
    max_reads: int = 1,
) -> dict | None:
    """Approve a pending agent grant through the native async DB path."""
    now = datetime.now(timezone.utc)
    async with UnitOfWork() as uow:
        grant = await uow.session.get(VaultAgentGrant, grant_id)
        if not grant or str(grant.user_id) != str(approved_by_user_id):
            return None
        if grant.status != "pending":
            return None
        if org_id and str(grant.org_id) != str(org_id):
            return None
        if org_id is None and grant.org_id is not None:
            return None
        secret = await _visible_secret_by_key(
            uow,
            user_id=approved_by_user_id,
            key_name=grant.key_name,
            org_id=org_id,
        )
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
    org_id: str | None = None,
) -> dict | None:
    """Deny/revoke an agent grant through the native async DB path."""
    now = datetime.now(timezone.utc)
    async with UnitOfWork() as uow:
        grant = await uow.session.get(VaultAgentGrant, grant_id)
        if not grant or str(grant.user_id) != str(denied_by_user_id):
            return None
        if org_id and str(grant.org_id) != str(org_id):
            return None
        if org_id is None and grant.org_id is not None:
            return None
        grant.status = "denied"
        grant.approved_by_user_id = denied_by_user_id
        grant.decided_at = now
        grant.expires_at = now
        await uow.session.flush()
        return _grant_to_dict(grant)


async def async_list_project_bindings(
    user_id: str,
    *,
    org_id: str | None = None,
    secret_id: int | None = None,
) -> list[dict]:
    """List project token bindings through the native async DB path."""
    if not user_id:
        raise ValueError("user_id is required to list project vault bindings")
    async with UnitOfWork() as uow:
        return [
            _binding_to_dict(binding, secret)
            for binding, secret in await _async_project_binding_rows(
                uow,
                user_id=user_id,
                org_id=org_id,
                secret_id=secret_id,
            )
        ]


async def async_bind_project_secret(
    secret_id: int,
    *,
    user_id: str,
    org_id: str | None,
    project_slug: str,
    env_name: str,
    target_registry_id: int | None = None,
) -> dict | None:
    """Bind an org-visible or legacy personal secret to a project/env name using native async DB access."""
    if not user_id:
        raise ValueError("user_id is required to bind a vault secret")
    async with UnitOfWork() as uow:
        secret = await _async_secret_by_id_for_actor(
            uow,
            secret_id=secret_id,
            user_id=user_id,
            org_id=org_id,
        )
        if not secret:
            return None
        binding = await _async_upsert_project_binding(
            uow,
            secret,
            user_id=user_id,
            org_id=org_id,
            project_slug=project_slug,
            env_name=env_name,
            target_registry_id=target_registry_id,
        )
        await uow.session.flush()
        return _binding_to_dict(binding, secret)


async def async_delete_project_binding(binding_id: int, *, user_id: str, org_id: str | None = None) -> bool:
    """Deactivate a project binding through the native async DB path."""
    async with UnitOfWork() as uow:
        binding = await uow.session.get(VaultProjectBinding, binding_id)
        if not binding:
            return False
        if org_id:
            if str(binding.org_id) != str(org_id):
                return False
        elif str(binding.user_id) != str(user_id) or binding.org_id is not None:
            return False
        binding.active = False
        binding.updated_at = datetime.now(timezone.utc)
        await uow.session.flush()
        return True


async def async_share_secret(
    secret_id: int,
    shared_with_user_id: str,
    owner_user_id: str,
    *,
    org_id: str | None = None,
) -> dict | None:
    """Share a secret with another user through the native async DB path."""
    async with UnitOfWork() as uow:
        secret = await uow.vault.get(secret_id)
        if not secret or secret.user_id != owner_user_id:
            return None

        sharer = await uow.session.get(User, owner_user_id)
        recipient = await uow.session.get(User, shared_with_user_id)
        if (
            not sharer
            or not recipient
            or not getattr(sharer, "org_id", None)
            or str(sharer.org_id) != str(recipient.org_id)
        ):
            return None
        if org_id and str(sharer.org_id) != str(org_id):
            return None
        if shared_with_user_id == owner_user_id:
            return None

        stmt = select(VaultShare).where(
            VaultShare.secret_id == secret_id,
            VaultShare.shared_with_user_id == shared_with_user_id,
        )
        existing_share = (await uow.session.scalars(stmt)).first()
        now = datetime.now(timezone.utc)
        if existing_share:
            existing_share.revoked_at = None
            existing_share.shared_at = now
            await uow.session.flush()
            share_id = existing_share.id
            shared_at = existing_share.shared_at
        else:
            share = VaultShare(
                secret_id=secret_id,
                shared_with_user_id=shared_with_user_id,
                shared_by_user_id=owner_user_id,
                shared_at=now,
            )
            uow.session.add(share)
            await uow.session.flush()
            share_id = share.id
            shared_at = share.shared_at

        await _async_log_access(owner_user_id, secret_id, secret.key_name, "share", "user", uow=uow)
        return {"id": share_id, "secret_id": secret_id, "shared_at": shared_at}


# ---------------------------------------------------------------------------
# PIN Protection
# ---------------------------------------------------------------------------

def _pin_hash_key(user_id: str) -> str:
    return f"pin:{user_id}:hash"


def _pin_failures_key(user_id: str) -> str:
    return f"pin:{user_id}:failures"


def _pin_lockout_key(user_id: str) -> str:
    return f"pin:{user_id}:lockout"


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


async def get_pin_status(user_id: str) -> dict:
    return await async_get_pin_status(user_id)


async def async_get_pin_status(user_id: str) -> dict:
    lockout = await async_get_config(_pin_lockout_key(user_id))
    locked_until = None
    if lockout:
        try:
            lockout_time = datetime.fromisoformat(lockout)
            if datetime.now(timezone.utc) < lockout_time:
                locked_until = lockout_time
        except ValueError:
            await async_delete_config(_pin_lockout_key(user_id))
    return {
        "has_pin": await async_has_pin(user_id),
        "failed_attempts": int(await async_get_config(_pin_failures_key(user_id)) or "0"),
        "locked_until": locked_until,
    }


async def set_pin(user_id: str, new_pin: str, current_pin: str | None = None) -> bool:
    return await async_set_pin(user_id, new_pin, current_pin)


async def async_set_pin(user_id: str, new_pin: str, current_pin: str | None = None) -> bool:
    if await async_has_pin(user_id) and not await async_verify_pin(user_id, current_pin or ""):
        return False
    pin_hash = bcrypt.hashpw(new_pin.encode(), bcrypt.gensalt()).decode()
    await async_set_config(_pin_hash_key(user_id), pin_hash)
    await async_set_config(_pin_failures_key(user_id), "0")
    await async_delete_config(_pin_lockout_key(user_id))
    return True


async def has_pin(user_id: str) -> bool:
    return await async_has_pin(user_id)


async def async_has_pin(user_id: str) -> bool:
    return await async_get_config(_pin_hash_key(user_id)) is not None


async def verify_pin(user_id: str, pin: str) -> bool:
    return await async_verify_pin(user_id, pin)


async def async_verify_pin(user_id: str, pin: str) -> bool:
    lockout = await async_get_config(_pin_lockout_key(user_id))
    if lockout:
        lockout_time = datetime.fromisoformat(lockout)
        if datetime.now(timezone.utc) < lockout_time:
            return False
        await async_delete_config(_pin_lockout_key(user_id))
        await async_set_config(_pin_failures_key(user_id), "0")

    stored_hash = await async_get_config(_pin_hash_key(user_id))
    if not stored_hash:
        return True

    if bcrypt.checkpw(pin.encode(), stored_hash.encode()):
        await async_set_config(_pin_failures_key(user_id), "0")
        return True

    attempts = int(await async_get_config(_pin_failures_key(user_id)) or "0") + 1
    await async_set_config(_pin_failures_key(user_id), str(attempts))
    if attempts >= VAULT_LOCKOUT_AFTER_FAILURES:
        lockout_until = datetime.now(timezone.utc) + VAULT_LOCKOUT_DURATION
        await async_set_config(_pin_lockout_key(user_id), lockout_until.isoformat())
    return False


async def generate_vault_token(user_id: str) -> tuple[str, datetime]:
    return await async_generate_vault_token(user_id)


async def async_generate_vault_token(user_id: str) -> tuple[str, datetime]:
    token = stdlib_secrets.token_urlsafe(32)
    now = _utcnow()
    expires = now + VAULT_SESSION_TTL
    async with UnitOfWork() as uow:
        uow.session.add(VaultSession(
            token_hash=_token_hash(token),
            user_id=user_id,
            created_at=now,
            expires_at=expires,
        ))
    return token, _as_utc(expires)


async def unlock_vault(user_id: str, pin: str) -> tuple[str, datetime] | None:
    return await async_unlock_vault(user_id, pin)


async def async_unlock_vault(user_id: str, pin: str) -> tuple[str, datetime] | None:
    if not await async_verify_pin(user_id, pin):
        return None
    return await async_generate_vault_token(user_id)


async def validate_vault_token(user_id: str, token: str | None) -> bool:
    return await async_validate_vault_token(user_id, token)


async def async_validate_vault_token(user_id: str, token: str | None) -> bool:
    if not token:
        return False
    now = _utcnow()
    async with UnitOfWork() as uow:
        session = await uow.session.get(VaultSession, _token_hash(token))
        if not session or str(session.user_id) != str(user_id):
            return False
        if session.revoked_at is not None:
            return False
        if now > _as_db_utc(session.expires_at):
            session.revoked_at = now
            return False
        session.last_seen_at = now
        return True


async def revoke_vault_token(user_id: str, token: str | None) -> None:
    await async_revoke_vault_token(user_id, token)


async def async_revoke_vault_token(user_id: str, token: str | None) -> None:
    if not token:
        return
    async with UnitOfWork() as uow:
        session = await uow.session.get(VaultSession, _token_hash(token))
        if session and str(session.user_id) == str(user_id) and session.revoked_at is None:
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
