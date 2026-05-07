from brain.systems.learning.policy import (
    CommunitySkillAutoUpdatePolicy,
    LearningDeploymentMode,
    LearningPolicyOverride,
    PrivateDataRedactionMode,
    build_learning_policy,
)


def test_hosted_defaults_are_safe_and_budget_capped():
    policy = build_learning_policy(env={})

    assert policy.enabled is True
    assert policy.deployment_mode == LearningDeploymentMode.HOSTED
    assert policy.after_run_sample_rate == 0.25
    assert policy.night_budget_units == 100_000
    assert policy.tenant_daily_budget_units == 250_000
    assert policy.active_context_policy_enabled is True
    assert policy.skill_quality_routing_enabled is True
    assert policy.after_run_learning_enabled is True
    assert policy.night_llm_adjudication_enabled is True
    assert policy.allowed_model_tiers == ("low", "medium")
    assert policy.external_eval_export_allowed is False
    assert policy.community_skill_auto_update_policy == CommunitySkillAutoUpdatePolicy.SECURITY_PATCH_ONLY
    assert policy.private_data_redaction_mode == PrivateDataRedactionMode.STRICT
    assert policy.risk_flags == ()


def test_self_hosted_defaults_prefer_local_and_low_intelligence_learning():
    policy = build_learning_policy(env={"ILLO_DEPLOYMENT_MODE": "self-hosted"})

    assert policy.enabled is True
    assert policy.deployment_mode == LearningDeploymentMode.SELF_HOSTED
    assert policy.after_run_sample_rate == 1.0
    assert policy.active_context_policy_enabled is True
    assert policy.skill_quality_routing_enabled is True
    assert policy.after_run_learning_enabled is True
    assert policy.night_llm_adjudication_enabled is True
    assert policy.allowed_model_tiers == ("local", "low")
    assert policy.external_eval_export_allowed is False
    assert policy.community_skill_auto_update_policy == CommunitySkillAutoUpdatePolicy.PATCH_ONLY
    assert policy.private_data_redaction_mode == PrivateDataRedactionMode.LOCAL_ONLY
    assert policy.to_payload()["deployment_mode"] == "self_hosted"


def test_env_overrides_are_explicit_and_deterministic():
    env = {
        "LEARNING_POLICY_ENABLED": "false",
        "LEARNING_POLICY_AFTER_RUN_SAMPLE_RATE": "0.125",
        "LEARNING_POLICY_NIGHT_BUDGET_UNITS": "123",
        "LEARNING_POLICY_TENANT_DAILY_BUDGET_UNITS": "456",
        "LEARNING_POLICY_ALLOWED_MODEL_TIERS": "local, low, low, medium",
        "LEARNING_POLICY_EXTERNAL_EVAL_EXPORT_ALLOWED": "true",
        "LEARNING_POLICY_COMMUNITY_SKILL_AUTO_UPDATE": "disabled",
        "LEARNING_POLICY_PRIVATE_DATA_REDACTION": "strict",
    }

    first = build_learning_policy(env=env)
    second = build_learning_policy(env=dict(reversed(list(env.items()))))

    assert second.to_payload() == first.to_payload()
    assert first.enabled is False
    assert first.active_context_policy_enabled is False
    assert first.skill_quality_routing_enabled is False
    assert first.after_run_learning_enabled is False
    assert first.night_llm_adjudication_enabled is False
    assert first.after_run_sample_rate == 0.125
    assert first.night_budget_units == 123
    assert first.tenant_daily_budget_units == 456
    assert first.allowed_model_tiers == ("local", "low", "medium")
    assert first.external_eval_export_allowed is True
    assert first.community_skill_auto_update_policy == CommunitySkillAutoUpdatePolicy.DISABLED
    assert first.private_data_redaction_mode == PrivateDataRedactionMode.STRICT
    assert first.risk_flags == ()


