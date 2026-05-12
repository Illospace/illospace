"""Telemetry helpers for agent runtime calls."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


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
    """Record a single API call to agent_api_calls. Fire-and-forget."""

    def _write() -> None:
        try:
            from sqlalchemy import text as sa_text
            from sqlalchemy.exc import SQLAlchemyError

            from brain.systems.runs.cortex.recording import trace_id_for_run_id
            from brain.platform.db.repositories.unit_of_work import UnitOfWork, open_unit_of_work

            trace_id = trace_id_for_run_id(run_id)
            with open_unit_of_work(UnitOfWork) as uow:
                params = {
                    "sid": session_id,
                    "did": run_id,
                    "turn": turn,
                    "trace_id": trace_id,
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
                try:
                    uow.session.execute(sa_text(
                        "INSERT INTO agent_api_calls "
                        "(session_id, run_id, trace_id, turn_number, model, tokens_input, tokens_output, "
                        "cache_read, cache_write, context_messages, system_prompt_chars, "
                        "status, stop_reason, latency_ms, error) "
                        "VALUES (:sid, :did, :trace_id, :turn, :model, :ti, :to, :cr, :cw, :ctx, :spc, "
                        ":status, :stop, :lat, :err)"
                    ), params)
                except SQLAlchemyError:
                    uow.session.rollback()
                    uow.session.execute(sa_text(
                        "INSERT INTO agent_api_calls "
                        "(session_id, run_id, turn_number, model, tokens_input, tokens_output, "
                        "cache_read, cache_write, context_messages, system_prompt_chars, "
                        "status, stop_reason, latency_ms, error) "
                        "VALUES (:sid, :did, :turn, :model, :ti, :to, :cr, :cw, :ctx, :spc, "
                        ":status, :stop, :lat, :err)"
                    ), params)
        except Exception as exc:
            logger.warning("agent_api_call_telemetry_failed: %s", exc)

    import threading

    threading.Thread(target=_write, daemon=True).start()
