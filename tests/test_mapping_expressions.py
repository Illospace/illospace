from unittest.mock import Mock

import pytest

from brain.platform.mapping_expressions import (
    MappingExpressionError,
    evaluate_mapping_expression,
    validate_mapping_expression,
)


def test_expression_dependencies_preserve_missing_values_and_read_clock_lazily():
    missing = object()
    source = {"present": None}
    resolve_path = Mock(side_effect=lambda item, path: item.get(path, missing))
    clock = Mock(return_value="2026-09-16T12:01:00Z")

    for expression in ("absent", {"path": "absent"}):
        assert evaluate_mapping_expression(
            expression, source, resolve_path=resolve_path, clock=clock,
        ) is missing
    assert evaluate_mapping_expression(
        {"path": "present"}, source, resolve_path=resolve_path, clock=clock,
    ) is None
    clock.assert_not_called()
    resolve_path.reset_mock()

    assert evaluate_mapping_expression(
        {"now": True}, source, resolve_path=resolve_path, clock=clock,
    ) == "2026-09-16T12:01:00Z"
    clock.assert_called_once_with()
    resolve_path.assert_not_called()


@pytest.mark.parametrize("condition, expected", [
    ({"field": "status", "exists": True}, True),
    ({"path": "absent", "exists": False}, True),
    ({"path": "status", "equals": "open"}, True),
    ({"path": "status", "not_equals": "open"}, False),
    ({"path": "status", "in": ["closed"]}, False),
    ({"path": "status"}, True),
])
def test_nested_expressions_keep_condition_template_and_literal_semantics(condition, expected):
    expression = {
        "if": condition,
        "then": {"template": "{status}/{absent}/{count}"},
        "else": {"if": {"path": "status"}, "then": "literal.path", "else": {"now": True}},
    }
    validate_mapping_expression("field", expression)
    clock = Mock(side_effect=AssertionError("Unselected branch read the clock"))
    assert evaluate_mapping_expression(
        expression, {"status": "open", "count": 0},
        resolve_path=lambda source, path: source.get(path), clock=clock,
    ) == ("open//0" if expected else "literal.path")


def test_generic_validation_allows_metadata_and_keeps_ordered_diagnostics():
    validate_mapping_expression("field", {"const": None, "metadata": "allowed"})
    with pytest.raises(MappingExpressionError) as caught:
        validate_mapping_expression("field", {
            "if": {"path": "", "in": "bad", "exists": "bad"},
            "else": {"now": False},
        })
    assert caught.value.errors == (
        "field.if requires field or path",
        "field.if.in must be a list",
        "field.if.exists must be a boolean",
        "field.then is required for conditional mapping expressions",
        "field.else.now must be true",
    )


@pytest.mark.parametrize("expression, message", [
    ({"now": 1}, "mapping expression.now must be true"),
    ({"if": None}, "mapping expression.if must be an object"),
    ({"if": {}}, "mapping condition requires field or path"),
    ({"if": {"path": "status", "in": "open"}}, "mapping condition.in must be a list"),
])
def test_evaluation_errors_are_neutral(expression, message):
    with pytest.raises(MappingExpressionError) as caught:
        evaluate_mapping_expression(
            expression, {}, resolve_path=lambda source, path: None, clock=Mock(),
        )
    assert str(caught.value) == message
