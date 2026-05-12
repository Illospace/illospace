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
from sqlalchemy import case, inspect, or_, select
from sqlalchemy.exc import SQLAlchemyError

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
from brain.platform.db.repositories.unit_of_work import UnitOfWork, run_sync_with_unit_of_work

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
        return inspect(uow.session.connection()).has_table(table_name)
    except Exception:
        return True


def _log_access(user_id: str, secret_id: int | None, key_name: str,
                action: str, accessed_by: str = "user",
                uow: UnitOfWork | None = None) -> None:
    """Record a vault access event. Fail-silent.

    If ``uow`` is provided, uses the existing UnitOfWork session (avoids opening a new one).
    """
    try:
        entry = VaultAccessLog(
            user_id=user_id,
            secret_id=secret_id,
            key_name=key_name,
            action=action,
            accessed_by=_normalize_accessed_by(accessed_by),
        )
        if uow:
            uow.session.add(entry)
        else:
            with UnitOfWork() as _uow:
                _uow.session.add(entry)
    except Exception:
        pass  # Never break vault operations for audit logging


async def _async_log_access(
    user_id: str,
    secret_id: int | None,
    key_name: str,
    action: str,
    accessed_by: str = "user",
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

def get_secret(
    key_name: str,
    user_id: str,
    *,
    org_id: str | None = None,
    allow_shared: bool = True,
    allow_env_fallback: bool = False,
    accessed_by: str = "user",
) -> str | None:
    """Retrieve and decrypt a user-scoped secret.

    User-facing and agent-facing vault reads must be scoped to an authenticated
    user. Environment fallback is opt-in for explicit system integrations only;
    it is never used by dashboard reveal or the agent vault tool.
    """
    if not user_id:
        raise ValueError("user_id is required to read a vault secret")
    with UnitOfWork() as uow:
        # Try own secret first.
        secret = uow.vault.get_by_key(user_id, key_name)
        if not secret and allow_shared:
            # Then try active shares granted to this user.
            stmt = (
                select(Secret)
                .join(VaultShare, VaultShare.secret_id == Secret.id)
                .where(
                    Secret.key_name == key_name,
                    VaultShare.shared_with_user_id == user_id,
                    VaultShare.revoked_at.is_(None),
                )
                .limit(1)
            )
            if org_id:
                stmt = stmt.join(User, User.id == Secret.user_id).where(User.org_id == org_id)
            secret = uow.session.scalars(stmt).first()

        if secret:
            secret.last_accessed_at = datetime.now(timezone.utc)
            secret.access_count = (secret.access_count or 0) + 1
            _log_access(user_id, secret.id, key_name, "read", accessed_by, uow=uow)
            return _decrypt(bytes(secret.encrypted_value))

    if allow_env_fallback and (env_val := os.environ.get(key_name)):
        return env_val
    _record_missing(key_name, user_id=user_id, org_id=org_id)
    return None


async def async_get_secret(
    key_name: str,
    user_id: str,
    *,
    org_id: str | None = None,
    allow_shared: bool = True,
    allow_env_fallback: bool = False,
    accessed_by: str = "user",
) -> str | None:
    """Async retrieve/decrypt path for user-scoped secrets."""

    if not user_id:
        raise ValueError("user_id is required to read a vault secret")
    async with UnitOfWork() as uow:
        secret = await uow.vault.get_by_key(user_id, key_name)
        if not secret and allow_shared:
            stmt = (
                select(Secret)
                .join(VaultShare, VaultShare.secret_id == Secret.id)
                .where(
                    Secret.key_name == key_name,
                    VaultShare.shared_with_user_id == user_id,
                    VaultShare.revoked_at.is_(None),
                )
                .limit(1)
            )
            if org_id:
                stmt = stmt.join(User, User.id == Secret.user_id).where(User.org_id == org_id)
            result = await uow.session.scalars(stmt)
            secret = result.first()

        if secret:
            secret.last_accessed_at = datetime.now(timezone.utc)
            secret.access_count = (secret.access_count or 0) + 1
            await _async_log_access(user_id, secret.id, key_name, "read", accessed_by, uow=uow)
            return _decrypt(bytes(secret.encrypted_value))

    if allow_env_fallback and (env_val := os.environ.get(key_name)):
        return env_val
    return None


def set_secret(
    key_name: str,
    value: str,
    user_id: str,
    *,
    org_id: str | None = None,
    description: str = "",
    category: str = "general",
    agent_access_level: str | None = None,
) -> None:
    """Encrypt and upsert a secret, scoped to user."""
    encrypted = _encrypt(value)
    now = datetime.now(timezone.utc)
    if not user_id:
        raise ValueError("user_id is required to set a secret (user_id is NOT NULL)")
    normalized_agent_access = (
        normalize_agent_access_level(agent_access_level)
        if agent_access_level is not None
        else None
    )
    with UnitOfWork() as uow:
        existing = uow.vault.get_by_key(user_id, key_name)
        if existing:
            existing.encrypted_value = encrypted
            existing.description = description
            existing.category = category
            if normalized_agent_access is not None:
                existing.agent_access_level = normalized_agent_access
            existing.updated_at = now
            uow.session.flush()
            _log_access(user_id, existing.id, key_name, "write", uow=uow)
        else:
            secret = Secret(
                key_name=key_name,
                encrypted_value=encrypted,
                description=description,
                category=category,
                agent_access_level=normalized_agent_access or DEFAULT_VAULT_AGENT_ACCESS_LEVEL,
                user_id=user_id,
                created_at=now,
                updated_at=now,
            )
            uow.session.add(secret)
            uow.session.flush()
            _log_access(user_id, secret.id, key_name, "write", uow=uow)
    resolve_missing(key_name, user_id=user_id, org_id=org_id)


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
    """Async encrypt/upsert path for user-scoped secrets."""

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
        existing = await uow.vault.get_by_key(user_id, key_name)
        if existing:
            existing.encrypted_value = encrypted
            existing.description = description
            existing.category = category
            if normalized_agent_access is not None:
                existing.agent_access_level = normalized_agent_access
            existing.updated_at = now
            await uow.session.flush()
            await _async_log_access(user_id, existing.id, key_name, "write", uow=uow)
        else:
            secret = Secret(
                key_name=key_name,
                encrypted_value=encrypted,
                description=description,
                category=category,
                agent_access_level=normalized_agent_access or DEFAULT_VAULT_AGENT_ACCESS_LEVEL,
                user_id=user_id,
                created_at=now,
                updated_at=now,
            )
            uow.session.add(secret)
            await uow.session.flush()
            await _async_log_access(user_id, secret.id, key_name, "write", uow=uow)


def delete_secret(key_name: str, user_id: str) -> bool:
    """Delete a secret. Returns True if it existed."""
    if not user_id:
        raise ValueError("user_id is required to delete a vault secret")
    with UnitOfWork() as uow:
        secret = uow.vault.get_by_key(user_id, key_name)
        if not secret:
            return False
        _log_access(user_id, secret.id, key_name, "delete", "user", uow=uow)
        uow.session.delete(secret)
        return True


def list_secrets(
    user_id: str,
    category: str | None = None,
    *,
    org_id: str | None = None,
) -> list[dict]:
    """Return metadata for user's own + shared secrets (never includes encrypted_value)."""
    if not user_id:
        raise ValueError("user_id is required to list vault secrets")
    with UnitOfWork() as uow:
        results = []

        # Own secrets
        stmt = select(Secret).where(Secret.user_id == user_id)
        if category:
            stmt = stmt.where(Secret.category == category)
        own_secrets = uow.session.scalars(stmt).all()
        for s in own_secrets:
            results.append({
                "id": s.id, "key_name": s.key_name, "description": s.description,
                "category": s.category, "created_at": s.created_at,
                "updated_at": s.updated_at, "last_accessed_at": s.last_accessed_at,
                "access_count": s.access_count, "user_id": s.user_id,
                "agent_access_level": getattr(s, "agent_access_level", None) or DEFAULT_VAULT_AGENT_ACCESS_LEVEL,
                "shared_by_name": None, "is_shared": False,
            })

        # Shared secrets
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
        shared_rows = uow.session.execute(shared_stmt).all()
        for s, shared_by_name in shared_rows:
            results.append({
                "id": s.id, "key_name": s.key_name, "description": s.description,
                "category": s.category, "created_at": s.created_at,
                "updated_at": s.updated_at, "last_accessed_at": s.last_accessed_at,
                "access_count": s.access_count, "user_id": s.user_id,
                "agent_access_level": getattr(s, "agent_access_level", None) or DEFAULT_VAULT_AGENT_ACCESS_LEVEL,
                "shared_by_name": shared_by_name, "is_shared": True,
            })
        results.sort(key=lambda r: (r["category"], r["key_name"], r["is_shared"]))
        return results


async def async_list_secrets(
    user_id: str,
    category: str | None = None,
    *,
    org_id: str | None = None,
) -> list[dict]:
    """Async metadata listing for user's own + shared secrets."""

    if not user_id:
        raise ValueError("user_id is required to list vault secrets")
    async with UnitOfWork() as uow:
        results = []

        stmt = select(Secret).where(Secret.user_id == user_id)
        if category:
            stmt = stmt.where(Secret.category == category)
        own_result = await uow.session.scalars(stmt)
        for s in own_result.all():
            results.append({
                "id": s.id, "key_name": s.key_name, "description": s.description,
                "category": s.category, "created_at": s.created_at,
                "updated_at": s.updated_at, "last_accessed_at": s.last_accessed_at,
                "access_count": s.access_count, "user_id": s.user_id,
                "agent_access_level": getattr(s, "agent_access_level", None) or DEFAULT_VAULT_AGENT_ACCESS_LEVEL,
                "shared_by_name": None, "is_shared": False,
            })

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
            results.append({
                "id": s.id, "key_name": s.key_name, "description": s.description,
                "category": s.category, "created_at": s.created_at,
                "updated_at": s.updated_at, "last_accessed_at": s.last_accessed_at,
                "access_count": s.access_count, "user_id": s.user_id,
                "agent_access_level": getattr(s, "agent_access_level", None) or DEFAULT_VAULT_AGENT_ACCESS_LEVEL,
                "shared_by_name": shared_by_name, "is_shared": True,
            })
        results.sort(key=lambda r: (r["category"], r["key_name"], r["is_shared"]))
        return results


async def async_get_secret_record(key_name: str, user_id: str) -> Secret | None:
    """Async fetch of an owned secret metadata row."""

    if not user_id:
        raise ValueError("user_id is required to read a vault secret")
    async with UnitOfWork() as uow:
        return await uow.vault.get_by_key(user_id, key_name)


def reveal_secret(key_name: str, user_id: str, *, org_id: str | None = None) -> str | None:
    """Reveal a secret for the dashboard (updates access stats)."""
    return get_secret(key_name, user_id=user_id, org_id=org_id, accessed_by="user")


async def async_reveal_secret(key_name: str, user_id: str, *, org_id: str | None = None) -> str | None:
    """Async reveal path for the dashboard."""

    return await async_get_secret(key_name, user_id=user_id, org_id=org_id, accessed_by="user")


def require_secret(
    key_name: str,
    user_id: str,
    *,
    org_id: str | None = None,
    allow_env_fallback: bool = False,
) -> str:
    """Like get_secret but raises ValueError if not found anywhere."""
    value = get_secret(
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
            VaultProjectBinding.user_id == user_id,
            Secret.user_id == user_id,
            VaultProjectBinding.active == True,  # noqa: E712
        )
    )
    if org_id:
        stmt = stmt.where(or_(VaultProjectBinding.org_id == org_id, VaultProjectBinding.org_id.is_(None)))
    else:
        stmt = stmt.where(VaultProjectBinding.org_id.is_(None))
    if secret_id is not None:
        stmt = stmt.where(VaultProjectBinding.secret_id == secret_id)
    return stmt.order_by(VaultProjectBinding.project_slug, VaultProjectBinding.env_name)


