from __future__ import annotations

from types import SimpleNamespace

import pytest

from brain.platform.integrations.provider_auth_preflight import (
    ProviderAuthPreflightResult,
)
from brain.platform.integrations.provider_quota_preflight import (
    ProviderQuotaPreflightResult,
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
    assert route.model_policy == {"model": "openai/gpt-5.6-sol"}


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
    assert route.model_policy == {
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

    assert route.model_policy == {
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
        return ProviderAuthPreflightResult(
            status="passed",
            provider=route.provider,
            model=route.model,
        )

    async def quota_preflight(_session, *, route, run):
        quota_routes.append((route, run))
        return ProviderQuotaPreflightResult(
            status="passed",
            decision="admitted",
            provider=route.provider,
            model=route.model,
            usage_status="ok",
            used_percent=10.0,
            thresholds=ProviderQuotaThresholds(soft_percent=75.0, hard_percent=90.0),
            explicit_request=False,
        )

    monkeypatch.setattr(admission, "async_get_default_model", default_model)
    monkeypatch.setattr(admission, "async_preflight_cycle_external_auth", auth_preflight)
    monkeypatch.setattr(admission, "async_preflight_cycle_external_quota", quota_preflight)
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

    assert len(default_calls) == 1
    assert auth_routes == [outcome.route]
    assert quota_routes == [(outcome.route, run)]
    assert auth_routes[0] is quota_routes[0][0] is outcome.route
    assert outcome.route.model_policy == {
        "model": "openai/gpt-5.6-sol",
        "thinking": "low",
    }
    assert run.context_snapshot["auth_preflight"]["status"] == "passed"
    assert run.context_snapshot["quota_preflight"]["decision"] == "admitted"
