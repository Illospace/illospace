"""Cycle schedule parsing and presentation."""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from croniter import croniter

ONE_TIME_SCHEDULE_PREFIX = "at:"


def is_one_time_schedule_expr(expr: str | None) -> bool:
    return str(expr or "").strip().lower().startswith(ONE_TIME_SCHEDULE_PREFIX)


def _parse_one_time_run_at(expr: str, timezone_name: str) -> datetime:
    value = str(expr or "").strip()
    if not is_one_time_schedule_expr(value):
        raise ValueError("schedule_expr must start with at:")
    raw_at = value[len(ONE_TIME_SCHEDULE_PREFIX):].strip()
    if not raw_at:
        raise ValueError("one-time schedule requires an ISO timestamp")
    try:
        run_at = datetime.fromisoformat(raw_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("one-time schedule requires an ISO timestamp") from exc
    tz = ZoneInfo(validate_timezone_name(timezone_name))
    if run_at.tzinfo is None:
        run_at = run_at.replace(tzinfo=tz)
    return run_at.astimezone(timezone.utc)


def build_one_time_schedule_expr(run_at: str | datetime, timezone_name: str) -> str:
    tz = ZoneInfo(validate_timezone_name(timezone_name))
    if isinstance(run_at, datetime):
        parsed = run_at
    else:
        raw_at = str(run_at or "").strip()
        if not raw_at:
            raise ValueError("run_at is required")
        try:
            parsed = datetime.fromisoformat(raw_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("run_at must be an ISO timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    local_run_at = parsed.astimezone(tz).replace(second=0, microsecond=0)
    return f"{ONE_TIME_SCHEDULE_PREFIX}{local_run_at.isoformat()}"


def validate_schedule_expr(expr: str, timezone_name: str | None = None) -> str:
    value = (expr or "").strip()
    if is_one_time_schedule_expr(value):
        tz_name = validate_timezone_name(timezone_name or "UTC")
        return build_one_time_schedule_expr(
            _parse_one_time_run_at(value, tz_name),
            tz_name,
        )
    if len(value.split()) != 5 or not croniter.is_valid(value):
        raise ValueError("schedule_expr must be a valid 5-field cron expression")
    return value


def validate_timezone_name(name: str) -> str:
    value = (name or "").strip()
    if not value:
        raise ValueError("timezone is required")
    try:
        ZoneInfo(value)
    except Exception as exc:
        raise ValueError(f"Unknown timezone: {value}") from exc
    return value


def compute_next_run_at(
    schedule_expr: str,
    timezone_name: str,
    *,
    from_dt: datetime | None = None,
) -> datetime | None:
    if is_one_time_schedule_expr(schedule_expr):
        run_at = _parse_one_time_run_at(schedule_expr, timezone_name)
        return None if from_dt is not None and run_at <= from_dt else run_at

    tz = ZoneInfo(validate_timezone_name(timezone_name))
    baseline = from_dt or datetime.now(timezone.utc)
    local_baseline = baseline.astimezone(tz)
    iterator = croniter(validate_schedule_expr(schedule_expr), local_baseline)
    next_local = iterator.get_next(datetime)
    if next_local.tzinfo is None:
        next_local = next_local.replace(tzinfo=tz)
    return next_local.astimezone(timezone.utc)


def humanize_schedule(schedule_expr: str, timezone_name: str) -> str:
    if is_one_time_schedule_expr(schedule_expr):
        tz_name = validate_timezone_name(timezone_name)
        local_run_at = _parse_one_time_run_at(schedule_expr, tz_name).astimezone(ZoneInfo(tz_name))
        hour = local_run_at.strftime("%I").lstrip("0") or "0"
        return (
            f"Once at {local_run_at.strftime('%b')} {local_run_at.day}, "
            f"{local_run_at.year} {hour}:{local_run_at.strftime('%M %p')} ({tz_name})"
        )

    expr = validate_schedule_expr(schedule_expr)
    minute, hour, dom, month, dow = expr.split()
    tz = validate_timezone_name(timezone_name)

    if minute == "*" and hour == "*":
        return f"Every minute ({tz})"
    if minute == "0" and hour == "*":
        return f"Every hour ({tz})"
    if minute.isdigit() and hour.isdigit() and dom == "*" and month == "*" and dow == "*":
        dt = datetime(2000, 1, 1, int(hour), int(minute))
        return f"Every day at {dt.strftime('%-I:%M %p')} ({tz})"
    weekday_names = {
        "0": "Sundays",
        "1": "Mondays",
        "2": "Tuesdays",
        "3": "Wednesdays",
        "4": "Thursdays",
        "5": "Fridays",
        "6": "Saturdays",
        "7": "Sundays",
    }
    if minute.isdigit() and hour.isdigit() and dom == "*" and month == "*" and dow in weekday_names:
        dt = datetime(2000, 1, 1, int(hour), int(minute))
        return f"{weekday_names[dow]} at {dt.strftime('%-I:%M %p')} ({tz})"
    if (
        minute.isdigit()
        and hour.isdigit()
        and dom == "*"
        and month == "*"
        and dow in {"1", "2", "3", "4", "5"}
    ):
        dt = datetime(2000, 1, 1, int(hour), int(minute))
        return f"Weekdays at {dt.strftime('%-I:%M %p')} ({tz})"
    return f"{expr} ({tz})"


def safe_humanize_schedule(schedule_expr: str, timezone_name: str) -> str:
    try:
        return humanize_schedule(schedule_expr, timezone_name)
    except ValueError:
        return f"{schedule_expr} ({timezone_name})"
