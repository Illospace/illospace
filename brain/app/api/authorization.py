"""Authorization primitives and principal identity helpers for API routes."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from fastapi import Depends, HTTPException

PERMISSION_INTERNAL_API = "internal:api"
PERMISSION_RUN_MANAGE = "run:manage"
PERMISSION_RUN_APPROVE = "run:approve"
PERMISSION_RUN_CANCEL = "run:cancel"
PERMISSION_SCHEDULER_MANAGE = "scheduler:manage"
PERMISSION_MEMORY_MANAGE = "memory:manage"
PERMISSION_SKILLS_MANAGE = "skills:manage"
PERMISSION_SYSTEM_MANAGE = "system:manage"
PERMISSION_VAULT_SHARE = "vault:share"
PERMISSION_VAULT_AUDIT = "vault:audit"
PERMISSION_DOMAIN_READ = "domain:read"
PERMISSION_DOMAIN_WRITE = "domain:write"
PERMISSION_DOMAIN_MANAGE = "domain:manage"
PERMISSION_DOMAIN_AUDIT = "domain:audit"

OWNER_PERMISSIONS = frozenset(
    {
        PERMISSION_RUN_MANAGE,
        PERMISSION_RUN_APPROVE,
        PERMISSION_RUN_CANCEL,
        PERMISSION_SCHEDULER_MANAGE,
        PERMISSION_MEMORY_MANAGE,
        PERMISSION_SKILLS_MANAGE,
        PERMISSION_SYSTEM_MANAGE,
        PERMISSION_VAULT_SHARE,
        PERMISSION_VAULT_AUDIT,
        PERMISSION_DOMAIN_READ,
        PERMISSION_DOMAIN_WRITE,
        PERMISSION_DOMAIN_MANAGE,
        PERMISSION_DOMAIN_AUDIT,
    }
)
ADMIN_PERMISSIONS = OWNER_PERMISSIONS
INTERNAL_SERVICE_PERMISSIONS = frozenset(
    {
        PERMISSION_INTERNAL_API,
        PERMISSION_RUN_MANAGE,
        PERMISSION_RUN_APPROVE,
        PERMISSION_RUN_CANCEL,
        PERMISSION_SCHEDULER_MANAGE,
        PERMISSION_MEMORY_MANAGE,
        PERMISSION_SKILLS_MANAGE,
        PERMISSION_SYSTEM_MANAGE,
        PERMISSION_VAULT_SHARE,
        PERMISSION_VAULT_AUDIT,
        PERMISSION_DOMAIN_READ,
        PERMISSION_DOMAIN_WRITE,
        PERMISSION_DOMAIN_MANAGE,
        PERMISSION_DOMAIN_AUDIT,
    }
)


@dataclass(frozen=True)
class PrincipalIdentity:
    """Typed identity metadata that can still be exposed as legacy user dicts."""

    id: str
    principal_type: str
    role: str
    name: str
    email: str = ""
    org_id: str | None = None
    org_name: str = ""
    permissions: frozenset[str] = field(default_factory=frozenset)
    internal: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_user_context(self) -> dict[str, Any]:
        """Return the dict shape existing routes expect, with audit metadata."""
        return {
            "id": self.id,
            "name": self.name,
            "email": self.email,
            "role": self.role,
            "color": "#6366f1",
            "org_id": self.org_id,
            "org_name": self.org_name,
            "attribution_enabled": True,
            "default_provider": None,
            "internal": self.internal,
            "principal_type": self.principal_type,
            "permissions": sorted(self.permissions),
            "audit": {
                "principal_id": self.id,
                "principal_type": self.principal_type,
                "role": self.role,
                **dict(self.metadata),
            },
        }


def human_identity(ctx: Mapping[str, Any]) -> PrincipalIdentity:
    """Build typed metadata from a safe user context."""
    role = str(ctx.get("role") or "member")
    if role == "owner":
        permissions = OWNER_PERMISSIONS
    elif role == "admin":
        permissions = ADMIN_PERMISSIONS
    else:
        permissions = frozenset()
    return PrincipalIdentity(
        id=str(ctx["id"]),
        principal_type="human",
        role=role,
        name=str(ctx.get("name") or ""),
        email=str(ctx.get("email") or ""),
        org_id=str(ctx["org_id"]) if ctx.get("org_id") else None,
        org_name=str(ctx.get("org_name") or ""),
        permissions=permissions,
        internal=False,
        metadata={"auth_source": "session"},
    )


def service_principal_identity(
    name: str,
    *,
    token_source: str | None = None,
    permissions: frozenset[str] = INTERNAL_SERVICE_PERMISSIONS,
) -> PrincipalIdentity:
    """Create an explicit internal service principal for worker/tool calls."""
    principal_id = f"service:{name}"
    metadata: dict[str, Any] = {"auth_source": "service_principal"}
    if token_source:
        metadata["token_source"] = token_source
    return PrincipalIdentity(
        id=principal_id,
        principal_type="service",
        role="service",
        name=name,
        email=f"{name}@service.illo.local",
        permissions=permissions,
        internal=True,
        metadata=metadata,
    )


def service_principal_context(
    name: str,
    *,
    token_source: str | None = None,
    permissions: frozenset[str] = INTERNAL_SERVICE_PERMISSIONS,
) -> dict[str, Any]:
    return service_principal_identity(
        name,
        token_source=token_source,
        permissions=permissions,
    ).to_user_context()


def has_permission(user: Mapping[str, Any] | None, permission: str) -> bool:
    if not user:
        return False
    permissions = set(user.get("permissions") or [])
    if permission in permissions:
        return True
    role = user.get("role")
    if role == "owner":
        return permission in OWNER_PERMISSIONS
    if role == "admin":
        return permission in ADMIN_PERMISSIONS
    return False


def has_role(user: Mapping[str, Any] | None, *roles: str) -> bool:
    return bool(user and user.get("role") in set(roles))


def require_permission(permission: str) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """FastAPI dependency factory requiring an explicit permission."""
    from brain.app.api.auth import get_current_user

    def _dependency(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
        if not has_permission(user, permission):
            raise HTTPException(status_code=403, detail="Permission denied")
        return user

    return _dependency


def require_role(*roles: str) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """FastAPI dependency factory requiring any of the supplied roles."""
    from brain.app.api.auth import get_current_user

    allowed = set(roles)

    def _dependency(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
        if user.get("role") not in allowed:
            raise HTTPException(status_code=403, detail="Permission denied")
        return user

    return _dependency


def require_org_context(user: Mapping[str, Any]) -> str:
    """Return the caller org id or reject routes that require org scope."""
    org_id = user.get("org_id") if user else None
    if not org_id:
        raise HTTPException(status_code=403, detail="Organization context required")
    return str(org_id)


def can_manage_run(user: Mapping[str, Any] | None) -> bool:
    return has_permission(user, PERMISSION_RUN_MANAGE)


def _run_owner_matches(
    user: Mapping[str, Any] | None,
    run: Any | None,
) -> bool:
    if not user or run is None or not user.get("id"):
        return False
    run_user_id = None
    if isinstance(run, Mapping):
        run_user_id = run.get("user_id")
    else:
        run_user_id = getattr(run, "user_id", None)
    return run_user_id is not None and str(run_user_id) == str(user["id"])


def can_approve_run(
    user: Mapping[str, Any] | None,
    run: Any | None = None,
) -> bool:
    return has_permission(user, PERMISSION_RUN_APPROVE) or _run_owner_matches(
        user,
        run,
    )


def can_cancel_run(
    user: Mapping[str, Any] | None,
    run: Any | None = None,
) -> bool:
    return has_permission(user, PERMISSION_RUN_CANCEL) or _run_owner_matches(
        user,
        run,
    )


def can_manage_scheduler(user: Mapping[str, Any] | None) -> bool:
    return has_permission(user, PERMISSION_SCHEDULER_MANAGE)


def can_read_domains(user: Mapping[str, Any] | None) -> bool:
    return bool(user and user.get("org_id")) or has_permission(user, PERMISSION_DOMAIN_READ)


def can_write_domains(user: Mapping[str, Any] | None) -> bool:
    return bool(user and user.get("org_id")) or has_permission(user, PERMISSION_DOMAIN_WRITE)


def can_manage_domains(user: Mapping[str, Any] | None) -> bool:
    return bool(user and user.get("org_id")) or has_permission(user, PERMISSION_DOMAIN_MANAGE)


def can_audit_domains(user: Mapping[str, Any] | None) -> bool:
    return bool(user and user.get("org_id")) or has_permission(user, PERMISSION_DOMAIN_AUDIT)


def can_manage_memory(user: Mapping[str, Any] | None) -> bool:
    return has_permission(user, PERMISSION_MEMORY_MANAGE)


def can_manage_skills(user: Mapping[str, Any] | None) -> bool:
    return has_permission(user, PERMISSION_SKILLS_MANAGE)


def can_manage_system(user: Mapping[str, Any] | None) -> bool:
    return has_permission(user, PERMISSION_SYSTEM_MANAGE)


def can_share_vault(user: Mapping[str, Any] | None) -> bool:
    return has_permission(user, PERMISSION_VAULT_SHARE)


def can_audit_vault(user: Mapping[str, Any] | None) -> bool:
    return has_permission(user, PERMISSION_VAULT_AUDIT)
