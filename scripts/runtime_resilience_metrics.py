#!/usr/bin/env python3
"""Composite runtime resilience scorecard for async-first production paths.

This scorecard answers a broader question than the individual debt metrics:
are production I/O boundaries async-shaped, centralized, bounded by deadlines,
and free of unbounded concurrency shapes?
"""
from __future__ import annotations

import argparse
import ast
from collections import Counter
from dataclasses import dataclass
import importlib.util
import json
from pathlib import Path
import sys
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ROOTS = ("brain",)
RUNTIME_BOUNDARY_PREFIX = "brain.platform.async_io."
RUNTIME_BOUNDARY_MODULES = {
    "brain/platform/async_io.py",
}

DEADLINE_REQUIRED_BOUNDARIES = {
    "async_http_client",
    "async_http_get",
    "async_http_post",
    "check_output",
    "check_output_sync",
    "http_get",
    "http_post",
    "run_subprocess",
    "run_subprocess_sync",
    "sync_http_client",
}
PROCESS_LIFECYCLE_BOUNDARIES = {
    "popen",
    "popen_sync",
}
EXTERNAL_BOUNDARIES = DEADLINE_REQUIRED_BOUNDARIES | PROCESS_LIFECYCLE_BOUNDARIES


@dataclass(frozen=True)
class BoundaryMatch:
    path: str
    line_number: int
    category: str
    scope: str
    boundary: str
    has_deadline: bool
    line: str


def _load_metric_module(name: str):
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_async_io_metrics = _load_metric_module("async_io_debt_metrics")
_async_db_metrics = _load_metric_module("async_db_debt_metrics")
_runtime_boundary_metrics = _load_metric_module("runtime_boundary_metrics")


def _line(lines: list[str], node: ast.AST) -> str:
    return lines[node.lineno - 1].strip()


def _has_deadline(node: ast.Call) -> bool:
    for keyword in node.keywords:
        if keyword.arg != "timeout":
            continue
        if isinstance(keyword.value, ast.Constant) and keyword.value.value is None:
            return False
        return True
    return False


def _boundary_name(call_name: str | None) -> str | None:
    if not call_name or not call_name.startswith(RUNTIME_BOUNDARY_PREFIX):
        return None
    short_name = call_name.rsplit(".", 1)[-1]
    return short_name if short_name in EXTERNAL_BOUNDARIES else None


def boundary_matches(path: Path, rel_path: str, scope: str) -> Iterable[BoundaryMatch]:
    if scope not in _async_io_metrics.PRODUCTION_SCOPES or scope in _async_io_metrics.EDGE_SCOPES:
        return
    if rel_path in RUNTIME_BOUNDARY_MODULES:
        return
    tree = _async_io_metrics.parse_python(path)
    if tree is None:
        return
    aliases = _async_io_metrics._aliases(tree)
    lines = path.read_text(encoding="utf-8").splitlines()

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _async_io_metrics._call_name(node, aliases)
        boundary = _boundary_name(name)
        if boundary is None:
            continue
        has_deadline = _has_deadline(node)
        if boundary in PROCESS_LIFECYCLE_BOUNDARIES:
            category = "managed_process_boundary_refs"
        elif has_deadline:
            category = "deadline_bound_external_boundary_refs"
        else:
            category = "external_boundary_refs_missing_deadline"
        yield BoundaryMatch(
            path=rel_path,
            line_number=node.lineno,
            category=category,
            scope=scope,
            boundary=boundary,
            has_deadline=has_deadline,
            line=_line(lines, node),
        )


def iter_boundary_matches(paths: Iterable[Path]) -> Iterable[BoundaryMatch]:
    for path in paths:
        rel_path = path.relative_to(ROOT).as_posix()
        yield from boundary_matches(path, rel_path, _async_io_metrics.scope_for(rel_path))


def _summary_for(module, roots: Iterable[str], *, top: int) -> dict[str, object]:
    matches = list(module.iter_matches(module.git_visible_python_files(roots)))
    return module.summarize(matches, top=top)