def _project_binding_rows(
    uow: UnitOfWork,
    *,
    user_id: str,
    org_id: str | None = None,
    secret_id: int | None = None,
) -> list[tuple[VaultProjectBinding, Secret]]:
    if not _table_exists(uow, "vault_project_bindings"):
        return []
    try:
        return list(
            uow.session.execute(
                _project_binding_rows_stmt(
                    user_id=user_id,
                    org_id=org_id,
                    secret_id=secret_id,
                )
            ).all()
        )
    except SQLAlchemyError:
        uow.session.rollback()
        return []


def _matching_project_binding_rows(
    uow: UnitOfWork,
    *,
    user_id: str,
    org_id: str | None = None,
    project_slug: str | None = None,
    project_slugs: list[str] | tuple[str, ...] | set[str] | None = None,
    target_registry_id: int | None = None,
    secret_id: int | None = None,
) -> list[tuple[VaultProjectBinding, Secret]]:
    return [
        (binding, secret)
        for binding, secret in _project_binding_rows(
            uow,
            user_id=user_id,
            org_id=org_id,
            secret_id=secret_id,
        )
        if _binding_project_matches(
            binding,
            project_slug=project_slug,
            project_slugs=project_slugs,
            target_registry_id=target_registry_id,
        )
    ]


def _find_owned_secret_for_policy(
    uow: UnitOfWork,
    *,
    key_name: str,
    user_id: str,
) -> Secret | None:
    if not _table_exists(uow, "secrets"):
        return None
    try:
        stmt = select(Secret).where(
            Secret.user_id == user_id,
            Secret.key_name == key_name,
        )
        return uow.session.scalars(stmt.limit(1)).first()
    except SQLAlchemyError:
        uow.session.rollback()
        return None


