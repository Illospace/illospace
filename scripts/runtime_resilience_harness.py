#!/usr/bin/env python3
"""Run a lightweight runtime resilience harness.

The default harness is deterministic and local: it measures event-loop lag,
bounded queue drain, timeout cleanup, and cancellation cleanup without needing
a database or running API server. Pass --base-url to add a live HTTP probe.
"""
from __future__ import annotations

import argparse
import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import json
import statistics
import time
from typing import Any

import httpx


DEFAULT_LIVE_PATH = "/api/system/health/live"


@dataclass(frozen=True)
class HarnessConfig:
    concurrency: int = 16
    queue_items: int = 128
    queue_maxsize: int = 32
    queue_work_ms: float = 5.0
    timeout_tasks: int = 32
    timeout_ms: float = 25.0
    cancel_tasks: int = 32
    cancel_after_ms: float = 20.0
    cleanup_timeout_ms: float = 500.0
    lag_interval_ms: float = 5.0
    base_url: str | None = None
    live_path: str = DEFAULT_LIVE_PATH
    live_requests: int = 64
    live_timeout_ms: float = 1000.0


@dataclass(frozen=True)
class HarnessTargets:
    max_event_loop_lag_p95_ms: float = 50.0
    max_event_loop_lag_max_ms: float = 250.0
    max_queue_drain_ms: float = 5000.0
    max_queue_put_wait_p95_ms: float = 50.0
    max_timeout_cleanup_ms: float = 500.0
    max_cancel_cleanup_ms: float = 500.0
    max_live_api_p95_ms: float = 1000.0


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = (len(ordered) - 1) * pct
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    if lower == upper:
        return ordered[lower]
    weight = index - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _summary(values: list[float]) -> dict[str, float]:
    if not values:
        return {"count": 0, "min_ms": 0.0, "p50_ms": 0.0, "p95_ms": 0.0, "max_ms": 0.0, "avg_ms": 0.0}
    return {
        "count": len(values),
        "min_ms": min(values),
        "p50_ms": statistics.median(values),
        "p95_ms": percentile(values, 0.95),
        "max_ms": max(values),
        "avg_ms": statistics.fmean(values),
    }


async def _lag_probe(stop: asyncio.Event, *, interval_ms: float, samples: list[float]) -> None:
    interval_s = interval_ms / 1000
    expected = time.perf_counter() + interval_s
    while not stop.is_set():
        await asyncio.sleep(interval_s)
        now = time.perf_counter()
        samples.append(max(0.0, (now - expected) * 1000))
        expected = now + interval_s


async def _with_lag_probe(
    operation: Callable[[], Awaitable[dict[str, Any]]],
    *,
    interval_ms: float,
) -> tuple[dict[str, Any], dict[str, float]]:
    stop = asyncio.Event()
    samples: list[float] = []
    probe = asyncio.create_task(_lag_probe(stop, interval_ms=interval_ms, samples=samples))
    try:
        result = await operation()
    finally:
        stop.set()
        try:
            await asyncio.wait_for(probe, timeout=max(0.1, interval_ms / 1000 * 4))
        except asyncio.TimeoutError:
            probe.cancel()
            await asyncio.gather(probe, return_exceptions=True)
    return result, _summary(samples)


async def run_queue_probe(config: HarnessConfig) -> dict[str, Any]:
    queue: asyncio.Queue[int | None] = asyncio.Queue(maxsize=max(1, config.queue_maxsize))
    put_wait_ms: list[float] = []
    completed = 0
    max_depth = 0
    lock = asyncio.Lock()

    async def worker() -> None:
        nonlocal completed
        while True:
            item = await queue.get()
            try:
                if item is None:
                    return
                await asyncio.sleep(config.queue_work_ms / 1000)
                async with lock:
                    completed += 1
            finally:
                queue.task_done()

    workers = [
        asyncio.create_task(worker(), name=f"runtime-resilience-worker-{index}")
        for index in range(max(1, config.concurrency))
    ]
    started = time.perf_counter()
    for item in range(max(0, config.queue_items)):
        before_put = time.perf_counter()
        await queue.put(item)
        put_wait_ms.append((time.perf_counter() - before_put) * 1000)
        max_depth = max(max_depth, queue.qsize())
    await queue.join()
    drain_ms = (time.perf_counter() - started) * 1000

    for _ in workers:
        await queue.put(None)
    await asyncio.gather(*workers)
    return {
        "completed": completed,
        "expected": max(0, config.queue_items),
        "failures": max(0, config.queue_items) - completed,
        "drain_ms": drain_ms,
        "max_depth": max_depth,
        "maxsize": queue.maxsize,
        "put_wait": _summary(put_wait_ms),
    }


