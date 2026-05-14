#!/usr/bin/env python3
"""Measure runtime-boundary policy drift for async-first production paths.

This is a maintenance metric, not the async debt gate. The async debt metric
answers "can this block the event loop?" This metric answers "is production
I/O flowing through named runtime policies instead of bespoke call sites?"
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
ASYNC_IO_METRICS_PATH = ROOT / "scripts" / "async_io_debt_metrics.py"
DEFAULT_ROOTS = ("brain",)

HTTP_METHODS = {"delete", "get", "head", "options", "patch", "post", "put", "request", "stream"}
SUBPROCESS_METHODS = {
    "call",
    "check_call",
    "check_output",
    "getoutput",
    "getstatusoutput",
    "Popen",
    "run",
}
RAW_BLOCKING_BOUNDARIES = {
    "asyncio.to_thread",
    "anyio.to_thread.run_sync",
    "starlette.concurrency.run_in_threadpool",
}
RUNTIME_BOUNDARY_PREFIX = "brain.platform.async_io."
RUNTIME_BOUNDARY_MODULES = {
    "brain/platform/async_io.py",
}
INFORMATIONAL_CATEGORIES = {
    "named_runtime_boundary_refs",
}


@dataclass(frozen=True)
class Match:
    path: str
    line_number: int
    category: str
    scope: str
    in_async_function: bool
    has_timeout: bool
    line: str


def _load_async_io_metrics():
    spec = importlib.util.spec_from_file_location("async_io_debt_metrics", ASYNC_IO_METRICS_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_async_io_metrics = _load_async_io_metrics()


def _line(lines: list[str], node: ast.AST) -> str:
    return lines[node.lineno - 1].strip()


def _aliases(tree: ast.Module) -> dict[str, str]:
    return _async_io_metrics._aliases(tree)


def _call_name(node: ast.Call, aliases: dict[str, str]) -> str | None:
    return _async_io_metrics._call_name(node, aliases)


def _scope_for(rel_path: str) -> str:
    return _async_io_metrics.scope_for(rel_path)


def _production_files(roots: Iterable[str]) -> list[Path]:
    return _async_io_metrics.git_visible_python_files(roots)


def _parse(path: Path) -> ast.Module | None:
    return _async_io_metrics.parse_python(path)


def _async_lines(tree: ast.Module) -> set[int]:
    return _async_io_metrics.async_function_lines(tree)


def _has_timeout(node: ast.Call) -> bool:
    return any(keyword.arg == "timeout" for keyword in node.keywords)


def _is_raw_blocking_boundary(name: str | None) -> bool:
    if name in RAW_BLOCKING_BOUNDARIES:
        return True
    return bool(name and name.endswith(".run_in_executor"))


def _is_runtime_boundary(name: str | None) -> bool:
    return bool(name and name.startswith(RUNTIME_BOUNDARY_PREFIX))


def _is_http_policy_ref(name: str | None) -> bool:
    if name is None:
        return False
    if name in {"httpx.Client", "httpx.AsyncClient", "requests.Session", "urllib.request.urlopen"}:
        return True
    if name.startswith("requests.") and name.rsplit(".", 1)[-1] in HTTP_METHODS:
        return True
    if name.startswith("httpx.") and name.rsplit(".", 1)[-1] in HTTP_METHODS:
        return True
    if name.startswith("urllib.request.") and name.rsplit(".", 1)[-1] in {"urlopen", "urlretrieve"}:
        return True
    return False


def _is_sync_external(match: Match) -> bool:
    if match.category == "raw_subprocess_policy_refs":
        return True
    if match.category != "raw_http_client_policy_refs":
        return False
    return "httpx.AsyncClient" not in match.line


def _is_subprocess_policy_ref(name: str | None) -> bool:
    if name is None:
        return False
    if name.startswith("subprocess.") and name.rsplit(".", 1)[-1] in SUBPROCESS_METHODS:
        return True
    return name in {"os.system", "os.popen"}


def _category_for_call(name: str | None, *, rel_path: str) -> str | None:
    if rel_path in RUNTIME_BOUNDARY_MODULES:
        return None
    if _is_runtime_boundary(name):
        return "named_runtime_boundary_refs"
    if _is_raw_blocking_boundary(name):
        return "raw_blocking_boundary_refs_outside_runtime"
    if _is_subprocess_policy_ref(name):
        return "raw_subprocess_policy_refs"
    if _is_http_policy_ref(name):
        return "raw_http_client_policy_refs"
    return None


def ast_matches(path: Path, rel_path: str, scope: str) -> Iterable[Match]:
    if scope not in _async_io_metrics.PRODUCTION_SCOPES or scope in _async_io_metrics.EDGE_SCOPES:
        return
    tree = _parse(path)
    if tree is None:
        return
    aliases = _aliases(tree)
    async_lines = _async_lines(tree)
    lines = path.read_text(encoding="utf-8").splitlines()

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node, aliases)
        category = _category_for_call(name, rel_path=rel_path)
        if category is None:
            continue
        yield Match(
            path=rel_path,
            line_number=node.lineno,
            category=category,
            scope=scope,
            in_async_function=node.lineno in async_lines,
            has_timeout=_has_timeout(node),
            line=_line(lines, node),
        )


def iter_matches(paths: Iterable[Path]) -> Iterable[Match]:
    for path in paths:
        rel_path = path.relative_to(ROOT).as_posix()
        yield from ast_matches(path, rel_path, _scope_for(rel_path))


def summarize(matches: list[Match], *, top: int) -> dict[str, object]:
    by_category = Counter(match.category for match in matches)
    raw_categories = {
        "raw_blocking_boundary_refs_outside_runtime",
        "raw_http_client_policy_refs",
        "raw_subprocess_policy_refs",
    }
    raw_matches = [match for match in matches if match.category in raw_categories]
    raw_external_matches = [
        match
        for match in matches
        if match.category in {"raw_http_client_policy_refs", "raw_subprocess_policy_refs"}
    ]
    missing_timeout_matches = [
        match
        for match in raw_external_matches
        if not match.has_timeout
    ]
    sync_edge_matches = [match for match in raw_external_matches if not match.in_async_function]
    async_external_matches = [match for match in raw_external_matches if match.in_async_function]
    sync_external_in_async_matches = [
        match
        for match in async_external_matches
        if _is_sync_external(match)
    ]
    named_boundary_count = by_category["named_runtime_boundary_refs"]
    raw_policy_count = len(raw_matches)
    coverage_denominator = named_boundary_count + raw_policy_count
    timeout_denominator = len(raw_external_matches)

    metrics = {
        "raw_policy_refs_outside_runtime": raw_policy_count,
        "raw_blocking_boundary_refs_outside_runtime": by_category["raw_blocking_boundary_refs_outside_runtime"],
        "raw_subprocess_policy_refs": by_category["raw_subprocess_policy_refs"],
        "raw_http_client_policy_refs": by_category["raw_http_client_policy_refs"],
        "raw_external_refs_missing_timeout_policy": len(missing_timeout_matches),
        "async_external_io_refs_without_policy": len(async_external_matches),
        "sync_external_io_in_async_refs": len(sync_external_in_async_matches),
        "undocumented_sync_edge_refs": len(sync_edge_matches),
        "named_runtime_boundary_refs": named_boundary_count,
        "runtime_boundary_coverage_percent": round((named_boundary_count / coverage_denominator * 100), 1)
        if coverage_denominator
        else 100.0,
        "raw_external_timeout_coverage_percent": round(
            ((timeout_denominator - len(missing_timeout_matches)) / timeout_denominator * 100),
            1,
        )
        if timeout_denominator
        else 100.0,
    }

    targets = {
        "raw_policy_refs_outside_runtime": 0,
        "raw_blocking_boundary_refs_outside_runtime": 0,
        "raw_subprocess_policy_refs": 0,
        "raw_http_client_policy_refs": 0,
        "raw_external_refs_missing_timeout_policy": 0,
        "async_external_io_refs_without_policy": 0,
        "sync_external_io_in_async_refs": 0,
        "undocumented_sync_edge_refs": 0,
        "runtime_boundary_coverage_percent": 100.0,
        "raw_external_timeout_coverage_percent": 100.0,
    }

    raw_by_scope = Counter(match.scope for match in raw_matches)
    raw_by_file = Counter(match.path for match in raw_matches)
    missing_timeout_by_file = Counter(match.path for match in missing_timeout_matches)

    return {
        "targets": targets,
        "metrics": metrics,
        "by_category": dict(sorted(by_category.items())),
        "raw_policy_by_scope": dict(sorted(raw_by_scope.items())),
        "top_raw_policy_files": raw_by_file.most_common(top),
        "top_missing_timeout_files": missing_timeout_by_file.most_common(top),
    }


def matches_payload(matches: list[Match]) -> list[dict[str, object]]:
    return [
        {
            "path": match.path,
            "line_number": match.line_number,
            "category": match.category,
            "scope": match.scope,
            "in_async_function": match.in_async_function,
            "has_timeout": match.has_timeout,
            "line": match.line,
            "debt": match.category not in INFORMATIONAL_CATEGORIES,
        }
        for match in matches
    ]


def print_text(summary: dict[str, object]) -> None:
    targets = summary["targets"]
    metrics = summary["metrics"]
    assert isinstance(targets, dict)
    assert isinstance(metrics, dict)

    print("Runtime boundary metrics")
    print()
    print("Metric                                      Current  Target")
    print("------------------------------------------  -------  ------")
    for name, target in targets.items():
        current = metrics.get(name, 0)
        print(f"{name:<42}  {current:>7}  {target:>6}")

    print()
    print("Informational")
    print(f"  named_runtime_boundary_refs       {metrics.get('named_runtime_boundary_refs', 0)}")

    print()
    print("Raw policy refs by scope")
    for scope, count in summary["raw_policy_by_scope"].items():
        print(f"  {scope:<18} {count}")

    print()
    print("By category")
    for category, count in summary["by_category"].items():
        print(f"  {category:<40} {count}")

    print()
    print("Top raw policy files")
    for path, count in summary["top_raw_policy_files"]:
        print(f"  {path:<72} {count}")

    print()
    print("Top missing-timeout files")
    for path, count in summary["top_missing_timeout_files"]:
        print(f"  {path:<72} {count}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument("--list", action="store_true", help="include individual matches in JSON output")
    parser.add_argument("--top", type=int, default=20, help="number of top files to show")
    parser.add_argument("--target", action="store_true", help="exit non-zero unless all zero/coverage targets are met")
    args = parser.parse_args(argv)

    matches = list(iter_matches(_production_files(DEFAULT_ROOTS)))
    summary = summarize(matches, top=args.top)
    if args.list:
        summary["matches"] = matches_payload(matches)

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
