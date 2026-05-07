from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import uuid


def _make_uow(session):
    uow = MagicMock()
    uow.__enter__ = MagicMock(return_value=uow)
    uow.__exit__ = MagicMock(return_value=False)
    uow.session = session
    return uow


def test_canary_scope_accepts_uuid_values():
    from brain.systems.routing.marketplace import _canary_allocation, _canary_bucket

    org_id = uuid.uuid4()
    user_id = uuid.uuid4()

    bucket = _canary_bucket(
        org_id=org_id,
        user_id=user_id,
        task_family="thread_reply",
        lane="coordinator",
        experiment_name="default",
        run_id=1214,
    )
    allocation = _canary_allocation(
        policy={"canary_percent": 10},
        org_id=org_id,
        user_id=user_id,
        task_family="thread_reply",
        lane="coordinator",
        experiment_name="default",
        run_id=1214,
    )

    assert 0 <= bucket < 100
    assert allocation["scope"]["org_id"] == str(org_id)
    assert allocation["scope"]["user_id"] == str(user_id)


def test_candidate_exclusion_keeps_runtime_openai_only():
    from brain.platform.providers.model_policy import ProviderResolution, SkillRoutingProfile, SkillRuntimeConfig
    from brain.systems.routing.marketplace import resolve_marketplace_routing

    session = MagicMock()
    session.execute.return_value.mappings.return_value.all.return_value = []
    session.scalars.return_value.all.return_value = []
    mock_uow = _make_uow(session)

    snapshots = {
        ("anthropic", "claude-sonnet-4-6"): {
            "provider": "anthropic",
            "model": "claude-sonnet-4-6",
            "window_start": datetime.now(timezone.utc),
            "window_end": datetime.now(timezone.utc),
            "p50_latency_ms": 1000,
            "p95_latency_ms": 1200,
            "error_rate": 0.0,
            "auth_fail_rate": 0.0,
            "rate_limit_rate": 0.0,
            "sample_count": 12,
            "source": "test",
        },
        ("openai", "gpt-5.4"): {
            "provider": "openai",
            "model": "gpt-5.4",
            "window_start": datetime.now(timezone.utc),
            "window_end": datetime.now(timezone.utc),
            "p50_latency_ms": 2000,
            "p95_latency_ms": 2200,
            "error_rate": 0.0,
            "auth_fail_rate": 0.0,
            "rate_limit_rate": 0.0,
            "sample_count": 12,
            "source": "test",
        },
    }

    def load_snapshot(_session, provider, model):
        return snapshots.get((provider, model))

    with patch("brain.systems.routing.marketplace.UnitOfWork", return_value=mock_uow), \
         patch("brain.systems.routing.marketplace.persist_routing_decision", side_effect=lambda _session, decision: decision), \
         patch("brain.systems.routing.marketplace.resolve_provider_selection", return_value=ProviderResolution(provider="openai", source="fallback", explicit=False)), \
         patch("brain.systems.routing.marketplace.resolve_skill_routing_profile", return_value=SkillRoutingProfile(
             skill_name="develop",
             reasoning_effort="high",
             model_tier="medium",
             thinking_tier="high",
         )), \
         patch("brain.systems.routing.marketplace.resolve_skill_runtime", return_value=SkillRuntimeConfig(
             provider="openai",
             model_name="gpt-5.4",
             reasoning_effort="medium",
             model_tier="medium",
             thinking_tier="medium",
         )), \
         patch("brain.systems.routing.marketplace.get_provider_model_map", side_effect=lambda provider, **kwargs: {
             "anthropic": {"high": "claude-opus-4-6", "medium": "claude-sonnet-4-6"},
             "openai": {"high": "gpt-5.4-pro", "medium": "gpt-5.4"},
         }[provider]), \
         patch("brain.systems.routing.marketplace._load_latest_health_snapshot", side_effect=load_snapshot), \
         patch("brain.systems.routing.marketplace._load_verifier_evidence", return_value={"sample_count": 12, "success_rate": 0.92}), \
         patch("brain.systems.services.runtime_introspection.get_provider_auth_status", side_effect=lambda **kwargs: {"authenticated": kwargs.get("provider") == "openai"}):
        decision = resolve_marketplace_routing(
            task_family="develop",
            lane="worker",
            skill_name="develop",
            user_id="user-1",
            org_id="org-1",
            run_id=7,
            legacy_provider="openai",
            legacy_model="openai/gpt-5.4",
            legacy_reasoning_effort="medium",
        )

    assert decision.decision_mode == "shadow"
    assert decision.selected_provider == "openai"
    assert decision.selected_model == "gpt-5.4"
    assert {candidate["provider"] for candidate in decision.candidate_scores} == {"openai"}
    included = next(candidate for candidate in decision.candidate_scores if candidate["provider"] == "openai")
    assert included["eligible"] is True


