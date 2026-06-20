"""Filesystem skill bundle utility coverage."""
from __future__ import annotations

import hashlib

import pytest

from brain.systems.skills.bundles import SkillBundleAssetType, SkillBundleError, load_skill_bundle


def _write_bundle(root, *, manifest: str | None = None, skill: str | None = None):
    if manifest is not None:
        (root / "skill.toml").write_text(manifest, encoding="utf-8")
    if skill is not None:
        (root / "SKILL.md").write_text(skill, encoding="utf-8")


def _basic_manifest(*, extra: str = "") -> str:
    return f"""
schema_version = 1
name = "develop"
display_name = "Develop"
version = "1.4.0"
description = "Implement scoped code changes with evidence and tests."
license = "Apache-2.0"
source = "illo-core"
visibility = "public"

[routing]
triggers = ["fix bug", "implement feature"]

[runtime]
default_thinking_tier = "high"

[permissions]
toolsets = ["workspace_read", "workspace_write"]

[loading]
summary = "SKILL.md#summary"
examples = "examples/"
templates = "templates/"
schemas = "schemas/"
evals = "evals/"
references = "references/"
scripts = "scripts/"
{extra}
"""


def test_load_skill_bundle_happy_path_discovers_assets(tmp_path):
    _write_bundle(
        tmp_path,
        manifest=_basic_manifest(),
        skill="# Develop\n\n## Summary\nDo focused code work.\n",
    )
    (tmp_path / "examples" / "nested").mkdir(parents=True)
    (tmp_path / "examples" / "nested" / "happy-path.md").write_text(
        "Ship the smallest complete fix.\n",
        encoding="utf-8",
    )
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "plan.md").write_text("- inspect\n- test\n", encoding="utf-8")
    (tmp_path / "schemas").mkdir()
    (tmp_path / "schemas" / "evidence.schema.json").write_text(
        '{"type":"object"}\n',
        encoding="utf-8",
    )
    (tmp_path / "evals").mkdir()
    (tmp_path / "evals" / "golden.jsonl").write_text('{"ok":true}\n', encoding="utf-8")
    (tmp_path / "references").mkdir()
    (tmp_path / "references" / "image.bin").write_bytes(b"\x00\x01\x02")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "verify.py").write_text("print('ok')\n", encoding="utf-8")
    (tmp_path / "notes.md").write_text("not an asset\n", encoding="utf-8")

    bundle = load_skill_bundle(tmp_path)

    assert bundle.manifest.schema_version == 1
    assert bundle.manifest.name == "develop"
    assert bundle.manifest.version == "1.4.0"
    assert bundle.manifest.license == "Apache-2.0"
    assert bundle.manifest.routing.triggers == ["fix bug", "implement feature"]
    assert bundle.manifest.runtime.default_thinking_tier == "high"
    assert bundle.manifest.permissions.toolsets == [
        "workspace_read",
        "workspace_write",
    ]
    assert bundle.manifest.raw["runtime"]["default_thinking_tier"] == "high"
    assert len(bundle.digest) == 64
    assert [asset.path for asset in bundle.assets] == [
        "evals/golden.jsonl",
        "examples/nested/happy-path.md",
        "references/image.bin",
        "schemas/evidence.schema.json",
        "scripts/verify.py",
        "templates/plan.md",
    ]

    assets = {asset.path: asset for asset in bundle.assets}
    example = assets["examples/nested/happy-path.md"]
    assert example.kind == SkillBundleAssetType.EXAMPLE.value
    assert example.mime_type == "text/markdown"
    assert example.content_text == "Ship the smallest complete fix.\n"
    assert example.sha256 == hashlib.sha256(example.content_text.encode("utf-8")).hexdigest()
    assert assets["references/image.bin"].content_text is None
    assert assets["scripts/verify.py"].kind == SkillBundleAssetType.SCRIPT.value
    assert assets["scripts/verify.py"].content_text == "print('ok')\n"

    assert bundle.to_db_bundle_payload(namespace="illo_core") == {
        "namespace": "illo_core",
        "name": "develop",
        "display_name": "Develop",
        "description": "Implement scoped code changes with evidence and tests.",
        "visibility": "public",
        "source_kind": "illo-core",
        "trust_level": "public",
    }
    assert bundle.to_db_version_payload()["content_digest"] == f"sha256:{bundle.digest}"
    assert bundle.to_db_asset_payloads()[0]["path"] == "evals/golden.jsonl"


