"""Environment variable parsing helpers."""

from __future__ import annotations

import os
from collections.abc import Mapping

_FALSE_VALUES = {"0", "false", "no", "off", ""}
_TRUE_VALUES = {"1", "true", "yes", "on"}


def env_flag(
    name: str,
    default: bool = False,
    *,
    env: Mapping[str, str] | None = None,
    true_only: bool = False,
    true_values: set[str] | None = None,
    false_values: set[str] | None = None,
) -> bool:
    """Read a boolean env var with explicit defaults.

    ``true_only=True`` matches helpers that only enable on known true values.
    ``true_values``/``false_values`` let legacy call sites preserve their exact
    accepted spellings while still sharing parsing mechanics.
    """

    source = os.environ if env is None else env
    value = source.get(name)
    if value is None:
        return default
    normalized = str(value).strip().lower()
    if true_only:
        return normalized in (true_values or _TRUE_VALUES)
    return normalized not in (false_values or _FALSE_VALUES)


def env_int(
    name: str,
    default: int | str,
    *,
    env: Mapping[str, str] | None = None,
    minimum: int | None = None,
) -> int:
    """Read an integer env var, falling back to ``default`` on parse errors."""

    source = os.environ if env is None else env
    value = source.get(name)
    try:
        result = int(default if value is None else value)
    except (TypeError, ValueError):
        result = int(default)
    return max(minimum, result) if minimum is not None else result


def env_float(
    name: str,
    default: float,
    *,
    env: Mapping[str, str] | None = None,
) -> float:
    """Read a float env var, falling back to ``default`` on parse errors."""

    source = os.environ if env is None else env
    value = source.get(name)
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