def test_candidate_pool_preserves_explicit_anthropic_route():
    from brain.platform.providers.model_policy import ProviderResolution, SkillRoutingProfile, SkillRuntimeConfig
    from brain.systems.routing.marketplace import resolve_marketplace_routing

    session = MagicMock()
    session.execute.return_value.mappings.return_value.all.return_value = []
    session.scalars.return_value.all.return_value = []
    mock_uow = _make_uow(session)

    with patch("brain.systems.routing.marketplace.UnitOfWork", return_value=mock_uow), \
         patch("brain.systems.routing.marketplace.persist_routing_decision", side_effect=lambda _session, decision: decision), \
         patch("brain.systems.routing.marketplace.resolve_provider_selection", return_value=ProviderResolution(provider="anthropic", source="preferred_provider", explicit=False)), \
         patch("brain.systems.routing.marketplace.resolve_skill_routing_profile", return_value=SkillRoutingProfile(
             skill_name="develop",
             reasoning_effort="high",
             model_tier="medium",
             thinking_tier="high",
         )), \
         patch("brain.systems.routing.marketplace.resolve_skill_runtime", return_value=SkillRuntimeConfig(
             provider="anthropic",
             model_name="claude-sonnet-4-6",
             reasoning_effort="medium",
             model_tier="medium",
             thinking_tier="medium",
         )), \
         patch("brain.systems.routing.marketplace.get_provider_model_map", return_value={"medium": "claude-sonnet-4-6"}), \
         patch("brain.systems.routing.marketplace._load_latest_health_snapshot", return_value=None), \
         patch("brain.systems.routing.marketplace._load_verifier_evidence", return_value={"sample_count": 0, "success_rate": None}), \
         patch("brain.systems.services.runtime_introspection.get_provider_auth_status", return_value={"authenticated": True}):
        decision = resolve_marketplace_routing(
            task_family="develop",
            lane="worker",
            skill_name="develop",
            user_id="user-1",
            org_id="org-1",
            run_id=17,
            legacy_provider="anthropic",
            legacy_model="anthropic/claude-sonnet-4-6",
            legacy_reasoning_effort="medium",
        )

    assert decision.selected_provider == "anthropic"
    assert decision.selected_model == "claude-sonnet-4-6"
    assert {candidate["provider"] for candidate in decision.candidate_scores} == {"anthropic"}


