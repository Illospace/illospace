from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_metrics_module():
    root = Path(__file__).resolve().parents[1]
    path = root / "scripts" / "runtime_resilience_metrics.py"
    spec = importlib.util.spec_from_file_location("runtime_resilience_metrics", path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_boundary_scan_requires_deadlines_for_named_external_boundaries(tmp_path):
    metrics = _load_metrics_module()
    path = tmp_path / "boundaries.py"
    path.write_text(
        '''
from brain.platform.async_io import async_http_client, popen_sync, run_subprocess_sync

def worker():
    run_subprocess_sync(["git", "status"])
    run_subprocess_sync(["git", "status"], timeout=2)
    async_http_client(timeout=3)
    popen_sync(["sleep", "1"])
''',
        encoding="utf-8",
    )

    matches = list(metrics.boundary_matches(path, "brain/systems/example.py", "systems"))
    categories = [match.category for match in matches]

    assert categories.count("external_boundary_refs_missing_deadline") == 1
    assert categories.count("deadline_bound_external_boundary_refs") == 2
    assert categories.count("managed_process_boundary_refs") == 1


def test_scorecard_composes_static_resilience_debt():
    metrics = _load_metrics_module()
    async_io_summary = {
        "targets": {},
        "metrics": {
            "production_async_io_debt": 1,
            "unbounded_create_task_refs": 0,
            "unbounded_gather_refs": 1,
            "unbounded_queue_refs": 0,
            "isolated_sync_boundary_refs": 2,
            "outer_async_runner_refs": 3,
            "sync_io_edge_refs": 4,
        },
    }
    async_db_summary = {
        "targets": {},
        "metrics": {
            "repo_wide_sync_shaped_refs": 2,
            "required_sqlalchemy_run_sync_refs": 1,
        },
        "exemptions": {},
    }
    runtime_boundary_summary = {
        "targets": {},
        "metrics": {
            "raw_policy_refs_outside_runtime": 3,
            "runtime_boundary_coverage_percent": 90.0,
            "raw_external_timeout_coverage_percent": 80.0,
            "named_runtime_boundary_refs": 10,
        },
    }
    external_matches = [
        metrics.BoundaryMatch(
            path="brain/systems/example.py",
            line_number=10,
            category="external_boundary_refs_missing_deadline",
            scope="systems",
            boundary="run_subprocess_sync",
            has_deadline=False,
            line="run_subprocess_sync(cmd)",
        ),
        metrics.BoundaryMatch(
            path="brain/systems/example.py",
            line_number=11,
            category="deadline_bound_external_boundary_refs",
            scope="systems",
            boundary="http_post",
            has_deadline=True,
            line="http_post(url, timeout=3)",
        ),
    ]

    summary = metrics.summarize_scorecard(
        async_io_summary=async_io_summary,
        async_db_summary=async_db_summary,
        runtime_boundary_summary=runtime_boundary_summary,
        external_boundary_matches=external_matches,
        top=5,
    )

    assert summary["metrics"]["runtime_resilience_static_debt"] == 8
    assert summary["metrics"]["external_boundary_refs_missing_deadline"] == 1
    assert summary["metrics"]["external_boundary_deadline_coverage_percent"] == 50.0
