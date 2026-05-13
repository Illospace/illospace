"""Telemetry helpers for agent runtime calls."""

from __future__ import annotations

from typing import Any
import logging

logger = logging.getLogger(__name__)


def _api_call_params(
    *,
    session_id: str | None,
    run_id: int | None,
    turn: int,
    model: str,
    tokens_input: int,
    tokens_output: int,
    cache_read: int,
    cache_write: int,
    context_messages: int,
    system_prompt_chars: int,
    status: str,
    stop_reason: str | None,
    latency_ms: int,
    error: str | None,
) -> dict[str, Any]:
    from brain.systems.runs.ids import trace_id_for_run_id

    return {
        "sid": session_id,
        "did": run_id,
        "turn": turn,
        "trace_id": trace_id_for_run_id(run_id),
        "model": model,
        "ti": tokens_input,
        "to": tokens_output,
        "cr": cache_read,
        "cw": cache_write,
        "ctx": context_messages,
        "spc": system_prompt_chars,
        "status": status,
        "stop": stop_reason,
        "lat": latency_ms,
        "err": error,
    }


async def async_record_api_call(
    session_id: str | None = None,
    run_id: int | None = None,
    turn: int = 0,
    model: str = "",
    tokens_input: int = 0,
    tokens_output: int = 0,
    cache_read: int = 0,
    cache_write: int = 0,
    context_messages: int = 0,
    system_prompt_chars: int = 0,
    status: str = "",
    stop_reason: str | None = None,
    latency_ms: int = 0,
    error: str | None = None,
    *,
    session: Any | None = None,
) -> None:
    """Record a single API call to agent_api_calls using native async DB access."""
    try:
        from sqlalchemy import text as sa_text
        from sqlalchemy.exc import SQLAlchemyError

        from brain.platform.db.repositories.unit_of_work import UnitOfWork

        params = _api_call_params(
            session_id=session_id,
            run_id=run_id,
            turn=turn,
            model=model,
            tokens_input=tokens_input,
            tokens_output=tokens_output,
            cache_read=cache_read,
            cache_write=cache_write,
            context_messages=context_messages,
            system_prompt_chars=system_prompt_chars,
            status=status,
            stop_reason=stop_reason,
            latency_ms=latency_ms,
            error=error,
        )

        async def _write(active_session: Any) -> None:
            try:
                await active_session.execute(sa_text(
                    "INSERT INTO agent_api_calls "
                    "(session_id, run_id, trace_id, turn_number, model, tokens_input, tokens_output, "
                    "cache_read, cache_write, context_messages, system_prompt_chars, "
                    "status, stop_reason, latency_ms, error) "
                    "VALUES (:sid, :did, :trace_id, :turn, :model, :ti, :to, :cr, :cw, :ctx, :spc, "
                    ":status, :stop, :lat, :err)"
                ), params)
            except SQLAlchemyError:
                await active_session.rollback()
                await active_session.execute(sa_text(
                    "INSERT INTO agent_api_calls "
                    "(session_id, run_id, turn_number, model, tokens_input, tokens_output, "
                    "cache_read, cache_write, context_messages, system_prompt_chars, "
                    "status, stop_reason, latency_ms, error) "
                    "VALUES (:sid, :did, :turn, :model, :ti, :to, :cr, :cw, :ctx, :spc, "
                    ":status, :stop, :lat, :err)"
                ), params)

        if session is not None:
            await _write(session)
            return
        async with UnitOfWork() as uow:
            await _write(uow.session)
    except Exception as exc:
        logger.warning("agent_api_call_telemetry_failed: %s", exc)


def record_api_call(
    session_id: str | None = None,
    run_id: int | None = None,
    turn: int = 0,
    model: str = "",
    tokens_input: int = 0,
    tokens_output: int = 0,
    cache_read: int = 0,
    cache_write: int = 0,
    context_messages: int = 0,
    system_prompt_chars: int = 0,
    status: str = "",
    stop_reason: str | None = None,
    latency_ms: int = 0,
    error: str | None = None,
) -> None:
    """Sync compatibility surface.

    Sync agent runtimes should not hide DB writes behind a background thread.
    Async runtimes should await ``async_record_api_call`` for persistence.
    """
    del (
        session_id,
        run_id,
        turn,
        model,
        tokens_input,
        tokens_output,
        cache_read,
        cache_write,
        context_messages,
        system_prompt_chars,
        status,
        stop_reason,
        latency_ms,
        error,
    )
    logger.debug("agent_api_call_telemetry_skipped_sync_runtime")