def test_active_within_provider_canary_uses_stronger_openai_model():
    from brain.platform.providers.model_policy import ProviderResolution, SkillRoutingProfile, SkillRuntimeConfig
    from brain.systems.routing.marketplace import resolve_marketplace_routing

    session = MagicMock()
    session.execute.return_value.mappings.return_value.all.return_value = []
    session.scalars.return_value.all.return_value = []
    mock_uow = _make_uow(session)

    snapshots = {
        ("openai", "gpt-5.4"): {
            "provider": "openai",
            "model": "gpt-5.4",
            "window_start": datetime.now(timezone.utc),
            "window_end": datetime.now(timezone.utc),
            "p50_latency_ms": 1600,
            "p95_latency_ms": 1900,
            "error_rate": 0.08,
            "auth_fail_rate": 0.0,
            "rate_limit_rate": 0.0,
            "sample_count": 18,
            "source": "test",
        },
        ("openai", "gpt-5.4-pro"): {
            "provider": "openai",
            "model": "gpt-5.4-pro",
            "window_start": datetime.now(timezone.utc),
            "window_end": datetime.now(timezone.utc),
            "p50_latency_ms": 900,
            "p95_latency_ms": 1100,
            "error_rate": 0.01,
            "auth_fail_rate": 0.0,
            "rate_limit_rate": 0.0,
            "sample_count": 24,
            "source": "test",
        },
    }

    verifier = {
        ("openai", "gpt-5.4"): {"sample_count": 18, "success_rate": 0.74},
        ("openai", "gpt-5.4-pro"): {"sample_count": 24, "success_rate": 0.98},
    }

    flags = {
        "shadow": False,
        "active": True,
        "force_legacy": False,
        "allow_provider_switch": False,
        "allow_model_switch_within_provider": True,
        "require_min_samples": 5,
        "lookback_hours": 24,
        "stale_after_hours": 24,
        "include_warm_state": False,
        "canary_percent": 100.0,
        "require_eval_pass": True,
        "min_eval_score": 0.85,
        "min_verifier_pass_rate": 0.85,
        "max_cost_ratio": 0.0,
        "max_p95_latency_ms": 0,
    }

    def load_snapshot(_session, provider, model):
        return snapshots.get((provider, model))

    def load_verifier(_session, provider, model, *, lookback_hours):
        return verifier.get((provider, model), {"sample_count": 0, "success_rate": None})

    with patch("brain.systems.routing.marketplace.UnitOfWork", return_value=mock_uow), \
         patch("brain.systems.routing.marketplace.get_routing_marketplace_flags", return_value=flags), \
         patch("brain.systems.routing.marketplace._maybe_refresh_health_snapshots"), \
         patch("brain.systems.routing.marketplace.persist_routing_decision", side_effect=lambda _session, decision: decision), \
         patch("brain.systems.routing.marketplace.resolve_provider_selection", return_value=ProviderResolution(provider="openai", source="fallback", explicit=False)), \
         patch("brain.systems.routing.marketplace.resolve_skill_routing_profile", return_value=SkillRoutingProfile(
             skill_name="develop",
             reasoning_effort="high",
             model_tier="high",
             thinking_tier="high",
         )), \
         patch("brain.systems.routing.marketplace.resolve_skill_runtime", return_value=SkillRuntimeConfig(
             provider="openai",
             model_name="gpt-5.4",
             reasoning_effort="medium",
             model_tier="medium",
             thinking_tier="medium",
         )), \
         patch("brain.systems.routing.marketplace.get_provider_model_map", side_effect=lambda provider, **kwargs: {
             "anthropic": {"high": "claude-opus-4-6", "medium": "claude-sonnet-4-6"},
             "openai": {"high": "gpt-5.4-pro", "medium": "gpt-5.4"},
         }[provider]), \
         patch("brain.systems.routing.marketplace._load_latest_health_snapshot", side_effect=load_snapshot), \
         patch("brain.systems.routing.marketplace._load_verifier_evidence", side_effect=load_verifier), \
         patch("brain.systems.services.runtime_introspection.get_provider_auth_status", side_effect=lambda **kwargs: {"authenticated": kwargs.get("provider") == "openai"}):
        decision = resolve_marketplace_routing(
            task_family="develop",
            lane="worker",
            skill_name="develop",
            user_id="user-1",
            org_id="org-1",
            run_id=11,
            legacy_provider="openai",
            legacy_model="openai/gpt-5.4",
            legacy_reasoning_effort="medium",
            genome_signals={"routing_eval_passed": True, "routing_eval_score": 0.93},
        )

    assert decision.decision_mode == "active"
    assert decision.fallback_used is False
    assert decision.selected_provider == "openai"
    assert decision.selected_model == "gpt-5.4-pro"
    assert decision.inputs["route_summary"]["selected"]["evidence_strength"] == "strong"
    assert decision.inputs["route_summary"]["fallback_reason"] is None
    assert decision.inputs["route_summary"]["canary"]["allocation"]["allocated"] is True
    assert decision.inputs["route_summary"]["canary"]["eval_gate"]["ok"] is True


