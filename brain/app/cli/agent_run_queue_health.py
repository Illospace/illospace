"""Deploy-facing AgentRun queue-starvation check."""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Sequence

from brain.systems.runs.cortex.queue_health import (
    QueueHealth,
    queued_backlog_health_snapshot_async,
)


def _queue_health_message(queue_health: QueueHealth) -> str:
    age = queue_health.oldest_queued_age_seconds
    age_label = f"{age}s" if age is not None else "unknown"
    threshold_label = f"{queue_health.watchdog_after_seconds:g}s"
    detail = (
        f"{queue_health.queued} run(s) queued; oldest queued for {age_label} "
        f"(threshold {threshold_label}); "
        f"{queue_health.recent_active_runs}/"
        f"{queue_health.configured_concurrency} runner slot(s) "
        "have recent claim activity"
    )
    if queue_health.stale_queued_backlog:
        return (
            f"AgentRun queue starvation: {detail}. "
            "No worker is claiming the queued backlog."
        )
    return f"AgentRun queue healthy: {detail}."


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check the shared AgentRun queue-starvation predicate.",
    )
    parser.add_argument(
        "--stale-after-seconds",
        type=float,
        default=None,
        help=(
            "Alarm after this many seconds without enough recent claims "
            "(default: ILLO_AGENT_RUN_QUEUED_WATCHDOG_SECONDS or 15)."
        ),
    )
    parser.add_argument(
        "--runner-concurrency",
        type=int,
        required=True,
        help="Configured concurrency of the worker service.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        queue_health = asyncio.run(
            queued_backlog_health_snapshot_async(
                stale_after_seconds=args.stale_after_seconds,
                configured_concurrency=args.runner_concurrency,
            )
        )
    except Exception as exc:
        print(
            "AgentRun queue health check failed: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 2
    message = _queue_health_message(queue_health)
    if queue_health.stale_queued_backlog:
        print(message, file=sys.stderr)
        return 1
    print(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