async def run_timeout_probe(config: HarnessConfig) -> dict[str, Any]:
    async def timeout_task() -> str:
        try:
            async with asyncio.timeout(config.timeout_ms / 1000):
                await asyncio.sleep(config.timeout_ms / 1000 * 10)
            return "completed"
        except TimeoutError:
            return "timed_out"

    started = time.perf_counter()
    results = await asyncio.gather(*(timeout_task() for _ in range(max(0, config.timeout_tasks))))
    cleanup_ms = (time.perf_counter() - started) * 1000
    timed_out = sum(1 for result in results if result == "timed_out")
    return {
        "tasks": max(0, config.timeout_tasks),
        "timed_out": timed_out,
        "unexpected_completed": len(results) - timed_out,
        "cleanup_ms": cleanup_ms,
    }


async def run_cancel_probe(config: HarnessConfig) -> dict[str, Any]:
    started = asyncio.Event()
    cleanup_seen = 0

    async def cancellable_task() -> None:
        nonlocal cleanup_seen
        started.set()
        try:
            while True:
                await asyncio.sleep(60)
        finally:
            cleanup_seen += 1

    tasks = [
        asyncio.create_task(cancellable_task(), name=f"runtime-resilience-cancel-{index}")
        for index in range(max(0, config.cancel_tasks))
    ]
    if tasks:
        await started.wait()
    await asyncio.sleep(config.cancel_after_ms / 1000)
    cleanup_started = time.perf_counter()
    for task in tasks:
        task.cancel()
    done, pending = await asyncio.wait(tasks, timeout=config.cleanup_timeout_ms / 1000)
    cleanup_ms = (time.perf_counter() - cleanup_started) * 1000
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    await asyncio.gather(*done, return_exceptions=True)
    return {
        "tasks": len(tasks),
        "cancelled": sum(1 for task in done if task.cancelled()),
        "cleanup_seen": cleanup_seen,
        "pending_after_cleanup": len(pending),
        "cleanup_ms": cleanup_ms,
    }


async def run_live_api_probe(config: HarnessConfig) -> dict[str, Any]:
    if not config.base_url:
        return {"skipped": True, "reason": "base_url not set"}

    timings: list[float] = []
    failures: list[str] = []
    limits = httpx.Limits(max_connections=max(1, config.concurrency), max_keepalive_connections=max(1, config.concurrency))
    timeout = httpx.Timeout(config.live_timeout_ms / 1000)
    semaphore = asyncio.Semaphore(max(1, config.concurrency))

    async with httpx.AsyncClient(base_url=config.base_url, timeout=timeout, limits=limits) as client:
        async def one_request(index: int) -> None:
            async with semaphore:
                started = time.perf_counter()
                try:
                    response = await client.get(config.live_path)
                    elapsed_ms = (time.perf_counter() - started) * 1000
                    timings.append(elapsed_ms)
                    if response.status_code >= 500:
                        failures.append(f"{index}:HTTP {response.status_code}")
                except Exception as exc:
                    timings.append((time.perf_counter() - started) * 1000)
                    failures.append(f"{index}:{type(exc).__name__}")

        await asyncio.gather(*(one_request(index) for index in range(max(0, config.live_requests))))

    return {
        "skipped": False,
        "requests": max(0, config.live_requests),
        "failures": len(failures),
        "first_error": failures[0] if failures else None,
        "latency": _summary(timings),
    }


