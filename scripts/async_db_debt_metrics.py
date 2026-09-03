#!/usr/bin/env python3
"""Count sync-shaped database debt while migrating Illospace to async-first DB IO.

The metric is intentionally about database debt, not every Python runtime
boundary. Top-level CLI event-loop runners, HTTP helper threads, and other
non-DB lifecycle code are outside this zero target; sync DB engines, sync UOW
facades, and SQLAlchemy greenlet bridges are not.
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

DEFAULT_ROOTS = ("brain", "scripts", "tests")
TEST_ROOT = "tests"

COMPAT_ISLANDS = {
    "brain/platform/db/legacy.py",
    "brain/platform/db/repositories/unit_of_work.py",
}

REQUIRED_SQLALCHEMY_RUN_SYNC = {
    "brain/platform/db/alembic/env.py": {
        "session_run_sync",
        "sync_session_method_call",
    },
    "scripts/sync_schema.py": {
        "session_run_sync",
    },
}
REQUIRED_SQLALCHEMY_RUN_SYNC_REASON = (
    "SQLAlchemy's async schema tooling requires AsyncConnection.run_sync to "
    "enter metadata inspection/DDL helpers that are synchronous by design."
)
REQUIRED_ACTIVATION_CLI_RUN_SYNC = {
    "brain/app/cli/activate_uwear_engineering_triage.py": {
        "session_run_sync",
    },
}
REQUIRED_ACTIVATION_CLI_RUN_SYNC_REASON = (
    "The one-shot Uwear triage activation CLI uses one AsyncConnection.run_sync "
    "boundary to execute its reflection-heavy, atomic SQLAlchemy Core migration; "
    "it is not imported by the application runtime."
)
OFFLINE_SQLITE_BENCH_SURFACES = {
    "scripts/bench_agent_run_fast_readme.py": {
        "sqlalchemy_create_engine",
        "sqlalchemy_sessionmaker",
    },
}
OFFLINE_SQLITE_BENCH_REASON = (
    "Offline smoke benches use an in-memory SQLite harness around the legacy "
    "sync AgentRunEngine; they do not touch production database access."
)
LEGACY_SYNC_TEST_HARNESSES = {
    "tests/conftest.py": {
        "sqlalchemy_create_engine",
        "sqlalchemy_sessionmaker",
    },
    "tests/test_agent_run_state_machine.py": {
        "sqlalchemy_create_engine",
        "sqlalchemy_sessionmaker",
    },
    "tests/test_chat_api_routes.py": {
        "sqlalchemy_create_engine",
        "sqlalchemy_sessionmaker",
        "sync_db_url",
    },
    "tests/test_notifications_api.py": {
        "sqlalchemy_create_engine",
        "sqlalchemy_sessionmaker",
        "sync_db_url",
    },
    "tests/test_pipeline.py": {
        "sqlalchemy_create_engine",
        "sqlalchemy_sessionmaker",
    },
    "tests/test_chantier_schema_migration.py": {
        "sqlalchemy_create_engine",
        "sync_session_method_call",
    },
    "tests/test_chantier_digest_migration.py": {
        "sqlalchemy_create_engine",
        "sync_session_method_call",
    },
    "tests/test_cycle_failure_guard.py": {
        "sqlalchemy_create_engine",
        "sync_session_method_call",
    },
    "tests/test_cycle_execution_policy_migration.py": {
        "sqlalchemy_create_engine",
        "sync_session_method_call",
    },
    "tests/test_cold_start_reconciliation.py": {
        "sqlalchemy_create_engine",
    },
    "tests/test_exception_ping_gate.py": {
        "sqlalchemy_create_engine",
        "sync_session_method_call",
    },
    "tests/test_pr_tracker_schema_migration.py": {
        "sqlalchemy_create_engine",
        "sync_session_method_call",
    },
    "tests/test_scheduler.py": {
        "sqlalchemy_create_engine",
        "sqlalchemy_sync_session",
    },
    "tests/test_scheduler_alert_latch_migration.py": {
        "sqlalchemy_create_engine",
    },
    "tests/test_scheduler_failure_guard_migration.py": {
        "sqlalchemy_create_engine",
        "sync_session_method_call",
    },
    "tests/test_storage_policy_migration.py": {
        "sqlalchemy_create_engine",
        "sync_session_method_call",
    },
    "tests/test_workspace_apps_service.py": {
        "sqlalchemy_create_engine",
        "sqlalchemy_sync_session",
    },
    "tests/test_uwear_triage_activation.py": {
        "sqlalchemy_create_engine",
        "sync_session_method_call",
    },
}
LEGACY_SYNC_TEST_HARNESS_REASON = (
    "Legacy sync-only test harnesses exercise sync stores or TestClient fixtures; "
    "they are documented outside the production async DB zero target."
)

SYNC_BRIDGE_PATTERNS = {
    "async_to_sync_bridge": "async_to_sync",
    "run_sync_with_unit_of_work": "run_sync_with_unit_of_work",
    "run_async_from_sync": "run_async_from_sync",
    "run_unit_of_work_task": "run_unit_of_work_task",
    "run_session_task": "run_session_task",
    "run_blocking_unit_of_work": "run_blocking_unit_of_work",
    "open_unit_of_work": "open_unit_of_work",
    "session_run_sync": "run_sync",
}

DIRECT_SYNC_PATTERNS = {
    "sync_unit_of_work_context": "UnitOfWork",
    "blocking_unit_of_work": "blocking",
}

SYNC_SURFACE_PATTERNS = {
    "sqlalchemy_sync_session": "Session",
    "sqlalchemy_create_engine": "create_engine",
    "sqlalchemy_sessionmaker": "sessionmaker",
    "sync_session_annotation": "Session",
    "sync_session_method_call": "session_db_call",
    "psycopg2": "psycopg2",
    "raw_sync_cursor": "get_cursor|get_conn",
    "sync_db_url": "DB_SYNC_URL",
}

PATTERNS = {
    **SYNC_BRIDGE_PATTERNS,
    **DIRECT_SYNC_PATTERNS,
    **SYNC_SURFACE_PATTERNS,
}

BRIDGE_CATEGORIES = frozenset(SYNC_BRIDGE_PATTERNS)
DIRECT_SYNC_CATEGORIES = frozenset(DIRECT_SYNC_PATTERNS)
SYNC_SURFACE_CATEGORIES = frozenset(SYNC_SURFACE_PATTERNS)

SYNC_DB_RECEIVER_NAMES = frozenset({
    "_session",
    "connection",
    "conn",
    "cur",
    "cursor",
    "db",
    "db_session",
    "rollback_cursor",
    "rollback_db",
    "session",
})
SYNC_DB_METHOD_NAMES = frozenset({
    "add",
    "add_all",
    "commit",
    "delete",
    "execute",
    "fetchall",
    "fetchone",
    "flush",
    "get",
    "merge",
    "query",
    "refresh",
    "rollback",
    "scalar",
    "scalars",
})
SYNC_DB_STRONG_METHOD_NAMES = SYNC_DB_METHOD_NAMES - {"get"}

PRODUCTION_SCOPES = {
    "api",
    "app_runtime",
    "jobs",
    "systems",
    "platform",
    "brain_other",
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
        if not raw_path.endswith(".py"):
            continue
        if raw_path == SELF:
            continue
        path = ROOT / raw_path
        if path.is_file():
            paths.append(path)

    if paths:
        return sorted(set(paths))

    fallback: list[Path] = []
    for root in roots:
        base = ROOT / root
        if not base.exists():
            continue
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
    if rel_path in COMPAT_ISLANDS:
        return "compat_island"
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


def async_function_lines(path: Path) -> set[int]:
    tree = parse_python(path)
    if tree is None:
        return set()
    lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef):
            end = getattr(node, "end_lineno", node.lineno)
            lines.update(range(node.lineno, end + 1))
    return lines


def sync_function_lines(path: Path) -> set[int]:
    tree = parse_python(path)
    if tree is None:
        return set()
    lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            end = getattr(node, "end_lineno", node.lineno)
            lines.update(range(node.lineno, end + 1))
    return lines


def sync_session_function_lines(path: Path) -> set[int]:
    tree = parse_python(path)
    if tree is None:
        return set()
    lines: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        annotations = [
            *(arg.annotation for arg in node.args.posonlyargs),
            *(arg.annotation for arg in node.args.args),
            *(arg.annotation for arg in node.args.kwonlyargs),
            node.returns,
        ]
        if not any(_annotation_mentions_sync_session(annotation) for annotation in annotations):
            continue
        end = getattr(node, "end_lineno", node.lineno)
        lines.update(range(node.lineno, end + 1))
    return lines


def parse_python(path: Path) -> ast.Module | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return None


def _node_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _dotted_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _dotted_name(node.value)
        if parent is None:
            return node.attr
        return f"{parent}.{node.attr}"
    return None


def _call_name(node: ast.Call) -> str | None:
    return _node_name(node.func)


def _line(lines: list[str], node: ast.AST) -> str:
    return lines[node.lineno - 1].strip()


def _annotation_mentions_sync_session(node: ast.AST | None) -> bool:
    if node is None:
        return False
    for child in ast.walk(node):
        dotted = _dotted_name(child)
        if dotted == "Session" or dotted == "sqlalchemy.orm.Session":
            return True
        if dotted and dotted.endswith(".Session") and "AsyncSession" not in dotted:
            return True
    return False


def _db_receiver_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        if node.attr in SYNC_DB_RECEIVER_NAMES:
            return node.attr
        if isinstance(node.value, ast.Name) and node.value.id in SYNC_DB_RECEIVER_NAMES:
            return node.value.id
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


def ast_matches(path: Path, rel_path: str, scope: str, async_lines: set[int]) -> Iterable[Match]:
    tree = parse_python(path)
    if tree is None:
        return
    lines = path.read_text(encoding="utf-8").splitlines()
    sync_lines = sync_function_lines(path)
    typed_sync_session_lines = sync_session_function_lines(path)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "psycopg2" or alias.name.startswith("psycopg2."):
                    yield _match(rel_path, lines, scope, async_lines, node, "psycopg2")

        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            imported_names = {alias.name for alias in node.names}
            if module == "brain.platform.db.repositories.unit_of_work":
                for category, name in SYNC_BRIDGE_PATTERNS.items():
                    if name in imported_names and category != "session_run_sync":
                        yield _match(rel_path, lines, scope, async_lines, node, category)
            if module == "brain.platform.async_bridge" and "run_async_from_sync" in imported_names:
                yield _match(rel_path, lines, scope, async_lines, node, "run_async_from_sync")
            if module == "asgiref.sync" and "async_to_sync" in imported_names:
                yield _match(rel_path, lines, scope, async_lines, node, "async_to_sync_bridge")
            if module == "brain.platform.db.legacy":
                for category, names in {
                    "sqlalchemy_create_engine": {"legacy_engine"},
                    "sqlalchemy_sessionmaker": {"legacy_session_factory"},
                    "raw_sync_cursor": {"get_cursor", "get_conn"},
                }.items():
                    if imported_names.intersection(names):
                        yield _match(rel_path, lines, scope, async_lines, node, category)

        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            for category, function_name in SYNC_BRIDGE_PATTERNS.items():
                if category != "session_run_sync" and node.name == function_name:
                    yield _match(rel_path, lines, scope, async_lines, node, category)
            if node.name in {"get_cursor", "get_conn"}:
                yield _match(rel_path, lines, scope, async_lines, node, "raw_sync_cursor")
            if isinstance(node, ast.FunctionDef):
                annotations = [
                    *(arg.annotation for arg in node.args.posonlyargs),
                    *(arg.annotation for arg in node.args.args),
                    *(arg.annotation for arg in node.args.kwonlyargs),
                    node.returns,
                ]
                if any(_annotation_mentions_sync_session(annotation) for annotation in annotations):
                    yield _match(rel_path, lines, scope, async_lines, node, "sync_session_annotation")

        elif isinstance(node, ast.With):
            for item in node.items:
                context_expr = item.context_expr
                if isinstance(context_expr, ast.Call) and _call_name(context_expr) == "UnitOfWork":
                    yield _match(rel_path, lines, scope, async_lines, node, "sync_unit_of_work_context")

        elif isinstance(node, ast.Call):
            name = _call_name(node)
            dotted_name = _dotted_name(node.func)
            for category, function_name in SYNC_BRIDGE_PATTERNS.items():
                if category != "session_run_sync" and name == function_name:
                    yield _match(rel_path, lines, scope, async_lines, node, category)
            if isinstance(node.func, ast.Attribute):
                if node.func.attr == "run_sync":
                    yield _match(rel_path, lines, scope, async_lines, node, "session_run_sync")
                if node.func.attr == "blocking" and isinstance(node.func.value, ast.Name) and node.func.value.id == "UnitOfWork":
                    yield _match(rel_path, lines, scope, async_lines, node, "blocking_unit_of_work")
                db_method = node.func.attr
                is_typed_session_call = node.lineno in typed_sync_session_lines and db_method in SYNC_DB_METHOD_NAMES
                is_strong_untyped_session_call = db_method in SYNC_DB_STRONG_METHOD_NAMES
                if (
                    node.lineno in sync_lines
                    and node.lineno not in async_lines
                    and (is_typed_session_call or is_strong_untyped_session_call)
                    and _db_receiver_name(node.func.value) in SYNC_DB_RECEIVER_NAMES
                ):
                    yield _match(rel_path, lines, scope, async_lines, node, "sync_session_method_call")
            if name == "create_engine":
                yield _match(rel_path, lines, scope, async_lines, node, "sqlalchemy_create_engine")
            if name == "sessionmaker":
                yield _match(rel_path, lines, scope, async_lines, node, "sqlalchemy_sessionmaker")
            if name == "Session":
                yield _match(rel_path, lines, scope, async_lines, node, "sqlalchemy_sync_session")
            if name in {"get_cursor", "get_conn"}:
                yield _match(rel_path, lines, scope, async_lines, node, "raw_sync_cursor")

        elif isinstance(node, ast.Attribute):
            if node.attr == "DB_SYNC_URL":
                yield _match(rel_path, lines, scope, async_lines, node, "sync_db_url")

        elif isinstance(node, ast.Name):
            if node.id == "DB_SYNC_URL":
                yield _match(rel_path, lines, scope, async_lines, node, "sync_db_url")


def iter_matches(paths: Iterable[Path]) -> Iterable[Match]:
    for path in paths:
        rel_path = path.relative_to(ROOT).as_posix()
        scope = scope_for(rel_path)
        async_lines = async_function_lines(path)
        yield from ast_matches(path, rel_path, scope, async_lines)


def zero_target_exemption_reason(match: Match) -> str | None:
    if match.category in REQUIRED_SQLALCHEMY_RUN_SYNC.get(match.path, set()):
        return REQUIRED_SQLALCHEMY_RUN_SYNC_REASON
    if match.category in REQUIRED_ACTIVATION_CLI_RUN_SYNC.get(match.path, set()):
        return REQUIRED_ACTIVATION_CLI_RUN_SYNC_REASON
    if match.category in OFFLINE_SQLITE_BENCH_SURFACES.get(match.path, set()):
        return OFFLINE_SQLITE_BENCH_REASON
    if match.category in LEGACY_SYNC_TEST_HARNESSES.get(match.path, set()):
        return LEGACY_SYNC_TEST_HARNESS_REASON
    return None


def summarize(matches: list[Match], *, top: int) -> dict[str, object]:
    zero_target_matches = [
        match for match in matches
        if zero_target_exemption_reason(match) is None
    ]
    by_scope = Counter(match.scope for match in zero_target_matches)
    by_category = Counter(match.category for match in zero_target_matches)
    by_file = Counter(match.path for match in zero_target_matches)

    production_matches = [
        match
        for match in zero_target_matches
        if match.scope in PRODUCTION_SCOPES
    ]
    production_bridge_matches = [
        match
        for match in production_matches
        if match.category in BRIDGE_CATEGORIES or match.category in DIRECT_SYNC_CATEGORIES
    ]
    source_matches = [match for match in zero_target_matches if match.scope != "tests"]
    required_sqlalchemy_run_sync_refs = sum(
        1 for match in matches
        if zero_target_exemption_reason(match) == REQUIRED_SQLALCHEMY_RUN_SYNC_REASON
    )
    required_activation_cli_run_sync_refs = sum(
        1 for match in matches
        if zero_target_exemption_reason(match) == REQUIRED_ACTIVATION_CLI_RUN_SYNC_REASON
    )
    offline_sqlite_bench_refs = sum(
        1 for match in matches
        if zero_target_exemption_reason(match) == OFFLINE_SQLITE_BENCH_REASON
    )
    legacy_sync_test_harness_refs = sum(
        1 for match in matches
        if zero_target_exemption_reason(match) == LEGACY_SYNC_TEST_HARNESS_REASON
    )

    metrics = {
        "repo_wide_sync_shaped_refs": len(zero_target_matches),
        "source_sync_shaped_refs": len(source_matches),
        "production_sync_shaped_refs": len(production_matches),
        "production_bridge_refs": len(production_bridge_matches),
        "api_sync_shaped_refs": by_scope["api"],
        "async_function_bridge_refs": sum(
            1
            for match in zero_target_matches
            if match.in_async_function
            and (match.category in BRIDGE_CATEGORIES or match.category in DIRECT_SYNC_CATEGORIES)
        ),
        "direct_sync_unit_of_work_refs": sum(
            1 for match in zero_target_matches if match.category in DIRECT_SYNC_CATEGORIES
        ),
        "sync_bridge_helper_refs": sum(1 for match in zero_target_matches if match.category in BRIDGE_CATEGORIES),
        "sync_sqlalchemy_surface_refs": sum(1 for match in zero_target_matches if match.category in SYNC_SURFACE_CATEGORIES),
        "compat_island_refs": by_scope["compat_island"],
        "migration_refs": by_scope["migrations"],
        "cli_and_script_refs": by_scope["cli"] + by_scope["scripts"],
        "test_refs": by_scope["tests"],
        "required_sqlalchemy_run_sync_refs": required_sqlalchemy_run_sync_refs,
        "required_activation_cli_run_sync_refs": required_activation_cli_run_sync_refs,
        "offline_sqlite_bench_refs": offline_sqlite_bench_refs,
        "legacy_sync_test_harness_refs": legacy_sync_test_harness_refs,
    }

    return {
        "targets": {
            "repo_wide_sync_shaped_refs": 0,
            "source_sync_shaped_refs": 0,
            "production_sync_shaped_refs": 0,
            "production_bridge_refs": 0,
            "api_sync_shaped_refs": 0,
            "async_function_bridge_refs": 0,
            "direct_sync_unit_of_work_refs": 0,
            "sync_bridge_helper_refs": 0,
            "sync_sqlalchemy_surface_refs": 0,
            "compat_island_refs": 0,
            "migration_refs": 0,
            "cli_and_script_refs": 0,
            "test_refs": 0,
        },
        "exemptions": {
            "required_sqlalchemy_run_sync_refs": {
                "count": required_sqlalchemy_run_sync_refs,
                "reason": REQUIRED_SQLALCHEMY_RUN_SYNC_REASON,
            },
            "required_activation_cli_run_sync_refs": {
                "count": required_activation_cli_run_sync_refs,
                "reason": REQUIRED_ACTIVATION_CLI_RUN_SYNC_REASON,
            },
            "offline_sqlite_bench_refs": {
                "count": offline_sqlite_bench_refs,
                "reason": OFFLINE_SQLITE_BENCH_REASON,
            },
            "legacy_sync_test_harness_refs": {
                "count": legacy_sync_test_harness_refs,
                "reason": LEGACY_SYNC_TEST_HARNESS_REASON,
            },
        },
        "metrics": metrics,
        "by_scope": dict(sorted(by_scope.items())),
        "by_category": dict(sorted(by_category.items())),
        "top_files": by_file.most_common(top),
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
            "zero_target_exempt": zero_target_exemption_reason(match) is not None,
            "exemption_reason": zero_target_exemption_reason(match),
        }
        for match in matches
    ]


def print_text(summary: dict[str, object]) -> None:
    targets = summary["targets"]
    metrics = summary["metrics"]
    assert isinstance(targets, dict)
    assert isinstance(metrics, dict)

    print("Async DB debt metrics")
    print()
    print("Metric                                Current  Target")
    print("------------------------------------  -------  ------")
    for name, target in targets.items():
        current = metrics.get(name, 0)
        print(f"{name:<36}  {current:>7}  {target:>6}")

    print()
    print("By scope")
    for scope, count in summary["by_scope"].items():
        print(f"  {scope:<18} {count}")

    print()
    print("By category")
    for category, count in summary["by_category"].items():
        print(f"  {category:<30} {count}")

    exemptions = summary.get("exemptions", {})
    if isinstance(exemptions, dict) and exemptions:
        print()
        print("Documented zero-target exemptions")
        for name, payload in exemptions.items():
            if isinstance(payload, dict):
                print(f"  {name:<30} {payload.get('count', 0)}")

    print()
    print("Top files")
    for path, count in summary["top_files"]:
        print(f"  {path:<72} {count}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument("--exclude-tests", action="store_true", help="omit tests from repo-wide counts")
    parser.add_argument("--list", action="store_true", help="include individual matches in JSON output")
    parser.add_argument("--top", type=int, default=20, help="number of top files to show")
    parser.add_argument(
        "--target",
        choices=("repo-wide", "production", "api"),
        help="exit non-zero unless the selected zero target is met",
    )
    args = parser.parse_args(argv)

    roots = list(DEFAULT_ROOTS)
    if args.exclude_tests:
        roots.remove(TEST_ROOT)

    paths = git_visible_python_files(roots)
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
            "repo-wide": "repo_wide_sync_shaped_refs",
            "production": "production_sync_shaped_refs",
            "api": "api_sync_shaped_refs",
        }[args.target]
        metrics = summary["metrics"]
        assert isinstance(metrics, dict)
        return 0 if metrics[metric_name] == 0 else 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
