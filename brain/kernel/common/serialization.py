"""Shared JSON-safe serialization and stable digest helpers."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from enum import Enum, StrEnum
import dataclasses
import hashlib
import json
import math
from pathlib import Path
from typing import Any
from uuid import UUID



def jsonable(value: Any, *, enum_values: bool = False, sort_sets: bool = True) -> Any:
    """Return a deterministic, JSON-compatible representation for common objects."""

    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if enum_values and isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Enum):
        return value.value if enum_values else str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal):
        return float(value)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return jsonable(dataclasses.asdict(value), enum_values=enum_values, sort_sets=sort_sets)
    if isinstance(value, Mapping):
        return {
            str(key): jsonable(item, enum_values=enum_values, sort_sets=sort_sets)
            for key, item in value.items()
        }
    if isinstance(value, tuple):
        return [jsonable(item, enum_values=enum_values, sort_sets=sort_sets) for item in value]
    if isinstance(value, list):
        return [jsonable(item, enum_values=enum_values, sort_sets=sort_sets) for item in value]
    if isinstance(value, set):
        items = [jsonable(item, enum_values=enum_values, sort_sets=sort_sets) for item in value]
        return sorted(items) if sort_sets else items
    return value


def stable_digest(payload: Any, *, length: int = 64, enum_values: bool = False) -> str:
    """Return a stable SHA-256 digest prefix for a JSON-compatible payload."""

    raw = json.dumps(
        jsonable(payload, enum_values=enum_values),
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:length]


def json_safe(
    value: Any,
    *,
    depth: int = 4,
    max_text_chars: int = 4000,
    max_mapping_items: int = 50,
    max_sequence_items: int = 50,
) -> Any:
    """Bounded JSON-safe conversion for evidence/artifact payloads.

    This preserves the truncation semantics that evidence-oriented modules used
    locally while making the behavior reusable by domains/skills/runtime code.
    """

    if depth <= 0:
        return _clean_limited_text(value, max_text_chars)
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, str):
        return _clean_limited_text(value, max_text_chars)
    if isinstance(value, bytes):
        return {
            "bytes": len(value),
            "sha256": hashlib.sha256(value).hexdigest(),
        }
    if isinstance(value, (datetime, Path, Enum)):
        return str(value)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return json_safe(
            dataclasses.asdict(value),
            depth=depth - 1,
            max_text_chars=max_text_chars,
            max_mapping_items=max_mapping_items,
            max_sequence_items=max_sequence_items,
        )
    if isinstance(value, Mapping):
        items = list(value.items())[:max_mapping_items]
        result = {
            str(key): json_safe(
                val,
                depth=depth - 1,
                max_text_chars=max_text_chars,
                max_mapping_items=max_mapping_items,
                max_sequence_items=max_sequence_items,
            )
            for key, val in items
            if not _is_empty_value(val)
        }
        if len(value) > max_mapping_items:
            result["_truncated_keys"] = len(value) - max_mapping_items
        return result
    if isinstance(value, (list, tuple, set, frozenset)):
        values = list(value)
        result = [
            json_safe(
                item,
                depth=depth - 1,
                max_text_chars=max_text_chars,
                max_mapping_items=max_mapping_items,
                max_sequence_items=max_sequence_items,
            )
            for item in values[:max_sequence_items]
        ]
        if len(values) > max_sequence_items:
            result.append({"_truncated_items": len(values) - max_sequence_items})
        return result
    return _clean_limited_text(value, max_text_chars)


def _clean_limited_text(value: Any, limit: int) -> str:
    if value is None:
        return ""
    if isinstance(value, str) and value == "":
        return ""
    text = str(value).replace("\x00", "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 24)].rstrip() + f" ... ({len(text)} chars)"


def _is_empty_value(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value == ""
    if isinstance(value, Mapping):
        return len(value) == 0
    if isinstance(value, (list, tuple, set, frozenset)):
        return len(value) == 0
    return False
