from __future__ import annotations

import json

from brain.platform.integrations.codex_usage import CodexKnownUsage, CodexUsageReading
from brain.platform.integrations import provider_quota_preflight


def _usage(used_percent: float) -> CodexUsageReading:
    return CodexUsageReading(
        status="exhausted" if used_percent >= 100 else "ok",
        used_percent=used_percent,
        observed_at="2026-08-04T13:24:45Z",
        source_path="/tmp/codex/sessions/2026/08/04/rollout.jsonl",
        limit_id="codex",
        plan_type="pro",
    )


def _probe(*, explicit_request=False):
    return provider_quota_preflight.probe_provider_quota(
        provider="openai",
        model="openai/gpt-5.6-sol",
        explicit_request=explicit_request,
    )


def test_below_soft_limit_is_admitted(monkeypatch):
    monkeypatch.setattr(provider_quota_preflight, "read_codex_usage", lambda: _usage(74.9))

    result = _probe()

    assert result.status == "passed"
    assert result.decision == "admitted"
    assert result.used_percent == 74.9
    assert result.thresholds.to_dict() == {
        "soft_percent": 75.0,
        "hard_percent": 90.0,
    }


def test_soft_limit_defers_scheduled_run_but_admits_explicit_run(monkeypatch):
    monkeypatch.setattr(provider_quota_preflight, "read_codex_usage", lambda: _usage(80))

    scheduled = _probe(explicit_request=False)
    explicit = _probe(explicit_request=True)

    assert scheduled.status == "quota_deferred"
    assert scheduled.decision == "deferred"
    assert scheduled.deferred is True
    assert explicit.status == "passed"
    assert explicit.decision == "admitted"


def test_hard_limit_blocks_even_explicit_run(monkeypatch):
    monkeypatch.setattr(provider_quota_preflight, "read_codex_usage", lambda: _usage(90))

    result = _probe(explicit_request=True)

    assert result.status == "quota_blocked"
    assert result.decision == "blocked"
    assert result.blocked is True
    assert result.used_percent == 90.0


def test_real_exhausted_reading_blocks(monkeypatch):
    monkeypatch.setattr(provider_quota_preflight, "read_codex_usage", lambda: _usage(100))

    result = _probe()

    assert result.usage_status == "exhausted"
    assert result.decision == "blocked"
    assert result.used_percent == 100.0


def test_unknown_reading_fails_open_and_preserves_last_known_good(monkeypatch):
    reading = CodexUsageReading(
        status="unknown",
        reason="primary_missing",
        observed_at="2026-08-04T13:28:13Z",
        source_path="/tmp/codex/sessions/2026/08/04/rollout.jsonl",
        limit_id="codex",
        last_known_good=CodexKnownUsage(
            used_percent=31,
            observed_at="2026-08-04T13:24:45Z",
            source_path="/tmp/codex/sessions/2026/08/04/rollout.jsonl",
            plan_type="pro",
        ),
    )
    monkeypatch.setattr(provider_quota_preflight, "read_codex_usage", lambda: reading)

    result = _probe()

    assert result.status == "unknown"
    assert result.decision == "admitted"
    assert result.used_percent is None
    assert result.unknown_reason == "primary_missing"
    assert result.last_known_good == {
        "used_percent": 31,
        "observed_at": "2026-08-04T13:24:45Z",
        "source_path": "/tmp/codex/sessions/2026/08/04/rollout.jsonl",
        "limit_id": "codex",
        "plan_type": "pro",
    }


def test_thresholds_are_configurable(monkeypatch):
    monkeypatch.setenv("ILLO_CODEX_QUOTA_SOFT_PERCENT", "60")
    monkeypatch.setenv("ILLO_CODEX_QUOTA_HARD_PERCENT", "70")
    monkeypatch.setattr(provider_quota_preflight, "read_codex_usage", lambda: _usage(65))

    result = _probe()

    assert result.decision == "deferred"
    assert result.thresholds.to_dict() == {
        "soft_percent": 60.0,
        "hard_percent": 70.0,
    }


def test_live_reader_recovers_automatically_after_window_reset(tmp_path, monkeypatch):
    session_file = tmp_path / "sessions" / "2026" / "08" / "04" / "rollout.jsonl"
    session_file.parent.mkdir(parents=True)

    def write_usage(used_percent):
        session_file.write_text(
            json.dumps(
                {
                    "timestamp": "2026-08-04T13:24:45Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "rate_limits": {
                            "limit_id": "codex",
                            "primary": {"used_percent": used_percent},
                            "plan_type": "pro",
                        },
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )

    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    write_usage(95)

    blocked = _probe()
    write_usage(3)
    recovered = _probe()

    assert blocked.decision == "blocked"
    assert recovered.decision == "admitted"
    assert recovered.used_percent == 3.0


def test_non_subscription_route_skips_codex_quota_reader(monkeypatch):
    monkeypatch.setattr(
        provider_quota_preflight,
        "read_codex_usage",
        lambda: (_ for _ in ()).throw(AssertionError("reader should not run")),
    )

    result = provider_quota_preflight.probe_provider_quota(
        provider="anthropic",
        model="anthropic/claude-sonnet-4-6",
        explicit_request=False,
    )

    assert result.status == "skipped"
    assert result.decision == "admitted"
    assert result.usage_status is None
