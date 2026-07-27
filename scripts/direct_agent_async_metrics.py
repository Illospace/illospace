#!/usr/bin/env python3
"""Track the direct-agent migration from threaded sync runtime to native async."""

from __future__ import annotations

import argparse
import ast
from collections import Counter
from dataclasses import dataclass
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

FILES = {
    "direct_agent": "brain/systems/runs/direct_agent.py",
    "threaded_invocation": "brain/systems/runs/recipes/threaded_invocation.py",
    "recipes": (
        "brain/systems/runs/recipes/fast.py",
        "brain/systems/runs/recipes/workers.py",
    ),
    "retry": "brain/systems/runs/direct_loop/retry.py",
    "cancel": "brain/systems/runs/cancel.py",
}

SYNC_SESSION_NAMES = {
    "_load_session",
    "_load_session_handoff",
    "_save_session",
    "_save_session_handoff",
    "_runtime_apply_agent_session_side_effects",
}

BASELINES = {
    "missing_async_run_agent_entrypoint": 1,
    "recipe_thread_bridge_calls": 4,
    "threaded_direct_agent_run_blocking_calls": 2,
    "direct_agent_sync_session_refs": 6,
    "sync_run_agent_auth_resolver_refs": 1,
    "direct_retry_blocking_sleep_calls": 1,
    "cancel_token_asyncio_run_calls": 0,
}

TARGETS = {name: 0 for name in BASELINES}


@dataclass(frozen=True)
class MetricRef:
    metric: str
    path: str
    line_number: int
    line: str


def _read(rel_path: str) -> str:
    return (ROOT / rel_path).read_text(encoding="utf-8")


def _parse(rel_path: str) -> ast.Module:
    return ast.parse(_read(rel_path))


def _aliases(tree: ast.Module) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                aliases[alias.asname or alias.name.split(".", 1)[0]] = alias.name
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
        return node.attr if parent is None else f"{parent}.{node.attr}"
    return None


def _call_name(node: ast.Call, aliases: dict[str, str]) -> str | None:
    return _qualified_name(node.func, aliases)


def _line(rel_path: str, line_number: int) -> str:
    return _read(rel_path).splitlines()[line_number - 1].strip()


def _ref(metric: str, rel_path: str, node: ast.AST) -> MetricRef:
    return MetricRef(metric, rel_path, int(node.lineno), _line(rel_path, int(node.lineno)))


def _has_async_function(rel_path: str, name: str) -> bool:
    if not (ROOT / rel_path).exists():
        return False
    tree = _parse(rel_path)
    return any(isinstance(node, ast.AsyncFunctionDef) and node.name == name for node in ast.walk(tree))


def _call_refs(rel_path: str, metric: str, names: set[str]) -> list[MetricRef]:
    if not (ROOT / rel_path).exists():
        return []
    tree = _parse(rel_path)
    aliases = _aliases(tree)
    refs: list[MetricRef] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and (_call_name(node, aliases) or "").rsplit(".", 1)[-1] in names:
            refs.append(_ref(metric, rel_path, node))
    return refs


def _name_refs(rel_path: str, metric: str, names: set[str]) -> list[MetricRef]:
    if not (ROOT / rel_path).exists():
        return []
    tree = _parse(rel_path)
    refs: list[MetricRef] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in names:
            refs.append(_ref(metric, rel_path, node))
    return refs


def _function_call_refs(rel_path: str, function_name: str, metric: str, names: set[str]) -> list[MetricRef]:
    if not (ROOT / rel_path).exists():
        return []
    tree = _parse(rel_path)
    aliases = _aliases(tree)
    refs: list[MetricRef] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == function_name:
            for child in ast.walk(node):
                if isinstance(child, ast.Call) and (_call_name(child, aliases) or "").rsplit(".", 1)[-1] in names:
                    refs.append(_ref(metric, rel_path, child))
    return refs


def refs() -> list[MetricRef]:
    direct_agent = str(FILES["direct_agent"])
    threaded_invocation = str(FILES["threaded_invocation"])
    retry = str(FILES["retry"])
    cancel = str(FILES["cancel"])
    recipe_files = tuple(str(path) for path in FILES["recipes"])

    found: list[MetricRef] = []
    if not _has_async_function(direct_agent, "run_agent_async"):
        found.append(MetricRef(
            "missing_async_run_agent_entrypoint",
            direct_agent,
            1,
            "async def run_agent_async is not defined",
        ))
    for rel_path in recipe_files:
        found.extend(_call_refs(rel_path, "recipe_thread_bridge_calls", {"invoke_direct_agent_threaded"}))
    found.extend(_call_refs(
        threaded_invocation,
        "threaded_direct_agent_run_blocking_calls",
        {"run_blocking"},
    ))
    found.extend(_name_refs(direct_agent, "direct_agent_sync_session_refs", SYNC_SESSION_NAMES))
    found.extend(_function_call_refs(
        direct_agent,
        "run_agent",
        "sync_run_agent_auth_resolver_refs",
        {"_run_agent_sync_resolved_llm", "resolve_llm_client", "get_default_model"},
    ))
    found.extend(_call_refs(retry, "direct_retry_blocking_sleep_calls", {"sleep"}))
    found.extend(_call_refs(cancel, "cancel_token_asyncio_run_calls", {"run"}))
    return found


def summarize(found: list[MetricRef]) -> dict[str, object]:
    counts = Counter(ref.metric for ref in found)
    metrics = {name: int(counts[name]) for name in BASELINES}
    return {
        "metrics": metrics,
        "baselines": dict(BASELINES),
        "targets": dict(TARGETS),
        "refs": [ref.__dict__ for ref in found],
    }


def print_text(summary: dict[str, object]) -> None:
    metrics = summary["metrics"]
    baselines = summary["baselines"]
    targets = summary["targets"]
    assert isinstance(metrics, dict)
    assert isinstance(baselines, dict)
    assert isinstance(targets, dict)

    print("Direct agent async migration metrics")
    print()
    print("Metric                                      Current  Baseline  Target")
    print("------------------------------------------  -------  --------  ------")
    for name in baselines:
        print(f"{name:<42}  {metrics[name]:>7}  {baselines[name]:>8}  {targets[name]:>6}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument(
        "--target",
        choices=("baseline", "complete"),
        help=(
            "baseline passes while metrics stay at or below today's counts; "
            "complete fails until every async-migration metric reaches zero"
        ),
    )
    args = parser.parse_args(argv)

    summary = summarize(refs())
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print_text(summary)
    metrics = summary["metrics"]
    baselines = summary["baselines"]
    targets = summary["targets"]
    assert isinstance(metrics, dict)
    assert isinstance(baselines, dict)
    assert isinstance(targets, dict)
    if args.target == "baseline":
        return 0 if all(metrics[name] <= baselines[name] for name in baselines) else 1
    if args.target == "complete":
        return 0 if all(metrics[name] <= targets[name] for name in targets) else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
