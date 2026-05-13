"""Shared token usage aggregation helpers.

The live token ledger is ``agent_api_calls``. These helpers keep consumers from
reconstructing the same joins, cost math, and fallback metadata in several
routers.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any, Iterable

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from brain.platform.db.models.agent import AgentApiCall
from brain.platform.db.models.agent_run import AgentRunRow
from brain.systems.runs.modeling import calculate_cost


def _coerce_int(value: Any) -> int:
    return int(value or 0)


def _run_metadata(run: AgentRunRow) -> dict[str, Any]:
    value = getattr(run, "metadata_", None)
    return value if isinstance(value, dict) else {}


def _run_target_ref(run: AgentRunRow) -> dict[str, Any]:
    value = getattr(run, "target_ref", None)
    return value if isinstance(value, dict) else {}


def _run_model_policy(run: AgentRunRow) -> dict[str, Any]:
    value = getattr(run, "model_policy", None)
    return value if isinstance(value, dict) else {}


def infer_run_skill(run: AgentRunRow) -> str | None:
    metadata = _run_metadata(run)
    routing = metadata.get("routing") if isinstance(metadata.get("routing"), dict) else {}
    target_ref = _run_target_ref(run)
    for candidate in (
        routing.get("selected_skill"),
        metadata.get("skill_used"),
        target_ref.get("skill"),
        getattr(run, "recipe", None),
        getattr(run, "profile", None),
    ):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


def infer_run_model(run: AgentRunRow) -> str | None:
    policy = _run_model_policy(run)
    metadata = _run_metadata(run)
    for candidate in (
        policy.get("model"),
        metadata.get("model_used"),
        metadata.get("model"),
    ):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


def infer_run_event(run: AgentRunRow) -> str | None:
    target_ref = _run_target_ref(run)
    metadata = _run_metadata(run)
    for candidate in (target_ref.get("event"), metadata.get("event")):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


def _empty_usage() -> dict[str, Any]:
    return {
        "runs": 0,
        "api_calls": 0,
        "tokens_input": 0,
        "tokens_output": 0,
        "tokens_total": 0,
        "cache_read": 0,
        "cache_write": 0,
        "estimated_cost": 0.0,
        "last_used_at": None,
    }


def _add_call_usage(target: dict[str, Any], row: Any) -> None:
    input_tokens = _coerce_int(row.tokens_input)
    output_tokens = _coerce_int(row.tokens_output)
    cache_read = _coerce_int(row.cache_read)
    cache_write = _coerce_int(row.cache_write)
    target["api_calls"] += _coerce_int(row.api_calls)
    target["tokens_input"] += input_tokens
    target["tokens_output"] += output_tokens
    target["tokens_total"] += input_tokens + output_tokens
    target["cache_read"] += cache_read
    target["cache_write"] += cache_write
    last_call_at = getattr(row, "last_call_at", None)
    if last_call_at and (
        target["last_used_at"] is None or last_call_at > target["last_used_at"]
    ):
        target["last_used_at"] = last_call_at


def _model_cost(row: Any) -> float:
    return calculate_cost(
        model=getattr(row, "model", None),
        tokens_input=_coerce_int(row.tokens_input),
        tokens_output=_coerce_int(row.tokens_output),
        cache_read=_coerce_int(row.cache_read),
        cache_write=_coerce_int(row.cache_write),
    )


def _apply_status_filter(stmt: Any, statuses: Iterable[str] | None) -> Any:
    if statuses is None:
        return stmt
    status_values = [status for status in statuses if status]
    if not status_values:
        return stmt
    return stmt.where(AgentRunRow.status.in_(status_values))


def summarize_recent_run_usage(
    session: Session,
    *,
    limit: int = 500,
    org_id: str | None = None,
    since: datetime | None = None,
    statuses: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """Return recent runs with token/cost usage attached.

    This deliberately performs one bounded run query and one grouped usage query
    to avoid per-run lookups.
    """

    run_stmt = _recent_run_usage_stmt(
        limit=limit,
        org_id=org_id,
        since=since,
        statuses=statuses,
    )
    runs = list(session.scalars(run_stmt).all())
    return summarize_runs_usage(session, runs)


async def async_summarize_recent_run_usage(
    session: AsyncSession,
    *,
    limit: int = 500,
    org_id: str | None = None,
    since: datetime | None = None,
    statuses: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """Async recent-run token/cost usage aggregation."""

    run_stmt = _recent_run_usage_stmt(
        limit=limit,
        org_id=org_id,
        since=since,
        statuses=statuses,
    )
    result = await session.scalars(run_stmt)
    return await async_summarize_runs_usage(session, result.all())


def _recent_run_usage_stmt(
    *,
    limit: int,
    org_id: str | None,
    since: datetime | None,
    statuses: Iterable[str] | None,
):
    stmt = select(AgentRunRow).order_by(AgentRunRow.created_at.desc()).limit(limit)
    if org_id:
        stmt = stmt.where(AgentRunRow.org_id == org_id)
    if since:
        stmt = stmt.where(AgentRunRow.created_at >= since)
    return _apply_status_filter(stmt, statuses)


def summarize_run_usage(session: Session, run_id: int) -> dict[str, Any] | None:
    run = session.get(AgentRunRow, run_id)
    if run is None:
        return None
    rows = summarize_runs_usage(session, [run])
    return rows[0] if rows else None


def summarize_runs_usage(session: Session, runs: Iterable[AgentRunRow]) -> list[dict[str, Any]]:
    runs = list(runs)
    run_ids = [run.id for run in runs]

    usage_by_run: dict[int, dict[str, Any]] = {}
    model_rank: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for run in runs:
        row = _empty_usage()
        row.update(
            {
                "id": run.id,
                "trace_id": run.trace_id,
                "idea_id": run.thread_id,
                "thread_id": run.thread_id,
                "skill_used": infer_run_skill(run),
                "model_used": infer_run_model(run),
                "status": run.status,
                "created_at": run.created_at,
                "event": infer_run_event(run),
            }
        )
        usage_by_run[run.id] = row

    if not run_ids:
        return []

    for call_row in session.execute(_run_usage_call_stmt(run_ids)).all():
        row = usage_by_run.get(call_row.run_id)
        if row is None:
            continue
        _add_call_usage(row, call_row)
        cost = _model_cost(call_row)
        row["estimated_cost"] += cost
        model = call_row.model or row.get("model_used")
        if isinstance(model, str) and model:
            model_rank[call_row.run_id][model] += (
                _coerce_int(call_row.tokens_input)
                + _coerce_int(call_row.tokens_output)
                + cost
            )

    for run_id, ranks in model_rank.items():
        if ranks:
            usage_by_run[run_id]["model_used"] = max(ranks.items(), key=lambda item: item[1])[0]

    return [usage_by_run[run.id] for run in runs]


async def async_summarize_runs_usage(
    session: AsyncSession,
    runs: Iterable[AgentRunRow],
) -> list[dict[str, Any]]:
    runs = list(runs)
    run_ids = [run.id for run in runs]

    usage_by_run: dict[int, dict[str, Any]] = {}
    model_rank: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for run in runs:
        row = _empty_usage()
        row.update(
            {
                "id": run.id,
                "trace_id": run.trace_id,
                "idea_id": run.thread_id,
                "thread_id": run.thread_id,
                "skill_used": infer_run_skill(run),
                "model_used": infer_run_model(run),
                "status": run.status,
                "created_at": run.created_at,
                "event": infer_run_event(run),
            }
        )
        usage_by_run[run.id] = row

    if not run_ids:
        return []

    call_stmt = _run_usage_call_stmt(run_ids)
    for call_row in (await session.execute(call_stmt)).all():
        row = usage_by_run.get(call_row.run_id)
        if row is None:
            continue
        _add_call_usage(row, call_row)
        cost = _model_cost(call_row)
        row["estimated_cost"] += cost
        model = call_row.model or row.get("model_used")
        if isinstance(model, str) and model:
            model_rank[call_row.run_id][model] += (
                _coerce_int(call_row.tokens_input)
                + _coerce_int(call_row.tokens_output)
                + cost
            )

    for run_id, ranks in model_rank.items():
        if ranks:
            usage_by_run[run_id]["model_used"] = max(ranks.items(), key=lambda item: item[1])[0]

    return [usage_by_run[run.id] for run in runs]


def _run_usage_call_stmt(run_ids: list[int]):
    return (
        select(
            AgentApiCall.run_id.label("run_id"),
            AgentApiCall.model.label("model"),
            func.count(AgentApiCall.id).label("api_calls"),
            func.coalesce(func.sum(AgentApiCall.tokens_input), 0).label("tokens_input"),
            func.coalesce(func.sum(AgentApiCall.tokens_output), 0).label("tokens_output"),
            func.coalesce(func.sum(AgentApiCall.cache_read), 0).label("cache_read"),
            func.coalesce(func.sum(AgentApiCall.cache_write), 0).label("cache_write"),
            func.max(AgentApiCall.created_at).label("last_call_at"),
        )
        .where(AgentApiCall.run_id.in_(run_ids))
        .group_by(AgentApiCall.run_id, AgentApiCall.model)
    )


def summarize_member_token_usage(
    session: Session,
    *,
    org_id: str,
    since: datetime,
) -> dict[Any, dict[str, Any]]:
    """Aggregate org-scoped token usage by run owner."""

    usage_by_user: dict[Any, dict[str, Any]] = defaultdict(_empty_usage)

    for row in session.execute(_member_usage_stmt(org_id=org_id, since=since)).all():
        target = usage_by_user[row.user_id]
        target["runs"] = _coerce_int(row.runs)
        _add_call_usage(target, row)

    for row in session.execute(_member_cost_stmt(org_id=org_id, since=since)).all():
        usage_by_user[row.user_id]["estimated_cost"] += _model_cost(row)

    return dict(usage_by_user)


async def async_summarize_member_token_usage(
    session: AsyncSession,
    *,
    org_id: str,
    since: datetime,
) -> dict[Any, dict[str, Any]]:
    """Async aggregate of org-scoped token usage by run owner."""

    usage_by_user: dict[Any, dict[str, Any]] = defaultdict(_empty_usage)

    for row in (await session.execute(_member_usage_stmt(org_id=org_id, since=since))).all():
        target = usage_by_user[row.user_id]
        target["runs"] = _coerce_int(row.runs)
        _add_call_usage(target, row)

    for row in (await session.execute(_member_cost_stmt(org_id=org_id, since=since))).all():
        usage_by_user[row.user_id]["estimated_cost"] += _model_cost(row)

    return dict(usage_by_user)


def _member_usage_stmt(*, org_id: str, since: datetime):
    return (
        select(
            AgentRunRow.user_id.label("user_id"),
            func.count(func.distinct(AgentRunRow.id)).label("runs"),
            func.count(AgentApiCall.id).label("api_calls"),
            func.coalesce(func.sum(AgentApiCall.tokens_input), 0).label("tokens_input"),
            func.coalesce(func.sum(AgentApiCall.tokens_output), 0).label("tokens_output"),
            func.coalesce(func.sum(AgentApiCall.cache_read), 0).label("cache_read"),
            func.coalesce(func.sum(AgentApiCall.cache_write), 0).label("cache_write"),
            func.max(AgentApiCall.created_at).label("last_call_at"),
        )
        .join(AgentRunRow, AgentRunRow.id == AgentApiCall.run_id)
        .where(AgentRunRow.org_id == org_id, AgentApiCall.created_at >= since)
        .group_by(AgentRunRow.user_id)
    )


def _member_cost_stmt(*, org_id: str, since: datetime):
    return (
        select(
            AgentRunRow.user_id.label("user_id"),
            AgentApiCall.model.label("model"),
            func.coalesce(func.sum(AgentApiCall.tokens_input), 0).label("tokens_input"),
            func.coalesce(func.sum(AgentApiCall.tokens_output), 0).label("tokens_output"),
            func.coalesce(func.sum(AgentApiCall.cache_read), 0).label("cache_read"),
            func.coalesce(func.sum(AgentApiCall.cache_write), 0).label("cache_write"),
        )
        .join(AgentRunRow, AgentRunRow.id == AgentApiCall.run_id)
        .where(AgentRunRow.org_id == org_id, AgentApiCall.created_at >= since)
        .group_by(AgentRunRow.user_id, AgentApiCall.model)
    )


def summarize_token_totals(
    session: Session,
    *,
    since: datetime,
    org_id: str | None = None,
    thread_id: str | None = None,
    statuses: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Summarize run-linked token usage for budget and reporting surfaces."""

    filters = [AgentApiCall.created_at >= since]
    if org_id:
        filters.append(AgentRunRow.org_id == org_id)
    if thread_id:
        filters.append(AgentRunRow.thread_id == thread_id)

    total_stmt = (
        select(
            func.count(func.distinct(AgentRunRow.id)).label("runs"),
            func.count(AgentApiCall.id).label("api_calls"),
            func.coalesce(func.sum(AgentApiCall.tokens_input), 0).label("tokens_input"),
            func.coalesce(func.sum(AgentApiCall.tokens_output), 0).label("tokens_output"),
            func.coalesce(func.sum(AgentApiCall.cache_read), 0).label("cache_read"),
            func.coalesce(func.sum(AgentApiCall.cache_write), 0).label("cache_write"),
            func.max(AgentApiCall.created_at).label("last_call_at"),
        )
        .join(AgentRunRow, AgentRunRow.id == AgentApiCall.run_id)
        .where(*filters)
    )
    total_stmt = _apply_status_filter(total_stmt, statuses)
    totals = session.execute(total_stmt).one()

    result = _empty_usage()
    result["runs"] = _coerce_int(totals.runs)
    _add_call_usage(result, totals)

    cost_stmt = (
        select(
            AgentApiCall.model.label("model"),
            func.coalesce(func.sum(AgentApiCall.tokens_input), 0).label("tokens_input"),
            func.coalesce(func.sum(AgentApiCall.tokens_output), 0).label("tokens_output"),
            func.coalesce(func.sum(AgentApiCall.cache_read), 0).label("cache_read"),
            func.coalesce(func.sum(AgentApiCall.cache_write), 0).label("cache_write"),
        )
        .join(AgentRunRow, AgentRunRow.id == AgentApiCall.run_id)
        .where(*filters)
        .group_by(AgentApiCall.model)
    )
    cost_stmt = _apply_status_filter(cost_stmt, statuses)

    result["estimated_cost"] = sum(_model_cost(row) for row in session.execute(cost_stmt).all())
    return result