def test_active_canary_requires_eval_gate_before_switching_models():
    from brain.platform.providers.model_policy import ProviderResolution, SkillRoutingProfile, SkillRuntimeConfig
    from brain.systems.routing.marketplace import resolve_marketplace_routing

    session = MagicMock()
    session.execute.return_value.mappings.return_value.all.return_value = []
    session.scalars.return_value.all.return_value = []
    mock_uow = _make_uow(session)

    snapshots = {
        ("openai", "gpt-5.4"): {
            "provider": "openai",
            "model": "gpt-5.4",
            "window_start": datetime.now(timezone.utc),
            "window_end": datetime.now(timezone.utc),
            "p50_latency_ms": 1600,
            "p95_latency_ms": 1900,
            "error_rate": 0.08,
            "auth_fail_rate": 0.0,
            "rate_limit_rate": 0.0,
            "sample_count": 18,
            "source": "test",
        },
        ("openai", "gpt-5.4-pro"): {
            "provider": "openai",
            "model": "gpt-5.4-pro",
            "window_start": datetime.now(timezone.utc),
            "window_end": datetime.now(timezone.utc),
            "p50_latency_ms": 900,
            "p95_latency_ms": 1100,
            "error_rate": 0.01,
            "auth_fail_rate": 0.0,
            "rate_limit_rate": 0.0,
            "sample_count": 24,
            "source": "test",
        },
    }

    flags = {
        "shadow": False,
        "active": True,
        "force_legacy": False,
        "allow_provider_switch": False,
        "allow_model_switch_within_provider": True,
        "require_min_samples": 5,
        "lookback_hours": 24,
        "stale_after_hours": 24,
        "include_warm_state": False,
        "canary_percent": 100.0,
        "require_eval_pass": True,
        "min_eval_score": 0.85,
        "min_verifier_pass_rate": 0.85,
        "max_cost_ratio": 0.0,
        "max_p95_latency_ms": 0,
    }

    with patch("brain.systems.routing.marketplace.UnitOfWork", return_value=mock_uow), \
         patch("brain.systems.routing.marketplace.get_routing_marketplace_flags", return_value=flags), \
         patch("brain.systems.routing.marketplace._maybe_refresh_health_snapshots"), \
         patch("brain.systems.routing.marketplace.persist_routing_decision", side_effect=lambda _session, decision: decision), \
         patch("brain.systems.routing.marketplace.resolve_provider_selection", return_value=ProviderResolution(provider="openai", source="fallback", explicit=False)), \
         patch("brain.systems.routing.marketplace.resolve_skill_routing_profile", return_value=SkillRoutingProfile(
             skill_name="develop",
             reasoning_effort="high",
             model_tier="high",
             thinking_tier="high",
         )), \
         patch("brain.systems.routing.marketplace.resolve_skill_runtime", return_value=SkillRuntimeConfig(
             provider="openai",
             model_name="gpt-5.4",
             reasoning_effort="medium",
             model_tier="medium",
             thinking_tier="medium",
         )), \
         patch("brain.systems.routing.marketplace.get_provider_model_map", side_effect=lambda provider, **kwargs: {
             "openai": {"high": "gpt-5.4-pro", "medium": "gpt-5.4"},
             "anthropic": {"high": "claude-opus-4-6", "medium": "claude-sonnet-4-6"},
         }[provider]), \
         patch("brain.systems.routing.marketplace._load_latest_health_snapshot", side_effect=lambda _session, provider, model: snapshots.get((provider, model))), \
         patch("brain.systems.routing.marketplace._load_verifier_evidence", return_value={"sample_count": 24, "success_rate": 0.98}), \
         patch("brain.systems.services.runtime_introspection.get_provider_auth_status", side_effect=lambda **kwargs: {"authenticated": kwargs.get("provider") == "openai"}):
        decision = resolve_marketplace_routing(
            task_family="develop",
            lane="worker",
            skill_name="develop",
            user_id="user-1",
            org_id="org-1",
            run_id=13,
            legacy_provider="openai",
            legacy_model="openai/gpt-5.4",
            legacy_reasoning_effort="medium",
            genome_signals={"routing_eval_passed": False, "routing_eval_score": 0.4},
        )

    assert decision.decision_mode == "active"
    assert decision.fallback_used is True
    assert decision.selected_model == "gpt-5.4"
    assert decision.inputs["route_summary"]["fallback_reason"] == "eval_gate_failed"
    assert decision.inputs["route_summary"]["canary"]["eval_gate"]["ok"] is False


