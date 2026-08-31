"""Architecture and schema invariants for terminal run failures."""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from brain.systems.runs.failure_diagnostic import (
    DiagnosticValueState,
    RunFailureStage,
    failure_category_for_run_context,
    read_run_failure_diagnostic,
    run_failure_metadata,
)
from brain.systems.runs.failures import (
    DEFAULT_FAILED_RUN_MESSAGE,
    RunFailureCategory,
    safe_terminal_run_message,
)
from brain.systems.runs.status import RunStatus


ROOT = Path(__file__).resolve().parents[1]


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _is_failed_status(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "RunStatus"
        and node.attr == "FAILED"
    ) or (isinstance(node, ast.Constant) and node.value == "failed")


def test_production_failed_transitions_use_typed_api() -> None:
    violations: list[str] = []
    for path in (ROOT / "brain").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if _call_name(node) not in {"set_status", "set_status_with_result"}:
                continue
            status_arg = node.args[1] if len(node.args) > 1 else next(
                (
                    keyword.value
                    for keyword in node.keywords
                    if keyword.arg == "status"
                ),
                None,
            )
            if status_arg is not None and _is_failed_status(status_arg):
                violations.append(str(path.relative_to(ROOT)))

    assert violations == []


def test_production_has_no_hand_built_failure_envelope() -> None:
    violations: list[str] = []
    for path in (ROOT / "brain").rglob("*.py"):
        if path.name == "failure_diagnostic.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            has_category = any(
                isinstance(key, ast.Constant) and key.value == "category"
                for key in node.keys
                if key is not None
            )
            spreads_diagnostic = any(
                isinstance(value, ast.Call)
                and _call_name(value) == "failure_diagnostic_metadata"
                for key, value in zip(node.keys, node.values, strict=True)
                if key is None
            )
            if has_category and spreads_diagnostic:
                violations.append(str(path.relative_to(ROOT)))

    assert violations == []


def test_failure_envelope_builder_rejects_unknown_stage() -> None:
    with pytest.raises(ValueError, match="cannot write an unknown stage"):
        run_failure_metadata(
            category=RunFailureCategory.INTERNAL,
            stage=RunFailureStage.UNKNOWN,
        )


def test_failure_envelope_builder_rejects_inconsistent_types() -> None:
    with pytest.raises(TypeError, match="failure category"):
        run_failure_metadata(  # type: ignore[arg-type]
            category="internal",
            stage=RunFailureStage.RUNNER_EXECUTION,
        )
    with pytest.raises(TypeError, match="exception type"):
        run_failure_metadata(
            category=RunFailureCategory.INTERNAL,
            stage=RunFailureStage.RUNNER_EXECUTION,
            exception_type=RuntimeError("not a class"),  # type: ignore[arg-type]
        )


def test_pre_tool_preservation_failure_cannot_be_unknown_and_generic() -> None:
    category = failure_category_for_run_context(
        RunFailureCategory.INTERNAL,
        requires_durable_preservation=True,
        tool_execution_started=False,
        failure_stage=RunFailureStage.AGENT_EXECUTION,
    )
    envelope = run_failure_metadata(
        category=category,
        stage=RunFailureStage.AGENT_EXECUTION,
    )
    message = safe_terminal_run_message(RunStatus.FAILED, category)

    assert category == RunFailureCategory.PRESERVATION_SETUP
    assert envelope["stage"] == RunFailureStage.AGENT_EXECUTION.value
    assert message != DEFAULT_FAILED_RUN_MESSAGE
    assert not (
        envelope["stage"] == RunFailureStage.UNKNOWN.value
        and message == DEFAULT_FAILED_RUN_MESSAGE
    )


async def test_reader_redacts_inconsistent_exception_identity() -> None:
    class EmptyEventResult:
        def all(self) -> list[str]:
            return []

    class EmptyEventSession:
        async def scalars(self, _query):
            return EmptyEventResult()

    run = SimpleNamespace(
        id=1,
        status="failed",
        metadata_={
            "failure": {
                "category": "internal",
                "diagnostic_schema": "typed_v1",
                "stage": "runner_execution",
                "exception_class": "RuntimeError",
                "exception_module": "not.a.loaded.module",
            }
        },
    )

    diagnostic = await read_run_failure_diagnostic(EmptyEventSession(), run=run)

    assert diagnostic is not None
    assert diagnostic.stage == RunFailureStage.RUNNER_EXECUTION
    assert diagnostic.exception_class is None
    assert diagnostic.exception_class_state == DiagnosticValueState.REDACTED
