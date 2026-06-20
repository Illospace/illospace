"""Regression tests for product-owned built-in skills."""

from __future__ import annotations

from collections.abc import Mapping
import json
from typing import Any


CORE_BUILTINS = {
    "coordinate",
    "orchestrate",
    "report-workspace-blocker",
    "skill-authoring",
    "conversation-audit",
    "build-workspace-app",
    "manage-domains",
    "manage-projects",
}


def test_builtin_skills_are_limited_to_product_primitives():
    import brain.systems.skills.builtin as module

    assert set(module.BUILTIN_SKILLS) == CORE_BUILTINS
    assert not hasattr(module, "SKILL_RETIREMENTS")
    assert not hasattr(module, "BUILTIN_SKILL_RETIREMENTS")


def test_builtin_skills_have_structured_routing_metadata():
    from brain.systems.skills.builtin import BUILTIN_SKILLS

    for name, skill in BUILTIN_SKILLS.items():
        assert skill["name"] == name
        assert skill["description"]
        assert skill["procedure"]
        assert skill["source_kind"] == "illo-core"
        assert skill["trust_level"] == "illo_core"
        assert skill["thinking_tier"] in {"none", "low", "medium", "high", "xhigh"}
        assert _has_text_items(skill["triggers"], "pattern")
        assert _has_text_items(skill["guardrails"], "text")
        assert all(item.get("severity") for item in skill["guardrails"])


def test_builtin_skills_have_explicit_role_boundaries():
    from brain.systems.skills.builtin import BUILTIN_SKILLS

    required_sections = (
        "## Role",
        "## Use When",
        "## Do Not Use When",
        "## Context To Load",
        "## Operating Loop",
        "## Output Contract",
        "## Failure Modes",
    )

    for skill in BUILTIN_SKILLS.values():
        procedure = skill["procedure"]
        for section in required_sections:
            assert section in procedure
        assert _has_text_items(skill["pitfalls"], "text")
        assert _has_text_items(skill["refinements"], "text")


def test_builtin_skill_bundles_parse_and_mirror_bootstrap_procedures():
    from brain.systems.skills.builtin import (
        BUILTIN_SKILL_BUNDLE_ROOT,
        BUILTIN_SKILLS,
    )
    from brain.systems.skills.bundles import load_skill_bundle

    for name, skill in BUILTIN_SKILLS.items():
        bundle = load_skill_bundle(BUILTIN_SKILL_BUNDLE_ROOT / name)
        assert bundle.manifest.name == name
        assert bundle.manifest.source == "illo-core"
        assert bundle.skill_markdown == skill["procedure"]
        assert bundle.manifest.runtime.default_thinking_tier == skill["thinking_tier"]
        assert bundle.manifest.routing.triggers == skill["triggers"]
        assert bundle.assets


def test_tool_heavy_builtin_bundles_have_progressive_assets():
    from brain.systems.skills.builtin import BUILTIN_SKILL_BUNDLE_ROOT
    from brain.systems.skills.bundles import load_skill_bundle

    expected_assets = {
        "skill-authoring": {
            "templates/SKILL.md",
            "templates/skill.toml",
            "schemas/skill-authoring-output.schema.json",
            "examples/private-db-skill.md",
            "references/versioning.md",
        },
        "build-workspace-app": {
            "templates/sandboxed-html-app.html",
            "templates/thumbnail.html",
            "schemas/workspace-app-output.schema.json",
            "examples/app-local-state.md",
            "examples/domain-backed-app.md",
            "references/host-bridge.md",
        },
        "manage-domains": {
            "templates/domain-schema.json",
            "schemas/domain-change-output.schema.json",
            "examples/good-crm-domain.md",
            "examples/overmodeled-domain.md",
            "references/versioning-conflicts.md",
        },
    }

    for name, required_paths in expected_assets.items():
        bundle = load_skill_bundle(BUILTIN_SKILL_BUNDLE_ROOT / name)
        actual_paths = {asset.path for asset in bundle.assets}
        assert required_paths <= actual_paths
        for asset in bundle.assets:
            if asset.path.startswith("schemas/"):
                assert asset.content_text is not None
                json.loads(asset.content_text)


def test_coordinate_owns_routing_before_orchestration():
    from brain.systems.skills.builtin import BUILTIN_SKILLS

    procedure = BUILTIN_SKILLS["coordinate"]["procedure"]
    for expected in (
        "## Routing Ladder",
        "brain_skills",
        "skill_view",
        "memory as stale",
        "single tool",
        "internal orchestration protocol",
        "external state",
    ):
        assert expected in procedure


def test_orchestrate_is_internal_protocol_not_default_coordinator():
    from brain.systems.skills.builtin import BUILTIN_SKILLS

    skill = BUILTIN_SKILLS["orchestrate"]
    assert "Internal orchestration protocol" in skill["description"]
    assert "You are not a general conversation skill" in skill["procedure"]
    assert any(
        trigger.get("direction") == "against"
        for trigger in skill["triggers"]
    )


def test_orchestrate_keeps_runtime_contract_and_memory_lifecycle():
    from brain.systems.skills.builtin import BUILTIN_SKILLS

    procedure = BUILTIN_SKILLS["orchestrate"]["procedure"]
    for expected in (
        "brain_skills",
        "AgentRun graph",
        "OBJECTIVE",
        "SCOPE",
        "INPUT",
        "OUTPUT",
        "DONE WHEN",
        "AgentRun graph started",
        "AgentRun graph completed/failed",
        "session_promote",
        "session_close",
    ):
        assert expected in procedure


def test_report_workspace_blocker_routes_to_headless_worker():
    from brain.systems.skills.builtin import BUILTIN_SKILLS

    skill = BUILTIN_SKILLS["report-workspace-blocker"]
    assert "tickets" in skill["description"]
    assert "spawn_worker" in skill["procedure"]
    assert "headless=true" in skill["procedure"]
    assert any(trigger["pattern"] == "report this bug" for trigger in skill["triggers"])
    assert any("Search for duplicates" in guardrail["text"] for guardrail in skill["guardrails"])


def _has_text_items(items: Any, key: str) -> bool:
    if not isinstance(items, list) or not items:
        return False
    for item in items:
        if not isinstance(item, Mapping) or not str(item.get(key) or "").strip():
            return False
    return True
