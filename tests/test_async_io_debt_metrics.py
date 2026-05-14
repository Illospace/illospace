from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_metrics_module():
    root = Path(__file__).resolve().parents[1]
    path = root / "scripts" / "async_io_debt_metrics.py"
    spec = importlib.util.spec_from_file_location("async_io_debt_metrics", path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_metric_counts_blocking_io_shapes_in_async_production_code(tmp_path):
    metrics = _load_metrics_module()
    path = tmp_path / "blocking.py"
    path.write_text(
        '''
import asyncio
import subprocess
import time
from pathlib import Path

TEXT = "requests.get and time.sleep in a string are not code"

async def handler():
    time.sleep(1)
    Path("payload.json").read_text()
    subprocess.run(["echo", "hi"])
    asyncio.create_task(handler())
    await asyncio.gather(handler())
    queue = asyncio.Queue()
    return queue
''',
        encoding="utf-8",
    )

    matches = list(metrics.ast_matches(path, "brain/app/api/blocking.py", "api"))
    categories = [match.category for match in matches]

    assert categories.count("blocking_sleep_in_async_refs") == 1
    assert categories.count("sync_filesystem_in_async_refs") == 1
    assert categories.count("sync_subprocess_refs") == 1
    assert categories.count("unbounded_create_task_refs") == 1
    assert categories.count("unbounded_gather_refs") == 1
    assert categories.count("unbounded_queue_refs") == 1


def test_metric_counts_sync_http_and_inner_event_loop_runners(tmp_path):
    metrics = _load_metrics_module()
    path = tmp_path / "http.py"
    path.write_text(
        '''
import asyncio
import httpx
import requests
from urllib.request import urlopen

def sync_helper():
    response = requests.get("https://example.com")
    client = httpx.Client()
    page = urlopen("https://example.com")
    return response, client, page

def bad_runner(coro):
    return asyncio.run(coro)

def main(coro):
    return asyncio.run(coro)
''',
        encoding="utf-8",
    )

    matches = list(metrics.ast_matches(path, "brain/jobs/http.py", "jobs"))
    categories = [match.category for match in matches]

    assert categories.count("sync_http_client_refs") == 3
    assert categories.count("asyncio_run_inner_refs") == 1
    assert categories.count("outer_async_runner_refs") == 1


def test_metric_ignores_true_edges_and_isolated_sync_boundaries(tmp_path):
    metrics = _load_metrics_module()
    path = tmp_path / "edge.py"
    path.write_text(
        '''
import asyncio
import requests
from pathlib import Path

async def handler():
    await asyncio.to_thread(requests.get, "https://example.com")
    await asyncio.to_thread(lambda: Path("payload.json").read_text())
''',
        encoding="utf-8",
    )

    cli_matches = list(metrics.ast_matches(path, "brain/app/cli/edge.py", "cli"))
    production_matches = list(metrics.ast_matches(path, "brain/app/api/edge.py", "api"))
    categories = [match.category for match in production_matches]

    assert cli_matches == []
    assert categories.count("isolated_sync_boundary_refs") == 2
    assert "sync_http_client_refs" not in categories
    assert "sync_filesystem_in_async_refs" not in categories


def test_metric_summary_separates_debt_from_isolated_boundaries():
    metrics = _load_metrics_module()
    matches = [
        metrics.Match(
            path="brain/app/api/routes.py",
            line_number=10,
            category="sync_http_client_refs",
            scope="api",
            in_async_function=False,
            line="requests.get(url)",
        ),
        metrics.Match(
            path="brain/app/api/routes.py",
            line_number=11,
            category="isolated_sync_boundary_refs",
            scope="api",
            in_async_function=True,
            line="await asyncio.to_thread(fn)",
        ),
    ]

    summary = metrics.summarize(matches, top=10)

    assert summary["metrics"]["production_async_io_debt"] == 1
    assert summary["metrics"]["api_async_io_debt"] == 1
    assert summary["metrics"]["isolated_sync_boundary_refs"] == 1
    assert summary["informational"]["isolated_sync_boundary_refs"] == 1