def test_budget_env_names_remain_supported_for_shared_policy_fields():
    policy = build_learning_policy(env={
        "LEARNING_BUDGET_DEPLOYMENT_MODE": "self_hosted",
        "LEARNING_BUDGET_AFTER_RUN_SAMPLE_RATE": "0.5",
        "LEARNING_BUDGET_NIGHT_TOKENS": "2000",
        "LEARNING_BUDGET_TENANT_DAILY_TOKENS": "3000",
    })

    assert policy.deployment_mode == LearningDeploymentMode.SELF_HOSTED
    assert policy.after_run_sample_rate == 0.5
    assert policy.night_budget_units == 2000
    assert policy.tenant_daily_budget_units == 3000


def test_learning_policy_feature_kill_switches_parse_enabled_and_disabled_env_names():
    policy = build_learning_policy(env={
        "LEARNING_POLICY_ACTIVE_CONTEXT_POLICY_ENABLED": "false",
        "LEARNING_POLICY_SKILL_QUALITY_ROUTING_DISABLED": "true",
        "LEARNING_POLICY_AFTER_RUN_LEARNING_DISABLED": "1",
        "LEARNING_POLICY_NIGHT_LLM_ADJUDICATION_ENABLED": "off",
    })

    assert policy.enabled is True
    assert policy.active_context_policy_enabled is False
    assert policy.skill_quality_routing_enabled is False
    assert policy.after_run_learning_enabled is False
    assert policy.night_llm_adjudication_enabled is False
    assert policy.to_payload()["applied_overrides"][0]["values"] == {
        "active_context_policy_enabled": False,
        "skill_quality_routing_enabled": False,
        "after_run_learning_enabled": False,
        "night_llm_adjudication_enabled": False,
    }


def test_night_llm_adjudication_kill_switch_blocks_memory_provider_flag(monkeypatch):
    from brain.systems.memory.truth_maintenance import llm_adjudication_enabled

    monkeypatch.setenv("MEMORY_TRUTH_ADJUDICATION_MODEL", "provider-model")
    monkeypatch.delenv("LEARNING_POLICY_NIGHT_LLM_ADJUDICATION_ENABLED", raising=False)
    monkeypatch.setenv("LEARNING_POLICY_NIGHT_LLM_ADJUDICATION_DISABLED", "1")

    assert llm_adjudication_enabled() is False


def test_org_and_tenant_overrides_apply_without_persistence():
    org_override = LearningPolicyOverride(
        scope="org",
        scope_id="org-1",
        source="config",
        after_run_sample_rate=0.0,
        skill_quality_routing_enabled=False,
        external_eval_export_allowed=True,
        private_data_redaction_mode="strict",
    )
    tenant_override = LearningPolicyOverride(
        scope="tenant",
        scope_id="tenant-1",
        source="config",
        enabled=False,
        allowed_model_tiers=("local", "low"),
    )

    policy = build_learning_policy(
        env={},
        org_override=org_override,
        tenant_override=tenant_override,
    )

    assert policy.enabled is False
    assert policy.after_run_sample_rate == 0.0
    assert policy.skill_quality_routing_enabled is False
    assert policy.external_eval_export_allowed is True
    assert policy.allowed_model_tiers == ("local", "low")
    assert [override["scope"] for override in policy.to_payload()["applied_overrides"]] == [
        "org",
        "tenant",
    ]


def test_risk_flags_call_out_unsafe_override_combinations():
    policy = build_learning_policy(env={
        "LEARNING_POLICY_EXTERNAL_EVAL_EXPORT_ALLOWED": "true",
        "LEARNING_POLICY_PRIVATE_DATA_REDACTION": "disabled",
        "LEARNING_POLICY_ALLOWED_MODEL_TIERS": "low,high",
        "LEARNING_POLICY_COMMUNITY_SKILL_AUTO_UPDATE": "minor",
    })

    assert set(policy.risk_flags) == {
        "external_eval_export_without_redaction",
        "community_skill_auto_update_allows_minor_versions",
        "high_intelligence_learning_model_tier_enabled",
    }


def test_legacy_cost_tier_aliases_normalize_to_intelligence_tiers():
    policy = build_learning_policy(env={
        "LEARNING_POLICY_ALLOWED_MODEL_TIERS": "local,cheap,standard,premium",
    })

    assert policy.allowed_model_tiers == ("local", "low", "medium", "high")
    assert policy.risk_flags == ("high_intelligence_learning_model_tier_enabled",)
