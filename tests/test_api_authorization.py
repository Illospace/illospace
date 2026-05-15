import pytest
from fastapi import HTTPException

from brain.app.api.authorization import (
    PERMISSION_RUN_MANAGE,
    PERMISSION_MEMORY_MANAGE,
    PERMISSION_SCHEDULER_MANAGE,
    PERMISSION_SKILLS_MANAGE,
    PERMISSION_SYSTEM_MANAGE,
    PERMISSION_VAULT_AUDIT,
    PERMISSION_VAULT_SHARE,
    can_manage_run,
    can_manage_memory,
    can_manage_scheduler,
    can_manage_skills,
    can_manage_system,
    can_audit_vault,
    can_share_vault,
    has_permission,
    require_org_context,
    require_permission,
    require_role,
    service_principal_context,
)
from brain.app.api.config import validate_auth_config


def test_production_rejects_enabled_dev_fallback():
    with pytest.raises(RuntimeError, match="AUTH_DEV_FALLBACK_ENABLED"):
        validate_auth_config(env="production", auth_dev_fallback_enabled=True)


def test_production_allows_disabled_dev_fallback():
    validate_auth_config(env="production", auth_dev_fallback_enabled=False)


def test_service_principal_context_has_audit_identity_and_permissions():
    user = service_principal_context("scheduler", token_source="illo_api_token")

    assert user["id"] == "service:scheduler"
    assert user["principal_type"] == "service"
    assert user["role"] == "service"
    assert user["internal"] is True
    assert user["audit"]["principal_id"] == "service:scheduler"
    assert user["audit"]["token_source"] == "illo_api_token"
    assert can_manage_run(user) is True
    assert can_manage_scheduler(user) is True
    assert can_manage_memory(user) is True
    assert can_manage_skills(user) is True
    assert can_manage_system(user) is True
    assert can_share_vault(user) is True
    assert can_audit_vault(user) is True


def test_owner_and_admin_roles_keep_management_permissions():
    owner = {"role": "owner"}
    admin = {"role": "admin"}
    member = {"role": "member"}

    assert has_permission(owner, PERMISSION_RUN_MANAGE) is True
    assert has_permission(admin, PERMISSION_SCHEDULER_MANAGE) is True
    assert has_permission(owner, PERMISSION_SKILLS_MANAGE) is True
    assert has_permission(admin, PERMISSION_SYSTEM_MANAGE) is True
    assert has_permission(owner, PERMISSION_VAULT_SHARE) is True
    assert has_permission(admin, PERMISSION_VAULT_AUDIT) is True
    assert has_permission(member, PERMISSION_MEMORY_MANAGE) is False


def test_org_members_can_manage_team_skills():
    member = {"role": "member", "org_id": "org-1", "permissions": []}
    outsider = {"role": "member", "permissions": []}

    assert can_manage_skills(member) is True
    assert can_manage_skills(outsider) is False


def test_require_org_context_returns_org_id_or_raises():
    assert require_org_context({"org_id": "org-1"}) == "org-1"

    with pytest.raises(HTTPException) as exc_info:
        require_org_context({"role": "service", "principal_type": "service"})

    assert exc_info.value.status_code == 403


def test_dependency_helpers_accept_authorized_user_and_reject_otherwise():
    service_user = service_principal_context("worker")
    member = {"role": "member", "permissions": []}

    assert require_permission(PERMISSION_RUN_MANAGE)(service_user) == service_user
    assert require_role("owner", "service")(service_user) == service_user

    with pytest.raises(HTTPException) as permission_exc:
        require_permission(PERMISSION_RUN_MANAGE)(member)
    with pytest.raises(HTTPException) as role_exc:
        require_role("owner")(member)

    assert permission_exc.value.status_code == 403
    assert role_exc.value.status_code == 403
