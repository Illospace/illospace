#!/usr/bin/env python3
"""Count production event-loop blocking debt for async-first service paths.

This metric is broader than database debt. It looks for sync I/O or concurrency
shapes that can block API and worker event loops unless they are moved to an
explicit async boundary such as asyncio.to_thread() or run_in_executor().
"""
from __future__ import annotations

import argparse
import ast
from collections import Counter
from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
SELF = Path(__file__).resolve().relative_to(ROOT).as_posix()

DEFAULT_ROOTS = ("brain",)

PRODUCTION_SCOPES = {
    "api",
    "app_runtime",
    "jobs",
    "systems",
    "platform",
    "brain_other",
}
WORKER_SCOPES = {
    "app_runtime",
    "jobs",
    "systems",
    "platform",
    "brain_other",
}
EDGE_SCOPES = {
    "cli",
    "migrations",
    "uploaded_artifacts",
}

REQUESTS_METHODS = {"delete", "get", "head", "options", "patch", "post", "put", "request"}
SUBPROCESS_METHODS = {
    "call",
    "check_call",
    "check_output",
    "getoutput",
    "getstatusoutput",
    "Popen",
    "run",
}
PATH_IO_METHODS = {
    "glob",
    "iterdir",
    "mkdir",
    "open",
    "read_bytes",
    "read_text",
    "rglob",
    "rmdir",
    "stat",
    "unlink",
    "write_bytes",
    "write_text",
}
OS_IO_METHODS = {
    "listdir",
    "makedirs",
    "remove",
    "rename",
    "replace",
    "rmdir",
    "scandir",
    "stat",
    "unlink",
    "walk",
}
SHUTIL_IO_METHODS = {
    "copy",
    "copy2",
    "copyfile",
    "copytree",
    "move",
    "rmtree",
}

ISOLATION_BOUNDARIES = {
    "asyncio.to_thread",
    "anyio.to_thread.run_sync",
    "brain.platform.async_io.run_blocking",
    "run_blocking",
    "starlette.concurrency.run_in_threadpool",
}
OUTER_RUNNER_FUNCTIONS = {
    "_heartbeat_run_once",
    "cli",
    "_mark_run_failed_after_runner_error",
    "_materialize_project_context",
    "main",
    "_main",
    "publish",
    "queue_status",
    "_reap_stale_runs_if_due",
    "<module>",
    "RunCancelToken.is_set",
    "run_cli",
    "run_queued_once",
    "_run_action_manifest_coro",
    "_scheduler_thread_main",
}
PATHISH_NAME_PARTS = {
    "artifact",
    "bundle",
    "cache",
    "dir",
    "directory",
    "export",
    "file",
    "folder",
    "journal",
    "log",
    "path",
    "repo",
    "root",
    "upload",
    "workspace",
}


@dataclass(frozen=True)
class Match:
    path: str
    line_number: int
    category: str
    scope: str
    in_async_function: bool
    line: str


def git_visible_python_files(roots: Iterable[str]) -> list[Path]:
    args = [
        "git",
        "ls-files",
        "--cached",
        "--modified",
        "--others",
        "--exclude-standard",
        "--",
        *roots,
    ]
    try:
        output = subprocess.check_output(args, cwd=ROOT, text=True)
    except (OSError, subprocess.CalledProcessError):
        output = ""

    paths: list[Path] = []
    for raw_path in output.splitlines():
        if not raw_path.endswith(".py") or raw_path == SELF:
            continue
        path = ROOT / raw_path
        if path.is_file():
            paths.append(path)

    if paths:
        return sorted(set(paths))

    fallback: list[Path] = []
    for root in roots:
        base = ROOT / root
        if base.exists():
            fallback.extend(path for path in base.rglob("*.py") if path.is_file())
    return sorted(path for path in set(fallback) if path.resolve() != Path(__file__).resolve())


def scope_for(rel_path: str) -> str:
    if rel_path.startswith("tests/"):
        return "tests"
    if rel_path.startswith("scripts/"):
        return "scripts"
    if rel_path.startswith("brain/uploads/"):
        return "uploaded_artifacts"
    if rel_path.startswith("brain/platform/db/alembic/"):
        return "migrations"
    if rel_path.startswith("brain/app/api/"):
        return "api"
    if rel_path.startswith("brain/app/cli/"):
        return "cli"
    if rel_path.startswith("brain/app/"):
        return "app_runtime"
    if rel_path.startswith("brain/jobs/"):
        return "jobs"
    if rel_path.startswith("brain/systems/"):
        return "systems"
    if rel_path.startswith("brain/platform/"):
        return "platform"
    if rel_path.startswith("brain/"):
        return "brain_other"
    return "other"


