"""Shared token usage aggregation helpers.

The live token ledger is ``agent_api_calls``. These helpers keep consumers from
reconstructing the same joins, cost math, and fallback metadata in several
routers.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any, Iterable

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.db.models.agent import AgentApiCall
from brain.platform.db.models.agent_run import AgentRunRow
from brain.platform.providers.model_policy import calculate_model_cost

USAGE_COUNT_FIELDS = (
    "api_calls",
    "tokens_input",
    "tokens_output",
    "tokens_total",
    "cache_read",
    "cache_write",
)


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
        "by_effort": [],
    }


def usage_totals_payload(
    usage: dict[str, Any] | None = None,
    *,
    include_runs: bool = False,
) -> dict[str, Any]:
    """Return the stable token/cost reporting contract without run metadata."""

    source = usage or {}
    payload = {
        key: _coerce_int(source.get(key))
        for key in USAGE_COUNT_FIELDS
    }
    if include_runs:
        payload = {"runs": _coerce_int(source.get("runs")), **payload}
    payload["estimated_cost"] = round(float(source.get("estimated_cost") or 0), 6)
    payload["by_effort"] = [
        {
            **{key: _coerce_int(item.get(key)) for key in USAGE_COUNT_FIELDS},
            "effort": item.get("effort"),
            "estimated_cost": round(float(item.get("estimated_cost") or 0), 6),
        }
        for item in source.get("by_effort") or []
        if isinstance(item, dict)
    ]
    return payload


def merge_usage_totals(
    target: dict[str, Any],
    usage: dict[str, Any] | None,
    *,
    runs: int = 0,
) -> None:
    """Merge one stable usage payload into another in place."""

    source = usage_totals_payload(usage)
    if "runs" in target:
        target["runs"] += int(runs)
    for key in USAGE_COUNT_FIELDS:
        target[key] += source[key]
    target["estimated_cost"] = round(
        float(target.get("estimated_cost") or 0) + source["estimated_cost"],
        6,
    )
    by_effort = {
        item.get("effort"): item
        for item in target.get("by_effort") or []
        if isinstance(item, dict)
    }
    for item in source["by_effort"]:
        effort = item.get("effort")
        effort_usage = by_effort.setdefault(effort, _effort_usage_row(effort))
        for key in USAGE_COUNT_FIELDS:
            effort_usage[key] += item[key]
        effort_usage["estimated_cost"] = round(
            effort_usage["estimated_cost"] + item["estimated_cost"],
            6,
        )
    target["by_effort"] = _sorted_effort_usage(by_effort)


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
    return calculate_model_cost(
        model=getattr(row, "model", None),
        tokens_input=_coerce_int(row.tokens_input),
        tokens_output=_coerce_int(row.tokens_output),
        cache_read=_coerce_int(row.cache_read),
        cache_write=_coerce_int(row.cache_write),
    )


def _effort_usage_row(effort: str | None) -> dict[str, Any]:
    return {
        "effort": effort,
        "api_calls": 0,
        "tokens_input": 0,
        "tokens_output": 0,
        "tokens_total": 0,
        "cache_read": 0,
        "cache_write": 0,
        "estimated_cost": 0.0,
    }


def _add_effort_usage(
    usage_by_effort: dict[str | None, dict[str, Any]],
    row: Any,
    *,
    cost: float,
) -> None:
    raw_effort = getattr(row, "effort", None)
    effort = str(raw_effort).strip().lower() if raw_effort else None
    target = usage_by_effort.setdefault(effort, _effort_usage_row(effort))
    input_tokens = _coerce_int(row.tokens_input)
    output_tokens = _coerce_int(row.tokens_output)
    target["api_calls"] += _coerce_int(row.api_calls)
    target["tokens_input"] += input_tokens
    target["tokens_output"] += output_tokens
    target["tokens_total"] += input_tokens + output_tokens
    target["cache_read"] += _coerce_int(row.cache_read)
    target["cache_write"] += _coerce_int(row.cache_write)
    target["estimated_cost"] = round(target["estimated_cost"] + cost, 6)


def _sorted_effort_usage(
    usage_by_effort: dict[str | None, dict[str, Any]],
) -> list[dict[str, Any]]:
    return sorted(
        usage_by_effort.values(),
        key=lambda item: (item["effort"] is None, str(item["effort"] or "")),
    )


def _apply_status_filter(stmt: Any, statuses: Iterable[str] | None) -> Any:
    if statuses is None:
        return stmt
    status_values = [status for status in statuses if status]
    if not status_values:
        return stmt
    return stmt.where(AgentRunRow.status.in_(status_values))


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


async def async_summarize_run_usage(session: AsyncSession, run_id: int) -> dict[str, Any] | None:
    run = await session.get(AgentRunRow, run_id)
    if run is None:
        return None
    rows = await async_summarize_runs_usage(session, [run])
    return rows[0] if rows else None


async def async_summarize_run_usage_in_savepoint(
    session: AsyncSession,
    run_id: int,
) -> dict[str, Any] | None:
    """Read one run's usage without letting a failed report abort caller writes."""

    begin_nested = getattr(session, "begin_nested", None)
    if not callable(begin_nested):
        return await async_summarize_run_usage(session, run_id)
    async with begin_nested():
        return await async_summarize_run_usage(session, run_id)


