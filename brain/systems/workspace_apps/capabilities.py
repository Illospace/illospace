"""Canonical capability operation metadata for workspace apps."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


DOMAIN_BINDING_KIND = "domain"
SYSTEM_BINDING_KIND = "system"
CAPABILITY_BINDING_KINDS = frozenset({DOMAIN_BINDING_KIND, SYSTEM_BINDING_KIND})


@dataclass(frozen=True)
class CapabilityOperation:
    key: str
    kind: str
    write: bool = False
    broker: bool = True


DOMAIN_OPERATION_DEFINITIONS = (
    CapabilityOperation("schema", DOMAIN_BINDING_KIND),
    CapabilityOperation("list", DOMAIN_BINDING_KIND),
    CapabilityOperation("query", DOMAIN_BINDING_KIND),
    CapabilityOperation("get", DOMAIN_BINDING_KIND),
    CapabilityOperation("create", DOMAIN_BINDING_KIND, write=True),
    CapabilityOperation("update", DOMAIN_BINDING_KIND, write=True),
    CapabilityOperation("archive", DOMAIN_BINDING_KIND, write=True),
    CapabilityOperation("aggregate", DOMAIN_BINDING_KIND),
    CapabilityOperation("bulkUpdate", DOMAIN_BINDING_KIND, write=True),
    CapabilityOperation("history", DOMAIN_BINDING_KIND, broker=False),
    CapabilityOperation("listRelations", DOMAIN_BINDING_KIND, broker=False),
    CapabilityOperation("createRelation", DOMAIN_BINDING_KIND, write=True, broker=False),
    CapabilityOperation("archiveRelation", DOMAIN_BINDING_KIND, write=True, broker=False),
)

SYSTEM_OPERATION_DEFINITIONS = (
    CapabilityOperation("schema", SYSTEM_BINDING_KIND),
    CapabilityOperation("list", SYSTEM_BINDING_KIND),
    CapabilityOperation("query", SYSTEM_BINDING_KIND),
    CapabilityOperation("get", SYSTEM_BINDING_KIND),
    CapabilityOperation("aggregate", SYSTEM_BINDING_KIND),
)

DOMAIN_OPERATIONS = frozenset(operation.key for operation in DOMAIN_OPERATION_DEFINITIONS)
DOMAIN_BROKER_OPERATIONS = frozenset(
    operation.key for operation in DOMAIN_OPERATION_DEFINITIONS if operation.broker
)
DOMAIN_WRITE_OPERATIONS = frozenset(
    operation.key for operation in DOMAIN_OPERATION_DEFINITIONS if operation.write
)
SYSTEM_READ_OPERATIONS = frozenset(operation.key for operation in SYSTEM_OPERATION_DEFINITIONS)

COMMON_DOMAIN_BINDING_OPERATIONS = [
    "schema",
    "list",
    "get",
    "query",
    "create",
    "update",
    "archive",
]
COMMON_SYSTEM_BINDING_OPERATIONS = ["schema", "list", "query", "get", "aggregate"]

_DOMAIN_CANONICAL_BY_COMPACT = {
    re.sub(r"[^a-z0-9]+", "", operation.lower()): operation for operation in DOMAIN_OPERATIONS
}
_DOMAIN_BROKER_CANONICAL_BY_COMPACT = {
    re.sub(r"[^a-z0-9]+", "", operation.lower()): operation for operation in DOMAIN_BROKER_OPERATIONS
}
_SYSTEM_CANONICAL_BY_COMPACT = {
    re.sub(r"[^a-z0-9]+", "", operation.lower()): operation for operation in SYSTEM_READ_OPERATIONS
}
_DOMAIN_OPERATION_ALIAS_EXPANSIONS = {
    "read": ("schema", "list", "get", "query"),
    "readonly": ("schema", "list", "get", "query"),
    "write": ("create", "update"),
    "writes": ("create", "update"),
    "mutate": ("create", "update"),
    "mutation": ("create", "update"),
    "upsert": ("create", "update"),
    "delete": ("archive",),
    "remove": ("archive",),
    "destroy": ("archive",),
    "crud": tuple(COMMON_DOMAIN_BINDING_OPERATIONS),
}


def is_domain_write_operation(operation: str) -> bool:
    return operation in DOMAIN_WRITE_OPERATIONS


def normalize_domain_operations(value: Any, *, broker_only: bool = False) -> tuple[list[str], bool]:
    default_operations = list(COMMON_DOMAIN_BINDING_OPERATIONS)
    raw_items, changed = _operation_items(value)
    if not raw_items:
        return default_operations, True

    operations: list[str] = []
    for raw_item in raw_items:
        canonical = _domain_operation_expansion(raw_item, broker_only=broker_only)
        if canonical != [str(raw_item)]:
            changed = True
        for operation in canonical:
            if operation and operation not in operations:
                operations.append(operation)

    return (operations, changed) if operations else (default_operations, True)


def normalize_system_operations(value: Any) -> tuple[list[str], bool]:
    raw_items, changed = _operation_items(value)
    if not raw_items:
        return list(COMMON_SYSTEM_BINDING_OPERATIONS), True

    operations: list[str] = []
    for raw_item in raw_items:
        raw = str(raw_item or "").strip()
        compact = _compact_operation(raw)
        if compact in {"read", "readonly"}:
            expanded = ("schema", "list", "query", "get")
            changed = True
        elif compact in _SYSTEM_CANONICAL_BY_COMPACT:
            expanded = (_SYSTEM_CANONICAL_BY_COMPACT[compact],)
            if expanded[0] != raw:
                changed = True
        else:
            expanded = (raw,)
        for operation in expanded:
            if operation and operation not in operations:
                operations.append(operation)

    return (operations, changed) if operations else (list(COMMON_SYSTEM_BINDING_OPERATIONS), True)


def _operation_items(value: Any) -> tuple[list[Any], bool]:
    if isinstance(value, str):
        return [item for item in re.split(r"[\s,|/]+", value) if item], True
    if isinstance(value, list):
        return value, False
    return [], True


def _domain_operation_expansion(value: Any, *, broker_only: bool) -> list[str]:
    raw = str(value or "").strip()
    if not raw:
        return []
    compact = _compact_operation(raw)
    if compact in _DOMAIN_OPERATION_ALIAS_EXPANSIONS:
        return list(_DOMAIN_OPERATION_ALIAS_EXPANSIONS[compact])
    canonical_by_compact = _DOMAIN_BROKER_CANONICAL_BY_COMPACT if broker_only else _DOMAIN_CANONICAL_BY_COMPACT
    if compact in canonical_by_compact:
        return [canonical_by_compact[compact]]
    return [raw]


def _compact_operation(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())
