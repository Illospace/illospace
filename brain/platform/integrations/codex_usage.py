"""Read Codex subscription usage from local session event logs."""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Mapping


CodexUsageStatus = Literal["ok", "unknown", "exhausted"]


@dataclass(frozen=True, slots=True)
class CodexKnownUsage:
    used_percent: float
    observed_at: str
    source_path: str
    limit_id: str = "codex"
    plan_type: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "used_percent": self.used_percent,
            "observed_at": self.observed_at,
            "source_path": self.source_path,
            "limit_id": self.limit_id,
            "plan_type": self.plan_type,
        }


@dataclass(frozen=True, slots=True)
class CodexUsageReading:
    status: CodexUsageStatus
    used_percent: float | None = None
    reason: str | None = None
    observed_at: str | None = None
    source_path: str | None = None
    limit_id: str | None = None
    plan_type: str | None = None
    last_known_good: CodexKnownUsage | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "used_percent": self.used_percent,
            "reason": self.reason,
            "observed_at": self.observed_at,
            "source_path": self.source_path,
            "limit_id": self.limit_id,
            "plan_type": self.plan_type,
            "last_known_good": (
                self.last_known_good.to_dict() if self.last_known_good else None
            ),
        }


def codex_home_path(path: str | Path | None = None) -> Path:
    """Return the configured Codex home, defaulting to ``~/.codex``."""

    if path is not None:
        return Path(path).expanduser()
    configured = str(os.environ.get("CODEX_HOME") or "").strip()
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


