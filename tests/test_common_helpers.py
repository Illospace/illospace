from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest

from brain.kernel.common.coercion import (
    as_mapping,
    clamp,
    coerce_datetime,
    coerce_float,
    coerce_int,
    drop_none,
    int_or_none,
    object_to_dict,
    optional_text,
)
from brain.kernel.common.env import env_flag, env_float, env_int
from brain.kernel.common.serialization import json_safe, jsonable, stable_digest
from brain.kernel.common.time import ensure_utc, utcnow


def test_coercion_helpers_preserve_legacy_defaults() -> None:
    assert as_mapping({"a": 1}) == {"a": 1}
    assert as_mapping(None) == {}
    assert optional_text("  hello  ") == "hello"
    assert optional_text("   ") is None
    assert drop_none({"a": 1, "b": None}) == {"a": 1}
    assert clamp(None) == 0.0
    assert clamp(3, upper=2.0) == 2.0
    assert coerce_float("2.5") == 2.5
    assert coerce_float("bad", default=None) is None
    assert coerce_int("7") == 7
    assert coerce_int("bad", default=-1) == -1
    assert int_or_none("8") == 8
    assert int_or_none("bad") is None


def test_object_to_dict_handles_common_model_shapes() -> None:
    class ToDict:
        def to_dict(self) -> dict[str, int]:
            return {"value": 1}

    class ModelDump:
        def model_dump(self) -> dict[str, int]:
            return {"value": 2}

    class Plain:
        visible = "class-only"

        def __init__(self) -> None:
            self.public = 3
            self._private = 4

    assert object_to_dict(ToDict()) == {"value": 1}
    assert object_to_dict(ModelDump()) == {"value": 2}
    assert object_to_dict(Plain()) == {"public": 3}


def test_datetime_and_time_helpers_are_timezone_aware() -> None:
    parsed = coerce_datetime("2026-01-02T03:04:05Z")
    assert parsed == datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    assert coerce_datetime("not-a-date") is None
    now = utcnow()
    assert now.tzinfo is not None
    assert ensure_utc(datetime(2026, 1, 2, tzinfo=timezone.utc)) == datetime(
        2026,
        1,
        2,
        tzinfo=timezone.utc,
    )
    with pytest.raises(ValueError):
        ensure_utc(datetime(2026, 1, 2))


def test_env_helpers_support_strict_and_permissive_boolean_semantics() -> None:
    env = {"YES": "yes", "NO": "off", "COUNT": "4", "BAD": "oops", "RATIO": "0.25"}
    assert env_flag("YES", env=env) is True
    assert env_flag("NO", default=True, env=env) is False
    assert env_flag("NO", default=True, env=env, true_only=True) is False
    assert env_flag("MISSING", default=True, env=env) is True
    assert env_int("COUNT", 1, env=env) == 4
    assert env_int("BAD", 3, env=env) == 3
    assert env_int("BAD", "3", env=env, minimum=5) == 5
    assert env_float("RATIO", 1.0, env=env) == 0.25
    assert env_float("BAD", 1.0, env=env) == 1.0
    assert env_flag("NO", default=True, env=env, false_values={"0", "false", "no", "off", ""}) is False
    assert env_flag("DISABLED", default=True, env={"DISABLED": "disabled"}, false_values={"0", "false", "no", "off", ""}) is True
    assert env_flag("ENABLED", default=False, env={"ENABLED": "enabled"}, true_only=True, true_values={"1", "true", "yes", "on", "enabled"}) is True


def test_serialization_helpers_are_stable_and_bounded() -> None:
    payload = {
        "when": datetime(2026, 1, 2, tzinfo=timezone.utc),
        "items": {"b", "a"},
        "workspace_id": UUID("00000000-0000-0000-0000-000000000123"),
    }
    assert jsonable(payload) == {
        "when": "2026-01-02T00:00:00+00:00",
        "items": ["a", "b"],
        "workspace_id": "00000000-0000-0000-0000-000000000123",
    }
    assert stable_digest({"b": 2, "a": 1}, length=12) == stable_digest({"a": 1, "b": 2}, length=12)
    safe = json_safe({"text": "x" * 20, "empty": {}, "none": None}, max_text_chars=10)
    assert "empty" not in safe
    assert "none" not in safe
    assert safe["text"].endswith("chars)")
