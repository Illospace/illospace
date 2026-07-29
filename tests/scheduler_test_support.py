"""Shared SQLite setup and model factories for scheduler tests."""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.sqlite.base import SQLiteDDLCompiler, SQLiteTypeCompiler

from brain.platform.db.models.scheduler import (
    SchedulerFailureGuardLatch,
    SchedulerFailureGuardTriggerState,
    SchedulerJob,
    SchedulerLease,
    SchedulerRun,
    SchedulerRunStep,
)


def _patch_sqlite_for_pg_types() -> None:
    if not hasattr(SQLiteTypeCompiler, "visit_JSONB"):
        SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "TEXT"
    if not hasattr(SQLiteTypeCompiler, "visit_ARRAY"):
        SQLiteTypeCompiler.visit_ARRAY = lambda self, type_, **kw: "TEXT"

    original = SQLiteDDLCompiler.get_column_default_string
    if getattr(original, "_scheduler_test_patch", False):
        return

    def patched(self, column, **kw):
        result = original(self, column, **kw)
        if result:
            result = re.sub(r"::jsonb", "", result)
            result = re.sub(r"::text\[\]", "", result)
        return result

    patched._scheduler_test_patch = True
    SQLiteDDLCompiler.get_column_default_string = patched


def _register_sqlite_functions(dbapi_conn, connection_record) -> None:
    del connection_record
    dbapi_conn.create_function("NOW", 0, lambda: datetime.utcnow().isoformat())
    dbapi_conn.create_function("gen_random_uuid", 0, lambda: str(uuid.uuid4()))


def _register_sqlite_adapters() -> None:
    import sqlite3

    sqlite3.register_adapter(list, lambda value: json.dumps(value))
    sqlite3.register_adapter(dict, lambda value: json.dumps(value))


async def make_scheduler_test_session(async_sqlite_session_factory):
    _patch_sqlite_for_pg_types()
    _register_sqlite_adapters()
    return await async_sqlite_session_factory(
        [
            SchedulerJob.__table__,
            SchedulerFailureGuardLatch.__table__,
            SchedulerFailureGuardTriggerState.__table__,
            SchedulerRun.__table__,
            SchedulerLease.__table__,
            SchedulerRunStep.__table__,
        ],
        connect_listener=_register_sqlite_functions,
    )


def make_scheduler_job(**overrides) -> SchedulerJob:
    defaults = {
        "job_key": "nightly_sleep",
        "family": "nightly_sleep",
        "program_key": "nightly_sleep",
        "handler_kind": "scheduler_builtin",
        "handler_ref": "brain.app.scheduler.programs:nightly_sleep",
        "cron_expr": "0 3 * * *",
        "timezone": "UTC",
        "enabled": True,
        "owner_mode": "scheduler",
        "priority": 100,
        "max_concurrency": 1,
        "default_payload": {"name": "Nightly Sleep"},
        "task_contract": {
            "owner_user_id": "user-1",
            "org_id": "org-1",
            "memory_scope": {"visibility": "private", "user_id": "user-1"},
            "allowed_actions": ["scheduler.run"],
            "success_criteria": ["Nightly work completes"],
        },
        "next_run_at": datetime(2026, 4, 21, 3, 0, tzinfo=timezone.utc),
    }
    defaults.update(overrides)
    return SchedulerJob(**defaults)


def guard_trigger(guard: dict[str, Any], kind: str) -> dict[str, Any]:
    triggers = guard["triggers"]
    assert isinstance(triggers, list)
    return next(trigger for trigger in triggers if trigger["kind"] == kind)


async def guard_latches(
    session,
    job: SchedulerJob,
) -> dict[str, SchedulerFailureGuardLatch]:
    result = await session.scalars(
        select(SchedulerFailureGuardLatch).where(
            SchedulerFailureGuardLatch.job_id == job.id
        )
    )
    return {latch.trigger_kind: latch for latch in result.all()}


async def guard_trigger_states(
    session,
    job: SchedulerJob,
) -> dict[str, SchedulerFailureGuardTriggerState]:
    result = await session.scalars(
        select(SchedulerFailureGuardTriggerState).where(
            SchedulerFailureGuardTriggerState.job_id == job.id
        )
    )
    return {state.trigger_kind: state for state in result.all()}
