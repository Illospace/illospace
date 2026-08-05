from __future__ import annotations

import json
import os

import pytest

from brain.platform.integrations.codex_usage import (
    CodexKnownUsage,
    CodexUnknownUsageReading,
    CodexUsageUnknownReason,
    read_codex_usage,
)


def _event(
    used_percent=None,
    *,
    timestamp="2026-08-04T13:24:45Z",
    limit_id="codex",
    primary_marker=True,
    plan_type="pro",
):
    primary = {"used_percent": used_percent} if primary_marker else None
    return {
        "timestamp": timestamp,
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {},
            "rate_limits": {
                "limit_id": limit_id,
                "primary": primary,
                "secondary": None,
                "plan_type": plan_type,
            },
        },
    }


def _session_file(tmp_path, *, name="rollout.jsonl"):
    path = tmp_path / "sessions" / "2026" / "08" / "04" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _write_events(path, *events):
    path.write_text(
        "".join(json.dumps(event) + "\n" for event in events),
        encoding="utf-8",
    )


def test_reads_newest_real_codex_usage(tmp_path):
    path = _session_file(tmp_path)
    _write_events(path, _event(29), _event(31, timestamp="2026-08-04T13:24:46Z"))

    reading = read_codex_usage(tmp_path)

    assert isinstance(reading, CodexKnownUsage)
    assert reading.to_dict()["status"] == "ok"
    assert reading.used_percent == 31.0
    assert reading.observed_at == "2026-08-04T13:24:46Z"
    assert reading.limit_id == "codex"
    assert reading.plan_type == "pro"


def test_real_codex_usage_at_one_hundred_is_exhausted(tmp_path):
    path = _session_file(tmp_path)
    _write_events(path, _event(100))

    reading = read_codex_usage(tmp_path)

    assert isinstance(reading, CodexKnownUsage)
    assert reading.to_dict()["status"] == "exhausted"
    assert reading.used_percent == 100.0


def test_degenerate_premium_payload_is_unknown_with_last_known_good(tmp_path):
    path = _session_file(tmp_path)
    _write_events(
        path,
        _event(31, timestamp="2026-08-04T13:24:45Z"),
        _event(
            None,
            timestamp="2026-08-04T13:28:13Z",
            limit_id="premium",
            primary_marker=False,
            plan_type=None,
        ),
    )

    reading = read_codex_usage(tmp_path)

    assert isinstance(reading, CodexUnknownUsageReading)
    assert reading.reason == "unexpected_limit_id"
    assert reading.limit_id == "premium"
    assert reading.last_known_good is not None
    assert reading.last_known_good.used_percent == 31.0
    assert reading.last_known_good.observed_at == "2026-08-04T13:24:45Z"


def test_null_primary_on_codex_limit_is_unknown_not_zero_or_exhausted(tmp_path):
    path = _session_file(tmp_path)
    _write_events(path, _event(None, primary_marker=False))

    reading = read_codex_usage(tmp_path)

    assert isinstance(reading, CodexUnknownUsageReading)
    assert reading.reason == "primary_missing"
    assert reading.last_known_good is None


def test_malformed_newest_line_is_unknown_with_older_known_reading(tmp_path):
    path = _session_file(tmp_path)
    _write_events(path, _event(42))
    with path.open("a", encoding="utf-8") as handle:
        handle.write("{not valid json\n")

    reading = read_codex_usage(tmp_path)

    assert isinstance(reading, CodexUnknownUsageReading)
    assert reading.reason == "malformed_line"
    assert reading.last_known_good is not None
    assert reading.last_known_good.used_percent == 42.0


def test_newer_auth_error_is_unknown_with_older_known_reading(tmp_path):
    path = _session_file(tmp_path)
    _write_events(
        path,
        _event(52),
        {
            "timestamp": "2026-08-04T13:30:00Z",
            "type": "event_msg",
            "payload": {
                "type": "error",
                "error": {"code": "unauthorized", "status": 401},
            },
        },
    )

    reading = read_codex_usage(tmp_path)

    assert isinstance(reading, CodexUnknownUsageReading)
    assert reading.reason == "auth_error"
    assert reading.last_known_good is not None
    assert reading.last_known_good.used_percent == 52.0


def test_missing_sessions_directory_is_unknown(tmp_path):
    reading = read_codex_usage(tmp_path)

    assert isinstance(reading, CodexUnknownUsageReading)
    assert reading.reason == "sessions_dir_missing"


def test_only_newest_session_file_controls_current_verdict(tmp_path):
    older = _session_file(tmp_path, name="older.jsonl")
    newer = _session_file(tmp_path, name="newer.jsonl")
    _write_events(older, _event(12, timestamp="2026-08-04T12:00:00Z"))
    _write_events(newer, {"timestamp": "2026-08-04T13:00:00Z", "type": "response_item"})
    os.utime(older, (1, 1))
    os.utime(newer, (2, 2))

    reading = read_codex_usage(tmp_path)

    assert isinstance(reading, CodexUnknownUsageReading)
    assert reading.reason == "token_count_missing"
    assert reading.last_known_good is not None
    assert reading.last_known_good.used_percent == 12.0


def test_codex_home_environment_selects_usage_root(tmp_path, monkeypatch):
    path = _session_file(tmp_path)
    _write_events(path, _event(17))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))

    reading = read_codex_usage()
    assert isinstance(reading, CodexKnownUsage)
    assert reading.used_percent == 17.0


def test_usage_reading_serialization_is_byte_compatible():
    known = CodexKnownUsage(
        used_percent=31.0,
        observed_at="2026-08-04T13:24:45Z",
        source_path="/tmp/codex/sessions/rollout.jsonl",
        plan_type="pro",
    )
    unknown = CodexUnknownUsageReading(
        reason=CodexUsageUnknownReason.PRIMARY_MISSING,
        observed_at="2026-08-04T13:28:13Z",
        source_path="/tmp/codex/sessions/rollout.jsonl",
        limit_id="codex",
    )

    assert json.dumps(known.to_dict(), separators=(",", ":")).encode() == (
        b'{"status":"ok","used_percent":31.0,"reason":null,'
        b'"observed_at":"2026-08-04T13:24:45Z",'
        b'"source_path":"/tmp/codex/sessions/rollout.jsonl",'
        b'"limit_id":"codex","plan_type":"pro","last_known_good":null}'
    )
    assert json.dumps(unknown.to_dict(), separators=(",", ":")).encode() == (
        b'{"status":"unknown","used_percent":null,"reason":"primary_missing",'
        b'"observed_at":"2026-08-04T13:28:13Z",'
        b'"source_path":"/tmp/codex/sessions/rollout.jsonl",'
        b'"limit_id":"codex","plan_type":null,"last_known_good":null}'
    )


def test_unknown_reasons_are_enumerable_and_reject_free_form_strings():
    assert {reason.value for reason in CodexUsageUnknownReason} == {
        "auth_error",
        "malformed_line",
        "primary_missing",
        "rate_limits_missing",
        "sessions_dir_empty",
        "sessions_dir_missing",
        "sessions_dir_unreadable",
        "session_file_empty",
        "session_file_unreadable",
        "token_count_missing",
        "unexpected_limit_id",
        "used_percent_invalid",
        "used_percent_missing",
    }
    with pytest.raises(TypeError):
        CodexUnknownUsageReading(reason="new_reason")