def collect_component_summaries(*, top: int) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    async_io_summary = _summary_for(_async_io_metrics, _async_io_metrics.DEFAULT_ROOTS, top=top)
    async_db_summary = _summary_for(_async_db_metrics, _async_db_metrics.DEFAULT_ROOTS, top=top)
    runtime_boundary_matches = list(
        _runtime_boundary_metrics.iter_matches(
            _runtime_boundary_metrics._production_files(_runtime_boundary_metrics.DEFAULT_ROOTS)
        )
    )
    runtime_boundary_summary = _runtime_boundary_metrics.summarize(runtime_boundary_matches, top=top)
    return async_io_summary, async_db_summary, runtime_boundary_summary


def summarize_scorecard(
    *,
    async_io_summary: dict[str, object],
    async_db_summary: dict[str, object],
    runtime_boundary_summary: dict[str, object],
    external_boundary_matches: list[BoundaryMatch],
    top: int,
) -> dict[str, object]:
    async_io_metrics = async_io_summary["metrics"]
    async_db_metrics = async_db_summary["metrics"]
    runtime_boundary_metrics = runtime_boundary_summary["metrics"]
    assert isinstance(async_io_metrics, dict)
    assert isinstance(async_db_metrics, dict)
    assert isinstance(runtime_boundary_metrics, dict)

    by_category = Counter(match.category for match in external_boundary_matches)
    missing_deadline_matches = [
        match for match in external_boundary_matches
        if match.category == "external_boundary_refs_missing_deadline"
    ]
    deadline_eligible = [
        match for match in external_boundary_matches
        if match.boundary in DEADLINE_REQUIRED_BOUNDARIES
    ]
    unbounded_concurrency_refs = sum(
        int(async_io_metrics.get(name, 0))
        for name in ("unbounded_create_task_refs", "unbounded_gather_refs", "unbounded_queue_refs")
    )

    static_debt = (
        int(async_io_metrics.get("production_async_io_debt", 0))
        + int(async_db_metrics.get("repo_wide_sync_shaped_refs", 0))
        + int(runtime_boundary_metrics.get("raw_policy_refs_outside_runtime", 0))
        + len(missing_deadline_matches)
        + unbounded_concurrency_refs
    )
    deadline_coverage = (
        round((len(deadline_eligible) - len(missing_deadline_matches)) / len(deadline_eligible) * 100, 1)
        if deadline_eligible
        else 100.0
    )

    metrics = {
        "runtime_resilience_static_debt": static_debt,
        "production_async_io_debt": int(async_io_metrics.get("production_async_io_debt", 0)),
        "repo_wide_sync_shaped_refs": int(async_db_metrics.get("repo_wide_sync_shaped_refs", 0)),
        "raw_policy_refs_outside_runtime": int(runtime_boundary_metrics.get("raw_policy_refs_outside_runtime", 0)),
        "unbounded_concurrency_refs": unbounded_concurrency_refs,
        "external_boundary_refs_missing_deadline": len(missing_deadline_matches),
        "external_boundary_deadline_coverage_percent": deadline_coverage,
        "runtime_boundary_coverage_percent": runtime_boundary_metrics.get("runtime_boundary_coverage_percent", 0),
        "raw_external_timeout_coverage_percent": runtime_boundary_metrics.get(
            "raw_external_timeout_coverage_percent",
            0,
        ),
    }
    targets = {
        "runtime_resilience_static_debt": 0,
        "production_async_io_debt": 0,
        "repo_wide_sync_shaped_refs": 0,
        "raw_policy_refs_outside_runtime": 0,
        "unbounded_concurrency_refs": 0,
        "external_boundary_refs_missing_deadline": 0,
        "external_boundary_deadline_coverage_percent": 100.0,
        "runtime_boundary_coverage_percent": 100.0,
        "raw_external_timeout_coverage_percent": 100.0,
    }
    by_file = Counter(match.path for match in missing_deadline_matches)

    return {
        "targets": targets,
        "metrics": metrics,
        "by_external_boundary_category": dict(sorted(by_category.items())),
        "top_missing_deadline_files": by_file.most_common(top),
        "informational": {
            "deadline_bound_external_boundary_refs": by_category["deadline_bound_external_boundary_refs"],
            "managed_process_boundary_refs": by_category["managed_process_boundary_refs"],
            "named_runtime_boundary_refs": runtime_boundary_metrics.get("named_runtime_boundary_refs", 0),
            "isolated_sync_boundary_refs": async_io_metrics.get("isolated_sync_boundary_refs", 0),
            "outer_async_runner_refs": async_io_metrics.get("outer_async_runner_refs", 0),
            "sync_io_edge_refs": async_io_metrics.get("sync_io_edge_refs", 0),
            "required_sqlalchemy_run_sync_refs": async_db_metrics.get("required_sqlalchemy_run_sync_refs", 0),
        },
        "components": {
            "async_io": {
                "metrics": async_io_summary["metrics"],
                "targets": async_io_summary["targets"],
            },
            "async_db": {
                "metrics": async_db_summary["metrics"],
                "targets": async_db_summary["targets"],
                "exemptions": async_db_summary.get("exemptions", {}),
            },
            "runtime_boundary": {
                "metrics": runtime_boundary_summary["metrics"],
                "targets": runtime_boundary_summary["targets"],
            },
        },
    }


