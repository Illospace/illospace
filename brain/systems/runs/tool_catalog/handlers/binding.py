"""Shared binding helpers for handlers backed by derived typed contracts."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TypeVar


_ContractT = TypeVar("_ContractT")


def bind_derived_tool_contract(
    values: Mapping[str, object],
    *,
    field_schema: Mapping[str, Mapping[str, object]],
    factory: Callable[[Mapping[str, object]], _ContractT],
    contract_name: str,
) -> _ContractT:
    """Validate derived field names and build the handler's typed contract."""

    unexpected = set(values) - field_schema.keys()
    if unexpected:
        unexpected_names = ", ".join(sorted(unexpected))
        raise TypeError(f"Unexpected {contract_name} fields: {unexpected_names}")
    return factory(values)
