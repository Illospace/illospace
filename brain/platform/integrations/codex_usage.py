"""Read Codex subscription usage from local session event logs."""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping, TypeAlias


class CodexUsageUnknownReason(StrEnum):
    AUTH_ERROR = "auth_error"
    MALFORMED_LINE = "malformed_line"
    PRIMARY_MISSING = "primary_missing"
    RATE_LIMITS_MISSING = "rate_limits_missing"
    SESSIONS_DIR_EMPTY = "sessions_dir_empty"
    SESSIONS_DIR_MISSING = "sessions_dir_missing"
    SESSIONS_DIR_UNREADABLE = "sessions_dir_unreadable"
    SESSION_FILE_EMPTY = "session_file_empty"
    SESSION_FILE_UNREADABLE = "session_file_unreadable"
    TOKEN_COUNT_MISSING = "token_count_missing"
    UNEXPECTED_LIMIT_ID = "unexpected_limit_id"
    USED_PERCENT_INVALID = "used_percent_invalid"
    USED_PERCENT_MISSING = "used_percent_missing"


@dataclass(frozen=True, slots=True, kw_only=True)
class CodexKnownUsage:
    used_percent: float
    observed_at: str
    source_path: str
    limit_id: str = "codex"
    plan_type: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return _codex_usage_to_dict(self)


@dataclass(frozen=True, slots=True, kw_only=True)
class CodexUnknownUsageReading:
    reason: CodexUsageUnknownReason
    observed_at: str | None = None
    source_path: str | None = None
    limit_id: str | None = None
    plan_type: str | None = None
    last_known_good: CodexKnownUsage | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.reason, CodexUsageUnknownReason):
            raise TypeError("reason must be a CodexUsageUnknownReason")

    def to_dict(self) -> dict[str, Any]:
        return _codex_usage_to_dict(self)


CodexUsageReading: TypeAlias = CodexKnownUsage | CodexUnknownUsageReading


def _codex_usage_to_dict(reading: CodexUsageReading) -> dict[str, Any]:
    """Serialize the legacy flat reading shape at its persistence boundary."""

    if isinstance(reading, CodexKnownUsage):
        return {
            "status": "exhausted" if reading.used_percent >= 100 else "ok",
            "used_percent": reading.used_percent,
            "reason": None,
            "observed_at": reading.observed_at,
            "source_path": reading.source_path,
            "limit_id": reading.limit_id,
            "plan_type": reading.plan_type,
            "last_known_good": None,
        }
    return {
        "status": "unknown",
        "used_percent": None,
        "reason": reading.reason,
        "observed_at": reading.observed_at,
        "source_path": reading.source_path,
        "limit_id": reading.limit_id,
        "plan_type": reading.plan_type,
        "last_known_good": (
            {
                "used_percent": reading.last_known_good.used_percent,
                "observed_at": reading.last_known_good.observed_at,
                "source_path": reading.last_known_good.source_path,
                "limit_id": reading.last_known_good.limit_id,
                "plan_type": reading.last_known_good.plan_type,
            }
            if reading.last_known_good is not None
            else None
        ),
    }


def codex_home_path(path: str | Path | None = None) -> Path:
    """Return the configured Codex home, defaulting to ``~/.codex``."""

    if path is not None:
        return Path(path).expanduser()
    configured = str(os.environ.get("CODEX_HOME") or "").strip()
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


