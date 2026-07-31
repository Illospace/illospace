"""Best-effort settlement bridge from agent runs to cycle runs."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def async_finalize_cycle_run_if_needed(
    run_id: int,
    *,
    status: str,
    error: str | None = None,
) -> None:
    try:
        from brain.systems.cycles.service import async_finalize_cycle_run_from_run

        await async_finalize_cycle_run_from_run(int(run_id), status=status, error=error)
    except Exception:
        logger.exception(
            "cycle_run_settlement_failed",
            extra={"run_id": run_id, "status": status},
        )
