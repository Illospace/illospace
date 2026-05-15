from dataclasses import dataclass

import pytest

from brain.platform.db.repositories.memory_visibility import (
    MemoryVisibilityContext,
    VALID_MEMORY_VISIBILITIES,
    memory_is_visible,
    memory_visibility_sql,
    normalize_memory_visibility,
    require_memory_visible,
)


@dataclass(frozen=True)
class MemoryRow:
    user_id: str
    org_id: str | None
    visibility: str | None = None


def test_memory_visibility_matrix_matches_tenant_boundaries():
    owner = MemoryVisibilityContext(user_id="user-a", org_id="org-a")
    same_org_peer = MemoryVisibilityContext(user_id="user-b", org_id="org-a")
    other_org = MemoryVisibilityContext(user_id="user-c", org_id="org-b")
    anonymous = MemoryVisibilityContext()

    private_memory = MemoryRow(user_id="user-a", org_id="org-a", visibility="private")
    team_memory = MemoryRow(user_id="user-a", org_id="org-a", visibility="team")
    org_memory = MemoryRow(user_id="user-a", org_id="org-a", visibility="org")

    assert memory_is_visible(private_memory, owner)
    assert not memory_is_visible(private_memory, same_org_peer)
    assert not memory_is_visible(private_memory, anonymous)

    for shared in (team_memory, org_memory):
        assert memory_is_visible(shared, owner)
        assert memory_is_visible(shared, same_org_peer)
        assert not memory_is_visible(shared, other_org)
        assert not memory_is_visible(shared, anonymous)


def test_service_memory_visibility_is_global_but_human_scope_is_not():
    service = MemoryVisibilityContext.system()
    unknown_visibility = MemoryRow(user_id="someone", org_id="elsewhere", visibility="custom")

    assert memory_is_visible(unknown_visibility, service)
    assert not memory_is_visible(unknown_visibility, MemoryVisibilityContext(user_id="someone"))


def test_memory_visibility_sql_uses_the_same_scope_inputs_as_in_memory_policy():
    sql, params = memory_visibility_sql(
        MemoryVisibilityContext(user_id="user-a", org_id="org-a"),
        alias="m",
        user_param="uid",
        org_param="oid",
    )

    assert "m.user_id = :uid" in sql
    assert "m.org_id = :oid" in sql
    assert "COALESCE(m.visibility, 'private') = 'private'" in sql
    assert "COALESCE(m.visibility, 'private') IN ('team', 'org')" in sql
    assert params == {"uid": "user-a", "oid": "org-a"}

    service_sql, service_params = memory_visibility_sql(MemoryVisibilityContext.system())
    assert service_sql == ""
    assert service_params == {}


def test_memory_visibility_normalization_falls_back_closed():
    for visibility in VALID_MEMORY_VISIBILITIES:
        assert normalize_memory_visibility(visibility.upper()) == visibility

    assert normalize_memory_visibility("workspace") == "private"
    assert normalize_memory_visibility(None, fallback="org") == "org"


def test_require_memory_visible_raises_for_suppressed_rows():
    memory = MemoryRow(user_id="user-a", org_id="org-a", visibility="private")

    assert require_memory_visible(memory, MemoryVisibilityContext(user_id="user-a")) is memory
    with pytest.raises(LookupError):
        require_memory_visible(memory, MemoryVisibilityContext(user_id="user-b", org_id="org-a"))