def _secret_has_project_binding(
    uow: UnitOfWork,
    *,
    secret_id: int,
    user_id: str,
    org_id: str | None = None,
    project_slug: str | None = None,
    project_slugs: list[str] | tuple[str, ...] | set[str] | None = None,
    target_registry_id: int | None = None,
) -> VaultProjectBinding | None:
    rows = _matching_project_binding_rows(
        uow,
        user_id=user_id,
        org_id=org_id,
        project_slug=project_slug,
        project_slugs=project_slugs,
        target_registry_id=target_registry_id,
        secret_id=secret_id,
    )
    return rows[0][0] if rows else None


def _upsert_project_binding(
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
        VaultProjectBinding.user_id == user_id,
        VaultProjectBinding.project_slug == clean_project_slug,
        VaultProjectBinding.env_name == clean_env_name,
    )
    existing = uow.session.scalars(stmt.limit(1)).first()
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


def bind_project_secret(
    secret_id: int,
    *,
    user_id: str,
    org_id: str | None = None,
    project_slug: str,
    env_name: str,
    target_registry_id: int | None = None,
) -> dict | None:
    """Bind a user's own secret to a project/env name."""
    if not user_id:
        raise ValueError("user_id is required to bind a vault secret")
    with UnitOfWork() as uow:
        secret = uow.session.get(Secret, secret_id)
        if not secret or str(secret.user_id) != str(user_id):
            return None
        binding = _upsert_project_binding(
            uow,
            secret,
            user_id=user_id,
            org_id=org_id,
            project_slug=project_slug,
            env_name=env_name,
            target_registry_id=target_registry_id,
        )
        uow.session.flush()
        return _binding_to_dict(binding, secret)


def bind_project_secret_by_key(
    key_name: str,
    *,
    user_id: str,
    org_id: str | None = None,
    project_slug: str,
    env_name: str,
    target_registry_id: int | None = None,
) -> dict | None:
    """Bind one of the current user's own secrets to a project/env name."""
    if not user_id:
        raise ValueError("user_id is required to bind a vault secret")
    clean_key_name = (key_name or "").strip()
    if not clean_key_name:
        raise ValueError("key_name is required")
    with UnitOfWork() as uow:
        secret = _find_owned_secret_for_policy(uow, key_name=clean_key_name, user_id=user_id)
        if secret is None:
            return None
        binding = _upsert_project_binding(
            uow,
            secret,
            user_id=user_id,
            org_id=org_id,
            project_slug=project_slug,
            env_name=env_name,
            target_registry_id=target_registry_id,
        )
        uow.session.flush()
        return _binding_to_dict(binding, secret)


def list_project_bindings(
    *,
    user_id: str,
    org_id: str | None = None,
    secret_id: int | None = None,
) -> list[dict]:
    """List project token bindings owned by the current user."""
    if not user_id:
        raise ValueError("user_id is required to list project vault bindings")
    with UnitOfWork() as uow:
        return [
            _binding_to_dict(binding, secret)
            for binding, secret in _project_binding_rows(
                uow,
                user_id=user_id,
                org_id=org_id,
                secret_id=secret_id,
            )
        ]