def test_active_within_provider_canary_falls_back_with_clear_reason_when_sparse():
    from brain.platform.providers.model_policy import ProviderResolution, SkillRoutingProfile, SkillRuntimeConfig
    from brain.systems.routing.marketplace import resolve_marketplace_routing

    session = MagicMock()
    session.execute.return_value.mappings.return_value.all.return_value = []
    session.scalars.return_value.all.return_value = []
    mock_uow = _make_uow(session)

    flags = {
        "shadow": False,
        "active": True,
        "force_legacy": False,
        "allow_provider_switch": False,
        "allow_model_switch_within_provider": True,
        "require_min_samples": 5,
        "lookback_hours": 24,
        "stale_after_hours": 24,
        "include_warm_state": False,
    }

    with patch("brain.systems.routing.marketplace.UnitOfWork", return_value=mock_uow), \
         patch("brain.systems.routing.marketplace.get_routing_marketplace_flags", return_value=flags), \
         patch("brain.systems.routing.marketplace._maybe_refresh_health_snapshots"), \
         patch("brain.systems.routing.marketplace.persist_routing_decision", side_effect=lambda _session, decision: decision), \
         patch("brain.systems.routing.marketplace.resolve_provider_selection", return_value=ProviderResolution(provider="openai", source="fallback", explicit=False)), \
         patch("brain.systems.routing.marketplace.resolve_skill_routing_profile", return_value=SkillRoutingProfile(
             skill_name="develop",
             reasoning_effort="high",
             model_tier="high",
             thinking_tier="high",
         )), \
         patch("brain.systems.routing.marketplace.resolve_skill_runtime", return_value=SkillRuntimeConfig(
             provider="openai",
             model_name="gpt-5.4",
             reasoning_effort="medium",
             model_tier="medium",
             thinking_tier="medium",
         )), \
         patch("brain.systems.routing.marketplace.get_provider_model_map", side_effect=lambda provider, **kwargs: {
             "anthropic": {"high": "claude-opus-4-6", "medium": "claude-sonnet-4-6"},
             "openai": {"high": "gpt-5.4-pro", "medium": "gpt-5.4"},
         }[provider]), \
         patch("brain.systems.routing.marketplace._load_latest_health_snapshot", return_value=None), \
         patch("brain.systems.routing.marketplace._load_verifier_evidence", return_value={"sample_count": 0, "success_rate": None}), \
         patch("brain.systems.services.runtime_introspection.get_provider_auth_status", side_effect=lambda **kwargs: {"authenticated": kwargs.get("provider") == "openai"}):
        decision = resolve_marketplace_routing(
            task_family="develop",
            lane="worker",
            skill_name="develop",
            user_id="user-1",
            org_id="org-1",
            run_id=12,
            legacy_provider="openai",
            legacy_model="openai/gpt-5.4",
            legacy_reasoning_effort="medium",
        )

    assert decision.decision_mode == "active"
    assert decision.fallback_used is True
    assert decision.selected_provider == "openai"
    assert decision.selected_model == "gpt-5.4"
    assert decision.inputs["route_summary"]["fallback_reason"] == "no_eligible_candidates"
    assert decision.constraints["fallback_reason"] == "no_eligible_candidates"
    assert all(candidate["eligible"] is False for candidate in decision.candidate_scores)


def test_scoring_falls_back_to_legacy_when_evidence_is_sparse():
    from brain.platform.providers.model_policy import ProviderResolution, SkillRoutingProfile, SkillRuntimeConfig
    from brain.systems.routing.marketplace import resolve_marketplace_routing

    session = MagicMock()
    session.execute.return_value.mappings.return_value.all.return_value = []
    session.scalars.return_value.all.return_value = []
    mock_uow = _make_uow(session)

    with patch("brain.systems.routing.marketplace.UnitOfWork", return_value=mock_uow), \
         patch("brain.systems.routing.marketplace.persist_routing_decision", side_effect=lambda _session, decision: decision), \
         patch("brain.systems.routing.marketplace.resolve_provider_selection", return_value=ProviderResolution(provider="openai", source="fallback", explicit=False)), \
         patch("brain.systems.routing.marketplace.resolve_skill_routing_profile", return_value=SkillRoutingProfile(
             skill_name="coordinate",
             reasoning_effort=None,
             model_tier="medium",
             thinking_tier="medium",
         )), \
         patch("brain.systems.routing.marketplace.resolve_skill_runtime", return_value=SkillRuntimeConfig(
             provider="openai",
             model_name="gpt-5.4",
             reasoning_effort="medium",
             model_tier="medium",
             thinking_tier="medium",
         )), \
         patch("brain.systems.routing.marketplace.get_provider_model_map", side_effect=lambda provider, **kwargs: {
             "anthropic": {"medium": "claude-sonnet-4-6"},
             "openai": {"medium": "gpt-5.4"},
         }[provider]), \
         patch("brain.systems.routing.marketplace._load_latest_health_snapshot", return_value=None), \
         patch("brain.systems.routing.marketplace._load_verifier_evidence", return_value={"sample_count": 0, "success_rate": None}), \
         patch("brain.systems.services.runtime_introspection.get_provider_auth_status", return_value={"authenticated": True}):
        decision = resolve_marketplace_routing(
            task_family="coordinate",
            lane="coordinator",
            skill_name="coordinate",
            user_id="user-1",
            org_id="org-1",
            run_id=8,
            legacy_provider="openai",
            legacy_model="openai/gpt-5.4",
            legacy_reasoning_effort="medium",
        )

    assert decision.fallback_used is True
    assert decision.decision_mode == "shadow"
    assert decision.selected_provider == "openai"
    assert decision.selected_model == "gpt-5.4"
    assert all(candidate["eligible"] is False for candidate in decision.candidate_scores)


