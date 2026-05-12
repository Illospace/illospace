"""SkillBundleIOService coverage."""
from __future__ import annotations

import re

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.sqlite.base import SQLiteDDLCompiler, SQLiteTypeCompiler
from sqlalchemy.orm import Session

from brain.platform.db.models.skill import Skill
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
)
from brain.platform.db.repositories.skills import SkillRepository
from brain.platform.db.services.skill_bundle_io import SkillBundleIOService
from brain.systems.skills.bundles import SkillBundleError, load_skill_bundle

ORG_ID = "11111111-1111-4111-8111-111111111111"
USER_ID = "33333333-3333-4333-8333-333333333333"


def _patch_sqlite_for_pg_types():
    if not hasattr(SQLiteTypeCompiler, "visit_JSONB"):
        SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "TEXT"
    if not hasattr(SQLiteTypeCompiler, "visit_ARRAY"):
        SQLiteTypeCompiler.visit_ARRAY = lambda self, type_, **kw: "TEXT"
    if not hasattr(SQLiteTypeCompiler, "visit_VECTOR"):
        SQLiteTypeCompiler.visit_VECTOR = lambda self, type_, **kw: "TEXT"
    SQLiteTypeCompiler.visit_UUID = lambda self, type_, **kw: "TEXT"
    SQLiteTypeCompiler.visit_uuid = lambda self, type_, **kw: "TEXT"

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
    Skill.__table__.create(engine, checkfirst=True)
    SkillAsset.__table__.create(engine, checkfirst=True)
    SkillInstallation.__table__.create(engine, checkfirst=True)
    SkillOverlay.__table__.create(engine, checkfirst=True)
    session = Session(engine)
    yield session
    session.close()


@pytest.fixture
def service(session):
    return SkillBundleIOService(
        SkillRepository(session),
        SkillBundleRepository(session),
    )


def _write_bundle(
    root,
    *,
    version: str = "1.0.0",
    procedure: str = "# Develop\n",
    source: str = "illo-core",
    visibility: str = "public",
    runtime: str = "",
):
    (root / "skill.toml").write_text(
        f"""
schema_version = 1
name = "develop"
display_name = "Develop"
version = "{version}"
description = "Implement scoped code changes."
license = "Apache-2.0"
source = "{source}"
visibility = "{visibility}"

[routing]
triggers = ["fix bug"]

{runtime}
[permissions]
toolsets = ["workspace_read"]

[loading]
summary = "SKILL.md#summary"
procedure = "SKILL.md#procedure"
examples = "examples/"
""",
        encoding="utf-8",
    )
    (root / "SKILL.md").write_text(procedure, encoding="utf-8")
    (root / "examples").mkdir(exist_ok=True)
    (root / "examples" / "happy.md").write_text("Do the small thing.\n", encoding="utf-8")


def test_import_bundle_materializes_skill_installation_and_assets(service, session, tmp_path):
    _write_bundle(tmp_path)

    result = service.import_bundle(
        tmp_path,
        namespace="illo_core",
        org_id=ORG_ID,
        user_id=USER_ID,
        installed_by_user_id=USER_ID,
        trust_level="illo_core",
    )

    skill = session.get(Skill, result["skill"]["id"])
    installation = session.get(SkillInstallation, result["installation"]["id"])
    version = session.get(SkillBundleVersion, result["version"]["id"])
    assets = session.query(SkillAsset).order_by(SkillAsset.path).all()

    assert skill is not None
    assert skill.name == "develop"
    assert skill.procedure == "# Develop\n"
    assert skill.bundle_version_id == version.id
    assert skill.bundle_digest == version.content_digest
    assert skill.effective_digest == version.content_digest
    assert skill.source_kind == "illo-core"
    assert skill.trust_level == "illo_core"
    assert skill.skill_installation_id == installation.id

    assert installation.skill_id == skill.id
    assert installation.installed_digest == version.content_digest
    assert installation.update_policy == "manual"
    assert [asset.path for asset in assets] == ["SKILL.md", "examples/happy.md"]
    assert result["assets"] == 2


