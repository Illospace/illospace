"""Shared mapping expressions, independent of transports and domain services."""
from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any


MAPPING_KEYS = ("const", "path", "template", "if", "now")
MAPPING_DESCRIPTION = "const, path, template, now, or if/then/else"
_TEMPLATE_RE = re.compile(r"\{([a-zA-Z_][\w.-]*)\}")
_PATH_TEMPLATE_RE = re.compile(r"\{\{|\}\}|\{([^{}]*)\}|[{}]")


class MappingExpressionError(ValueError):
    """Invalid expression, with ordered diagnostics for boundary translation."""

    def __init__(self, *errors: str) -> None:
        self.errors = errors
        super().__init__("; ".join(errors))


def validate_mapping_expression(
    field_path: str,
    expr: Any,
    *,
    branch_literal: bool = False,
) -> None:
    """Validate the full language; callers may restrict accepted keys further."""
    errors: list[str] = []
    _collect_expression_errors(field_path, expr, errors, branch_literal=branch_literal)
    if errors:
        raise MappingExpressionError(*errors)


def evaluate_mapping_expression(
    expr: Any,
    source: Mapping[str, Any],
    *,
    resolve_path: Callable[[Any, str], Any],
    clock: Callable[[], str],
) -> Any:
    """Evaluate using the caller's path semantics and ISO timestamp clock.

    Missing path values pass through unchanged. Top-level strings are paths;
    non-mapping conditional branches are literals. Validation is separate so
    callers can impose their own accepted expression vocabulary.
    """
    if isinstance(expr, Mapping):
        if "const" in expr:
            return expr.get("const")
        if "path" in expr:
            return resolve_path(source, str(expr.get("path") or ""))
        if "template" in expr:
            return render_mapping_template(str(expr.get("template") or ""), source, resolve_path=resolve_path)
        if "now" in expr:
            if expr.get("now") is True:
                return clock()
            raise MappingExpressionError("mapping expression.now must be true")
        if "if" in expr:
            condition = expr.get("if")
            if not isinstance(condition, Mapping):
                raise MappingExpressionError("mapping expression.if must be an object")
            matches = _condition_matches(condition, source, resolve_path=resolve_path)
            branch = expr.get("then") if matches else expr.get("else")
            if isinstance(branch, Mapping):
                return evaluate_mapping_expression(branch, source, resolve_path=resolve_path, clock=clock)
            return branch
        raise MappingExpressionError("mapping expressions must use const, path, template, now, or if/then/else")
    if expr is None:
        return None
    return resolve_path(source, str(expr))


def _condition_matches(
    condition: Mapping[str, Any],
    source: Mapping[str, Any],
    *,
    resolve_path: Callable[[Any, str], Any],
) -> bool:
    path = condition.get("path") if "path" in condition else condition.get("field")
    if path is None:
        raise MappingExpressionError("mapping condition requires field or path")
    value = resolve_path(source, str(path))
    if "exists" in condition:
        return (value is not None) is bool(condition.get("exists"))
    if "equals" in condition:
        return value == condition.get("equals")
    if "not_equals" in condition:
        return value != condition.get("not_equals")
    if "in" in condition:
        options = condition.get("in")
        if not isinstance(options, list):
            raise MappingExpressionError("mapping condition.in must be a list")
        return value in options
    return bool(value)


def render_mapping_template(
    template: str,
    source: Mapping[str, Any],
    *,
    resolve_path: Callable[[Any, str], Any],
) -> str:
    """Substitute path placeholders with the caller's path lookup."""
    def replace(match: re.Match[str]) -> str:
        value = resolve_path(source, match.group(1))
        return "" if value is None else str(value)

    return _TEMPLATE_RE.sub(replace, template)


def render_path_template(
    template: str,
    source: Mapping[str, Any],
    *,
    resolve_path: Callable[[Any, str], Any],
    missing: Any,
) -> Any:
    """Resolve a plain path or interpolate paths, preserving the caller's sentinel.

    Doubled braces are literals. Any missing, null, or empty placeholder makes
    the entire result missing; malformed templates still raise an error.
    """
    if "{" not in template:
        return resolve_path(source, template)

    has_missing = False

    def replace(match: re.Match[str]) -> str:
        nonlocal has_missing
        token = match.group(0)
        if token in ("{{", "}}"):
            return token[0]
        if token in ("{", "}"):
            raise MappingExpressionError("path template contains an unmatched brace")
        path = match.group(1)
        if not path.strip():
            raise MappingExpressionError("path template placeholder requires a non-empty path")
        value = resolve_path(source, path)
        if value is missing or value is None or value == "":
            has_missing = True
            return ""
        return str(value)

    rendered = _PATH_TEMPLATE_RE.sub(replace, template)
    return missing if has_missing else rendered


def _collect_expression_errors(
    field_path: str,
    expr: Any,
    errors: list[str],
    *,
    branch_literal: bool = False,
) -> None:
    if isinstance(expr, Mapping):
        present = [key for key in MAPPING_KEYS if key in expr]
        if not present:
            errors.append(f"{field_path} mapping expressions must use {MAPPING_DESCRIPTION}")
            return
        if len(present) > 1:
            errors.append(f"{field_path} mapping expression must use only one of const, path, template, now, or if")
            return
        key = present[0]
        if key == "path" and not isinstance(expr.get("path"), str):
            errors.append(f"{field_path}.path must be a string")
        elif key == "template" and not isinstance(expr.get("template"), str):
            errors.append(f"{field_path}.template must be a string")
        elif key == "now" and expr.get("now") is not True:
            errors.append(f"{field_path}.now must be true")
        elif key == "if":
            _collect_condition_errors(f"{field_path}.if", expr.get("if"), errors)
            if "then" not in expr:
                errors.append(f"{field_path}.then is required for conditional mapping expressions")
            else:
                _collect_expression_errors(
                    f"{field_path}.then",
                    expr.get("then"),
                    errors,
                    branch_literal=True,
                )
            if "else" in expr:
                _collect_expression_errors(
                    f"{field_path}.else",
                    expr.get("else"),
                    errors,
                    branch_literal=True,
                )
        return
    if expr is None:
        return
    if isinstance(expr, str):
        if not branch_literal and not expr.strip():
            errors.append(f"{field_path} mapping path must not be empty")
        return
    if not branch_literal:
        errors.append(f"{field_path} mapping expression must be a string path or object")


def _collect_condition_errors(field_path: str, condition: Any, errors: list[str]) -> None:
    if not isinstance(condition, Mapping):
        errors.append(f"{field_path} must be an object")
        return
    path = condition.get("path") if "path" in condition else condition.get("field")
    if not isinstance(path, str) or not path.strip():
        errors.append(f"{field_path} requires field or path")
    if "in" in condition and not isinstance(condition.get("in"), list):
        errors.append(f"{field_path}.in must be a list")
    if "exists" in condition and not isinstance(condition.get("exists"), bool):
        errors.append(f"{field_path}.exists must be a boolean")
