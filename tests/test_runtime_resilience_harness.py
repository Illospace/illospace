from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_harness_module():
    root = Path(__file__).resolve().parents[1]
    path = root / "scripts" / "runtime_resilience_harness.py"
    spec = importlib.util.spec_from_file_location("runtime_resilience_harness", path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_percentile_interpolates_values():
    harness = _load_harness_module()

    assert harness.percentile([10, 20, 30], 0.5) == 20
    assert harness.percentile([0, 100], 0.95) == 95


def test_evaluate_fails_on_cleanup_and_queue_debt():
    harness = _load_harness_module()
    summary = {
        "metrics": {
            "event_loop_lag_p95_ms": 1.0,
            "event_loop_lag_max_ms": 2.0,
            "queue_drain_ms": 10.0,
            "queue_put_wait_p95_ms": 1.0,
            "queue_failures": 1,
            "timeout_cleanup_ms": 10.0,
            "timeout_unexpected_completed": 0,
            "cancel_cleanup_ms": 10.0,
            "cancel_pending_after_cleanup": 1,
            "live_api_p95_ms": 0.0,
            "live_api_failures": 0,
        }
    }

    evaluation = harness.evaluate(summary, harness.HarnessTargets())

    assert evaluation["passed"] is False
    assert evaluation["checks"]["queue_failures"] is False
    assert evaluation["checks"]["cancel_pending_after_cleanup"] is False


def test_local_harness_produces_passing_default_shape():
    harness = _load_harness_module()
    config = harness.HarnessConfig(
        concurrency=4,
        queue_items=16,
        queue_maxsize=4,
        queue_work_ms=1.0,
        timeout_tasks=4,
        timeout_ms=5.0,
        cancel_tasks=4,
        cancel_after_ms=1.0,
        cleanup_timeout_ms=200.0,
        live_requests=0,
    )

    summary = harness.asyncio.run(harness.run_harness(config))
    evaluation = harness.evaluate(summary, harness.HarnessTargets(max_queue_drain_ms=1000.0))

    assert summary["scenarios"]["live_api"]["result"]["skipped"] is True
    assert summary["metrics"]["queue_failures"] == 0
    assert summary["metrics"]["timeout_unexpected_completed"] == 0
    assert summary["metrics"]["cancel_pending_after_cleanup"] == 0
    assert evaluation["passed"] is True
