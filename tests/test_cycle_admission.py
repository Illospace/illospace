from __future__ import annotations

from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from brain.platform.integrations.codex_usage import CodexKnownUsage
from brain.platform.integrations.provider_auth_preflight import (
    ProviderAuthBlockedPreflightResult,
    ProviderAuthPassedPreflightResult,
)
from brain.platform.integrations.provider_quota_preflight import (
    ProviderQuotaBlockedPreflightResult,
    ProviderQuotaDeferredPreflightResult,
    ProviderQuotaPassedPreflightResult,
    ProviderQuotaThresholds,
)
from brain.systems.cycles import admission


def _cycle(**overrides):
    values = {
        "user_id": "user-1",
        "org_id": "org-1",
        "model_override": "openai/gpt-5.5",
        "thinking_override": "high",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _usage(used_percent: float) -> CodexKnownUsage:
    return CodexKnownUsage(
        used_percent=used_percent,
        observed_at="2026-08-04T13:24:45Z",
        source_path="/tmp/codex/sessions/rollout.jsonl",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("raw_model", [None, "", "default", "DEFAULT"])
async def test_cycle_route_resolves_default_model_once(monkeypatch, raw_model):
    calls = []

    async def default_model(session, **kwargs):
        calls.append((session, kwargs))
        return "openai/gpt-5.6-sol"

    monkeypatch.setattr(admission, "async_get_default_model", default_model)
    session = object()
    run = SimpleNamespace(
        context_snapshot={
            "revision": {
                "model_override": raw_model,
                "thinking_override": None,
            }
        }
    )

    route = await admission.async_resolve_cycle_provider_route(
        session,
        cycle=_cycle(),
        run=run,
    )

    assert calls == [
        (
            session,
            {
                "include_provider_prefix": True,
                "user_id": "user-1",
                "org_id": "org-1",
            },
        )
    ]
    assert route.model == "openai/gpt-5.6-sol"
    assert route.provider == "openai"
    assert route.work_intake_model_policy == {"model": "openai/gpt-5.6-sol"}


@pytest.mark.asyncio
async def test_cycle_route_uses_bound_revision_instead_of_live_cycle():
    route = await admission.async_resolve_cycle_provider_route(
        object(),
        cycle=_cycle(
            model_override="anthropic/claude-opus-5",
            thinking_override="low",
        ),
        run=SimpleNamespace(
            context_snapshot={
                "revision": {
                    "model_override": "gpt-5.6-luna",
                    "thinking_override": "xhigh",
                }
            }
        ),
    )

    assert route.model == "openai/gpt-5.6-luna"
    assert route.provider == "openai"
    assert route.work_intake_model_policy == {
        "model": "openai/gpt-5.6-luna",
        "thinking": "xhigh",
    }


@pytest.mark.asyncio
async def test_cycle_route_falls_back_to_live_cycle_without_snapshot():
    route = await admission.async_resolve_cycle_provider_route(
        object(),
        cycle=_cycle(model_override="openai/gpt-5.6-luna", thinking_override="low"),
        run=SimpleNamespace(context_snapshot=None),
    )

    assert route.work_intake_model_policy == {
        "model": "openai/gpt-5.6-luna",
        "thinking": "low",
    }


@pytest.mark.asyncio
async def test_cycle_admission_derives_one_route_shared_by_both_preflights(monkeypatch):
    default_calls = []
    auth_routes = []
    quota_routes = []

    async def default_model(_session, **kwargs):
        default_calls.append(kwargs)
        return "openai/gpt-5.6-sol"

    async def auth_preflight(_session, *, route):
        auth_routes.append(route)
        return ProviderAuthPassedPreflightResult(
            provider=route.provider,
            model=route.model,
        )

    def quota_preflight(*, route, run):
        quota_routes.append((route, run))
        return ProviderQuotaPassedPreflightResult(
            provider=route.provider,
            model=route.model,
            usage=_usage(10.0),
            thresholds=ProviderQuotaThresholds(soft_percent=75.0, hard_percent=90.0),
            explicit_request=False,
        )

    monkeypatch.setattr(admission, "async_get_default_model", default_model)
    monkeypatch.setattr(admission, "async_preflight_cycle_external_auth", auth_preflight)
    monkeypatch.setattr(admission, "preflight_cycle_external_quota", quota_preflight)
    run = SimpleNamespace(
        context_snapshot={
            "revision": {"model_override": None, "thinking_override": "low"},
            "launch_context": {"origin": "scheduled_cycle"},
        }
    )

    outcome = await admission.async_prepare_cycle_run_admission(
        object(),
        cycle=_cycle(),
        run=run,
    )

    assert isinstance(outcome, admission.CycleAdmissionAdmitted)
    assert len(default_calls) == 1
    assert auth_routes == [outcome.route]
    assert quota_routes == [(outcome.route, run)]
    assert auth_routes[0] is quota_routes[0][0] is outcome.route
    assert outcome.route.work_intake_model_policy == {
        "model": "openai/gpt-5.6-sol",
        "thinking": "low",
    }
    assert run.context_snapshot["auth_preflight"]["status"] == "passed"
    assert run.context_snapshot["quota_preflight"]["decision"] == "admitted"


def test_cycle_route_enforces_and_derives_its_invariants():
    route = admission.CycleProviderRoute(
        user_id="user-1",
        org_id="org-1",
        model="openai/gpt-5.6-sol",
        thinking="high",
    )

    policy = route.work_intake_model_policy
    policy["model"] = "anthropic/claude-opus-5"

    assert route.provider == "openai"
    assert route.work_intake_model_policy == {
        "model": "openai/gpt-5.6-sol",
        "thinking": "high",
    }
    assert not hasattr(route, "__dict__")
    with pytest.raises(FrozenInstanceError):
        route.model = "anthropic/claude-opus-5"


@pytest.mark.parametrize(
    ("model", "thinking"),
    [
        ("gpt-5.6-sol", None),
        ("openai/gpt-5.6-sol", "ultra"),
    ],
)
def test_cycle_route_rejects_noncanonical_construction(model, thinking):
    with pytest.raises(ValueError):
        admission.CycleProviderRoute(
            user_id="user-1",
            org_id="org-1",
            model=model,
            thinking=thinking,
        )


def test_cycle_rejection_union_rejects_contradictory_construction():
    auth = ProviderAuthBlockedPreflightResult(
        provider="openai",
        model="openai/gpt-5.5",
        credential="OpenAI Codex / ChatGPT",
        error_code="provider_credential_unavailable",
        visible_message="Reconnect OpenAI.",
    )

    with pytest.raises(TypeError):
        admission.CycleAdmissionRejected(
            status="auth_blocked",
            error="Reconnect OpenAI.",
            skip_reason="quota_soft_limit",
            notice_kind="quota",
            notice=auth,
        )


@pytest.mark.parametrize(
    "rejection_type",
    [
        admission.CycleAdmissionAuthBlocked,
        admission.CycleAdmissionQuotaBlocked,
        admission.CycleAdmissionQuotaDeferred,
    ],
)
def test_cycle_rejection_variants_reject_independent_settlement_fields(rejection_type):
    with pytest.raises(TypeError):
        rejection_type(notice=object(), status="auth_blocked")


@pytest.mark.asyncio
async def test_cycle_admission_returns_complete_auth_rejection(monkeypatch):
    auth = ProviderAuthBlockedPreflightResult(
        provider="openai",
        model="openai/gpt-5.5",
        credential="OpenAI Codex / ChatGPT",
        error_code="provider_credential_unavailable",
        visible_message="Reconnect OpenAI.",
    )

    async def auth_preflight(_session, *, route):
        return auth

    def unexpected_quota(**_kwargs):
        raise AssertionError("quota must not run after an auth rejection")

    monkeypatch.setattr(admission, "async_preflight_cycle_external_auth", auth_preflight)
    monkeypatch.setattr(admission, "preflight_cycle_external_quota", unexpected_quota)
    run = SimpleNamespace(context_snapshot={})

    outcome = await admission.async_prepare_cycle_run_admission(
        object(),
        cycle=_cycle(),
        run=run,
    )

    assert isinstance(outcome, admission.CycleAdmissionAuthBlocked)
    assert outcome.notice is auth
    assert run.context_snapshot["auth_preflight"]["status"] == "auth_blocked"
    assert "quota_preflight" not in run.context_snapshot


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("quota_type", "decision", "message", "expected_type"),
    [
        (
            ProviderQuotaBlockedPreflightResult,
            "blocked",
            "Quota is blocked.",
            admission.CycleAdmissionQuotaBlocked,
        ),
        (
            ProviderQuotaDeferredPreflightResult,
            "deferred",
            "Quota is deferred.",
            admission.CycleAdmissionQuotaDeferred,
        ),
    ],
)
async def test_cycle_admission_returns_complete_quota_rejection(
    monkeypatch,
    quota_type,
    decision,
    message,
    expected_type,
):
    quota = quota_type(
        provider="openai",
        model="openai/gpt-5.5",
        usage=_usage(90.0),
        thresholds=ProviderQuotaThresholds(soft_percent=75.0, hard_percent=90.0),
        visible_message=message,
    )

    async def auth_preflight(_session, *, route):
        return ProviderAuthPassedPreflightResult(
            provider=route.provider,
            model=route.model,
        )

    def quota_preflight(*, route, run):
        return quota

    monkeypatch.setattr(admission, "async_preflight_cycle_external_auth", auth_preflight)
    monkeypatch.setattr(admission, "preflight_cycle_external_quota", quota_preflight)
    run = SimpleNamespace(context_snapshot={})

    outcome = await admission.async_prepare_cycle_run_admission(
        object(),
        cycle=_cycle(),
        run=run,
    )

    assert isinstance(outcome, expected_type)
    assert outcome.notice is quota
    assert run.context_snapshot["quota_preflight"]["decision"] == decision
