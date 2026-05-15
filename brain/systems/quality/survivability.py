"""Capability survivability scoring.

This module scores whether a product capability has the evidence needed to
survive realistic change. It intentionally does not use line coverage. A
capability earns confidence through critical invariants, contracts, real
integration coverage, user journeys, adversarial tests, and static guardrails.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from fnmatch import fnmatch
import json
from pathlib import Path
import subprocess
from typing import Any


DEFAULT_CATEGORY_WEIGHTS: dict[str, float] = {
    "critical_invariants": 0.30,
    "contracts": 0.20,
    "real_integration": 0.20,
    "user_journeys": 0.15,
    "adversarial": 0.10,
    "static_safety": 0.05,
}

DEFAULT_THRESHOLDS_BY_CRITICALITY = {
    5: 0.95,
    4: 0.90,
    3: 0.85,
    2: 0.80,
    1: 0.75,
}

IGNORED_REPO_DIRS = {
    ".git",
    ".pytest_cache",
    ".svelte-kit",
    "__pycache__",
    "build",
    "node_modules",
    "venv",
}


@dataclass(frozen=True)
class EvidenceResult:
    pattern: str
    present: bool
    matches: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "pattern": self.pattern,
            "present": self.present,
            "matches": list(self.matches),
        }


@dataclass(frozen=True)
class CategoryResult:
    category: str
    weight: float
    score: float
    present: int
    total: int
    evidence: tuple[EvidenceResult, ...] = ()

    @property
    def missing_patterns(self) -> list[str]:
        if self.total == 0:
            return ["no evidence configured"]
        return [item.pattern for item in self.evidence if not item.present]

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "weight": self.weight,
            "score": round(self.score, 4),
            "present": self.present,
            "total": self.total,
            "missing_patterns": self.missing_patterns,
            "evidence": [item.to_dict() for item in self.evidence],
        }


@dataclass(frozen=True)
class CapabilityResult:
    capability_id: str
    name: str
    criticality: int
    threshold: float
    score: float
    status: str
    impacted: bool
    changed_files: tuple[str, ...] = ()
    categories: tuple[CategoryResult, ...] = ()

    @property
    def missing_patterns(self) -> list[str]:
        missing: list[str] = []
        for category in self.categories:
            missing.extend(f"{category.category}: {pattern}" for pattern in category.missing_patterns)
        return missing

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability_id": self.capability_id,
            "name": self.name,
            "criticality": self.criticality,
            "threshold": self.threshold,
            "score": round(self.score, 4),
            "status": self.status,
            "impacted": self.impacted,
            "changed_files": list(self.changed_files),
            "missing_patterns": self.missing_patterns,
            "categories": [category.to_dict() for category in self.categories],
        }


@dataclass(frozen=True)
class SurvivabilityReport:
    overall_score: float
    impacted_score: float | None
    unmapped_changed_files: tuple[str, ...] = ()
    capabilities: tuple[CapabilityResult, ...] = ()

    @property
    def impacted_capabilities(self) -> tuple[CapabilityResult, ...]:
        return tuple(capability for capability in self.capabilities if capability.impacted)

    def to_dict(self) -> dict[str, Any]:
        return {
            "overall_score": round(self.overall_score, 4),
            "impacted_score": None if self.impacted_score is None else round(self.impacted_score, 4),
            "unmapped_changed_files": list(self.unmapped_changed_files),
            "capabilities": [capability.to_dict() for capability in self.capabilities],
            "impacted_capabilities": [
                capability.to_dict() for capability in self.impacted_capabilities
            ],
        }


def load_survivability_config(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def collect_repo_files(repo_root: Path) -> list[str]:
    files: list[str] = []
    for path in repo_root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(repo_root)
        if any(part in IGNORED_REPO_DIRS for part in relative.parts):
            continue
        files.append(relative.as_posix())
    return sorted(files)


def changed_files_from_git(repo_root: Path, base_ref: str) -> list[str]:
    commands = [
        ["git", "diff", "--name-only", f"{base_ref}...HEAD"],
        ["git", "diff", "--name-only", base_ref],
    ]
    last_error = ""
    for command in commands:
        result = subprocess.run(
            command,
            cwd=repo_root,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return sorted(line.strip() for line in result.stdout.splitlines() if line.strip())
        last_error = result.stderr.strip()
    raise RuntimeError(f"Could not compute changed files from git base {base_ref!r}: {last_error}")


def assess_survivability(
    config: dict[str, Any],
    repo_root: Path,
    changed_files: list[str] | None = None,
) -> SurvivabilityReport:
    repo_files = collect_repo_files(repo_root)
    weights = _category_weights(config)
    changed_files = changed_files or []
    capabilities = tuple(
        _score_capability(capability, repo_root, repo_files, weights, changed_files)
        for capability in config.get("capabilities", [])
    )
    return SurvivabilityReport(
        overall_score=_weighted_capability_score(capabilities),
        impacted_score=_impacted_score(capabilities, changed_files),
        unmapped_changed_files=_unmapped_changed_files(capabilities, changed_files),
        capabilities=capabilities,
    )


def _category_weights(config: dict[str, Any]) -> dict[str, float]:
    configured = config.get("category_weights")
    if not configured:
        return DEFAULT_CATEGORY_WEIGHTS
    return {str(key): float(value) for key, value in configured.items()}


def _score_capability(
    capability: dict[str, Any],
    repo_root: Path,
    repo_files: list[str],
    weights: dict[str, float],
    changed_files: list[str],
) -> CapabilityResult:
    capability_id = str(capability["id"])
    categories: list[CategoryResult] = []
    evidence_by_category = capability.get("evidence", {})
    category_names = list(weights)
    category_names.extend(
        category for category in evidence_by_category if category not in weights
    )
    for category in category_names:
        patterns = evidence_by_category.get(category, [])
        patterns = list(patterns or [])
        evidence = tuple(
            _score_evidence_pattern(pattern, repo_root, repo_files)
            for pattern in patterns
        )
        total = len(evidence)
        present = sum(1 for item in evidence if item.present)
        score = present / total if total else 0.0
        categories.append(
            CategoryResult(
                category=str(category),
                weight=weights.get(str(category), 0.0),
                score=score,
                present=present,
                total=total,
                evidence=evidence,
            )
        )

    weighted_denominator = sum(category.weight for category in categories)
    score = (
        sum(category.weight * category.score for category in categories) / weighted_denominator
        if weighted_denominator
        else 0.0
    )

    criticality = int(capability.get("criticality", 3))
    threshold = float(
        capability.get(
            "threshold",
            DEFAULT_THRESHOLDS_BY_CRITICALITY.get(criticality, 0.85),
        )
    )
    impact_patterns = list(capability.get("paths", [])) + _evidence_patterns(evidence_by_category)
    impacted_files = tuple(
        file_path
        for file_path in changed_files
        if _matches_any(file_path, impact_patterns)
    )
    return CapabilityResult(
        capability_id=capability_id,
        name=str(capability.get("name", capability_id)),
        criticality=criticality,
        threshold=threshold,
        score=score,
        status="meets" if score >= threshold else "below",
        impacted=bool(impacted_files),
        changed_files=impacted_files,
        categories=tuple(categories),
    )


def _score_evidence_pattern(
    pattern: str,
    repo_root: Path,
    repo_files: list[str],
) -> EvidenceResult:
    file_pattern, selector = _split_evidence_selector(pattern)
    matches = tuple(
        file_path
        for file_path in repo_files
        if _path_matches(file_path, file_pattern)
        and _has_evidence_signal(repo_root / file_path, selector)
    )
    return EvidenceResult(pattern=pattern, present=bool(matches), matches=matches)


def _split_evidence_selector(pattern: str) -> tuple[str, str | None]:
    file_pattern, separator, selector = pattern.partition("::")
    return file_pattern, selector if separator else None


def _has_evidence_signal(path: Path, selector: str | None) -> bool:
    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.stat().st_size > 0 and selector is None

    if not content.strip():
        return False

    if _is_python_test_evidence(path):
        return _has_collectable_python_test(content, selector)

    return selector is None and _has_config_or_document_signal(path, content)


def _is_python_test_evidence(path: Path) -> bool:
    if path.suffix != ".py":
        return False
    return (
        path.name.startswith("test_")
        or path.name.endswith("_test.py")
        or "tests" in path.parts
    )


def _has_collectable_python_test(content: str, selector: str | None) -> bool:
    try:
        module = ast.parse(content)
    except SyntaxError:
        return False
    if _is_module_level_skipped(module):
        return False

    tests = _collectable_python_test_names(module)
    if selector:
        return selector in tests
    return bool(tests)


def _is_module_level_skipped(module: ast.Module) -> bool:
    for statement in module.body:
        if isinstance(statement, ast.Assign) and any(
            _target_name(target) == "pytestmark" for target in statement.targets
        ):
            if _contains_pytest_skip_marker(statement.value):
                return True
        if isinstance(statement, ast.Expr) and _is_pytest_skip_call(statement.value):
            return True
    return False


def _collectable_python_test_names(module: ast.Module) -> set[str]:
    tests: set[str] = set()
    for statement in module.body:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if statement.name.startswith("test_"):
                tests.add(statement.name)
            continue
        if isinstance(statement, ast.ClassDef) and statement.name.startswith("Test"):
            class_tests = {
                item.name
                for item in statement.body
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                and item.name.startswith("test_")
            }
            if class_tests:
                tests.add(statement.name)
                tests.update(f"{statement.name}.{name}" for name in class_tests)
                tests.update(f"{statement.name}::{name}" for name in class_tests)
    return tests


def _target_name(target: ast.expr) -> str | None:
    if isinstance(target, ast.Name):
        return target.id
    return None


def _contains_pytest_skip_marker(node: ast.AST) -> bool:
    if _is_pytest_skip_marker(node):
        return True
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return any(_contains_pytest_skip_marker(item) for item in node.elts)
    return False


def _is_pytest_skip_marker(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    dotted_name = _dotted_name(node.func)
    if dotted_name == "pytest.mark.skip":
        return True
    if dotted_name != "pytest.mark.skipif":
        return False
    return bool(
        node.args
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value is True
    )


def _is_pytest_skip_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    if _dotted_name(node.func) != "pytest.skip":
        return False
    return any(
        keyword.arg == "allow_module_level"
        and isinstance(keyword.value, ast.Constant)
        and keyword.value.value is True
        for keyword in node.keywords
    )


def _dotted_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _dotted_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return None


def _has_config_or_document_signal(path: Path, content: str) -> bool:
    lines = _non_comment_lines(content)

    if path.suffix == ".json":
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            return False
        return parsed not in ({}, [], None, "")

    if path.suffix in {".yaml", ".yml"}:
        return any(":" in line or line.startswith("- ") for line in lines)

    if path.suffix in {".ini", ".cfg", ".toml"}:
        return any("=" in line or line.startswith("[") for line in lines)

    if path.name == "Makefile":
        return any(":" in line or "=" in line for line in lines)

    return bool(lines)


def _non_comment_lines(content: str) -> list[str]:
    lines: list[str] = []
    for line in content.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            lines.append(stripped)
    return lines


def _evidence_patterns(evidence_by_category: dict[str, Any]) -> list[str]:
    patterns: list[str] = []
    for category_patterns in evidence_by_category.values():
        patterns.extend(
            _split_evidence_selector(str(pattern))[0]
            for pattern in list(category_patterns or [])
        )
    return patterns


def _matches_any(file_path: str, patterns: list[str]) -> bool:
    return any(_path_matches(file_path, pattern) for pattern in patterns)


def _path_matches(file_path: str, pattern: str) -> bool:
    normalized = pattern.strip()
    if normalized.endswith("/**"):
        prefix = normalized[:-3].rstrip("/") + "/"
        return file_path.startswith(prefix)
    return fnmatch(file_path, normalized)


def _weighted_capability_score(capabilities: tuple[CapabilityResult, ...]) -> float:
    denominator = sum(capability.criticality for capability in capabilities)
    if denominator == 0:
        return 0.0
    return sum(capability.score * capability.criticality for capability in capabilities) / denominator


def _impacted_score(
    capabilities: tuple[CapabilityResult, ...],
    changed_files: list[str],
) -> float | None:
    if not changed_files:
        return None
    impacted = tuple(capability for capability in capabilities if capability.impacted)
    if not impacted:
        return None
    return _weighted_capability_score(impacted)


def _unmapped_changed_files(
    capabilities: tuple[CapabilityResult, ...],
    changed_files: list[str],
) -> tuple[str, ...]:
    if not changed_files:
        return ()
    mapped = {
        file_path
        for capability in capabilities
        for file_path in capability.changed_files
    }
    return tuple(file_path for file_path in changed_files if file_path not in mapped)
