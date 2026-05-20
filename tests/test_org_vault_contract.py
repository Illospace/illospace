"""Contract tests for the org-owned vault model."""

from __future__ import annotations

from brain.platform.db.base import Base
from brain.platform.db.models.vault import (
    Secret,
    VaultAccessLog,
    VaultAgentGrant,
    VaultMissingRequest,
    VaultProjectBinding,
    VaultSession,
)


def _unique_columns(table) -> set[tuple[str, ...]]:
    return {
        tuple(constraint.columns.keys())
        for constraint in table.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }


def test_vault_secret_is_org_owned_not_user_owned():
    columns = set(Secret.__table__.c.keys())

    assert "org_id" in columns
    assert "user_id" not in columns
    assert {"created_by_user_id", "updated_by_user_id"}.issubset(columns)
    assert ("org_id", "key_name") in _unique_columns(Secret.__table__)
    assert not any("user_id" in columns for columns in _unique_columns(Secret.__table__))


def test_project_bindings_are_org_owned_not_user_owned():
    columns = set(VaultProjectBinding.__table__.c.keys())

    assert "org_id" in columns
    assert "user_id" not in columns
    assert "created_by_user_id" in columns
    assert ("org_id", "project_slug", "env_name") in _unique_columns(VaultProjectBinding.__table__)
    assert not any("user_id" in columns for columns in _unique_columns(VaultProjectBinding.__table__))


def test_vault_actor_fields_are_not_owner_fields():
    assert "user_id" not in set(VaultAccessLog.__table__.c.keys())
    assert {"org_id", "actor_user_id"}.issubset(VaultAccessLog.__table__.c.keys())

    assert "user_id" not in set(VaultAgentGrant.__table__.c.keys())
    assert {"org_id", "requested_by_user_id"}.issubset(VaultAgentGrant.__table__.c.keys())

    assert "user_id" not in set(VaultMissingRequest.__table__.c.keys())
    assert {"org_id", "actor_user_id"}.issubset(VaultMissingRequest.__table__.c.keys())

    assert "user_id" not in set(VaultSession.__table__.c.keys())
    assert {"org_id", "actor_user_id"}.issubset(VaultSession.__table__.c.keys())


def test_user_to_user_vault_sharing_is_removed_from_public_model():
    assert "vault_shares" not in Base.metadata.tables


def test_provider_keys_are_org_owned_with_only_codex_user_exception():
    tables = Base.metadata.tables

    assert "user_api_keys" not in tables
    assert "api_key_shares" not in tables
    assert "org_api_keys" in tables
    assert "user_codex_connections" in tables
    assert "default_api_key_id" not in tables["users"].c
    assert "default_provider" not in tables["users"].c
