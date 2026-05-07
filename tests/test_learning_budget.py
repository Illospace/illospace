from brain.systems.learning.budget import (
    BudgetDecisionAction,
    BudgetLane,
    LearningBudgetEntry,
    LearningBudgetLedger,
    LearningBudgetPolicy,
    LearningCostEstimate,
    ProviderLocation,
    should_run_learning_task,
)


def test_hot_path_allows_only_safe_low_latency_learning_by_default():
    policy = LearningBudgetPolicy()

    decision = should_run_learning_task(
        lane=BudgetLane.HOT_PATH,
        task_type="embedding",
        estimated_tokens=120,
        model_tier="embedding",
        provider_location=ProviderLocation.LOCAL,
        elapsed_ms=12,
        blocks_user_latency=True,
        org_id="org-1",
        user_id="user-1",
        policy=policy,
    )

    assert decision.action == BudgetDecisionAction.ALLOW
    assert decision.allowed is True
    assert decision.cost_estimate.scope == {"org_id": "org-1", "user_id": "user-1"}
    assert decision.cost_estimate.to_payload()["blocks_user_latency"] is True


def test_hot_path_defers_generation_unless_explicitly_allowed():
    policy = LearningBudgetPolicy()

    decision = should_run_learning_task(
        lane="hot_path",
        task_type="policy_promotion",
        estimated_tokens=250,
        model_tier="small",
        provider_location="remote",
        blocks_user_latency=True,
        policy=policy,
    )

    assert decision.action == BudgetDecisionAction.DEFER
    assert "hot path" in decision.reason

    explicit = should_run_learning_task(
        lane="hot_path",
        task_type="policy_promotion",
        estimated_tokens=250,
        model_tier="small",
        provider_location="remote",
        blocks_user_latency=True,
        explicit_hot_path_allow=True,
        policy=policy,
    )

    assert explicit.action == BudgetDecisionAction.ALLOW


def test_budget_denial_is_skip_or_defer_not_error():
    ledger = LearningBudgetLedger(entries=(
        LearningBudgetEntry(
            lane=BudgetLane.NIGHT,
            task_type="reflection",
            cost=LearningCostEstimate(
                estimated_tokens=95_000,
                model_tier="small",
                provider_location="local",
                org_id="org-1",
            ),
        ),
    ))

    decision = should_run_learning_task(
        lane=BudgetLane.NIGHT,
        task_type="reflection",
        estimated_tokens=10_000,
        model_tier="small",
        provider_location="local",
        org_id="org-1",
        priority=5,
        policy=LearningBudgetPolicy(),
        ledger=ledger,
    )

    assert decision.action == BudgetDecisionAction.SKIP
    assert decision.allowed is False
    assert decision.remaining_tokens == 5_000
    assert "exhausted" in decision.reason


def test_tenant_daily_budget_caps_all_lanes_for_scope():
    policy = LearningBudgetPolicy(lane_token_limits={
        BudgetLane.HOT_PATH: 1_500,
        BudgetLane.AFTER_RUN: 20_000,
        BudgetLane.NIGHT: 100_000,
        BudgetLane.TENANT_DAILY: 1_000,
    })
    ledger = LearningBudgetLedger(entries=(
        LearningBudgetEntry(
            lane=BudgetLane.AFTER_RUN,
            task_type="sample",
            cost=LearningCostEstimate(
                estimated_tokens=900,
                model_tier="small",
                provider_location="local",
                org_id="org-1",
                user_id="user-1",
            ),
        ),
    ))

    decision = should_run_learning_task(
        lane=BudgetLane.HOT_PATH,
        task_type="metadata",
        estimated_tokens=200,
        model_tier="metadata",
        provider_location="local",
        org_id="org-1",
        user_id="user-1",
        policy=policy,
        ledger=ledger,
    )

    assert decision.action == BudgetDecisionAction.SKIP
    assert decision.reason == "tenant daily learning budget exhausted"


def test_after_run_sampling_is_deterministic_and_deferable():
    policy = LearningBudgetPolicy(after_run_sample_rate=0.0)

    first = should_run_learning_task(
        lane=BudgetLane.AFTER_RUN,
        task_type="example_capture",
        estimated_tokens=500,
        sample_key="run-1",
        policy=policy,
    )
    second = should_run_learning_task(
        lane=BudgetLane.AFTER_RUN,
        task_type="example_capture",
        estimated_tokens=500,
        sample_key="run-1",
        policy=policy,
    )

    assert first.action == BudgetDecisionAction.DEFER
    assert second.to_payload() == first.to_payload()


def test_night_requires_priority_and_spends_remaining_budget():
    policy = LearningBudgetPolicy(night_min_priority=3)

    low_priority = should_run_learning_task(
        lane=BudgetLane.NIGHT,
        task_type="reflection",
        estimated_tokens=500,
        priority=1,
        policy=policy,
    )
    high_priority = should_run_learning_task(
        lane=BudgetLane.NIGHT,
        task_type="reflection",
        estimated_tokens=500,
        priority=3,
        policy=policy,
    )

    assert low_priority.action == BudgetDecisionAction.DEFER
    assert high_priority.action == BudgetDecisionAction.ALLOW
    assert "priority" in high_priority.reason


def test_env_parsing_supports_hosted_and_self_hosted_defaults():
    policy = LearningBudgetPolicy.from_env({
        "LEARNING_BUDGET_DEPLOYMENT_MODE": "hosted",
        "LEARNING_BUDGET_ALLOW_REMOTE": "false",
        "LEARNING_BUDGET_HOT_PATH_TOKENS": "42",
        "LEARNING_BUDGET_AFTER_RUN_SAMPLE_RATE": "0.25",
        "LEARNING_BUDGET_ALLOW_HOT_PATH_GENERATION": "yes",
    })

    assert policy.allow_remote_provider is False
    assert policy.deployment_mode == "hosted"
    assert policy.allow_hot_path_generation is True
    assert policy.limit_for(BudgetLane.HOT_PATH) == 42
    assert policy.after_run_sample_rate == 0.25

    decision = should_run_learning_task(
        lane=BudgetLane.AFTER_RUN,
        task_type="example_capture",
        estimated_tokens=10,
        provider_location=ProviderLocation.REMOTE,
        policy=policy,
    )

    assert decision.action == BudgetDecisionAction.DEFER
    assert decision.reason == "remote learning provider disabled by policy"

    self_hosted = LearningBudgetPolicy.from_env({
        "LEARNING_BUDGET_DEPLOYMENT_MODE": "self_hosted",
    })

    assert self_hosted.deployment_mode == "self_hosted"
    assert self_hosted.allow_remote_provider is False


def test_runtime_settings_exposes_learning_budget_defaults(monkeypatch):
    import brain.systems.services.runtime_introspection as runtime_settings_service

    monkeypatch.setenv("LEARNING_BUDGET_NIGHT_TOKENS", "12345")

    settings = runtime_settings_service.get_learning_budget_runtime_settings()

    assert settings["lane_token_limits"]["night"] == 12345
    assert settings["enabled"] is True