async def run_harness(config: HarnessConfig) -> dict[str, Any]:
    queue_result, queue_lag = await _with_lag_probe(
        lambda: run_queue_probe(config),
        interval_ms=config.lag_interval_ms,
    )
    timeout_result, timeout_lag = await _with_lag_probe(
        lambda: run_timeout_probe(config),
        interval_ms=config.lag_interval_ms,
    )
    cancel_result, cancel_lag = await _with_lag_probe(
        lambda: run_cancel_probe(config),
        interval_ms=config.lag_interval_ms,
    )
    live_result, live_lag = await _with_lag_probe(
        lambda: run_live_api_probe(config),
        interval_ms=config.lag_interval_ms,
    )
    lag_p95_values = [
        queue_lag["p95_ms"],
        timeout_lag["p95_ms"],
        cancel_lag["p95_ms"],
        live_lag["p95_ms"],
    ]
    lag_max_values = [
        queue_lag["max_ms"],
        timeout_lag["max_ms"],
        cancel_lag["max_ms"],
        live_lag["max_ms"],
    ]
    return {
        "config": {
            "concurrency": config.concurrency,
            "queue_items": config.queue_items,
            "queue_maxsize": config.queue_maxsize,
            "queue_work_ms": config.queue_work_ms,
            "timeout_tasks": config.timeout_tasks,
            "timeout_ms": config.timeout_ms,
            "cancel_tasks": config.cancel_tasks,
            "cancel_after_ms": config.cancel_after_ms,
            "cleanup_timeout_ms": config.cleanup_timeout_ms,
            "lag_interval_ms": config.lag_interval_ms,
            "base_url": config.base_url,
            "live_path": config.live_path,
            "live_requests": config.live_requests,
            "live_timeout_ms": config.live_timeout_ms,
        },
        "metrics": {
            "event_loop_lag_p95_ms": max(lag_p95_values),
            "event_loop_lag_max_ms": max(lag_max_values),
            "queue_drain_ms": queue_result["drain_ms"],
            "queue_put_wait_p95_ms": queue_result["put_wait"]["p95_ms"],
            "queue_failures": queue_result["failures"],
            "timeout_cleanup_ms": timeout_result["cleanup_ms"],
            "timeout_unexpected_completed": timeout_result["unexpected_completed"],
            "cancel_cleanup_ms": cancel_result["cleanup_ms"],
            "cancel_pending_after_cleanup": cancel_result["pending_after_cleanup"],
            "live_api_p95_ms": 0.0 if live_result.get("skipped") else live_result["latency"]["p95_ms"],
            "live_api_failures": 0 if live_result.get("skipped") else live_result["failures"],
        },
        "scenarios": {
            "queue": {"result": queue_result, "event_loop_lag": queue_lag},
            "timeout": {"result": timeout_result, "event_loop_lag": timeout_lag},
            "cancel": {"result": cancel_result, "event_loop_lag": cancel_lag},
            "live_api": {"result": live_result, "event_loop_lag": live_lag},
        },
    }


def evaluate(summary: dict[str, Any], targets: HarnessTargets) -> dict[str, Any]:
    metrics = summary["metrics"]
    checks = {
        "event_loop_lag_p95_ms": metrics["event_loop_lag_p95_ms"] <= targets.max_event_loop_lag_p95_ms,
        "event_loop_lag_max_ms": metrics["event_loop_lag_max_ms"] <= targets.max_event_loop_lag_max_ms,
        "queue_drain_ms": metrics["queue_drain_ms"] <= targets.max_queue_drain_ms,
        "queue_put_wait_p95_ms": metrics["queue_put_wait_p95_ms"] <= targets.max_queue_put_wait_p95_ms,
        "queue_failures": metrics["queue_failures"] == 0,
        "timeout_cleanup_ms": metrics["timeout_cleanup_ms"] <= targets.max_timeout_cleanup_ms,
        "timeout_unexpected_completed": metrics["timeout_unexpected_completed"] == 0,
        "cancel_cleanup_ms": metrics["cancel_cleanup_ms"] <= targets.max_cancel_cleanup_ms,
        "cancel_pending_after_cleanup": metrics["cancel_pending_after_cleanup"] == 0,
        "live_api_p95_ms": metrics["live_api_p95_ms"] <= targets.max_live_api_p95_ms,
        "live_api_failures": metrics["live_api_failures"] == 0,
    }
    target_payload = {
        "max_event_loop_lag_p95_ms": targets.max_event_loop_lag_p95_ms,
        "max_event_loop_lag_max_ms": targets.max_event_loop_lag_max_ms,
        "max_queue_drain_ms": targets.max_queue_drain_ms,
        "max_queue_put_wait_p95_ms": targets.max_queue_put_wait_p95_ms,
        "max_timeout_cleanup_ms": targets.max_timeout_cleanup_ms,
        "max_cancel_cleanup_ms": targets.max_cancel_cleanup_ms,
        "max_live_api_p95_ms": targets.max_live_api_p95_ms,
    }
    passed = all(checks.values())
    return {"passed": passed, "checks": checks, "targets": target_payload}


