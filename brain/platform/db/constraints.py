"""Small helpers for model-owned database constraints."""

from __future__ import annotations


def sql_string_literal(value: str) -> str:
    """Return a SQL string literal for static constraint expressions."""
    return "'" + str(value).replace("'", "''") + "'"


def sql_string_list(values: tuple[str, ...]) -> str:
    """Return a comma-separated SQL literal list."""
    return ", ".join(sql_string_literal(value) for value in values)


def check_in_constraint(column_name: str, values: tuple[str, ...]) -> str:
    """Return a stable ``column IN (...)`` check expression."""
    if not values:
        raise ValueError("check constraint values cannot be empty")
    return f"{column_name} IN ({sql_string_list(values)})"
