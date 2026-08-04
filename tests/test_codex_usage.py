from __future__ import annotations

import json
import os

from brain.platform.integrations.codex_usage import read_codex_usage


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

    assert reading.status == "ok"
    assert reading.used_percent == 31.0
    assert reading.reason is None
    assert reading.observed_at == "2026-08-04T13:24:46Z"
    assert reading.limit_id == "codex"
    assert reading.plan_type == "pro"


def test_real_codex_usage_at_one_hundred_is_exhausted(tmp_path):
    path = _session_file(tmp_path)
    _write_events(path, _event(100))

    reading = read_codex_usage(tmp_path)

    assert reading.status == "exhausted"
    assert reading.used_percent == 100.0
    assert reading.reason is None


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

    assert reading.status == "unknown"
    assert reading.reason == "unexpected_limit_id"
    assert reading.used_percent is None
    assert reading.limit_id == "premium"
    assert reading.last_known_good is not None
    assert reading.last_known_good.used_percent == 31.0
    assert reading.last_known_good.observed_at == "2026-08-04T13:24:45Z"


def test_null_primary_on_codex_limit_is_unknown_not_zero_or_exhausted(tmp_path):
    path = _session_file(tmp_path)
    _write_events(path, _event(None, primary_marker=False))

    reading = read_codex_usage(tmp_path)

    assert reading.status == "unknown"
    assert reading.reason == "primary_missing"
    assert reading.used_percent is None
    assert reading.last_known_good is None


def test_malformed_newest_line_is_unknown_with_older_known_reading(tmp_path):
    path = _session_file(tmp_path)
    _write_events(path, _event(42))
    with path.open("a", encoding="utf-8") as handle:
        handle.write("{not valid json\n")

    reading = read_codex_usage(tmp_path)

    assert reading.status == "unknown"
    assert reading.reason == "malformed_line"
    assert reading.used_percent is None
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

    assert reading.status == "unknown"
    assert reading.reason == "auth_error"
    assert reading.used_percent is None
    assert reading.last_known_good is not None
    assert reading.last_known_good.used_percent == 52.0


def test_missing_sessions_directory_is_unknown(tmp_path):
    reading = read_codex_usage(tmp_path)

    assert reading.status == "unknown"
    assert reading.reason == "sessions_dir_missing"
    assert reading.used_percent is None


def test_only_newest_session_file_controls_current_verdict(tmp_path):
    older = _session_file(tmp_path, name="older.jsonl")
    newer = _session_file(tmp_path, name="newer.jsonl")
    _write_events(older, _event(12, timestamp="2026-08-04T12:00:00Z"))
    _write_events(newer, {"timestamp": "2026-08-04T13:00:00Z", "type": "response_item"})
    os.utime(older, (1, 1))
    os.utime(newer, (2, 2))

    reading = read_codex_usage(tmp_path)

    assert reading.status == "unknown"
    assert reading.reason == "token_count_missing"
    assert reading.last_known_good is not None
    assert reading.last_known_good.used_percent == 12.0


def test_codex_home_environment_selects_usage_root(tmp_path, monkeypatch):
    path = _session_file(tmp_path)
    _write_events(path, _event(17))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))

    assert read_codex_usage().used_percent == 17.0