def parse_python(path: Path) -> ast.Module | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return None


def async_function_lines(tree: ast.Module) -> set[int]:
    lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef):
            end = getattr(node, "end_lineno", node.lineno)
            lines.update(range(node.lineno, end + 1))
    return lines


def function_names_by_line(tree: ast.Module) -> dict[int, str]:
    names: dict[int, str] = {}
    module_end = max((getattr(node, "end_lineno", getattr(node, "lineno", 1)) for node in ast.walk(tree)), default=1)
    for line_number in range(1, module_end + 1):
        names[line_number] = "<module>"

    def visit_body(parent: ast.AST, prefix: tuple[str, ...] = ()) -> None:
        for child in getattr(parent, "body", []):
            if isinstance(child, ast.ClassDef):
                visit_body(child, (*prefix, child.name))
                continue
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                qualified_name = ".".join((*prefix, child.name))
                end = getattr(child, "end_lineno", child.lineno)
                for line_number in range(child.lineno, end + 1):
                    names[line_number] = qualified_name
                visit_body(child, (*prefix, child.name))

    visit_body(tree)
    return names


def _line(lines: list[str], node: ast.AST) -> str:
    return lines[node.lineno - 1].strip()


def _aliases(tree: ast.Module) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                visible = alias.asname or alias.name.split(".", 1)[0]
                aliases[visible] = alias.name
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                visible = alias.asname or alias.name
                aliases[visible] = f"{module}.{alias.name}" if module else alias.name
    return aliases


def _qualified_name(node: ast.AST, aliases: dict[str, str]) -> str | None:
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        parent = _qualified_name(node.value, aliases)
        if parent is None:
            return node.attr
        return f"{parent}.{node.attr}"
    return None


def _call_name(node: ast.Call, aliases: dict[str, str]) -> str | None:
    return _qualified_name(node.func, aliases)


def _parent_map(tree: ast.Module) -> dict[ast.AST, ast.AST]:
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    return parents