def test_load_skill_bundle_strips_legacy_provider_runtime_fields(tmp_path):
    _write_bundle(
        tmp_path,
        manifest="""
schema_version = 1
name = "develop"
display_name = "Develop"
version = "1.4.0"
description = "Implement scoped code changes with evidence and tests."
license = "Apache-2.0"
source = "illo-core"
visibility = "public"

[runtime]
default_provider = "openai"
default_model = "gpt-5.5"
default_reasoning_effort = "xhigh"
service_tier = "priority"
auth_mode = "chatgpt"
""",
        skill="# Develop\n\n## Summary\nDo focused code work.\n",
    )

    bundle = load_skill_bundle(tmp_path)
    assert bundle.manifest.runtime.default_thinking_tier == "xhigh"
    assert "default_provider" not in bundle.manifest.raw["runtime"]
    assert "default_model" not in bundle.manifest.raw["runtime"]
    assert "default_reasoning_effort" not in bundle.manifest.raw["runtime"]
    assert "auth_mode" not in bundle.manifest.raw["runtime"]


def test_load_skill_bundle_requires_manifest_and_skill_markdown(tmp_path):
    _write_bundle(tmp_path, skill="# Missing manifest\n")
    with pytest.raises(SkillBundleError, match="missing required bundle file: skill.toml"):
        load_skill_bundle(tmp_path)

    (tmp_path / "skill.toml").write_text(_basic_manifest(), encoding="utf-8")
    (tmp_path / "SKILL.md").unlink()
    with pytest.raises(SkillBundleError, match="missing required bundle file: SKILL.md"):
        load_skill_bundle(tmp_path)


def test_load_skill_bundle_validates_required_manifest_fields(tmp_path):
    _write_bundle(
        tmp_path,
        manifest='schema_version = 1\nname = "bad"\nversion = "1.0.0"\n',
        skill="# Skill\n",
    )

    with pytest.raises(SkillBundleError, match="description"):
        load_skill_bundle(tmp_path)


def test_load_skill_bundle_rejects_invalid_nested_manifest_shape(tmp_path):
    _write_bundle(
        tmp_path,
        manifest="""
schema_version = 1
name = "develop"
version = "1.0.0"
description = "Do focused work."
permissions = "workspace_read"
""",
        skill="# Skill\n",
    )

    with pytest.raises(SkillBundleError, match="permissions"):
        load_skill_bundle(tmp_path)


@pytest.mark.parametrize(
    "bad_path",
    [
        "../outside.md",
        "/tmp/outside.md",
        "examples/../outside.md",
        "C:\\temp\\outside.md",
    ],
)
def test_load_skill_bundle_rejects_unsafe_manifest_asset_paths(tmp_path, bad_path):
    _write_bundle(
        tmp_path,
        manifest=_basic_manifest(extra=f"bad = {bad_path!r}"),
        skill="# Skill\n",
    )

    with pytest.raises(SkillBundleError, match="path"):
        load_skill_bundle(tmp_path)


def test_load_skill_bundle_rejects_asset_symlink_escape(tmp_path):
    _write_bundle(tmp_path, manifest=_basic_manifest(), skill="# Skill\n")
    outside = tmp_path.parent / "outside-reference.md"
    outside.write_text("outside\n", encoding="utf-8")
    (tmp_path / "references").mkdir()
    (tmp_path / "references" / "outside.md").symlink_to(outside)

    with pytest.raises(SkillBundleError, match="symlinks are not allowed"):
        load_skill_bundle(tmp_path)


def test_load_skill_bundle_digest_is_stable_and_content_sensitive(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()

    first_manifest = """
schema_version = 1
name = "develop"
version = "1.4.0"
description = "Implement scoped code changes with evidence and tests."

[loading]
examples = "examples/"
"""
    second_manifest = """
description = "Implement scoped code changes with evidence and tests."
version = "1.4.0"
name = "develop"
schema_version = 1

[loading]
examples = "examples/"
"""
    _write_bundle(first, manifest=first_manifest, skill="# Develop\n")
    _write_bundle(second, manifest=second_manifest, skill="# Develop\n")

    for root in (first, second):
        (root / "examples").mkdir()
        (root / "examples" / "b.md").write_text("bravo\n", encoding="utf-8")
        (root / "examples" / "a.md").write_text("alpha\n", encoding="utf-8")

    first_bundle = load_skill_bundle(first)
    second_bundle = load_skill_bundle(second)
    assert first_bundle.digest == second_bundle.digest
    assert load_skill_bundle(first).digest == first_bundle.digest

    (second / "examples" / "a.md").write_text("changed\n", encoding="utf-8")
    assert load_skill_bundle(second).digest != first_bundle.digest
