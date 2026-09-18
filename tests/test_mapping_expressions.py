from unittest.mock import Mock, call

import pytest

from brain.platform.mapping_expressions import (
    MappingExpressionError,
    evaluate_mapping_expression,
    render_path_template,
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


@pytest.mark.parametrize("value", [object(), None, "", 253, False, {"key": "value"}])
def test_path_template_plain_path_preserves_resolution(value):
    source = {"payload": {"issues": [{"key": value}]}}
    resolve_path = Mock(return_value=value)
    path = "payload.issues.0.key"
    assert render_path_template(
        path, source, resolve_path=resolve_path, missing=object(),
    ) is value
    resolve_path.assert_called_once_with(source, path)


def test_path_template_composes_github_identity():
    source = {"hints": {"repo": "uwear-ai/uwear-website", "number": 253}}
    resolve_path = Mock(side_effect=[source["hints"]["repo"], source["hints"]["number"]])
    assert render_path_template(
        "github:{hints.repo}:issue:{hints.number}", source,
        resolve_path=resolve_path, missing=object(),
    ) == "github:uwear-ai/uwear-website:issue:253"
    assert resolve_path.call_args_list == [call(source, "hints.repo"), call(source, "hints.number")]


@pytest.mark.parametrize("part", ["missing", "null", "empty"])
@pytest.mark.parametrize("position", [0, 1])
def test_path_template_missing_part_makes_entire_identity_missing(part, position):
    missing = object()
    values = ["uwear-ai/uwear-website", 253]
    values[position] = {"missing": missing, "null": None, "empty": ""}[part]
    assert render_path_template(
        "github:{hints.repo}:issue:{hints.number}", {},
        resolve_path=Mock(side_effect=values), missing=missing,
    ) is missing


@pytest.mark.parametrize("value, expected", [(0, "id:0"), (False, "id:False")])
def test_path_template_keeps_present_falsy_values(value, expected):
    assert render_path_template(
        "id:{value}", {}, resolve_path=Mock(return_value=value), missing=object(),
    ) == expected


@pytest.mark.parametrize("template, expected", [
    ("{{literal}}:{hints.number}", "{literal}:253"),
    ("{{{hints.number}}}", "{253}"),
    ("{{}}", "{}"),
])
def test_path_template_escapes_braces(template, expected):
    assert render_path_template(
        template, {}, resolve_path=Mock(return_value=253), missing=object(),
    ) == expected


def test_path_template_without_opening_brace_is_always_a_path():
    source = {"key}}": "value"}
    resolve_path = Mock(return_value="value")
    assert render_path_template(
        "key}}", source, resolve_path=resolve_path, missing=object(),
    ) == "value"
    resolve_path.assert_called_once_with(source, "key}}")


@pytest.mark.parametrize("template", [
    "github:{hints.repo", "github:{}", "github:{ }", "{outer{inner}}",
    "{hints.repo}:{", "{hints.repo}:{}", "{hints.repo}}",
])
def test_path_template_rejects_malformed_syntax_even_after_missing_part(template):
    missing = object()
    with pytest.raises(MappingExpressionError, match="path template"):
        render_path_template(
            template, {}, resolve_path=Mock(return_value=missing), missing=missing,
        )