def _ancestor_calls(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> Iterable[ast.Call]:
    current = parents.get(node)
    while current is not None:
        if isinstance(current, ast.Call):
            yield current
        current = parents.get(current)


def _isolation_call(name: str | None) -> bool:
    if name in ISOLATION_BOUNDARIES:
        return True
    return bool(name and name.endswith(".run_in_executor"))


def _is_isolated(node: ast.AST, parents: dict[ast.AST, ast.AST], aliases: dict[str, str]) -> bool:
    for call in _ancestor_calls(node, parents):
        if _isolation_call(_call_name(call, aliases)):
            return True
    return False


def _has_positive_maxsize(node: ast.Call) -> bool:
    if node.args:
        first = node.args[0]
        return isinstance(first, ast.Constant) and isinstance(first.value, int) and first.value > 0
    for keyword in node.keywords:
        if keyword.arg == "maxsize":
            value = keyword.value
            return isinstance(value, ast.Constant) and isinstance(value.value, int) and value.value > 0
    return False


def _is_tracked_task_creation(node: ast.Call, parents: dict[ast.AST, ast.AST]) -> bool:
    parent = parents.get(node)
    if isinstance(parent, ast.Assign | ast.AnnAssign):
        return True
    if isinstance(parent, ast.Return):
        return True
    return False


def _is_pathish_receiver(node: ast.AST) -> bool:
    if isinstance(node, ast.Call):
        return _is_pathish_receiver(node.func)
    if isinstance(node, ast.Name):
        value = node.id.lower()
        return any(part in value for part in PATHISH_NAME_PARTS)
    if isinstance(node, ast.Attribute):
        return _is_pathish_receiver(node.value) or any(part in node.attr.lower() for part in PATHISH_NAME_PARTS)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return _is_pathish_receiver(node.left) or _is_pathish_receiver(node.right)
    return False


def _is_path_io_call(name: str, node: ast.Call) -> bool:
    if name.startswith("pathlib.Path.") and name.rsplit(".", 1)[-1] in PATH_IO_METHODS:
        return True
    if not isinstance(node.func, ast.Attribute):
        return False
    if node.func.attr not in PATH_IO_METHODS:
        return False
    return _is_pathish_receiver(node.func.value)


def _category_for_call(
    name: str | None,
    node: ast.Call,
    *,
    function_name: str | None,
    in_async: bool,
) -> str | None:
    if name is None:
        return None

    if _isolation_call(name):
        return "isolated_sync_boundary_refs"
    if name in {"asyncio.run", "trio.run", "anyio.run"}:
        if function_name in OUTER_RUNNER_FUNCTIONS:
            return "outer_async_runner_refs"
        return "asyncio_run_inner_refs"
    if name.endswith(".run_until_complete"):
        return "run_until_complete_inner_refs"
    if name in {"asyncio.create_task", "create_task"}:
        return "unbounded_create_task_refs"
    if name in {"asyncio.gather", "gather"}:
        return "unbounded_gather_refs"
    if name in {"asyncio.Queue", "queue.Queue", "Queue"} and not _has_positive_maxsize(node):
        return "unbounded_queue_refs"
    if in_async and name in {"time.sleep", "sleep"}:
        return "blocking_sleep_in_async_refs"

    if name == "requests.Session":
        return "sync_http_client_refs" if in_async else "sync_io_edge_refs"
    if name.startswith("requests.") and name.rsplit(".", 1)[-1] in REQUESTS_METHODS:
        return "sync_http_client_refs" if in_async else "sync_io_edge_refs"
    if name in {"httpx.Client", "urllib.request.urlopen"}:
        return "sync_http_client_refs" if in_async else "sync_io_edge_refs"
    if name.startswith("urllib.request.") and name.rsplit(".", 1)[-1] in {"urlopen", "urlretrieve"}:
        return "sync_http_client_refs" if in_async else "sync_io_edge_refs"

    if name.startswith("subprocess.") and name.rsplit(".", 1)[-1] in SUBPROCESS_METHODS:
        return "sync_subprocess_refs" if in_async else "sync_io_edge_refs"
    if name in {"os.system", "os.popen"}:
        return "sync_subprocess_refs" if in_async else "sync_io_edge_refs"

    if in_async and name == "open":
        return "sync_filesystem_in_async_refs"
    if in_async and _is_path_io_call(name, node):
        return "sync_filesystem_in_async_refs"
    if in_async and name.startswith("os.") and name.rsplit(".", 1)[-1] in OS_IO_METHODS:
        return "sync_filesystem_in_async_refs"
    if in_async and name.startswith("shutil.") and name.rsplit(".", 1)[-1] in SHUTIL_IO_METHODS:
        return "sync_filesystem_in_async_refs"

    return None


def _match(
    rel_path: str,
    lines: list[str],
    scope: str,
    async_lines: set[int],
    node: ast.AST,
    category: str,
) -> Match:
    return Match(
        path=rel_path,
        line_number=node.lineno,
        category=category,
        scope=scope,
        in_async_function=node.lineno in async_lines,
        line=_line(lines, node),
    )


def ast_matches(path: Path, rel_path: str, scope: str) -> Iterable[Match]:
    if scope not in PRODUCTION_SCOPES or scope in EDGE_SCOPES:
        return
    tree = parse_python(path)
    if tree is None:
        return
    aliases = _aliases(tree)
    parents = _parent_map(tree)
    async_lines = async_function_lines(tree)
    function_names = function_names_by_line(tree)
    lines = path.read_text(encoding="utf-8").splitlines()

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        category = _category_for_call(
            _call_name(node, aliases),
            node,
            function_name=function_names.get(node.lineno),
            in_async=node.lineno in async_lines,
        )
        if category is None:
            continue
        if category == "unbounded_create_task_refs" and _is_tracked_task_creation(node, parents):
            continue
        if category not in {"isolated_sync_boundary_refs", "outer_async_runner_refs", "sync_io_edge_refs"} and _is_isolated(node, parents, aliases):
            continue
        yield _match(rel_path, lines, scope, async_lines, node, category)


def iter_matches(paths: Iterable[Path]) -> Iterable[Match]:
    for path in paths:
        rel_path = path.relative_to(ROOT).as_posix()
        yield from ast_matches(path, rel_path, scope_for(rel_path))


def summarize(matches: list[Match], *, top: int) -> dict[str, object]:
    informational_categories = {
        "isolated_sync_boundary_refs",
        "outer_async_runner_refs",
        "sync_io_edge_refs",
    }
    debt_matches = [match for match in matches if match.category not in informational_categories]
    isolated_matches = [match for match in matches if match.category == "isolated_sync_boundary_refs"]
    outer_runner_matches = [match for match in matches if match.category == "outer_async_runner_refs"]
    sync_edge_matches = [match for match in matches if match.category == "sync_io_edge_refs"]
    by_scope = Counter(match.scope for match in debt_matches)
    by_category = Counter(match.category for match in debt_matches)
    by_file = Counter(match.path for match in debt_matches)

    metrics = {
        "production_async_io_debt": len(debt_matches),
        "api_async_io_debt": by_scope["api"],
        "worker_async_io_debt": sum(by_scope[scope] for scope in WORKER_SCOPES),
        "blocking_sleep_in_async_refs": by_category["blocking_sleep_in_async_refs"],
        "sync_http_client_refs": by_category["sync_http_client_refs"],
        "sync_subprocess_refs": by_category["sync_subprocess_refs"],
        "sync_filesystem_in_async_refs": by_category["sync_filesystem_in_async_refs"],
        "asyncio_run_inner_refs": by_category["asyncio_run_inner_refs"],
        "run_until_complete_inner_refs": by_category["run_until_complete_inner_refs"],
        "unbounded_create_task_refs": by_category["unbounded_create_task_refs"],
        "unbounded_gather_refs": by_category["unbounded_gather_refs"],
        "unbounded_queue_refs": by_category["unbounded_queue_refs"],
        "isolated_sync_boundary_refs": len(isolated_matches),
        "outer_async_runner_refs": len(outer_runner_matches),
        "sync_io_edge_refs": len(sync_edge_matches),
    }

    return {
        "targets": {
            "production_async_io_debt": 0,
            "api_async_io_debt": 0,
            "worker_async_io_debt": 0,
            "blocking_sleep_in_async_refs": 0,
            "sync_http_client_refs": 0,
            "sync_subprocess_refs": 0,
            "sync_filesystem_in_async_refs": 0,
            "asyncio_run_inner_refs": 0,
            "run_until_complete_inner_refs": 0,
            "unbounded_create_task_refs": 0,
            "unbounded_gather_refs": 0,
            "unbounded_queue_refs": 0,
        },
        "metrics": metrics,
        "by_scope": dict(sorted(by_scope.items())),
        "by_category": dict(sorted(by_category.items())),
        "top_files": by_file.most_common(top),
        "informational": {
            "isolated_sync_boundary_refs": len(isolated_matches),
            "outer_async_runner_refs": len(outer_runner_matches),
            "sync_io_edge_refs": len(sync_edge_matches),
        },
    }


def matches_payload(matches: list[Match]) -> list[dict[str, object]]:
    return [
        {
            "path": match.path,
            "line_number": match.line_number,
            "category": match.category,
            "scope": match.scope,
            "in_async_function": match.in_async_function,
            "line": match.line,
            "debt": match.category not in {
                "isolated_sync_boundary_refs",
                "outer_async_runner_refs",
                "sync_io_edge_refs",
            },
        }
        for match in matches
    ]


def print_text(summary: dict[str, object]) -> None:
    targets = summary["targets"]
    metrics = summary["metrics"]
    assert isinstance(targets, dict)
    assert isinstance(metrics, dict)

    print("Async IO debt metrics")
    print()
    print("Metric                              Current  Target")
    print("----------------------------------  -------  ------")
    for name, target in targets.items():
        current = metrics.get(name, 0)
        print(f"{name:<34}  {current:>7}  {target:>6}")

    print()
    print("Informational")
    informational = summary.get("informational", {})
    if isinstance(informational, dict):
        for name, count in informational.items():
            print(f"  {name:<32} {count}")

    print()
    print("By scope")
    for scope, count in summary["by_scope"].items():
        print(f"  {scope:<18} {count}")

    print()
    print("By category")
    for category, count in summary["by_category"].items():
        print(f"  {category:<32} {count}")

    print()
    print("Top files")
    for path, count in summary["top_files"]:
        print(f"  {path:<72} {count}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument("--list", action="store_true", help="include individual matches in JSON output")
    parser.add_argument("--top", type=int, default=20, help="number of top files to show")
    parser.add_argument(
        "--target",
        choices=("production", "api", "worker"),
        help="exit non-zero unless the selected zero target is met",
    )
    args = parser.parse_args(argv)

    paths = git_visible_python_files(DEFAULT_ROOTS)
    matches = list(iter_matches(paths))
    summary = summarize(matches, top=args.top)
    if args.list:
        summary["matches"] = matches_payload(matches)

    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print_text(summary)

    if args.target:
        metric_name = {
            "production": "production_async_io_debt",
            "api": "api_async_io_debt",
            "worker": "worker_async_io_debt",
        }[args.target]
        metrics = summary["metrics"]
        assert isinstance(metrics, dict)
        return 0 if metrics[metric_name] == 0 else 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
