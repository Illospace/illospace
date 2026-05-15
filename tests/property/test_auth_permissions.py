from brain.app.api.authorization import (
    ADMIN_PERMISSIONS,
    INTERNAL_SERVICE_PERMISSIONS,
    OWNER_PERMISSIONS,
    PERMISSION_INTERNAL_API,
    has_permission,
    has_role,
    human_identity,
    service_principal_context,
)


def test_human_permission_matrix_stays_role_based_and_explicit():
    owner = human_identity(
        {
            "id": "owner-1",
            "name": "Owner",
            "email": "owner@example.test",
            "role": "owner",
            "org_id": "org-1",
            "org_name": "Org",
        }
    ).to_user_context()
    admin = {**owner, "id": "admin-1", "role": "admin", "permissions": []}
    member = {**owner, "id": "member-1", "role": "member", "permissions": []}
    delegated_member = {**member, "permissions": ["run:cancel"]}

    for permission in OWNER_PERMISSIONS:
        assert has_permission(owner, permission)
    for permission in ADMIN_PERMISSIONS:
        assert has_permission(admin, permission)
    for permission in OWNER_PERMISSIONS:
        assert not has_permission(member, permission)

    assert has_permission(delegated_member, "run:cancel")
    assert not has_permission(delegated_member, "run:approve")
    assert not has_permission(owner, PERMISSION_INTERNAL_API)


def test_service_principals_are_the_only_internal_api_callers():
    service = service_principal_context("worker", token_source="signed-token")

    assert service["principal_type"] == "service"
    assert service["internal"] is True
    assert service["audit"]["token_source"] == "signed-token"
    assert has_permission(service, PERMISSION_INTERNAL_API)
    for permission in INTERNAL_SERVICE_PERMISSIONS:
        assert has_permission(service, permission)

    human_owner = human_identity(
        {
            "id": "owner-1",
            "name": "Owner",
            "email": "owner@example.test",
            "role": "owner",
            "org_id": "org-1",
            "org_name": "Org",
        }
    ).to_user_context()
    assert not has_permission(human_owner, PERMISSION_INTERNAL_API)


def test_role_checks_do_not_confuse_permissions_with_membership():
    principal = service_principal_context("worker")
    owner = {"id": "owner-1", "role": "owner", "permissions": []}
    delegated_member = {"id": "member-1", "role": "member", "permissions": ["system:manage"]}

    assert has_role(owner, "owner")
    assert not has_role(delegated_member, "owner", "admin")
    assert not has_role(principal, "owner", "admin")