def test_routing_decision_logging_persists_row():
    from brain.systems.routing.marketplace import RoutingDecisionResult, persist_routing_decision

    added_rows = []
    session = MagicMock()
    session.execute.return_value.scalar_one_or_none.return_value = None

    def _add(row):
        added_rows.append(row)

    def _flush():
        if added_rows:
            added_rows[-1].id = 123

    session.add.side_effect = _add
    session.flush.side_effect = _flush

    decision = RoutingDecisionResult(
        run_id=99,
        task_family="coordinate",
        lane="coordinator",
        decision_mode="shadow",
        selected_provider="openai",
        selected_model="gpt-5.4",
        selected_reasoning_effort="medium",
        legacy_provider="openai",
        legacy_model="openai/gpt-5.4",
        legacy_reasoning_effort="medium",
        inputs={"task_family": "coordinate"},
        candidate_scores=[{"provider": "openai", "model": "gpt-5.4", "score": 0.9}],
        constraints={"provider_resolution": {"provider": "openai"}},
        experiment_id=None,
        applied=False,
        fallback_used=False,
    )

    result = persist_routing_decision(session, decision)

    assert session.add.called is True
    assert session.flush.called is True
    assert added_rows[0].selected_model == "gpt-5.4"
    assert result.decision_id == 123


def test_routing_marketplace_snapshot_exposes_fallback_reason():
    from types import SimpleNamespace

    from brain.systems.routing.marketplace import get_routing_marketplace_snapshot

    health_row = SimpleNamespace(
        provider="openai",
        model="gpt-5.4",
        window_start=datetime.now(timezone.utc),
        window_end=datetime.now(timezone.utc),
        p50_latency_ms=1200,
        p95_latency_ms=1800,
        error_rate=0.1,
        auth_fail_rate=0.0,
        rate_limit_rate=0.0,
        sample_count=14,
        source="test",
    )
    decision_row = SimpleNamespace(
        run_id=1,
        task_family="develop",
        lane="worker",
        decision_mode="active",
        selected_provider="openai",
        selected_model="gpt-5.4",
        selected_reasoning_effort="medium",
        applied=True,
        fallback_used=True,
        created_at=datetime.now(timezone.utc),
        inputs={
            "route_summary": {
                "fallback_reason": "canary_evidence_not_strong",
                "legacy": {"score": 0.51},
                "selected": {"score": 0.49},
                "candidate_count": 2,
                "eligible_candidate_count": 1,
            }
        },
        constraints={
            "fallback_reason": "canary_evidence_not_strong",
            "route_summary": {
                "fallback_reason": "canary_evidence_not_strong",
                "legacy": {"score": 0.51},
                "selected": {"score": 0.49},
                "candidate_count": 2,
                "eligible_candidate_count": 1,
            },
        },
    )

    def _result(rows):
        result = MagicMock()
        result.scalars.return_value.all.return_value = rows
        return result

    session = MagicMock()
    session.execute.side_effect = [_result([health_row]), _result([decision_row])]
    mock_uow = _make_uow(session)

    with patch("brain.systems.routing.marketplace.UnitOfWork", return_value=mock_uow):
        snapshot = get_routing_marketplace_snapshot(user_id="user-1", org_id="org-1", provider="openai")

    assert snapshot["healthy"] is True
    assert snapshot["latest_decisions"][0]["fallback_reason"] == "canary_evidence_not_strong"
    assert snapshot["latest_decisions"][0]["selected_over_legacy_delta"] == -0.02