def delete_project_binding(
    binding_id: int,
    *,
    user_id: str,
    org_id: str | None = None,
) -> bool:
    """Deactivate a project binding owned by the current user."""
    with UnitOfWork() as uow:
        binding = uow.session.get(VaultProjectBinding, binding_id)
        if not binding or str(binding.user_id) != str(user_id):
            return False
        if not _binding_org_matches(binding, org_id):
            return False
        binding.active = False
        binding.updated_at = datetime.now(timezone.utc)
        uow.session.flush()
        return True


def resolve_project_bound_env_tokens(
    *,
    user_id: str,
    org_id: str | None = None,
    project_slug: str | None = None,
    project_slugs: list[str] | tuple[str, ...] | set[str] | None = None,
    target_registry_id: int | None = None,
) -> dict[str, str]:
    """Return env-name/token pairs for project-bound secrets.

    Only the user's own tokens are eligible. Manual secrets are intentionally
    excluded; ask-level secrets become available only through a matching project
    binding, while available secrets still need a binding to define an env name.
    """
    if not user_id or (not project_slug and not project_slugs and target_registry_id is None):
        return {}
    with UnitOfWork() as uow:
        env: dict[str, str] = {}
        for binding, secret in _matching_project_binding_rows(
            uow,
            user_id=user_id,
            org_id=org_id,
            project_slug=project_slug,
            project_slugs=project_slugs,
            target_registry_id=target_registry_id,
        ):
            if normalize_agent_access_level(getattr(secret, "agent_access_level", None)) == VAULT_AGENT_ACCESS_MANUAL:
                continue
            secret.last_accessed_at = datetime.now(timezone.utc)
            secret.access_count = (secret.access_count or 0) + 1
            _log_access(user_id, secret.id, secret.key_name, "read", "agent", uow=uow)
            env[binding.env_name] = _decrypt(bytes(secret.encrypted_value))
        return env


# ---------------------------------------------------------------------------
# Sharing
# ---------------------------------------------------------------------------

def share_secret(
    secret_id: int,
    shared_with_user_id: str,
    shared_by_user_id: str,
    *,
    org_id: str | None = None,
) -> dict | None:
    """Share a secret with another user. Returns share record or None if secret not found."""
    with UnitOfWork() as uow:
        # Verify the sharer owns the secret
        secret = uow.vault.get(secret_id)
        if not secret or secret.user_id != shared_by_user_id:
            return None

        sharer = uow.session.get(User, shared_by_user_id)
        recipient = uow.session.get(User, shared_with_user_id)
        if (
            not sharer
            or not recipient
            or not getattr(sharer, "org_id", None)
            or str(sharer.org_id) != str(recipient.org_id)
        ):
            return None
        if org_id and str(sharer.org_id) != str(org_id):
            return None
        if shared_with_user_id == shared_by_user_id:
            return None

        # Check for existing share (upsert logic)
        stmt = select(VaultShare).where(
            VaultShare.secret_id == secret_id,
            VaultShare.shared_with_user_id == shared_with_user_id,
        )
        existing_share = uow.session.scalars(stmt).first()
        now = datetime.now(timezone.utc)
        if existing_share:
            existing_share.revoked_at = None
            existing_share.shared_at = now
            uow.session.flush()
            share_id = existing_share.id
            shared_at = existing_share.shared_at
        else:
            share = VaultShare(
                secret_id=secret_id,
                shared_with_user_id=shared_with_user_id,
                shared_by_user_id=shared_by_user_id,
                shared_at=now,
            )
            uow.session.add(share)
            uow.session.flush()
            share_id = share.id
            shared_at = share.shared_at

        _log_access(shared_by_user_id, secret_id, secret.key_name, "share", "user", uow=uow)
        return {"id": share_id, "secret_id": secret_id, "shared_at": shared_at}


def revoke_share(share_id: int, user_id: str) -> bool:
    """Revoke a vault share. Only the original sharer can revoke."""
    with UnitOfWork() as uow:
        stmt = select(VaultShare).where(
            VaultShare.id == share_id,
            VaultShare.shared_by_user_id == user_id,
            VaultShare.revoked_at.is_(None),
        )
        share = uow.session.scalars(stmt).first()
        if share:
            share.revoked_at = datetime.now(timezone.utc)
            _log_access(user_id, share.secret_id, "", "revoke", "user", uow=uow)
            return True
        return False


# ---------------------------------------------------------------------------
# Per-user API key management
# ---------------------------------------------------------------------------

