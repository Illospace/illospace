from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_metrics_module():
    root = Path(__file__).resolve().parents[1]
    path = root / "scripts" / "runtime_boundary_metrics.py"
    spec = importlib.util.spec_from_file_location("runtime_boundary_metrics", path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_runtime_boundary_metric_counts_raw_and_named_boundaries(tmp_path):
    metrics = _load_metrics_module()
    path = tmp_path / "boundaries.py"
    path.write_text(
        '''
import asyncio
import httpx
import subprocess
from brain.platform.async_io import run_subprocess, read_text

async def async_handler():
    await asyncio.to_thread(lambda: None)
    httpx.AsyncClient()
    subprocess.run(["echo", "hi"], timeout=1)
    await run_subprocess(["echo", "hi"], timeout=1)
    await read_text("payload.txt")

def sync_edge():
    httpx.Client(timeout=3)
    subprocess.Popen(["sleep", "1"])
''',
        encoding="utf-8",
    )

    matches = list(metrics.ast_matches(path, "brain/systems/example.py", "systems"))
    categories = [match.category for match in matches]

    assert categories.count("raw_blocking_boundary_refs_outside_runtime") == 1
    assert categories.count("raw_http_client_policy_refs") == 2
    assert categories.count("raw_subprocess_policy_refs") == 2
    assert categories.count("named_runtime_boundary_refs") == 2

    summary = metrics.summarize(matches, top=10)
    assert summary["metrics"]["raw_policy_refs_outside_runtime"] == 5
    assert summary["metrics"]["async_external_io_refs_without_policy"] == 2
    assert summary["metrics"]["sync_external_io_in_async_refs"] == 1
    assert summary["metrics"]["undocumented_sync_edge_refs"] == 2
    assert summary["metrics"]["raw_external_refs_missing_timeout_policy"] == 2
    assert summary["metrics"]["named_runtime_boundary_refs"] == 2


def test_runtime_boundary_metric_ignores_boundary_module(tmp_path):
    metrics = _load_metrics_module()
    path = tmp_path / "async_io.py"
    path.write_text(
        '''
import asyncio
import subprocess

async def run_blocking(func):
    return await asyncio.to_thread(func)

async def run_subprocess(args):
    return await run_blocking(subprocess.run, args)
''',
        encoding="utf-8",
    )

    assert list(metrics.ast_matches(path, "brain/platform/async_io.py", "platform")) == []
