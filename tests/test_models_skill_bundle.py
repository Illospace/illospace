"""Skill bundle registry model contracts."""

from brain.platform.db.models.skill_bundle import (
    SkillBundle,
    SkillBundleVersion,
    SkillInstallation,
)


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