def resolve_api_key(
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
    with UnitOfWork() as uow:
        # 1. User's chosen default key
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
            key_row = uow.session.scalars(stmt).first()
            if key_row:
                return _decrypt(bytes(key_row.encrypted_key)), "user_default"

            # 1b. Provider-specific fallback
            #
            # `users.default_api_key_id` is a single slot shared across providers.
            # For multiplayer/provider-mixed setups, allow a stable per-provider
            # fallback so a user can keep Anthropic and OpenAI credentials side by
            # side without one clobbering the other. Prefer label="default" when
            # present, otherwise fall back to the newest active key for that
            # provider (for legacy labels like "Claude Code").
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
            key_row = uow.session.scalars(stmt).first()
            if key_row:
                return _decrypt(bytes(key_row.encrypted_key)), "user_default"

        # 2. Org main key
        _org_id = org_id
        if not _org_id and user_id:
            user = uow.session.get(User, user_id)
            if user:
                _org_id = str(user.org_id)

        if _org_id:
            stmt = select(OrgApiKey).where(
                OrgApiKey.org_id == _org_id,
                OrgApiKey.provider == provider,
            )
            org_key = uow.session.scalars(stmt).first()
            if org_key:
                return _decrypt(bytes(org_key.encrypted_key)), "org_main"

    # 3. Environment variable (dev convenience — no DB needed)
    env_key = os.environ.get(f"{provider.upper()}_API_KEY")
    if env_key:
        return env_key, "env"

    return None, "none"


def update_resolved_api_key(
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
    if source not in {"user_default", "org_main"}:
        return False

    encrypted = _encrypt(api_key)

    with UnitOfWork() as uow:
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
            key_row = uow.session.scalars(stmt).first()

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
                key_row = uow.session.scalars(stmt).first()

            if key_row:
                key_row.encrypted_key = encrypted
                key_row.is_active = True
                uow.session.flush()
                return True

        if source == "org_main":
            _org_id = org_id
            if not _org_id and user_id:
                user = uow.session.get(User, user_id)
                if user:
                    _org_id = str(user.org_id)

            if _org_id:
                stmt = select(OrgApiKey).where(
                    OrgApiKey.org_id == _org_id,
                    OrgApiKey.provider == provider,
                )
                org_key = uow.session.scalars(stmt).first()
                if org_key:
                    org_key.encrypted_key = encrypted
                    uow.session.flush()
                    return True

    return False


def set_api_key(
    user_id: str,
    api_key: str,
    provider: str = "anthropic",
    label: str = "default",
) -> int:
    """Store an encrypted API key for a user. Returns the key ID."""
    encrypted = _encrypt(api_key)
    with UnitOfWork() as uow:
        # Check for existing key (upsert logic)
        stmt = select(UserApiKey).where(
            UserApiKey.user_id == user_id,
            UserApiKey.provider == provider,
            UserApiKey.label == label,
        )
        existing = uow.session.scalars(stmt).first()
        if existing:
            existing.encrypted_key = encrypted
            existing.is_active = True
            uow.session.flush()
            return existing.id
        else:
            key_obj = UserApiKey(
                user_id=user_id,
                provider=provider,
                encrypted_key=encrypted,
                label=label,
            )
            uow.session.add(key_obj)
            uow.session.flush()
            return key_obj.id


def share_api_key(
    api_key_id: int,
    shared_with_user_id: str,
    shared_by_user_id: str,
) -> int:
    """Share an API key with another user. Returns the share ID."""
    with UnitOfWork() as uow:
        # Verify ownership
        stmt = select(UserApiKey).where(
            UserApiKey.id == api_key_id,
            UserApiKey.user_id == shared_by_user_id,
        )
        key_row = uow.session.scalars(stmt).first()
        if not key_row:
            raise ValueError(f"API key {api_key_id} not found or not owned by {shared_by_user_id}")

        sharer = uow.session.get(User, shared_by_user_id)
        recipient = uow.session.get(User, shared_with_user_id)
        if (
            not sharer
            or not recipient
            or not getattr(sharer, "org_id", None)
            or str(sharer.org_id) != str(recipient.org_id)
        ):
            raise ValueError("API keys can only be shared with users in the same org")

        # Upsert share
        stmt = select(ApiKeyShare).where(
            ApiKeyShare.api_key_id == api_key_id,
            ApiKeyShare.shared_with_user_id == shared_with_user_id,
        )
        existing = uow.session.scalars(stmt).first()
        now = datetime.now(timezone.utc)
        if existing:
            existing.revoked_at = None
            existing.shared_at = now
            uow.session.flush()
            return existing.id
        else:
            share = ApiKeyShare(
                api_key_id=api_key_id,
                shared_with_user_id=shared_with_user_id,
                shared_by_user_id=shared_by_user_id,
                shared_at=now,
            )
            uow.session.add(share)
            uow.session.flush()
            return share.id


def revoke_api_key_share(share_id: int, user_id: str) -> bool:
    """Revoke a shared API key. Only the original sharer can revoke."""
    with UnitOfWork() as uow:
        stmt = select(ApiKeyShare).where(
            ApiKeyShare.id == share_id,
            ApiKeyShare.shared_by_user_id == user_id,
            ApiKeyShare.revoked_at.is_(None),
        )
        share = uow.session.scalars(stmt).first()
        if share:
            share.revoked_at = datetime.now(timezone.utc)
            return True
        return False


def record_api_key_usage(
    api_key_id: int,
    tokens_used: int,
    cost_usd: float,
) -> None:
    """Record token usage for spend tracking."""
    with UnitOfWork() as uow:
        key_obj = uow.session.get(UserApiKey, api_key_id)
        if key_obj:
            key_obj.last_used_at = datetime.now(timezone.utc)
            key_obj.total_tokens_used = (key_obj.total_tokens_used or 0) + tokens_used
            key_obj.estimated_cost_usd = float(key_obj.estimated_cost_usd or 0) + cost_usd


def get_vault_access_log(
    user_id: str,
    *,
    org_id: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Return access log scoped to the caller's org, or own rows without org."""
    with UnitOfWork() as uow:
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
        rows = uow.session.execute(stmt).all()
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


def get_org_users(org_id: str) -> list[dict]:
    """Return users in the org (for share picker). Delegates to auth module."""
    from brain.systems.auth.users import get_org_users as _get_org_users
    return _get_org_users(org_id)


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
    now = datetime.now(timezone.utc)
    try:
        with UnitOfWork() as uow:
            stmt = select(VaultMissingRequest).where(VaultMissingRequest.key_name == key_name)
            if org_id:
                stmt = stmt.where(VaultMissingRequest.org_id == org_id)
            elif user_id:
                stmt = stmt.where(VaultMissingRequest.user_id == user_id)
            else:
                return
            existing = uow.session.scalars(
                stmt
            ).first()
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


def record_missing_request(
    key_name: str,
    *,
    user_id: str | None = None,
    org_id: str | None = None,
) -> None:
    """Record a missing secret request from a non-read workflow."""
    _record_missing(key_name, user_id=user_id, org_id=org_id)


def get_missing_requests(
    *,
    user_id: str | None = None,
    org_id: str | None = None,
) -> list[dict]:
    if not user_id and not org_id:
        return []
    with UnitOfWork() as uow:
        stmt = (
            select(VaultMissingRequest)
            .where(VaultMissingRequest.resolved == False)  # noqa: E712
        )
        if org_id:
            stmt = stmt.where(VaultMissingRequest.org_id == org_id)
        elif user_id:
            stmt = stmt.where(VaultMissingRequest.user_id == user_id)
        stmt = stmt.order_by(VaultMissingRequest.last_requested.desc()).limit(20)
        rows = uow.session.scalars(stmt).all()
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


def resolve_missing(
    key_name: str,
    *,
    user_id: str | None = None,
    org_id: str | None = None,
) -> None:
    try:
        with UnitOfWork() as uow:
            stmt = select(VaultMissingRequest).where(VaultMissingRequest.key_name == key_name)
            if org_id:
                stmt = stmt.where(VaultMissingRequest.org_id == org_id)
            elif user_id:
                stmt = stmt.where(VaultMissingRequest.user_id == user_id)
            for existing in uow.session.scalars(stmt).all():
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


def list_agent_grants(
    user_id: str,
    *,
    org_id: str | None = None,
    statuses: list[str] | tuple[str, ...] | None = None,
    limit: int = 50,
) -> list[dict]:
    """List vault access grants visible to the vault owner."""
    with UnitOfWork() as uow:
        stmt = select(VaultAgentGrant).where(VaultAgentGrant.user_id == user_id)
        if org_id:
            stmt = stmt.where(VaultAgentGrant.org_id == org_id)
        if statuses:
            stmt = stmt.where(VaultAgentGrant.status.in_(tuple(statuses)))
        stmt = stmt.order_by(VaultAgentGrant.requested_at.desc()).limit(limit)
        return [_grant_to_dict(grant) for grant in uow.session.scalars(stmt).all()]


def approve_agent_grant(
    grant_id: int,
    *,
    approved_by_user_id: str,
    org_id: str | None = None,
    ttl_minutes: int = 15,
    max_reads: int | None = None,
) -> dict | None:
    """Approve a pending agent grant for this user's vault."""
    now = datetime.now(timezone.utc)
    with UnitOfWork() as uow:
        grant = uow.session.get(VaultAgentGrant, grant_id)
        if not grant or str(grant.user_id) != str(approved_by_user_id):
            return None
        if grant.status != "pending":
            return None
        if org_id and str(grant.org_id) != str(org_id):
            return None
        if org_id is None and grant.org_id is not None:
            return None
        grant.status = "approved"
        grant.approved_by_user_id = approved_by_user_id
        grant.decided_at = now
        grant.expires_at = now + timedelta(minutes=max(1, min(int(ttl_minutes or 15), 60)))
        grant.max_reads = max(1, min(int(max_reads or grant.max_reads or 1), 25))
        grant.read_count = 0
        grant.last_used_at = None
        uow.session.flush()
        return _grant_to_dict(grant)


def deny_agent_grant(
    grant_id: int,
    *,
    denied_by_user_id: str,
    org_id: str | None = None,
) -> dict | None:
    """Deny/revoke an agent grant for this user's vault."""
    now = datetime.now(timezone.utc)
    with UnitOfWork() as uow:
        grant = uow.session.get(VaultAgentGrant, grant_id)
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
        uow.session.flush()
        return _grant_to_dict(grant)


def authorize_agent_secret_read(
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
    if not user_id:
        return {"allowed": False, "status": "denied", "reason": "authenticated user context required"}
    clean_org_id = org_id or None

    with UnitOfWork() as uow:
        secret = _find_owned_secret_for_policy(uow, key_name=key_name, user_id=user_id)
        if secret:
            access_level = normalize_agent_access_level(getattr(secret, "agent_access_level", None))
            if access_level == VAULT_AGENT_ACCESS_AVAILABLE:
                return {
                    "allowed": True,
                    "status": "available",
                    "policy": {"agent_access_level": access_level},
                }
            if access_level == VAULT_AGENT_ACCESS_MANUAL:
                return {
                    "allowed": False,
                    "status": "denied",
                    "reason": "secret is marked manual and cannot be auto-read by agents",
                    "policy": {"agent_access_level": access_level},
                }
            binding = _secret_has_project_binding(
                uow,
                secret_id=secret.id,
                user_id=user_id,
                org_id=clean_org_id,
                project_slug=project_slug,
                project_slugs=project_slugs,
                target_registry_id=target_registry_id,
            )
            if binding:
                return {
                    "allowed": True,
                    "status": "project_bound",
                    "policy": {"agent_access_level": access_level},
                    "binding": _binding_to_dict(binding, secret),
                }

    if not run_id:
        return {"allowed": False, "status": "denied", "reason": "run-scoped grant required"}
    clean_reason = (reason or "").strip()
    if len(clean_reason) < 8:
        return {"allowed": False, "status": "denied", "reason": "agent must provide a specific access reason"}

    now = datetime.now(timezone.utc)
    with UnitOfWork() as uow:
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
        grant = uow.session.scalars(
            approved_stmt.order_by(VaultAgentGrant.expires_at.desc()).limit(1).with_for_update()
        ).first()
        if grant:
            grant.read_count = int(grant.read_count or 0) + 1
            grant.last_used_at = now
            if grant.read_count >= int(grant.max_reads or 1):
                grant.status = "used"
            uow.session.flush()
            return {"allowed": True, "status": "approved", "grant": _grant_to_dict(grant)}

        pending_stmt = select(VaultAgentGrant).where(
            *base,
            VaultAgentGrant.status == "pending",
        )
        if clean_org_id:
            pending_stmt = pending_stmt.where(VaultAgentGrant.org_id == clean_org_id)
        else:
            pending_stmt = pending_stmt.where(VaultAgentGrant.org_id.is_(None))
        pending = uow.session.scalars(
            pending_stmt.order_by(VaultAgentGrant.requested_at.desc()).limit(1).with_for_update()
        ).first()
        if pending:
            pending.reason = clean_reason
            pending.requested_by = requested_by
            pending.requested_at = now
            uow.session.flush()
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
        uow.session.flush()
        return {"allowed": False, "status": "pending", "grant": _grant_to_dict(pending)}


async def async_delete_secret(key_name: str, user_id: str) -> bool:
    return await run_sync_with_unit_of_work(delete_secret, key_name, user_id)


async def async_revoke_share(share_id: int, user_id: str) -> bool:
    return await run_sync_with_unit_of_work(revoke_share, share_id, user_id)


async def async_get_missing_requests(
    user_id: str,
    *,
    org_id: str | None = None,
    include_resolved: bool = False,
) -> list[dict]:
    return await run_sync_with_unit_of_work(
        get_missing_requests,
        user_id,
        org_id=org_id,
        include_resolved=include_resolved,
    )


async def async_get_vault_access_log(user_id: str, *, org_id: str | None = None, limit: int = 100) -> list[dict]:
    return await run_sync_with_unit_of_work(get_vault_access_log, user_id, org_id=org_id, limit=limit)


async def async_get_org_users(org_id: str) -> list[dict]:
    return await run_sync_with_unit_of_work(get_org_users, org_id)


async def async_list_agent_grants(
    user_id: str,
    *,
    org_id: str | None = None,
    statuses: list[str] | None = None,
) -> list[dict]:
    return await run_sync_with_unit_of_work(list_agent_grants, user_id, org_id=org_id, statuses=statuses)


async def async_approve_agent_grant(
    grant_id: int,
    *,
    approved_by_user_id: str,
    org_id: str | None = None,
    ttl_minutes: int = 15,
    max_reads: int = 1,
) -> dict | None:
    return await run_sync_with_unit_of_work(
        approve_agent_grant,
        grant_id,
        approved_by_user_id=approved_by_user_id,
        org_id=org_id,
        ttl_minutes=ttl_minutes,
        max_reads=max_reads,
    )


async def async_deny_agent_grant(
    grant_id: int,
    *,
    denied_by_user_id: str,
    org_id: str | None = None,
) -> dict | None:
    return await run_sync_with_unit_of_work(
        deny_agent_grant,
        grant_id,
        denied_by_user_id=denied_by_user_id,
        org_id=org_id,
    )


async def async_list_project_bindings(user_id: str, *, org_id: str | None = None) -> list[dict]:
    return await run_sync_with_unit_of_work(list_project_bindings, user_id=user_id, org_id=org_id)


async def async_bind_project_secret(
    secret_id: int,
    *,
    user_id: str,
    org_id: str | None,
    project_slug: str,
    env_name: str,
    target_registry_id: str | None = None,
) -> dict | None:
    return await run_sync_with_unit_of_work(
        bind_project_secret,
        secret_id,
        user_id=user_id,
        org_id=org_id,
        project_slug=project_slug,
        env_name=env_name,
        target_registry_id=target_registry_id,
    )


async def async_delete_project_binding(binding_id: int, *, user_id: str, org_id: str | None = None) -> bool:
    return await run_sync_with_unit_of_work(delete_project_binding, binding_id, user_id=user_id, org_id=org_id)


async def async_share_secret(
    secret_id: int,
    shared_with_user_id: str,
    owner_user_id: str,
    *,
    org_id: str | None = None,
) -> dict | None:
    return await run_sync_with_unit_of_work(
        share_secret,
        secret_id,
        shared_with_user_id,
        owner_user_id,
        org_id=org_id,
    )


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


def _utcnow_naive() -> datetime:
    """Return UTC wall time for timestamp columns that do not store tzinfo."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _as_db_utc(value: datetime) -> datetime:
    """Normalize DB-loaded timestamp values to UTC-naive for comparison."""
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def get_pin_status(user_id: str) -> dict:
    lockout = _get_config(_pin_lockout_key(user_id))
    locked_until = None
    if lockout:
        try:
            lockout_time = datetime.fromisoformat(lockout)
            if datetime.now(timezone.utc) < lockout_time:
                locked_until = lockout_time
        except ValueError:
            _delete_config(_pin_lockout_key(user_id))
    return {
        "has_pin": has_pin(user_id),
        "failed_attempts": int(_get_config(_pin_failures_key(user_id)) or "0"),
        "locked_until": locked_until,
    }


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


def set_pin(user_id: str, new_pin: str, current_pin: str | None = None) -> bool:
    if has_pin(user_id) and not verify_pin(user_id, current_pin or ""):
        return False
    pin_hash = bcrypt.hashpw(new_pin.encode(), bcrypt.gensalt()).decode()
    _set_config(_pin_hash_key(user_id), pin_hash)
    _set_config(_pin_failures_key(user_id), "0")
    _delete_config(_pin_lockout_key(user_id))
    return True


async def async_set_pin(user_id: str, new_pin: str, current_pin: str | None = None) -> bool:
    if await async_has_pin(user_id) and not await async_verify_pin(user_id, current_pin or ""):
        return False
    pin_hash = bcrypt.hashpw(new_pin.encode(), bcrypt.gensalt()).decode()
    await async_set_config(_pin_hash_key(user_id), pin_hash)
    await async_set_config(_pin_failures_key(user_id), "0")
    await async_delete_config(_pin_lockout_key(user_id))
    return True


def has_pin(user_id: str) -> bool:
    return _get_config(_pin_hash_key(user_id)) is not None


async def async_has_pin(user_id: str) -> bool:
    return await async_get_config(_pin_hash_key(user_id)) is not None


def verify_pin(user_id: str, pin: str) -> bool:
    lockout = _get_config(_pin_lockout_key(user_id))
    if lockout:
        lockout_time = datetime.fromisoformat(lockout)
        if datetime.now(timezone.utc) < lockout_time:
            return False
        else:
            _delete_config(_pin_lockout_key(user_id))
            _set_config(_pin_failures_key(user_id), "0")

    stored_hash = _get_config(_pin_hash_key(user_id))
    if not stored_hash:
        return True

    if bcrypt.checkpw(pin.encode(), stored_hash.encode()):
        _set_config(_pin_failures_key(user_id), "0")
        return True

    attempts = int(_get_config(_pin_failures_key(user_id)) or "0") + 1
    _set_config(_pin_failures_key(user_id), str(attempts))
    if attempts >= VAULT_LOCKOUT_AFTER_FAILURES:
        lockout_until = datetime.now(timezone.utc) + VAULT_LOCKOUT_DURATION
        _set_config(_pin_lockout_key(user_id), lockout_until.isoformat())
    return False


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


def generate_vault_token(user_id: str) -> tuple[str, datetime]:
    token = stdlib_secrets.token_urlsafe(32)
    now = _utcnow_naive()
    expires = now + VAULT_SESSION_TTL
    with UnitOfWork() as uow:
        uow.session.add(VaultSession(
            token_hash=_token_hash(token),
            user_id=user_id,
            created_at=now,
            expires_at=expires,
        ))
    return token, _as_utc(expires)


async def async_generate_vault_token(user_id: str) -> tuple[str, datetime]:
    token = stdlib_secrets.token_urlsafe(32)
    now = _utcnow_naive()
    expires = now + VAULT_SESSION_TTL
    async with UnitOfWork() as uow:
        uow.session.add(VaultSession(
            token_hash=_token_hash(token),
            user_id=user_id,
            created_at=now,
            expires_at=expires,
        ))
    return token, _as_utc(expires)


def unlock_vault(user_id: str, pin: str) -> tuple[str, datetime] | None:
    if not verify_pin(user_id, pin):
        return None
    return generate_vault_token(user_id)


async def async_unlock_vault(user_id: str, pin: str) -> tuple[str, datetime] | None:
    if not await async_verify_pin(user_id, pin):
        return None
    return await async_generate_vault_token(user_id)


def validate_vault_token(user_id: str, token: str | None) -> bool:
    if not token:
        return False
    now = _utcnow_naive()
    with UnitOfWork() as uow:
        session = uow.session.get(VaultSession, _token_hash(token))
        if not session or str(session.user_id) != str(user_id):
            return False
        if session.revoked_at is not None:
            return False
        if now > _as_db_utc(session.expires_at):
            session.revoked_at = now
            return False
        session.last_seen_at = now
        return True


async def async_validate_vault_token(user_id: str, token: str | None) -> bool:
    if not token:
        return False
    now = _utcnow_naive()
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


def revoke_vault_token(user_id: str, token: str | None) -> None:
    if not token:
        return
    with UnitOfWork() as uow:
        session = uow.session.get(VaultSession, _token_hash(token))
        if session and str(session.user_id) == str(user_id) and session.revoked_at is None:
            session.revoked_at = _utcnow_naive()


async def async_revoke_vault_token(user_id: str, token: str | None) -> None:
    if not token:
        return
    async with UnitOfWork() as uow:
        session = await uow.session.get(VaultSession, _token_hash(token))
        if session and str(session.user_id) == str(user_id) and session.revoked_at is None:
            session.revoked_at = _utcnow_naive()


def _get_config(key: str) -> str | None:
    with UnitOfWork() as uow:
        config = uow.session.scalars(
            select(VaultConfig).where(VaultConfig.key == key)
        ).first()
        return config.value if config else None


async def async_get_config(key: str) -> str | None:
    async with UnitOfWork() as uow:
        result = await uow.session.scalars(
            select(VaultConfig).where(VaultConfig.key == key)
        )
        config = result.first()
        return config.value if config else None


def _set_config(key: str, value: str) -> None:
    now = datetime.now(timezone.utc)
    with UnitOfWork() as uow:
        config = uow.session.scalars(
            select(VaultConfig).where(VaultConfig.key == key)
        ).first()
        if config:
            config.value = value
            config.updated_at = now
        else:
            uow.session.add(VaultConfig(key=key, value=value, updated_at=now))


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


def _delete_config(key: str) -> None:
    with UnitOfWork() as uow:
        config = uow.session.scalars(
            select(VaultConfig).where(VaultConfig.key == key)
        ).first()
        if config:
            uow.session.delete(config)


async def async_delete_config(key: str) -> None:
    async with UnitOfWork() as uow:
        result = await uow.session.scalars(
            select(VaultConfig).where(VaultConfig.key == key)
        )
        config = result.first()
        if config:
            await uow.session.delete(config)