def matches_payload(matches: list[BoundaryMatch]) -> list[dict[str, object]]:
    return [
        {
            "path": match.path,
            "line_number": match.line_number,
            "category": match.category,
            "scope": match.scope,
            "boundary": match.boundary,
            "has_deadline": match.has_deadline,
            "line": match.line,
            "debt": match.category == "external_boundary_refs_missing_deadline",
        }
        for match in matches
    ]


def print_text(summary: dict[str, object]) -> None:
    targets = summary["targets"]
    metrics = summary["metrics"]
    assert isinstance(targets, dict)
    assert isinstance(metrics, dict)

    print("Runtime resilience scorecard")
    print()
    print("Metric                                            Current  Target")
    print("------------------------------------------------  -------  ------")
    for name, target in targets.items():
        current = metrics.get(name, 0)
        print(f"{name:<48}  {current:>7}  {target:>6}")

    print()
    print("Informational")
    informational = summary["informational"]
    assert isinstance(informational, dict)
    for name, value in informational.items():
        print(f"  {name:<42} {value}")

    print()
    print("External boundary categories")
    categories = summary["by_external_boundary_category"]
    assert isinstance(categories, dict)
    for category, count in categories.items():
        print(f"  {category:<42} {count}")

    print()
    print("Top missing-deadline files")
    for path, count in summary["top_missing_deadline_files"]:
        print(f"  {path:<72} {count}")


def build_summary(*, top: int) -> tuple[dict[str, object], list[BoundaryMatch]]:
    async_io_summary, async_db_summary, runtime_boundary_summary = collect_component_summaries(top=top)
    external_matches = list(iter_boundary_matches(_async_io_metrics.git_visible_python_files(DEFAULT_ROOTS)))
    summary = summarize_scorecard(
        async_io_summary=async_io_summary,
        async_db_summary=async_db_summary,
        runtime_boundary_summary=runtime_boundary_summary,
        external_boundary_matches=external_matches,
        top=top,
    )
    return summary, external_matches


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument("--list", action="store_true", help="include individual external-boundary matches in JSON")
    parser.add_argument("--top", type=int, default=20, help="number of top files to show")
    parser.add_argument("--target", action="store_true", help="exit non-zero unless static targets are met")
    args = parser.parse_args(argv)

    summary, external_matches = build_summary(top=args.top)
    if args.list:
        summary["external_boundary_matches"] = matches_payload(external_matches)

    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print_text(summary)

    if not args.target:
        return 0

    metrics = summary["metrics"]
    targets = summary["targets"]
    assert isinstance(metrics, dict)
    assert isinstance(targets, dict)
    for name, target in targets.items():
        if metrics.get(name) != target:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
