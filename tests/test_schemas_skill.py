"""Pydantic schema tests."""
import pytest
import pydantic
from datetime import datetime
from brain.platform.db.schemas.skills import (
    SkillBundleAssetType,
    SkillBundleInstallStatus,
    SkillBundleManifest,
    SkillBundleReviewStatus,
    SkillBundleSourceKind,
    SkillBundleTrustLevel,
    SkillBundleUpdatePolicy,
    SkillRead,
    SkillCreate,
    SkillUpdate,
    SkillExport,
)


def test_skill_read_from_dict():
    data = {
        "id": 1, "name": "deploy", "description": "Deploy things",
        "procedure": "steps", "version": 3, "maturity": "proficient",
        "confidence": 0.85, "use_count": 20, "success_count": 17,
        "failure_count": 2, "partial_count": 1, "avg_duration_sec": 45.2,
        "last_used": "2026-03-17T10:00:00", "pitfalls": [{"text": "watch out"}],
        "refinements": [], "triggers": [], "auto_emerged": False, "thinking_tier": "medium",
        "success_rate": 0.85, "children": [], "executions": [],
    }
    skill = SkillRead.model_validate(data)
    assert skill.name == "deploy"
    assert skill.success_rate == 0.85


def test_skill_create_defaults():
    data = {"name": "new-skill", "procedure": "do things carefully"}
    skill = SkillCreate.model_validate(data)
    assert skill.thinking_tier == "medium"
    assert skill.description == ""
    assert skill.guardrails == []


def test_skill_create_rejects_empty_name():
    with pytest.raises(pydantic.ValidationError):
        SkillCreate.model_validate({"name": "", "procedure": "stuff"})


def test_skill_update_all_optional():
    data = {"name": "renamed"}
    update = SkillUpdate.model_validate(data)
    assert update.name == "renamed"
    assert update.procedure is None


def test_skill_update_accepts_thinking_tier_only():
    update = SkillUpdate.model_validate({
        "thinking_tier": "xhigh",
    })
    assert update.thinking_tier == "xhigh"


def test_skill_update_accepts_editable_skill_sections():
    update = SkillUpdate.model_validate({
        "guardrails": [{"severity": "warning", "text": "check secrets"}],
        "triggers": [{"direction": "for", "pattern": "frontend"}],
        "pitfalls": ["avoid stale context"],
        "refinements": ["prefer focused patches"],
    })
    assert update.guardrails[0]["text"] == "check secrets"
    assert update.triggers[0]["pattern"] == "frontend"


def test_skill_read_serialization():
    data = {
        "id": 1, "name": "test", "description": None,
        "procedure": "p", "version": 1, "maturity": "emerging",
        "confidence": 0.3, "use_count": 0, "success_count": 0,
        "failure_count": 0, "partial_count": 0, "avg_duration_sec": None,
        "last_used": None, "pitfalls": [], "refinements": [], "triggers": [],
        "auto_emerged": False, "thinking_tier": "medium",
        "success_rate": 0.0, "children": [], "executions": [],
    }
    skill = SkillRead.model_validate(data)
    dumped = skill.model_dump()
    assert isinstance(dumped, dict)
    assert dumped["name"] == "test"


def test_skill_read_accepts_bundle_source_and_trust_enums():
    skill = SkillRead.model_validate({
        "id": 1,
        "name": "draft-skill",
        "procedure": "p",
        "source_kind": "agent_draft",
        "trust_level": "agent_draft",
    })

    assert skill.source_kind == "agent_draft"
    assert skill.trust_level == "agent_draft"
    assert skill.model_dump()["source_kind"] == "agent_draft"


def test_skill_bundle_manifest_schema_validates_nested_specs():
    manifest = SkillBundleManifest.model_validate({
        "schema_version": 1,
        "name": "develop",
        "version": "1.0.0",
        "description": "Do focused work.",
        "source": "marketplace",
        "visibility": "public",
        "routing": {"triggers": ["fix bug"], "embedding_text": "develop skill"},
        "runtime": {
            "default_thinking_tier": "high",
        },
        "permissions": {
            "toolsets": ["workspace_read"],
            "tools": [{"kind": "mcp", "name": "brain"}],
            "requires_review": True,
        },
    })

    assert manifest.source == SkillBundleSourceKind.MARKETPLACE.value
    assert manifest.visibility == SkillBundleTrustLevel.PUBLIC.value
    assert manifest.routing.triggers == ["fix bug"]
    assert manifest.runtime.default_thinking_tier == "high"
    assert manifest.permissions.tools[0].kind == "mcp"


def test_skill_bundle_enums_expose_hosted_contract_values():
    assert SkillBundleSourceKind.SELF_HOSTED.value == "self_hosted"
    assert SkillBundleTrustLevel.AGENT_DRAFT.value == "agent_draft"
    assert SkillBundleAssetType.PROCEDURE.value == "procedure"
    assert SkillBundleAssetType.SCRIPT.value == "script"
    assert SkillBundleUpdatePolicy.MANUAL.value == "manual"
    assert SkillBundleReviewStatus.APPROVED.value == "approved"
    assert SkillBundleInstallStatus.ACTIVE.value == "active"