def _unknown(
    reason: CodexUsageUnknownReason,
    *,
    observed_at: str | None = None,
    source_path: Path | None = None,
    limit_id: str | None = None,
    plan_type: str | None = None,
) -> CodexUsageReading:
    return CodexUnknownUsageReading(
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
            CodexUsageUnknownReason.AUTH_ERROR,
            observed_at=_event_timestamp(data, source_path),
            source_path=source_path,
        )

    token_payload = _token_count_payload(data)
    if token_payload is None:
        return None

    observed_at = _event_timestamp(data, source_path)
    if token_payload.get("error"):
        return _unknown(
            CodexUsageUnknownReason.AUTH_ERROR,
            observed_at=observed_at,
            source_path=source_path,
        )
    rate_limits = _rate_limits(data, token_payload)
    if not rate_limits:
        return _unknown(
            CodexUsageUnknownReason.RATE_LIMITS_MISSING,
            observed_at=observed_at,
            source_path=source_path,
        )
    if rate_limits.get("error"):
        return _unknown(
            CodexUsageUnknownReason.AUTH_ERROR,
            observed_at=observed_at,
            source_path=source_path,
        )

    limit_id_value = rate_limits.get("limit_id")
    limit_id = str(limit_id_value).strip() if limit_id_value is not None else None
    plan_value = rate_limits.get("plan_type")
    plan_type = str(plan_value).strip() if plan_value is not None else None
    if limit_id != "codex":
        return _unknown(
            CodexUsageUnknownReason.UNEXPECTED_LIMIT_ID,
            observed_at=observed_at,
            source_path=source_path,
            limit_id=limit_id,
            plan_type=plan_type,
        )

    primary = rate_limits.get("primary")
    if not isinstance(primary, Mapping):
        return _unknown(
            CodexUsageUnknownReason.PRIMARY_MISSING,
            observed_at=observed_at,
            source_path=source_path,
            limit_id=limit_id,
            plan_type=plan_type,
        )

    used_percent = primary.get("used_percent")
    if isinstance(used_percent, bool) or not isinstance(used_percent, (int, float)):
        return _unknown(
            CodexUsageUnknownReason.USED_PERCENT_MISSING,
            observed_at=observed_at,
            source_path=source_path,
            limit_id=limit_id,
            plan_type=plan_type,
        )
    normalized_percent = float(used_percent)
    if not math.isfinite(normalized_percent) or normalized_percent < 0:
        return _unknown(
            CodexUsageUnknownReason.USED_PERCENT_INVALID,
            observed_at=observed_at,
            source_path=source_path,
            limit_id=limit_id,
            plan_type=plan_type,
        )
    return CodexKnownUsage(
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
                return _unknown(
                    CodexUsageUnknownReason.MALFORMED_LINE,
                    source_path=source_path,
                )
            continue
        if not isinstance(data, Mapping):
            if malformed_is_verdict:
                return _unknown(
                    CodexUsageUnknownReason.MALFORMED_LINE,
                    source_path=source_path,
                )
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
            if not isinstance(reading, CodexKnownUsage):
                continue
            return reading
    return None


def read_codex_usage(path: str | Path | None = None) -> CodexUsageReading:
    """Read the newest local Codex usage verdict and retain older known usage.

    Unknown inputs always remain unknown. In particular, degenerate entitlement
    payloads are not converted to either zero or one hundred percent.
    """

    sessions_path = codex_home_path(path) / "sessions"
    try:
        if not sessions_path.exists():
            return _unknown(CodexUsageUnknownReason.SESSIONS_DIR_MISSING)
        if not sessions_path.is_dir():
            return _unknown(CodexUsageUnknownReason.SESSIONS_DIR_UNREADABLE)
        files = _session_files(sessions_path)
    except OSError:
        return _unknown(CodexUsageUnknownReason.SESSIONS_DIR_UNREADABLE)
    if not files:
        return _unknown(CodexUsageUnknownReason.SESSIONS_DIR_EMPTY)

    newest = files[0]
    try:
        newest_lines = _read_lines(newest)
    except (OSError, UnicodeError):
        verdict = _unknown(
            CodexUsageUnknownReason.SESSION_FILE_UNREADABLE,
            source_path=newest,
        )
    else:
        if not any(line.strip() for line in newest_lines):
            verdict = _unknown(
                CodexUsageUnknownReason.SESSION_FILE_EMPTY,
                source_path=newest,
            )
        else:
            verdict = _scan_lines(
                newest_lines,
                source_path=newest,
                malformed_is_verdict=True,
            ) or _unknown(
                CodexUsageUnknownReason.TOKEN_COUNT_MISSING,
                source_path=newest,
            )

    if isinstance(verdict, CodexKnownUsage):
        return verdict
    return CodexUnknownUsageReading(
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
    "CodexUsageUnknownReason",
    "CodexUnknownUsageReading",
    "codex_home_path",
    "read_codex_usage",
]