def print_text(summary: dict[str, Any], evaluation: dict[str, Any]) -> None:
    metrics = summary["metrics"]
    targets = evaluation["targets"]
    print("Runtime resilience harness")
    print()
    print("Metric                              Current  Target")
    print("----------------------------------  -------  ------")
    rows = [
        ("event_loop_lag_p95_ms", "max_event_loop_lag_p95_ms"),
        ("event_loop_lag_max_ms", "max_event_loop_lag_max_ms"),
        ("queue_drain_ms", "max_queue_drain_ms"),
        ("queue_put_wait_p95_ms", "max_queue_put_wait_p95_ms"),
        ("timeout_cleanup_ms", "max_timeout_cleanup_ms"),
        ("cancel_cleanup_ms", "max_cancel_cleanup_ms"),
        ("live_api_p95_ms", "max_live_api_p95_ms"),
    ]
    for metric_name, target_name in rows:
        print(f"{metric_name:<34}  {metrics[metric_name]:>7.1f}  {targets[target_name]:>6.1f}")
    print(f"{'queue_failures':<34}  {metrics['queue_failures']:>7}  {0:>6}")
    print(f"{'timeout_unexpected_completed':<34}  {metrics['timeout_unexpected_completed']:>7}  {0:>6}")
    print(f"{'cancel_pending_after_cleanup':<34}  {metrics['cancel_pending_after_cleanup']:>7}  {0:>6}")
    print(f"{'live_api_failures':<34}  {metrics['live_api_failures']:>7}  {0:>6}")
    print()
    print("Result")
    print(f"  {'PASS' if evaluation['passed'] else 'FAIL'}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument("--target", action="store_true", help="exit non-zero when thresholds fail")
    parser.add_argument("--concurrency", type=int, default=16)
    parser.add_argument("--queue-items", type=int, default=128)
    parser.add_argument("--queue-maxsize", type=int, default=32)
    parser.add_argument("--queue-work-ms", type=float, default=5.0)
    parser.add_argument("--timeout-tasks", type=int, default=32)
    parser.add_argument("--timeout-ms", type=float, default=25.0)
    parser.add_argument("--cancel-tasks", type=int, default=32)
    parser.add_argument("--cancel-after-ms", type=float, default=20.0)
    parser.add_argument("--cleanup-timeout-ms", type=float, default=500.0)
    parser.add_argument("--lag-interval-ms", type=float, default=5.0)
    parser.add_argument("--base-url", default=None, help="optional live API base URL")
    parser.add_argument("--live-path", default=DEFAULT_LIVE_PATH)
    parser.add_argument("--live-requests", type=int, default=64)
    parser.add_argument("--live-timeout-ms", type=float, default=1000.0)
    parser.add_argument("--max-event-loop-lag-p95-ms", type=float, default=50.0)
    parser.add_argument("--max-event-loop-lag-max-ms", type=float, default=250.0)
    parser.add_argument("--max-queue-drain-ms", type=float, default=5000.0)
    parser.add_argument("--max-queue-put-wait-p95-ms", type=float, default=50.0)
    parser.add_argument("--max-timeout-cleanup-ms", type=float, default=500.0)
    parser.add_argument("--max-cancel-cleanup-ms", type=float, default=500.0)
    parser.add_argument("--max-live-api-p95-ms", type=float, default=1000.0)
    return parser.parse_args(argv)


async def async_main(args: argparse.Namespace) -> int:
    config = HarnessConfig(
        concurrency=max(1, args.concurrency),
        queue_items=max(0, args.queue_items),
        queue_maxsize=max(1, args.queue_maxsize),
        queue_work_ms=max(0.0, args.queue_work_ms),
        timeout_tasks=max(0, args.timeout_tasks),
        timeout_ms=max(1.0, args.timeout_ms),
        cancel_tasks=max(0, args.cancel_tasks),
        cancel_after_ms=max(0.0, args.cancel_after_ms),
        cleanup_timeout_ms=max(1.0, args.cleanup_timeout_ms),
        lag_interval_ms=max(1.0, args.lag_interval_ms),
        base_url=args.base_url,
        live_path=args.live_path,
        live_requests=max(0, args.live_requests),
        live_timeout_ms=max(1.0, args.live_timeout_ms),
    )
    targets = HarnessTargets(
        max_event_loop_lag_p95_ms=args.max_event_loop_lag_p95_ms,
        max_event_loop_lag_max_ms=args.max_event_loop_lag_max_ms,
        max_queue_drain_ms=args.max_queue_drain_ms,
        max_queue_put_wait_p95_ms=args.max_queue_put_wait_p95_ms,
        max_timeout_cleanup_ms=args.max_timeout_cleanup_ms,
        max_cancel_cleanup_ms=args.max_cancel_cleanup_ms,
        max_live_api_p95_ms=args.max_live_api_p95_ms,
    )
    summary = await run_harness(config)
    evaluation = evaluate(summary, targets)
    payload = {**summary, "evaluation": evaluation}
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print_text(summary, evaluation)
    return 1 if args.target and not evaluation["passed"] else 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(async_main(parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
