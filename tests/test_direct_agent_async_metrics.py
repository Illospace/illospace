from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_metrics_module():
    root = Path(__file__).resolve().parents[1]
    path = root / "scripts" / "direct_agent_async_metrics.py"
    spec = importlib.util.spec_from_file_location("direct_agent_async_metrics", path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_direct_agent_async_migration_metrics_are_below_current_baseline():
    metrics_module = _load_metrics_module()
    summary = metrics_module.summarize(metrics_module.refs())

    metrics = summary["metrics"]
    baselines = summary["baselines"]
    targets = summary["targets"]

    assert all(target == 0 for target in targets.values())
    for name, baseline in baselines.items():
        assert metrics[name] <= baseline, f"{name} grew from {baseline} to {metrics[name]}"


def test_direct_agent_async_migration_baseline_names_core_rewrite_goals():
    metrics_module = _load_metrics_module()

    assert set(metrics_module.BASELINES) == {
        "missing_async_run_agent_entrypoint",
        "recipe_thread_bridge_calls",
        "threaded_direct_agent_run_blocking_calls",
        "direct_agent_sync_session_refs",
        "sync_run_agent_auth_resolver_refs",
        "direct_retry_blocking_sleep_calls",
        "cancel_token_asyncio_run_calls",
    }


def test_direct_agent_async_completion_gate_requires_zero_legacy_count():
    metrics_module = _load_metrics_module()

    assert metrics_module.main(["--target", "complete"]) == 0
