#!/usr/bin/env python3
"""Count sync/async migration leftovers in production AgentRun runtime paths.

This is a focused companion to async_io_debt_metrics.py. It tracks legacy sync
facades that are known to fail or waste turns when invoked from the async
AgentRun worker, especially MCP sync wrappers and old direct_agent telemetry
surfaces.
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

SYNC_MCP_FACADES = {
    "tool_brain_recall",
    "tool_brain_guardrails",
    "tool_brain_skills",
    "tool_skill_view",
    "tool_skill_asset",
    "tool_brain_encode",
}
SYNC_MCP_RUNNER_NAMES = {"_run_mcp_sync"}
SYNC_MCP_MODULE = "brain.app.mcp.server"

LEGACY_DIRECT_AGENT_SYNC_NAMES = {
    "_invoke_tool_handler",
    "_record_api_call",
}
DIRECT_AGENT_MODULE = "brain.systems.runs.direct_agent"

SYNC_TELEMETRY_NAMES = {"record_api_call"}
TELEMETRY_MODULE = "brain.systems.runs.direct_loop.telemetry"

@dataclass(frozen=True)
class Match:
    path: str
    line_number: int
    category: str
    scope: str
    in_async_function: bool
    line: str
    symbol: str | None = None


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


def _line_number(node: ast.AST) -> int:
    return int(getattr(node, "lineno", 1))


def _line(lines: list[str], node: ast.AST) -> str:
    lineno = _line_number(node)
    if lineno < 1 or lineno > len(lines):
        return ""
    return lines[lineno - 1].strip()


def _aliases(tree: ast.Module) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    aliases[alias.asname] = alias.name
                else:
                    aliases[alias.name.split(".", 1)[0]] = alias.name.split(".", 1)[0]
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


def _match(
    rel_path: str,
    lines: list[str],
    scope: str,
    async_lines: set[int],
    node: ast.AST,
    category: str,
    *,
    symbol: str | None = None,
) -> Match:
    line_number = _line_number(node)
    return Match(
        path=rel_path,
        line_number=line_number,
        category=category,
        scope=scope,
        in_async_function=line_number in async_lines,
        line=_line(lines, node),
        symbol=symbol,
    )


def _import_matches(node: ast.ImportFrom) -> Iterable[tuple[str, str, ast.AST]]:
    module = node.module or ""
    for alias in node.names:
        alias_node = alias if hasattr(alias, "lineno") else node
        if module == SYNC_MCP_MODULE and alias.name in SYNC_MCP_FACADES:
            yield "sync_mcp_facade_import_refs", alias.name, alias_node
        if module == DIRECT_AGENT_MODULE and alias.name in LEGACY_DIRECT_AGENT_SYNC_NAMES:
            yield "legacy_direct_agent_sync_import_refs", alias.name, alias_node
        if module == TELEMETRY_MODULE and alias.name in SYNC_TELEMETRY_NAMES:
            yield "legacy_sync_telemetry_import_refs", alias.name, alias_node


def _call_category(name: str | None) -> str | None:
    if name is None:
        return None
    if name in {f"{SYNC_MCP_MODULE}.{tool}" for tool in SYNC_MCP_FACADES}:
        return "sync_mcp_facade_call_refs"
    if name in SYNC_MCP_FACADES:
        return "sync_mcp_facade_call_refs"
    if name in {f"{DIRECT_AGENT_MODULE}.{tool}" for tool in LEGACY_DIRECT_AGENT_SYNC_NAMES}:
        return "legacy_direct_agent_sync_call_refs"
    if name in LEGACY_DIRECT_AGENT_SYNC_NAMES:
        return "legacy_direct_agent_sync_call_refs"
    if name == f"{TELEMETRY_MODULE}.record_api_call" or name == "record_api_call":
        return "legacy_sync_telemetry_call_refs"
    if name in SYNC_MCP_RUNNER_NAMES:
        return "sync_mcp_runner_call_refs"
    return None


def _value_ref_category(name: str | None) -> str | None:
    if name is None:
        return None
    if name in {f"{SYNC_MCP_MODULE}.{tool}" for tool in SYNC_MCP_FACADES}:
        return "sync_mcp_facade_value_refs"
    if name in SYNC_MCP_FACADES:
        return "sync_mcp_facade_value_refs"
    if name in {f"{DIRECT_AGENT_MODULE}.{tool}" for tool in LEGACY_DIRECT_AGENT_SYNC_NAMES}:
        return "legacy_direct_agent_sync_value_refs"
    if name in LEGACY_DIRECT_AGENT_SYNC_NAMES:
        return "legacy_direct_agent_sync_value_refs"
    if name == f"{TELEMETRY_MODULE}.record_api_call" or name == "record_api_call":
        return "legacy_sync_telemetry_value_refs"
    if name in SYNC_MCP_RUNNER_NAMES:
        return "sync_mcp_runner_value_refs"
    return None


def _definition_category(rel_path: str, node: ast.AST) -> str | None:
    if not isinstance(node, ast.FunctionDef):
        return None
    if rel_path == "brain/app/mcp/server.py" and node.name in SYNC_MCP_FACADES:
        return "sync_mcp_facade_definition_refs"
    if rel_path == "brain/app/mcp/server.py" and node.name in SYNC_MCP_RUNNER_NAMES:
        return "sync_mcp_runner_definition_refs"
    if rel_path == "brain/systems/runs/direct_agent.py" and node.name in LEGACY_DIRECT_AGENT_SYNC_NAMES:
        return "legacy_direct_agent_sync_definition_refs"
    if rel_path == "brain/systems/runs/direct_loop/telemetry.py" and node.name in SYNC_TELEMETRY_NAMES:
        return "legacy_sync_telemetry_definition_refs"
    return None


def _is_direct_call_func(node: ast.AST, parent: ast.AST | None) -> bool:
    return isinstance(parent, ast.Call) and parent.func is node


def _is_load_reference(node: ast.AST) -> bool:
    return isinstance(node, (ast.Name, ast.Attribute)) and isinstance(getattr(node, "ctx", None), ast.Load)


def ast_matches(path: Path, rel_path: str, scope: str) -> Iterable[Match]:
    if scope not in PRODUCTION_SCOPES:
        return
    tree = parse_python(path)
    if tree is None:
        return
    aliases = _aliases(tree)
    async_lines = async_function_lines(tree)
    lines = path.read_text(encoding="utf-8").splitlines()
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for category, symbol, match_node in _import_matches(node):
                yield _match(rel_path, lines, scope, async_lines, match_node, category, symbol=symbol)
            continue

        category = _definition_category(rel_path, node)
        if category is not None:
            yield _match(rel_path, lines, scope, async_lines, node, category, symbol=getattr(node, "name", None))
            continue

        if isinstance(node, ast.Call):
            category = _call_category(_qualified_name(node.func, aliases))
            if category is not None:
                yield _match(rel_path, lines, scope, async_lines, node, category)
            continue

        parent = parents.get(node)
        if _is_load_reference(node) and not _is_direct_call_func(node, parent):
            category = _value_ref_category(_qualified_name(node, aliases))
            if category is not None:
                symbol = node.id if isinstance(node, ast.Name) else node.attr
                yield _match(rel_path, lines, scope, async_lines, node, category, symbol=symbol)


def iter_matches(paths: Iterable[Path]) -> Iterable[Match]:
    for path in paths:
        rel_path = path.relative_to(ROOT).as_posix()
        yield from ast_matches(path, rel_path, scope_for(rel_path))


def summarize(matches: list[Match], *, top: int) -> dict[str, object]:
    by_category = Counter(match.category for match in matches)
    by_scope = Counter(match.scope for match in matches)
    by_file = Counter(match.path for match in matches)
    metrics = {
        "async_runtime_migration_debt": len(matches),
        "sync_mcp_runner_definition_refs": by_category["sync_mcp_runner_definition_refs"],
        "sync_mcp_runner_call_refs": by_category["sync_mcp_runner_call_refs"],
        "sync_mcp_runner_value_refs": by_category["sync_mcp_runner_value_refs"],
        "sync_mcp_facade_definition_refs": by_category["sync_mcp_facade_definition_refs"],
        "sync_mcp_facade_import_refs": by_category["sync_mcp_facade_import_refs"],
        "sync_mcp_facade_call_refs": by_category["sync_mcp_facade_call_refs"],
        "sync_mcp_facade_value_refs": by_category["sync_mcp_facade_value_refs"],
        "legacy_direct_agent_sync_definition_refs": by_category["legacy_direct_agent_sync_definition_refs"],
        "legacy_direct_agent_sync_import_refs": by_category["legacy_direct_agent_sync_import_refs"],
        "legacy_direct_agent_sync_call_refs": by_category["legacy_direct_agent_sync_call_refs"],
        "legacy_direct_agent_sync_value_refs": by_category["legacy_direct_agent_sync_value_refs"],
        "legacy_sync_telemetry_definition_refs": by_category["legacy_sync_telemetry_definition_refs"],
        "legacy_sync_telemetry_import_refs": by_category["legacy_sync_telemetry_import_refs"],
        "legacy_sync_telemetry_call_refs": by_category["legacy_sync_telemetry_call_refs"],
        "legacy_sync_telemetry_value_refs": by_category["legacy_sync_telemetry_value_refs"],
    }
    return {
        "targets": {name: 0 for name in metrics},
        "metrics": metrics,
        "by_category": dict(sorted(by_category.items())),
        "by_scope": dict(sorted(by_scope.items())),
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
            "symbol": match.symbol,
        }
        for match in matches
    ]


def print_text(summary: dict[str, object]) -> None:
    targets = summary["targets"]
    metrics = summary["metrics"]
    assert isinstance(targets, dict)
    assert isinstance(metrics, dict)

    print("Async runtime migration metrics")
    print()
    print("Metric                                      Current  Target")
    print("------------------------------------------  -------  ------")
    for name, target in targets.items():
        current = metrics.get(name, 0)
        print(f"{name:<42}  {current:>7}  {target:>6}")

    print()
    print("By category")
    for category, count in summary["by_category"].items():
        print(f"  {category:<40} {count}")

    print()
    print("Top files")
    for path, count in summary["top_files"]:
        print(f"  {count:>3}  {path}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("roots", nargs="*", default=list(DEFAULT_ROOTS))
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--include-matches", action="store_true")
    parser.add_argument("--top", type=int, default=12)
    parser.add_argument("--fail-on-debt", action="store_true")
    args = parser.parse_args()

    paths = git_visible_python_files(args.roots)
    matches = list(iter_matches(paths))
    summary = summarize(matches, top=args.top)
    payload: dict[str, object] = dict(summary)
    if args.include_matches:
        payload["matches"] = matches_payload(matches)

    if args.format == "json":
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print_text(summary)

    return 1 if args.fail_on_debt and matches else 0


if __name__ == "__main__":
    raise SystemExit(main())