def _unknown(
    reason: str,
    *,
    observed_at: str | None = None,
    source_path: Path | None = None,
    limit_id: str | None = None,
    plan_type: str | None = None,
) -> CodexUsageReading:
    return CodexUsageReading(
        status="unknown",
        reason=reason,
        observed_at=observed_at,
        source_path=str(source_path) if source_path else None,
        limit_id=limit_id,
        plan_type=plan_type,
    )


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _event_timestamp(data: Mapping[str, Any], source_path: Path) -> str:
    payload = _mapping(data.get("payload"))
    raw = data.get("timestamp") or payload.get("timestamp")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    try:
        modified = datetime.fromtimestamp(source_path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        modified = datetime.now(timezone.utc)
    return modified.isoformat()


def _token_count_payload(data: Mapping[str, Any]) -> Mapping[str, Any] | None:
    if data.get("type") == "token_count":
        return data
    payload = _mapping(data.get("payload"))
    if payload.get("type") == "token_count":
        return payload
    return None


def _is_auth_error(data: Mapping[str, Any]) -> bool:
    payload = _mapping(data.get("payload"))
    event_type = str(payload.get("type") or data.get("type") or "").lower()
    if event_type not in {"error", "auth_error", "authentication_error"}:
        return False
    error = payload.get("error") or data.get("error") or payload
    text = json.dumps(error, default=str).lower()
    return any(
        marker in text
        for marker in ("auth", "unauthorized", "forbidden", '"401"', '"403"')
    )


def _rate_limits(
    data: Mapping[str, Any], token_payload: Mapping[str, Any]
) -> Mapping[str, Any]:
    info = _mapping(token_payload.get("info"))
    return _mapping(
        token_payload.get("rate_limits")
        or info.get("rate_limits")
        or data.get("rate_limits")
    )


def _reading_from_event(
    data: Mapping[str, Any],
    *,
    source_path: Path,
) -> CodexUsageReading | None:
    if _is_auth_error(data):
        return _unknown(
            "auth_error",
            observed_at=_event_timestamp(data, source_path),
            source_path=source_path,
        )

    token_payload = _token_count_payload(data)
    if token_payload is None:
        return None

    observed_at = _event_timestamp(data, source_path)
    if token_payload.get("error"):
        return _unknown(
            "auth_error",
            observed_at=observed_at,
            source_path=source_path,
        )
    rate_limits = _rate_limits(data, token_payload)
    if not rate_limits:
        return _unknown(
            "rate_limits_missing",
            observed_at=observed_at,
            source_path=source_path,
        )
    if rate_limits.get("error"):
        return _unknown(
            "auth_error",
            observed_at=observed_at,
            source_path=source_path,
        )

    limit_id_value = rate_limits.get("limit_id")
    limit_id = str(limit_id_value).strip() if limit_id_value is not None else None
    plan_value = rate_limits.get("plan_type")
    plan_type = str(plan_value).strip() if plan_value is not None else None
    if limit_id != "codex":
        return _unknown(
            "unexpected_limit_id",
            observed_at=observed_at,
            source_path=source_path,
            limit_id=limit_id,
            plan_type=plan_type,
        )

    primary = rate_limits.get("primary")
    if not isinstance(primary, Mapping):
        return _unknown(
            "primary_missing",
            observed_at=observed_at,
            source_path=source_path,
            limit_id=limit_id,
            plan_type=plan_type,
        )

    used_percent = primary.get("used_percent")
    if isinstance(used_percent, bool) or not isinstance(used_percent, (int, float)):
        return _unknown(
            "used_percent_missing",
            observed_at=observed_at,
            source_path=source_path,
            limit_id=limit_id,
            plan_type=plan_type,
        )
    normalized_percent = float(used_percent)
    if not math.isfinite(normalized_percent) or normalized_percent < 0:
        return _unknown(
            "used_percent_invalid",
            observed_at=observed_at,
            source_path=source_path,
            limit_id=limit_id,
            plan_type=plan_type,
        )
    return CodexUsageReading(
        status="exhausted" if normalized_percent >= 100 else "ok",
        used_percent=normalized_percent,
        observed_at=observed_at,
        source_path=str(source_path),
        limit_id=limit_id,
        plan_type=plan_type,
    )


def _session_files(sessions_path: Path) -> list[Path]:
    files = list(sessions_path.glob("*/*/*/*.jsonl"))
    return sorted(
        files,
        key=lambda item: (item.stat().st_mtime_ns, str(item)),
        reverse=True,
    )


def _read_lines(path: Path) -> list[str]:
    with path.open("r", encoding="utf-8") as handle:
        return handle.readlines()


def _scan_lines(
    lines: list[str],
    *,
    source_path: Path,
    malformed_is_verdict: bool,
) -> CodexUsageReading | None:
    for raw_line in reversed(lines):
        if not raw_line.strip():
            continue
        try:
            data = json.loads(raw_line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            if malformed_is_verdict:
                return _unknown("malformed_line", source_path=source_path)
            continue
        if not isinstance(data, Mapping):
            if malformed_is_verdict:
                return _unknown("malformed_line", source_path=source_path)
            continue
        reading = _reading_from_event(data, source_path=source_path)
        if reading is not None:
            return reading
    return None


def _find_last_known(files: list[Path]) -> CodexKnownUsage | None:
    for source_path in files:
        try:
            lines = _read_lines(source_path)
        except (OSError, UnicodeError):
            continue
        for raw_line in reversed(lines):
            try:
                data = json.loads(raw_line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(data, Mapping):
                continue
            reading = _reading_from_event(data, source_path=source_path)
            if reading is None or reading.status == "unknown":
                continue
            return CodexKnownUsage(
                used_percent=float(reading.used_percent),
                observed_at=str(reading.observed_at),
                source_path=str(source_path),
                plan_type=reading.plan_type,
            )
    return None


def read_codex_usage(path: str | Path | None = None) -> CodexUsageReading:
    """Read the newest local Codex usage verdict and retain older known usage.

    Unknown inputs always remain unknown. In particular, degenerate entitlement
    payloads are not converted to either zero or one hundred percent.
    """

    sessions_path = codex_home_path(path) / "sessions"
    try:
        if not sessions_path.exists():
            return _unknown("sessions_dir_missing")
        if not sessions_path.is_dir():
            return _unknown("sessions_dir_unreadable")
        files = _session_files(sessions_path)
    except OSError:
        return _unknown("sessions_dir_unreadable")
    if not files:
        return _unknown("sessions_dir_empty")

    newest = files[0]
    try:
        newest_lines = _read_lines(newest)
    except (OSError, UnicodeError):
        verdict = _unknown("session_file_unreadable", source_path=newest)
    else:
        if not any(line.strip() for line in newest_lines):
            verdict = _unknown("session_file_empty", source_path=newest)
        else:
            verdict = _scan_lines(
                newest_lines,
                source_path=newest,
                malformed_is_verdict=True,
            ) or _unknown("token_count_missing", source_path=newest)

    if verdict.status != "unknown":
        return verdict
    return CodexUsageReading(
        status=verdict.status,
        reason=verdict.reason,
        observed_at=verdict.observed_at,
        source_path=verdict.source_path,
        limit_id=verdict.limit_id,
        plan_type=verdict.plan_type,
        last_known_good=_find_last_known(files),
    )


__all__ = [
    "CodexKnownUsage",
    "CodexUsageReading",
    "CodexUsageStatus",
    "codex_home_path",
    "read_codex_usage",
]
