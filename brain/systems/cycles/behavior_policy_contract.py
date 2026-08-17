"""Cycle behavior-policy snapshot contract shared by commands and adapters."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import Field as DataclassField
from dataclasses import dataclass, field as dataclass_field, fields, replace
from datetime import datetime, timezone
from typing import Any, ClassVar, Mapping, TypeAlias, cast

from brain.kernel.common.serialization import jsonable
from brain.platform.db.models.cycle import Cycle, CycleGuidance
from brain.systems.cycles.common import (
    canonical_execution_mode,
    cycle_executor_binding,
    validate_cycle_timeout_seconds,
    validate_executor_binding,
    validate_model_override,
    validate_nonempty_trimmed,
    validate_thinking_override,
)
from brain.systems.cycles.execution_policy_registry import (
    validate_cycle_execution_policy_key,
)
from brain.systems.cycles.schedules import (
    compute_next_run_at,
    validate_schedule_expr,
    validate_timezone_name,
)
from brain.systems.cycles.skill_refs import (
    validate_cycle_prompt,
    validate_cycle_skill_ids,
)

__all__ = [
    "CyclePolicySnapshot",
    "patch_ignores_none",
]

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]

_PATCH_IGNORE_NONE = "patch_ignore_none"


def patch_ignores_none(snapshot_field: DataclassField[Any]) -> bool:
    """Return whether a patch must retain this field when its value is null."""

    return bool(snapshot_field.metadata.get(_PATCH_IGNORE_NONE, False))


@dataclass(frozen=True)
class CyclePolicySnapshot:
    """One validated Cycle policy, with versioned JSON persistence.

    Scalar fields use the same name on the snapshot, Cycle, and patch.
    """

    SNAPSHOT_VERSION: ClassVar[int] = 2

    name: str = dataclass_field(metadata={_PATCH_IGNORE_NONE: True})
    prompt: str = dataclass_field(metadata={_PATCH_IGNORE_NONE: True})
    schedule_expr: str = dataclass_field(metadata={_PATCH_IGNORE_NONE: True})
    timezone: str = dataclass_field(metadata={_PATCH_IGNORE_NONE: True})
    enabled: bool = dataclass_field(metadata={_PATCH_IGNORE_NONE: True})
    max_concurrency: int
    timeout_seconds: int | None
    retry_policy: dict[str, JsonValue]
    model_override: str | None
    thinking_override: str | None
    execution_policy_key: str | None
    executor_binding: str
    skill_ids: list[int]
    target_idea_id: str | None
    guidance: list[str]

    @classmethod
    def configuration_field_names(cls) -> tuple[str, ...]:
        """Return the scalar fields rendered under ``configuration``."""

        return tuple(field.name for field in fields(cls) if field.name != "guidance")

    @classmethod
    def from_cycle(
        cls,
        cycle: Cycle,
        guidance_rows: list[CycleGuidance],
    ) -> CyclePolicySnapshot:
        """Build the effective policy from the live Cycle read model."""

        values = {
            field.name: deepcopy(getattr(cycle, field.name, None))
            for field in fields(cls)
            if field.name != "guidance"
        }
        values["max_concurrency"] = max(
            int(values["max_concurrency"] or 1),
            1,
        )
        values["enabled"] = bool(values["enabled"])
        values["retry_policy"] = dict(values["retry_policy"] or {})
        values["executor_binding"] = cycle_executor_binding(cycle)
        values["skill_ids"] = validate_cycle_skill_ids(values["skill_ids"])
        values["target_idea_id"] = (
            str(values["target_idea_id"])
            if values["target_idea_id"] is not None
            else None
        )
        values["guidance"] = [row.guidance for row in guidance_rows]
        return cls(**values).validated()

    def apply_to(self, cycle: Cycle) -> None:
        """Apply this policy explicitly to its live Cycle fields."""

        applied_at = datetime.now(timezone.utc)
        for field_name in self.configuration_field_names():
            setattr(cycle, field_name, deepcopy(getattr(self, field_name)))
        cycle.execution_mode = canonical_execution_mode()
        cycle.reopen_archived = True
        cycle.next_run_at = compute_next_run_at(
            cycle.schedule_expr,
            cycle.timezone,
        )
        cycle.receipt_monitoring_started_at = applied_at
        cycle.updated_at = applied_at

    def validated(self) -> CyclePolicySnapshot:
        """Return a normalized snapshot or raise for an invalid policy."""

        timezone_name = validate_timezone_name(self.timezone)
        if not isinstance(self.enabled, bool):
            raise ValueError("enabled must be a boolean")
        if not isinstance(self.retry_policy, dict):
            raise ValueError("retry_policy must be an object")
        if not isinstance(self.guidance, (list, tuple)):
            raise ValueError("guidance must be a list of strings")
        skill_ids = validate_cycle_skill_ids(self.skill_ids)
        retry_policy = cast(
            dict[str, JsonValue],
            jsonable(deepcopy(self.retry_policy)),
        )
        guidance = [
            validate_nonempty_trimmed(value, "guidance")
            for value in self.guidance
        ]
        if len(guidance) != len(set(guidance)):
            raise ValueError("guidance entries must be unique")
        return replace(
            self,
            name=validate_nonempty_trimmed(self.name, "name"),
            prompt=validate_cycle_prompt(self.prompt, skill_ids=skill_ids),
            schedule_expr=validate_schedule_expr(
                self.schedule_expr,
                timezone_name,
            ),
            timezone=timezone_name,
            enabled=self.enabled,
            max_concurrency=_validated_max_concurrency(self.max_concurrency),
            timeout_seconds=validate_cycle_timeout_seconds(self.timeout_seconds),
            retry_policy=retry_policy,
            model_override=validate_model_override(self.model_override),
            thinking_override=validate_thinking_override(self.thinking_override),
            execution_policy_key=validate_cycle_execution_policy_key(
                self.execution_policy_key
            ),
            executor_binding=validate_executor_binding(self.executor_binding),
            skill_ids=skill_ids,
            target_idea_id=(
                str(self.target_idea_id)
                if self.target_idea_id is not None
                else None
            ),
            guidance=sorted(guidance),
        )

    def encode(self) -> dict[str, Any]:
        """Encode the current schema for the JSON database boundary."""

        encoded = {
            field.name: jsonable(deepcopy(getattr(self, field.name)))
            for field in fields(self)
        }
        return {"snapshot_version": self.SNAPSHOT_VERSION, **encoded}

    @classmethod
    def decode(
        cls,
        snapshot: Mapping[str, Any],
        *,
        current: CyclePolicySnapshot,
    ) -> CyclePolicySnapshot:
        """Decode a legacy or versioned shape against today's effective policy.

        Positive versions remain readable for forward compatibility. Unknown
        fields are ignored, and missing fields retain their current values.
        """

        if not isinstance(snapshot, Mapping):
            raise ValueError("Cycle policy snapshot must be an object")
        snapshot_version = snapshot.get("snapshot_version")
        if snapshot_version is not None and (
            isinstance(snapshot_version, bool)
            or not isinstance(snapshot_version, int)
            or snapshot_version < 1
        ):
            raise ValueError("Cycle policy snapshot has an invalid version")

        validated_current = current.validated()
        decoded = {
            field.name: deepcopy(
                snapshot.get(field.name, getattr(validated_current, field.name))
            )
            for field in fields(cls)
        }
        return cls(**decoded).validated()


def _validated_max_concurrency(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("max_concurrency must be an integer >= 1")
    return value
