"""Costs router — run cost tracking with analytics."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text as sa_text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from brain.app.api.auth import get_current_user
from brain.app.api.deps import get_db, rate_limit
from brain.app.api.db_utils import run_db
from brain.systems.runs.cortex.recording import trace_id_for_run_id
from brain.platform.db.repositories.run import RunRepository

router = APIRouter(
    prefix="/api/costs",
    tags=["costs"],
    dependencies=[Depends(rate_limit)],
)


def _provider_model_key(raw_model: Any) -> tuple[str, str, str]:
    """Normalize model strings without letting legacy prefixes split cost groups."""
    value = str(raw_model or "").strip()
    if not value:
        return "unknown", "unknown", "unknown/unknown"

    lowered = value.lower()
    if lowered.startswith("local/"):
        model = value.split("/", 1)[1] or value
        return "local", model, f"local/{model}"
    if "gpu_server" in lowered or lowered.startswith("brain.platform.gpu/"):
        return "local", value, f"local/{value}"

    for separator in ("/", ":"):
        if separator in value:
            prefix, model = value.split(separator, 1)
            provider = prefix.strip().lower()
            model = model.strip()
            if provider in {"anthropic", "openai", "google", "local"} and model:
                return provider, model, f"{provider}/{model}"

    if lowered.startswith("claude-"):
        return "anthropic", value, f"anthropic/{value}"
    if lowered.startswith(("gpt-", "o1", "o3", "o4")):
        return "openai", value, f"openai/{value}"
    if lowered.startswith("gemini"):
        return "google", value, f"google/{value}"
    return "unknown", value, f"unknown/{value}"


def _fetch_agent_api_call_rows(db: Session, run_id: int) -> list[dict[str, Any]]:
    """Read optional LLM call telemetry while tolerating legacy/missing tables."""
    base_columns = (
        "turn_number, session_id, model, tokens_input, tokens_output, "
        "cache_read, cache_write, context_messages, system_prompt_chars, "
        "status, stop_reason, latency_ms, error, created_at "
    )
    try:
        rows = db.execute(sa_text(
            "SELECT trace_id, " + base_columns +
            "FROM agent_api_calls WHERE run_id = :did "
            "ORDER BY created_at, turn_number"
        ), {"did": run_id}).mappings().all()
        return [dict(row) for row in rows]
    except SQLAlchemyError:
        try:
            db.rollback()
        except Exception:
            pass

    try:
        rows = db.execute(sa_text(
            "SELECT " + base_columns +
            "FROM agent_api_calls WHERE run_id = :did "
            "ORDER BY created_at, turn_number"
        ), {"did": run_id}).mappings().all()
        return [dict(row) for row in rows]
    except SQLAlchemyError:
        try:
            db.rollback()
        except Exception:
            pass
        return []


def _serialize_run(d: Any) -> dict:
    provider, model, provider_model = _provider_model_key(d.model_used)
    return {
        "id": d.id,
        "trace_id": getattr(d, "trace_id", None) or trace_id_for_run_id(getattr(d, "id", None)),
        "idea_id": d.idea_id,
        "skill": d.skill_used,
        "model": d.model_used,
        "provider": provider,
        "normalized_model": model,
        "provider_model": provider_model,
        "input_tokens": d.tokens_input,
        "output_tokens": d.tokens_output,
        "tokens": d.tokens_total,
        "cache_read": getattr(d, "cache_read", None),
        "cache_write": getattr(d, "cache_write", None),
        "cost": float(d.estimated_cost) if d.estimated_cost else 0,
        "status": d.status,
        "timestamp": d.created_at.isoformat() if d.created_at else None,
        "event": getattr(d, "event", None),
    }


@router.get("/run/{run_id}")
async def run_breakdown(
    run_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    """Per-turn breakdown for a run — shows exactly where tokens went.

    Returns every API call made during this run: turn number, model,
    input/output/cache tokens, context size, latency, and which session
    (coordinator vs worker) made the call.
    """
    def _breakdown(sync_db: Session):
        rows = _fetch_agent_api_call_rows(sync_db, run_id)

        if not rows:
            return {
                "run_id": run_id,
                "trace_id": trace_id_for_run_id(run_id),
                "turns": [],
                "summary": None,
            }

        # Group by session to identify coordinator vs workers
        sessions: dict[str, list] = defaultdict(list)
        for r in rows:
            sessions[r["session_id"]].append(dict(r))

        turns = []
        totals = {
            "input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
            "api_calls": 0, "total_latency_ms": 0,
        }

        for r in rows:
            input_tok = r["tokens_input"] or 0
            output_tok = r["tokens_output"] or 0
            cr = r["cache_read"] or 0
            cw = r["cache_write"] or 0

            totals["input"] += input_tok
            totals["output"] += output_tok
            totals["cache_read"] += cr
            totals["cache_write"] += cw
            totals["api_calls"] += 1
            totals["total_latency_ms"] += r["latency_ms"] or 0

            provider, normalized_model, provider_model = _provider_model_key(r["model"])
            turns.append({
                "turn": r["turn_number"],
                "trace_id": r.get("trace_id") or trace_id_for_run_id(run_id),
                "session_id": r["session_id"],
                "model": r["model"],
                "provider": provider,
                "normalized_model": normalized_model,
                "provider_model": provider_model,
                "input": input_tok,
                "output": output_tok,
                "cache_read": cr,
                "cache_write": cw,
                "context_messages": r["context_messages"],
                "system_prompt_chars": r["system_prompt_chars"],
                "status": r["status"],
                "stop_reason": r["stop_reason"],
                "latency_ms": r["latency_ms"],
                "error": r["error"],
                "timestamp": r["created_at"].isoformat() if r["created_at"] else None,
            })

        # Identify coordinator (usually first session, or has "coordinator" in id)
        session_ids = list(sessions.keys())
        coordinator_id = next(
            (s for s in session_ids if "coordinator" in s), session_ids[0] if session_ids else None
        )

        # Per-session summaries
        session_summaries = []
        for sid, calls in sessions.items():
            s_input = sum(c["tokens_input"] or 0 for c in calls)
            s_output = sum(c["tokens_output"] or 0 for c in calls)
            s_cr = sum(c["cache_read"] or 0 for c in calls)
            s_cw = sum(c["cache_write"] or 0 for c in calls)

            # Context growth: first vs last turn
            first_ctx = calls[0].get("context_messages") or 0
            last_ctx = calls[-1].get("context_messages") or 0

            role = "coordinator" if sid == coordinator_id else "worker"
            session_summaries.append({
                "session_id": sid,
                "role": role,
                "api_calls": len(calls),
                "input": s_input,
                "output": s_output,
                "cache_read": s_cr,
                "cache_write": s_cw,
                "cache_hit_rate": round(s_cr / max(s_input + s_cr, 1), 3),
                "context_growth": f"{first_ctx} -> {last_ctx} messages",
                "total_latency_ms": sum(c["latency_ms"] or 0 for c in calls),
            })

        # Sort: biggest token consumer first
        session_summaries.sort(key=lambda x: x["input"] + x["output"], reverse=True)

        cache_hit_rate = round(
            totals["cache_read"] / max(totals["input"] + totals["cache_read"], 1), 3
        )

        return {
            "run_id": run_id,
            "trace_id": rows[0].get("trace_id") or trace_id_for_run_id(run_id),
            "summary": {
                **totals,
                "total_tokens": totals["input"] + totals["output"],
                "cache_hit_rate": cache_hit_rate,
            },
            "sessions": session_summaries,
            "turns": turns,
        }

    return await run_db(db, _breakdown)


@router.get("/")
async def list_costs(
    limit: int = Query(500, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    def _list(sync_db: Session):
        repo = RunRepository(sync_db)
        runs = repo.list_recent(limit=limit, summary_only=True)

        total_cost = sum(float(d.estimated_cost or 0) for d in runs)
        total_tokens = sum(d.tokens_total or 0 for d in runs)
        total_input = sum(d.tokens_input or 0 for d in runs)
        total_output = sum(d.tokens_output or 0 for d in runs)
        total_cache_read = sum(getattr(d, "cache_read", 0) or 0 for d in runs)
        total_cache_write = sum(getattr(d, "cache_write", 0) or 0 for d in runs)
        runs_with_tokens = sum(1 for d in runs if d.tokens_total)

    # Current month filter
        now = datetime.now(timezone.utc)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        month_runs = [
            d for d in runs if d.created_at and d.created_at >= month_start
        ]
        month_cost = sum(float(d.estimated_cost or 0) for d in month_runs)

    # Daily aggregation
        daily_map: dict[str, dict] = defaultdict(
            lambda: {"cost": 0.0, "runs": 0, "tokens": 0}
        )
        for d in runs:
            day = d.created_at.strftime("%Y-%m-%d") if d.created_at else "unknown"
            daily_map[day]["cost"] += float(d.estimated_cost or 0)
            daily_map[day]["runs"] += 1
            daily_map[day]["tokens"] += d.tokens_total or 0
        daily_list = [
            {
                "date": k,
                "cost": round(v["cost"], 6),
                "runs": v["runs"],
                "tokens": v["tokens"],
            }
            for k, v in sorted(daily_map.items())
        ]

    # By model
        model_map: dict[str, dict] = defaultdict(
            lambda: {
                "cost": 0.0,
                "runs": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "provider": "unknown",
                "normalized_model": "unknown",
            }
        )
        for d in runs:
            provider, normalized_model, m = _provider_model_key(d.model_used)
            model_map[m]["provider"] = provider
            model_map[m]["normalized_model"] = normalized_model
            model_map[m]["cost"] += float(d.estimated_cost or 0)
            model_map[m]["runs"] += 1
            model_map[m]["input_tokens"] += d.tokens_input or 0
            model_map[m]["output_tokens"] += d.tokens_output or 0
        by_model = sorted(
            [
                {"model": k, **v, "cost": round(v["cost"], 6)}
                for k, v in model_map.items()
            ],
            key=lambda x: x["cost"],
            reverse=True,
        )
        by_provider_model = by_model

    # By skill
        skill_map: dict[str, dict] = defaultdict(
            lambda: {"cost": 0.0, "runs": 0, "tokens": 0, "successes": 0, "failures": 0}
        )
        for d in runs:
            sk = d.skill_used or "unknown"
            skill_map[sk]["cost"] += float(d.estimated_cost or 0)
            skill_map[sk]["runs"] += 1
            skill_map[sk]["tokens"] += d.tokens_total or 0
            if d.status == "completed":
                skill_map[sk]["successes"] += 1
            elif d.status == "error":
                skill_map[sk]["failures"] += 1
        by_skill = sorted(
            [
                {"skill": k, **v, "cost": round(v["cost"], 6)}
                for k, v in skill_map.items()
            ],
            key=lambda x: x["cost"],
            reverse=True,
        )

    # Top ideas by cost
        idea_map: dict[str, dict] = defaultdict(
            lambda: {"cost": 0.0, "runs": 0, "tokens": 0}
        )
        for d in runs:
            idea = d.idea_id or "unknown"
            idea_map[idea]["cost"] += float(d.estimated_cost or 0)
            idea_map[idea]["runs"] += 1
            idea_map[idea]["tokens"] += d.tokens_total or 0
        top_ideas = sorted(
            [
                {"idea_id": k, **v, "cost": round(v["cost"], 6)}
                for k, v in idea_map.items()
            ],
            key=lambda x: x["cost"],
            reverse=True,
        )[:10]

        return {
            "summary": {
                "total_cost": round(total_cost, 6),
                "total_runs": len(runs),
                "total_tokens": total_tokens,
                "total_input_tokens": total_input,
                "total_output_tokens": total_output,
                "total_cache_read": total_cache_read,
                "total_cache_write": total_cache_write,
                "tracking_coverage": round(
                    runs_with_tokens / len(runs), 3
                )
                if runs
                else 0,
            },
            "month": {
                "month_cost": round(month_cost, 6),
                "month_runs": len(month_runs),
            },
            "daily": daily_list,
            "by_model": by_model,
            "by_provider_model": by_provider_model,
            "by_skill": by_skill,
            "runs": [_serialize_run(d) for d in runs],
            "top_ideas": top_ideas,
        }

    return await run_db(db, _list)
