"""Skill bundle registry model coverage."""

from sqlalchemy import inspect

from brain.platform.db.models.skill_bundle import (
    SkillAsset,
    SkillBundle,
    SkillBundleVersion,
    SkillInstallation,
    SkillOverlay,
)


def test_skill_bundle_columns():
    cols = {c.name for c in inspect(SkillBundle).columns}
    assert cols >= {
        "id",
        "namespace",
        "name",
        "display_name",
        "description",
        "owner_org_id",
        "owner_user_id",
        "visibility",
        "source_kind",
        "trust_level",
        "latest_approved_version_id",
        "archived",
        "created_at",
        "updated_at",
    }


def test_skill_bundle_version_columns():
    cols = {c.name for c in inspect(SkillBundleVersion).columns}
    assert cols >= {
        "id",
        "bundle_id",
        "semver",
        "content_digest",
        "manifest",
        "asset_root",
        "routing_card",
        "permissions",
        "compatibility",
        "eval_summary",
        "signature",
        "provenance",
        "created_by_user_id",
        "status",
        "published_at",
        "created_at",
    }


def test_skill_asset_columns():
    cols = {c.name for c in inspect(SkillAsset).columns}
    assert cols >= {
        "id",
        "bundle_version_id",
        "path",
        "asset_kind",
        "mime_type",
        "size_bytes",
        "content_digest",
        "storage_kind",
        "storage_uri",
        "content_text",
        "loading_budget_tokens",
        "metadata",
        "created_at",
    }


def test_skill_installation_columns():
    cols = {c.name for c in inspect(SkillInstallation).columns}
    assert cols >= {
        "id",
        "bundle_id",
        "bundle_version_id",
        "skill_id",
        "org_id",
        "user_id",
        "installed_by_user_id",
        "enabled",
        "enabled_scope",
        "pinned",
        "update_policy",
        "installed_digest",
        "review_status",
        "permission_grants",
        "disabled_sections",
        "loading_budgets",
        "rollback_bundle_version_id",
        "metadata",
        "archived",
        "created_at",
        "updated_at",
    }


def test_skill_overlay_columns():
    cols = {c.name for c in inspect(SkillOverlay).columns}
    assert cols >= {
        "id",
        "installation_id",
        "base_bundle_version_id",
        "overlay_revision",
        "status",
        "patch",
        "overlay_digest",
        "effective_digest",
        "author_user_id",
        "reason",
        "promoted_bundle_version_id",
        "created_at",
        "updated_at",
    }


def test_skill_bundle_uniqueness_contracts():
    bundle_constraints = {
        c.name for c in SkillBundle.__table__.constraints if c.name
    }
    version_constraints = {
        c.name for c in SkillBundleVersion.__table__.constraints if c.name
    }
    install_indexes = {i.name for i in SkillInstallation.__table__.indexes}

    assert "uq_skill_bundles_namespace_name" in bundle_constraints
    assert "uq_skill_bundle_versions_bundle_semver" in version_constraints
    assert "uq_skill_bundle_versions_bundle_digest" in version_constraints
    assert "uq_skill_installations_active_scope" in install_indexes
