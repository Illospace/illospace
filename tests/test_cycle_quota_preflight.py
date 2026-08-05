from __future__ import annotations

from types import SimpleNamespace

from brain.platform.integrations.codex_usage import (
    CodexKnownUsageReading,
    CodexUnknownUsageReading,
    CodexUsageUnknownReason,
)
from brain.platform.integrations.provider_quota_preflight import (
    ProviderQuotaDeferredPreflightResult,
    ProviderQuotaPassedPreflightResult,
    ProviderQuotaThresholds,
    ProviderQuotaUnknownPreflightResult,
)
from brain.systems.cycles.admission import CycleProviderRoute
from brain.systems.cycles import quota_preflight


def _route(model: str = "openai/gpt-5.6-sol") -> CycleProviderRoute:
    return CycleProviderRoute(
        user_id="user-1",
        org_id="org-1",
        model=model,
    )


def _usage(used_percent: float) -> CodexKnownUsageReading:
    return CodexKnownUsageReading(
        used_percent=used_percent,
        observed_at="2026-08-04T13:24:45Z",
        source_path="/tmp/codex/sessions/rollout.jsonl",
    )


def test_cycle_quota_marks_scheduled_origin_as_autonomous(monkeypatch):
    captured = {}

    def probe(**kwargs):
        captured.update(kwargs)
        return ProviderQuotaDeferredPreflightResult(
            provider=kwargs["provider"],
            model=kwargs["model"],
            usage=_usage(80),
            thresholds=ProviderQuotaThresholds(soft_percent=75, hard_percent=90),
        )

    monkeypatch.setattr(quota_preflight, "probe_provider_quota", probe)
    result = quota_preflight.preflight_cycle_external_quota(
        route=_route(),
        run=SimpleNamespace(
            context_snapshot={"launch_context": {"origin": "scheduled_cycle"}}
        ),
    )

    assert captured["explicit_request"] is False
    assert result.deferred is True
    assert "80%" in result.visible_message
    assert "75% soft limit" in result.visible_message


def test_cycle_quota_marks_manual_origin_as_explicit(monkeypatch):
    captured = {}

    def probe(**kwargs):
        captured.update(kwargs)
        return ProviderQuotaPassedPreflightResult(
            provider=kwargs["provider"],
            model=kwargs["model"],
            usage=_usage(80),
            thresholds=ProviderQuotaThresholds(soft_percent=75, hard_percent=90),
            explicit_request=kwargs["explicit_request"],
        )

    monkeypatch.setattr(quota_preflight, "probe_provider_quota", probe)
    result = quota_preflight.preflight_cycle_external_quota(
        route=_route(),
        run=SimpleNamespace(
            context_snapshot={"launch_context": {"origin": "manual_cycle"}}
        ),
    )

    assert captured["explicit_request"] is True
    assert result.decision == "admitted"
    assert result.visible_message is None


def test_cycle_quota_consumes_resolved_route(monkeypatch):
    captured = {}

    def probe(**kwargs):
        captured["probe"] = kwargs
        return ProviderQuotaUnknownPreflightResult(
            provider=kwargs["provider"],
            model=kwargs["model"],
            usage=CodexUnknownUsageReading(
                reason=CodexUsageUnknownReason.SESSIONS_DIR_MISSING,
            ),
            thresholds=ProviderQuotaThresholds(soft_percent=75, hard_percent=90),
        )

    monkeypatch.setattr(quota_preflight, "probe_provider_quota", probe)
    result = quota_preflight.preflight_cycle_external_quota(
        route=_route(),
        run=SimpleNamespace(context_snapshot={}),
    )

    assert captured["probe"]["model"] == "openai/gpt-5.6-sol"
    assert result.status == "unknown"
    assert result.decision == "admitted"