def test_import_bundle_applies_provider_neutral_runtime_tiers(service, session, tmp_path):
    _write_bundle(
        tmp_path,
        runtime="""
[runtime]
default_model_tier = "high"
default_thinking_tier = "xhigh"
""",
    )

    result = service.import_bundle(tmp_path, namespace="illo_core", org_id=ORG_ID)

    skill = session.get(Skill, result["skill"]["id"])
    assert skill.model_tier == "high"
    assert skill.thinking_tier == "xhigh"


def test_import_bundle_keeps_hosted_source_distinct_from_agent_draft(
    service,
    session,
    tmp_path,
):
    _write_bundle(tmp_path, source="self_hosted", visibility="private_local")

    result = service.import_bundle(tmp_path, namespace="self_hosted", org_id=ORG_ID)

    skill = session.get(Skill, result["skill"]["id"])
    bundle = session.get(SkillBundle, result["bundle"]["id"])

    assert skill.source_kind == "self_hosted"
    assert skill.trust_level == "private_local"
    assert bundle.source_kind == "self_hosted"
    assert bundle.source_kind != "agent_draft"


def test_import_bundle_is_idempotent_for_same_version(service, session, tmp_path):
    _write_bundle(tmp_path)

    first = service.import_bundle(tmp_path, namespace="illo_core", org_id=ORG_ID)
    second = service.import_bundle(tmp_path, namespace="illo_core", org_id=ORG_ID)

    assert second["version"]["existing"] is True
    assert second["version"]["id"] == first["version"]["id"]
    assert second["installation"]["id"] == first["installation"]["id"]
    assert session.query(SkillAsset).count() == 2


def test_import_bundle_rejects_bad_manifest_before_persistence(
    service,
    session,
    tmp_path,
):
    (tmp_path / "skill.toml").write_text(
        """
schema_version = 1
name = "develop"
version = "1.0.0"
description = "Implement scoped code changes."
source = "self_hosted"
visibility = "private_local"

[runtime]
default_model_tier = "turbo"
""",
        encoding="utf-8",
    )
    (tmp_path / "SKILL.md").write_text("# Develop\n", encoding="utf-8")

    with pytest.raises(SkillBundleError, match="default_model_tier"):
        service.import_bundle(tmp_path, namespace="self_hosted", org_id=ORG_ID)

    assert session.query(SkillBundle).count() == 0
    assert session.query(SkillBundleVersion).count() == 0
    assert session.query(SkillInstallation).count() == 0


def test_import_bundle_updates_existing_install_with_rollback_pointer(
    service,
    session,
    tmp_path,
):
    _write_bundle(tmp_path, version="1.0.0", procedure="# Develop\nv1\n")
    first = service.import_bundle(tmp_path, namespace="illo_core", org_id=ORG_ID)

    _write_bundle(tmp_path, version="1.1.0", procedure="# Develop\nv2\n")
    second = service.import_bundle(tmp_path, namespace="illo_core", org_id=ORG_ID)

    skill = session.get(Skill, second["skill"]["id"])
    installation = session.get(SkillInstallation, second["installation"]["id"])

    assert second["version"]["id"] != first["version"]["id"]
    assert second["installation"]["rollback_bundle_version_id"] == first["version"]["id"]
    assert installation.rollback_bundle_version_id == first["version"]["id"]
    assert skill.procedure == "# Develop\nv2\n"
    assert skill.version == 2


def test_import_bundle_can_auto_bump_core_bundle_when_semver_stays_stale(
    service,
    session,
    tmp_path,
):
    _write_bundle(tmp_path, version="1.0.0", procedure="# Develop\nv1\n")
    first = service.import_bundle(tmp_path, namespace="illo_core", org_id=ORG_ID)

    _write_bundle(tmp_path, version="1.0.0", procedure="# Develop\nv2\n")
    second = service.import_bundle(
        tmp_path,
        namespace="illo_core",
        org_id=ORG_ID,
        auto_bump_conflicting_semver=True,
    )
    third = service.import_bundle(
        tmp_path,
        namespace="illo_core",
        org_id=ORG_ID,
        auto_bump_conflicting_semver=True,
    )

    skill = session.get(Skill, second["skill"]["id"])
    installation = session.get(SkillInstallation, second["installation"]["id"])
    version = session.get(SkillBundleVersion, second["version"]["id"])

    assert second["version"]["id"] != first["version"]["id"]
    assert second["version"]["semver"] == "1.0.1"
    assert second["version"]["existing"] is False
    assert third["version"]["id"] == second["version"]["id"]
    assert third["version"]["existing"] is True
    assert version.provenance["declared_semver"] == "1.0.0"
    assert version.provenance["auto_bumped_from_semver"] == "1.0.0"
    assert installation.rollback_bundle_version_id == first["version"]["id"]
    assert skill.procedure == "# Develop\nv2\n"
    assert skill.bundle_version_id == second["version"]["id"]
    assert skill.version == 2