async def async_summarize_run_trees_usage(
    session: AsyncSession,
    root_run_ids: Iterable[int],
) -> dict[int, dict[str, Any]]:
    """Aggregate each selected root run together with all of its descendants."""

    root_ids = list(dict.fromkeys(int(run_id) for run_id in root_run_ids))
    usage_by_root = {
        run_id: usage_totals_payload()
        for run_id in root_ids
    }
    if not root_ids:
        return usage_by_root

    runs = list(
        (
            await session.scalars(
                select(AgentRunRow).where(
                    or_(
                        AgentRunRow.id.in_(root_ids),
                        AgentRunRow.root_run_id.in_(root_ids),
                    )
                )
            )
        ).all()
    )
    raw_usage_by_run = {
        int(usage["id"]): usage
        for usage in await async_summarize_runs_usage(session, runs)
    }
    root_id_set = set(root_ids)
    for run in runs:
        run_id = int(run.id)
        tree_root_id = (
            run_id
            if run_id in root_id_set
            else int(run.root_run_id) if run.root_run_id in root_id_set else None
        )
        if tree_root_id is not None:
            merge_usage_totals(
                usage_by_root[tree_root_id],
                raw_usage_by_run.get(run_id),
            )
    return usage_by_root


async def async_summarize_run_tree_usage_in_savepoint(
    session: AsyncSession,
    root_run_id: int,
) -> dict[str, Any]:
    """Read a run tree's usage while containing reporting-query failures."""

    begin_nested = getattr(session, "begin_nested", None)
    if not callable(begin_nested):
        return (await async_summarize_run_trees_usage(session, [root_run_id]))[root_run_id]
    async with begin_nested():
        return (await async_summarize_run_trees_usage(session, [root_run_id]))[root_run_id]


async def async_summarize_runs_usage(
    session: AsyncSession,
    runs: Iterable[AgentRunRow],
) -> list[dict[str, Any]]:
    runs = list(runs)
    run_ids = [run.id for run in runs]

    usage_by_run: dict[int, dict[str, Any]] = {}
    model_rank: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    effort_usage_by_run: dict[int, dict[str | None, dict[str, Any]]] = defaultdict(dict)
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
        row["estimated_cost"] = round(row["estimated_cost"] + cost, 6)
        _add_effort_usage(effort_usage_by_run[call_row.run_id], call_row, cost=cost)
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
    for run_id, usage_by_effort in effort_usage_by_run.items():
        usage_by_run[run_id]["by_effort"] = _sorted_effort_usage(usage_by_effort)

    return [usage_by_run[run.id] for run in runs]


