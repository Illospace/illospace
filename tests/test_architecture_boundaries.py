"""Guardrails for backend architecture dependency boundaries."""
from __future__ import annotations

import ast
import json
import subprocess
from collections import Counter
from pathlib import Path
from typing import NamedTuple


ROOT = Path(__file__).resolve().parents[1]
ALLOWLIST_PATH = ROOT / "tests" / "fixtures" / "architecture-boundary-allowlist.json"

LAYERS = (
    "brain.app",
    "brain.jobs",
    "brain.systems",
    "brain.platform",
    "brain.contracts",
    "brain.kernel",
)

ALLOWED_IMPORTS = {
    "brain.app": {"brain.app", "brain.systems", "brain.platform", "brain.contracts", "brain.kernel"},
    "brain.jobs": {"brain.jobs", "brain.systems", "brain.platform", "brain.contracts", "brain.kernel"},
    "brain.systems": {"brain.systems", "brain.platform", "brain.contracts", "brain.kernel"},
    "brain.platform": {"brain.platform", "brain.contracts", "brain.kernel"},
    "brain.contracts": {"brain.contracts", "brain.kernel"},
    "brain.kernel": {"brain.kernel"},
}


class BoundaryImport(NamedTuple):
    source: str
    imported: str
    source_layer: str
    target_layer: str


def test_architecture_layer_imports_match_allowlist() -> None:
    """Fail when new cross-layer imports appear or stale allowlist debt remains."""

    actual = _boundary_violations()
    allowed = _allowlist()

    unexpected = sorted(set(actual) - set(allowed))
    stale = sorted(set(allowed) - set(actual))
    mismatched_counts = sorted(
        (violation, actual[violation], allowed[violation])
        for violation in set(actual) & set(allowed)
        if actual[violation] != allowed[violation]
    )

    assert not unexpected and not stale and not mismatched_counts, _format_failure(
        unexpected=unexpected,
        stale=stale,
        mismatched_counts=mismatched_counts,
    )


def test_architecture_boundary_allowlist_has_remediation_metadata() -> None:
    data = _load_allowlist_data()
    entries = data.get("entries")

    assert data.get("version") == 1
    assert isinstance(entries, list)
    assert entries == sorted(entries, key=_entry_sort_key)

    for entry in entries:
        assert isinstance(entry.get("source"), str) and entry["source"].endswith(".py")
        assert isinstance(entry.get("import"), str) and entry["import"].startswith("brain.")
        assert entry.get("source_layer") in LAYERS
        assert entry.get("target_layer") in LAYERS
        assert isinstance(entry.get("allowed_occurrences"), int)
        assert entry["allowed_occurrences"] > 0
        assert entry.get("owner") == "architecture-boundaries"
        assert entry.get("reason")
        assert entry.get("planned_removal")


def _boundary_violations() -> Counter[BoundaryImport]:
    violations: Counter[BoundaryImport] = Counter()
    for path in _brain_python_files():
        source = path.relative_to(ROOT).as_posix()
        module_parts = list(path.relative_to(ROOT).with_suffix("").parts)
        source_layer = _layer_for(".".join(module_parts))
        if source_layer is None:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=source)
        for node in ast.walk(tree):
            for imported in _resolve_import(module_parts, node):
                target_layer = _layer_for(imported)
                if target_layer is None or target_layer in ALLOWED_IMPORTS[source_layer]:
                    continue
                violations[
                    BoundaryImport(
                        source=source,
                        imported=imported,
                        source_layer=source_layer,
                        target_layer=target_layer,
                    )
                ] += 1
    return violations


def _allowlist() -> Counter[BoundaryImport]:
    entries = _load_allowlist_data()["entries"]
    allowed: Counter[BoundaryImport] = Counter()
    for entry in entries:
        allowed[
            BoundaryImport(
                source=entry["source"],
                imported=entry["import"],
                source_layer=entry["source_layer"],
                target_layer=entry["target_layer"],
            )
        ] = entry["allowed_occurrences"]
    return allowed


def _load_allowlist_data() -> dict:
    return json.loads(ALLOWLIST_PATH.read_text(encoding="utf-8"))


def _brain_python_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "brain"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        return [
            ROOT / line
            for line in result.stdout.splitlines()
            if line.endswith(".py")
            and (ROOT / line).exists()
        ]
    return [
        path
        for path in (ROOT / "brain").rglob("*.py")
        if "__pycache__" not in path.parts
        and "uploads" not in path.parts
        and not any(part.startswith(".") for part in path.relative_to(ROOT).parts)
    ]


def _resolve_import(module_parts: list[str], node: ast.AST) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if not isinstance(node, ast.ImportFrom):
        return []
    if node.level == 0:
        return [node.module] if node.module else []

    base = module_parts[:-1]
    keep = len(base) - (node.level - 1)
    if keep < 0:
        return []
    resolved = base[:keep]
    if node.module:
        resolved.extend(node.module.split("."))
    return [".".join(resolved)] if resolved else []


def _layer_for(module_name: str) -> str | None:
    return next(
        (
            layer
            for layer in LAYERS
            if module_name == layer or module_name.startswith(layer + ".")
        ),
        None,
    )


def _format_failure(
    *,
    unexpected: list[BoundaryImport],
    stale: list[BoundaryImport],
    mismatched_counts: list[tuple[BoundaryImport, int, int]],
) -> str:
    lines = [
        "Architecture boundary allowlist is out of sync.",
        "See tests/fixtures/architecture-boundary-allowlist.json.",
    ]
    if unexpected:
        lines.append("Unexpected new violations:")
        lines.extend(f"  - {_format_violation(item)}" for item in unexpected)
    if stale:
        lines.append("Stale allowlist entries:")
        lines.extend(f"  - {_format_violation(item)}" for item in stale)
    if mismatched_counts:
        lines.append("Changed violation counts:")
        lines.extend(
            f"  - {_format_violation(item)} actual={actual} allowed={allowed}"
            for item, actual, allowed in mismatched_counts
        )
    return "\n".join(lines)


def _format_violation(violation: BoundaryImport) -> str:
    return (
        f"{violation.source}: {violation.source_layer} -> "
        f"{violation.target_layer} via {violation.imported}"
    )


def _entry_sort_key(entry: dict) -> tuple[str, str, str, str]:
    return (
        entry.get("source", ""),
        entry.get("import", ""),
        entry.get("source_layer", ""),
        entry.get("target_layer", ""),
    )