def test_import_bundle_rejects_stale_semver_without_auto_bump(service, tmp_path):
    _write_bundle(tmp_path, version="1.0.0", procedure="# Develop\nv1\n")
    service.import_bundle(tmp_path, namespace="illo_core", org_id=ORG_ID)

    _write_bundle(tmp_path, version="1.0.0", procedure="# Develop\nv2\n")

    with pytest.raises(SkillBundleVersionConflict, match="different digest"):
        service.import_bundle(tmp_path, namespace="illo_core", org_id=ORG_ID)


def test_export_skill_bundle_writes_loadable_files(service, session, tmp_path):
    SkillRepository(session).create(
        name="debug-skill",
        description="Debug carefully.",
        procedure="# Debug\n\n## Procedure\nInspect, test, fix.\n",
        triggers=[{"pattern": "bug"}],
        model_tier="medium",
        thinking_tier="medium",
    )
    session.flush()

    exported = service.export_skill_bundle(
        "debug-skill",
        tmp_path / "bundle",
        version="0.2.0",
        license="Apache-2.0",
    )
    loaded = load_skill_bundle(tmp_path / "bundle")

    assert exported.digest == loaded.digest
    assert loaded.manifest.name == "debug-skill"
    assert loaded.manifest.version == "0.2.0"
    assert loaded.manifest.runtime.default_model_tier == "medium"
    assert loaded.manifest.runtime.default_thinking_tier == "medium"
    assert loaded.skill_markdown.startswith("# Debug")


def test_upsert_skill_asset_converts_legacy_skill_and_publishes_script(service, session):
    skill = SkillRepository(session).create(
        name="debug-skill",
        description="Debug carefully.",
        procedure="# Debug\n\n1. Inspect\n2. Test\n",
        triggers=[{"pattern": "bug"}],
        model_tier="medium",
        thinking_tier="medium",
    )
    session.flush()

    asset = service.upsert_skill_asset(
        skill.id,
        path="scripts/verify.py",
        content="print('ok')\n",
        asset_kind="script",
    )

    session.flush()
    session.refresh(skill)
    assets = session.query(SkillAsset).filter_by(bundle_version_id=skill.bundle_version_id).order_by(SkillAsset.path).all()
    version = session.get(SkillBundleVersion, skill.bundle_version_id)

    assert asset.path == "scripts/verify.py"
    assert asset.asset_kind == "script"
    assert asset.content_text == "print('ok')\n"
    assert asset.mime_type == "text/x-python"
    assert skill.skill_installation_id is not None
    assert skill.bundle_version_id is not None
    assert version.manifest["loading"]["scripts"] == "scripts/"
    assert [item.path for item in assets] == ["SKILL.md", "scripts/verify.py"]


def test_upsert_skill_asset_revisions_are_immutable_and_delete_removes_asset(service, session):
    skill = SkillRepository(session).create(
        name="debug-skill",
        description="Debug carefully.",
        procedure="# Debug\n\n1. Inspect\n2. Test\n",
        model_tier="medium",
        thinking_tier="medium",
    )
    session.flush()

    first_asset = service.upsert_skill_asset(
        skill.id,
        path="references/context.md",
        content="first\n",
    )
    first_version_id = first_asset.bundle_version_id

    second_asset = service.upsert_skill_asset(
        skill.id,
        path="references/context.md",
        content="second\n",
    )
    second_version_id = second_asset.bundle_version_id

    assert second_version_id != first_version_id
    assert session.get(SkillAsset, first_asset.id).content_text == "first\n"
    assert second_asset.content_text == "second\n"

    service.delete_skill_asset(skill.id, path="references/context.md")
    session.flush()
    session.refresh(skill)
    current_assets = session.query(SkillAsset).filter_by(bundle_version_id=skill.bundle_version_id).all()
    assert [asset.path for asset in current_assets] == ["SKILL.md"]