def _run_usage_call_stmt(run_ids: list[int]):
    return (
        select(
            AgentApiCall.run_id.label("run_id"),
            AgentApiCall.model.label("model"),
            AgentApiCall.effort.label("effort"),
            func.count(AgentApiCall.id).label("api_calls"),
            func.coalesce(func.sum(AgentApiCall.tokens_input), 0).label("tokens_input"),
            func.coalesce(func.sum(AgentApiCall.tokens_output), 0).label("tokens_output"),
            func.coalesce(func.sum(AgentApiCall.cache_read), 0).label("cache_read"),
            func.coalesce(func.sum(AgentApiCall.cache_write), 0).label("cache_write"),
            func.max(AgentApiCall.created_at).label("last_call_at"),
        )
        .where(AgentApiCall.run_id.in_(run_ids))
        .group_by(AgentApiCall.run_id, AgentApiCall.model, AgentApiCall.effort)
    )


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
        target = usage_by_user[row.user_id]
        cost = _model_cost(row)
        target["estimated_cost"] = round(target["estimated_cost"] + cost, 6)
        usage_by_effort = {
            item["effort"]: item
            for item in target["by_effort"]
        }
        _add_effort_usage(usage_by_effort, row, cost=cost)
        target["by_effort"] = _sorted_effort_usage(usage_by_effort)

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
            AgentApiCall.effort.label("effort"),
            func.count(AgentApiCall.id).label("api_calls"),
            func.coalesce(func.sum(AgentApiCall.tokens_input), 0).label("tokens_input"),
            func.coalesce(func.sum(AgentApiCall.tokens_output), 0).label("tokens_output"),
            func.coalesce(func.sum(AgentApiCall.cache_read), 0).label("cache_read"),
            func.coalesce(func.sum(AgentApiCall.cache_write), 0).label("cache_write"),
        )
        .join(AgentRunRow, AgentRunRow.id == AgentApiCall.run_id)
        .where(AgentRunRow.org_id == org_id, AgentApiCall.created_at >= since)
        .group_by(AgentRunRow.user_id, AgentApiCall.model, AgentApiCall.effort)
    )


async def async_summarize_token_totals(
    session: AsyncSession,
    *,
    since: datetime,
    org_id: str | None = None,
    thread_id: str | None = None,
    statuses: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Async summary of run-linked token usage for budget and reporting surfaces."""

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
    totals = (await session.execute(total_stmt)).one()

    result = _empty_usage()
    result["runs"] = _coerce_int(totals.runs)
    _add_call_usage(result, totals)

    cost_stmt = (
        select(
            AgentApiCall.model.label("model"),
            AgentApiCall.effort.label("effort"),
            func.count(AgentApiCall.id).label("api_calls"),
            func.coalesce(func.sum(AgentApiCall.tokens_input), 0).label("tokens_input"),
            func.coalesce(func.sum(AgentApiCall.tokens_output), 0).label("tokens_output"),
            func.coalesce(func.sum(AgentApiCall.cache_read), 0).label("cache_read"),
            func.coalesce(func.sum(AgentApiCall.cache_write), 0).label("cache_write"),
        )
        .join(AgentRunRow, AgentRunRow.id == AgentApiCall.run_id)
        .where(*filters)
        .group_by(AgentApiCall.model, AgentApiCall.effort)
    )
    cost_stmt = _apply_status_filter(cost_stmt, statuses)

    usage_by_effort: dict[str | None, dict[str, Any]] = {}
    for row in (await session.execute(cost_stmt)).all():
        cost = _model_cost(row)
        result["estimated_cost"] = round(result["estimated_cost"] + cost, 6)
        _add_effort_usage(usage_by_effort, row, cost=cost)
    result["by_effort"] = _sorted_effort_usage(usage_by_effort)
    return result
