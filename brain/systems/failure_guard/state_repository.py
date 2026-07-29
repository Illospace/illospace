"""Shared SQLAlchemy repository for failure-guard trigger state."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import json
from typing import Generic, Protocol, TypeVar, cast

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from brain.systems.failure_guard.core import (
    FailureGuardTriggerKind,
    FailureGuardTriggerState,
    JsonValue,
)


class FailureGuardTriggerStateRecord(Protocol):
    """ORM row shape required by the generic state repository."""

    trigger_kind: str
    trigger_state: dict[str, JsonValue]


RecordT = TypeVar("RecordT", bound=FailureGuardTriggerStateRecord)


def _json_document(
    state: FailureGuardTriggerState,
) -> dict[str, JsonValue]:
    """Validate and detach a state document before persistence."""
    try:
        serialized = json.dumps(dict(state))
    except (TypeError, ValueError) as exc:
        raise ValueError("Failure-guard trigger state must be JSON-safe") from exc
    return cast(dict[str, JsonValue], json.loads(serialized))


@dataclass
class SqlAlchemyFailureGuardStateStore(Generic[RecordT]):
    """Persist one guarded subject's trigger states in a dedicated table."""

    session: AsyncSession
    statement: Select[tuple[RecordT]]
    create_record: Callable[[str, dict[str, JsonValue]], RecordT]
    _records: dict[FailureGuardTriggerKind, RecordT] | None = field(
        default=None,
        init=False,
    )

    async def _load_records(
        self,
    ) -> dict[FailureGuardTriggerKind, RecordT]:
        if self._records is None:
            result = await self.session.scalars(self.statement)
            self._records = {
                FailureGuardTriggerKind(record.trigger_kind): record
                for record in result.all()
            }
        return self._records

    async def load_trigger_states(
        self,
    ) -> dict[FailureGuardTriggerKind, FailureGuardTriggerState]:
        records = await self._load_records()
        return {
            kind: dict(record.trigger_state)
            for kind, record in records.items()
        }

    async def save_trigger_state(
        self,
        trigger_kind: FailureGuardTriggerKind,
        state: FailureGuardTriggerState,
    ) -> None:
        records = await self._load_records()
        document = _json_document(state)
        record = records.get(trigger_kind)
        if record is None:
            record = self.create_record(str(trigger_kind), document)
            self.session.add(record)
            records[trigger_kind] = record
        else:
            record.trigger_state = document

    async def delete_trigger_state(
        self,
        trigger_kind: FailureGuardTriggerKind,
    ) -> None:
        records = await self._load_records()
        record = records.pop(trigger_kind, None)
        if record is not None:
            await self.session.delete(record)
