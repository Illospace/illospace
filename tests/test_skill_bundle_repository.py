"""SkillBundleRepository tests using in-memory SQLite."""
from __future__ import annotations

import re

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.sqlite.base import SQLiteDDLCompiler, SQLiteTypeCompiler
from sqlalchemy.orm import Session

from brain.platform.db.models.skill import Skill  # noqa: F401 - registers skills for FK lookup
from brain.platform.db.models.skill_bundle import (
    SkillAsset,
    SkillBundle,
    SkillBundleVersion,
    SkillInstallation,
    SkillOverlay,
)
from brain.platform.db.repositories.skill_bundles import (
    SkillBundleRepository,
    SkillBundleVersionConflict,
    SkillOverlayConflict,
)


def _patch_sqlite_for_pg_types():
    """Teach SQLite to render PostgreSQL-only types used by these models."""
    if not hasattr(SQLiteTypeCompiler, "visit_JSONB"):
        SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "TEXT"
    if not hasattr(SQLiteTypeCompiler, "visit_ARRAY"):
        SQLiteTypeCompiler.visit_ARRAY = lambda self, type_, **kw: "TEXT"

    original = SQLiteDDLCompiler.get_column_default_string

    def patched(self, column, **kw):
        result = original(self, column, **kw)
        if result:
            result = re.sub(r"::jsonb", "", result)
            result = re.sub(r"::text\[\]", "", result)
        return result

    SQLiteDDLCompiler.get_column_default_string = patched


@pytest.fixture
def session():
    _patch_sqlite_for_pg_types()
    engine = create_engine("sqlite://", echo=False)
    SkillBundle.__table__.create(engine, checkfirst=True)
    SkillBundleVersion.__table__.create(engine, checkfirst=True)
    SkillAsset.__table__.create(engine, checkfirst=True)
    SkillInstallation.__table__.create(engine, checkfirst=True)
    SkillOverlay.__table__.create(engine, checkfirst=True)
    session = Session(engine)
    yield session
    session.close()


@pytest.fixture
def repo(session):
    return SkillBundleRepository(session)


def _create_bundle_version(
    repo: SkillBundleRepository,
    *,
    namespace: str = "local",
    name: str = "test-skill",
    semver: str = "1.0.0",
    digest: str = "sha256:aaa",
) -> tuple[SkillBundle, SkillBundleVersion]:
    bundle = repo.get_or_create_bundle(namespace, name, display_name="Test Skill")
    version = repo.create_version(
        bundle,
        semver=semver,
        content_digest=digest,
        manifest={"name": name},
        permissions={"tools": []},
        status="approved",
    )
    return bundle, version


def test_get_or_create_bundle_and_idempotent_version_create(repo):
    bundle, version = _create_bundle_version(repo)

    same_bundle = repo.get_or_create_bundle("local", "test-skill")
    same_version = repo.create_version(
        same_bundle.id,
        semver="1.0.0",
        content_digest="sha256:aaa",
        manifest={"name": "ignored"},
        status="draft",
    )

    assert same_bundle.id == bundle.id
    assert same_version.id == version.id
    assert same_version.manifest == {"name": "test-skill"}
    assert same_version.status == "approved"


def test_create_version_rejects_conflicting_semver_digest(repo):
    bundle, _version = _create_bundle_version(repo)

    with pytest.raises(SkillBundleVersionConflict, match="different digest"):
        repo.create_version(
            bundle,
            semver="1.0.0",
            content_digest="sha256:bbb",
        )


def test_add_and_list_assets(repo):
    _bundle, version = _create_bundle_version(repo)

    repo.add_asset(
        version,
        path="SKILL.md",
        content_digest="sha256:skill",
        content_text="# Test Skill",
    )
    repo.add_asset(
        version.id,
        path="examples/basic.md",
        content_digest="sha256:example",
        asset_kind="example",
    )

    assets = repo.list_assets(version)

    assert [asset.path for asset in assets] == ["SKILL.md", "examples/basic.md"]
    assert assets[0].bundle_version_id == version.id


def test_create_installation_pins_exact_version_and_digest(repo):
    bundle, version = _create_bundle_version(repo)

    installation = repo.create_installation(
        version,
        org_id="org-1",
        user_id="user-1",
        enabled_scope="user",
        update_policy="manual",
        permission_grants=[{"kind": "tool", "name": "calendar"}],
        skill_id=42,
        installed_by_user_id="user-1",
        metadata={"source": "test"},
    )

    assert installation.bundle_id == bundle.id
    assert installation.bundle_version_id == version.id
    assert installation.installed_digest == version.content_digest
    assert installation.pinned is True
    assert installation.skill_id == 42
    assert installation.update_policy == "manual"
    assert installation.permission_grants == [{"kind": "tool", "name": "calendar"}]

    with pytest.raises(ValueError, match="installed_digest"):
        repo.create_installation(
            version,
            org_id="org-2",
            user_id="user-2",
            installed_digest="sha256:not-the-version",
        )


def test_overlay_revision_uniqueness_and_active_lookup(repo):
    _bundle, version = _create_bundle_version(repo)
    installation = repo.create_installation(
        version,
        org_id="org-1",
        user_id="user-1",
    )

    overlay = repo.add_overlay_revision(
        installation,
        overlay_revision=1,
        status="active",
        patch={"procedure": "Use the local override."},
        overlay_digest="sha256:overlay",
        effective_digest="sha256:effective",
    )

    active = repo.get_active_overlay(installation.id)
    assert active is not None
    assert active.id == overlay.id
    assert active.effective_digest == "sha256:effective"

    projection = repo.get_runtime_projection_metadata(
        installation_id=installation.id,
    )
    assert projection is not None
    assert projection["namespace"] == "local"
    assert projection["semver"] == "1.0.0"
    assert projection["effective_digest"] == "sha256:effective"
    assert projection["overlay_revision"] == 1

    with pytest.raises(SkillOverlayConflict, match="already exists"):
        repo.add_overlay_revision(
            installation.id,
            overlay_revision=1,
            patch={"procedure": "Duplicate revision."},
        )
