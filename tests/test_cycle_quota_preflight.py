from __future__ import annotations

from types import SimpleNamespace

from brain.platform.integrations.provider_quota_preflight import (
    ProviderQuotaPreflightResult,
    ProviderQuotaThresholds,
)
from brain.systems.cycles import quota_preflight


async def test_cycle_quota_marks_scheduled_origin_as_autonomous(monkeypatch):
    captured = {}

    def probe(**kwargs):
        captured.update(kwargs)
        return ProviderQuotaPreflightResult(
            status="quota_deferred",
            decision="deferred",
            provider=kwargs["provider"],
            model=kwargs["model"],
            usage_status="ok",
            used_percent=80,
            thresholds=ProviderQuotaThresholds(soft_percent=75, hard_percent=90),
        )

    monkeypatch.setattr(quota_preflight, "probe_provider_quota", probe)
    result = await quota_preflight.async_preflight_cycle_external_quota(
        object(),
        cycle=SimpleNamespace(
            user_id="user-1",
            org_id="org-1",
            model_override="openai/gpt-5.6-sol",
        ),
        run=SimpleNamespace(
            context_snapshot={"launch_context": {"origin": "scheduled_cycle"}}
        ),
    )

    assert captured["explicit_request"] is False
    assert result.deferred is True
    assert "80%" in result.visible_message
    assert "75% soft limit" in result.visible_message


async def test_cycle_quota_marks_manual_origin_as_explicit(monkeypatch):
    captured = {}

    def probe(**kwargs):
        captured.update(kwargs)
        return ProviderQuotaPreflightResult(
            status="passed",
            decision="admitted",
            provider=kwargs["provider"],
            model=kwargs["model"],
            usage_status="ok",
            used_percent=80,
            thresholds=ProviderQuotaThresholds(soft_percent=75, hard_percent=90),
            explicit_request=kwargs["explicit_request"],
        )

    monkeypatch.setattr(quota_preflight, "probe_provider_quota", probe)
    result = await quota_preflight.async_preflight_cycle_external_quota(
        object(),
        cycle=SimpleNamespace(
            user_id="user-1",
            org_id="org-1",
            model_override="openai/gpt-5.6-sol",
        ),
        run=SimpleNamespace(
            context_snapshot={"launch_context": {"origin": "manual_cycle"}}
        ),
    )

    assert captured["explicit_request"] is True
    assert result.decision == "admitted"
    assert result.visible_message is None


async def test_cycle_quota_resolves_default_model(monkeypatch):
    captured = {}

    async def default_model(_session, **kwargs):
        captured["default"] = kwargs
        return "openai/gpt-5.6-sol"

    def probe(**kwargs):
        captured["probe"] = kwargs
        return ProviderQuotaPreflightResult(
            status="unknown",
            decision="admitted",
            provider=kwargs["provider"],
            model=kwargs["model"],
            usage_status="unknown",
            unknown_reason="sessions_dir_missing",
            thresholds=ProviderQuotaThresholds(soft_percent=75, hard_percent=90),
        )

    monkeypatch.setattr(quota_preflight, "async_get_default_model", default_model)
    monkeypatch.setattr(quota_preflight, "probe_provider_quota", probe)
    result = await quota_preflight.async_preflight_cycle_external_quota(
        object(),
        cycle=SimpleNamespace(
            user_id="user-1",
            org_id="org-1",
            model_override=None,
        ),
        run=SimpleNamespace(context_snapshot={}),
    )

    assert captured["default"] == {
        "include_provider_prefix": True,
        "user_id": "user-1",
        "org_id": "org-1",
    }
    assert captured["probe"]["model"] == "openai/gpt-5.6-sol"
    assert result.status == "unknown"
    assert result.decision == "admitted"
