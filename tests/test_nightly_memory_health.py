"""Tests for the scheduled reconstructive-memory health inventory."""
from __future__ import annotations

import json
import sqlite3
from datetime import date

from sqlalchemy import func, select, text

from brain.jobs.pipelines.nightly_memory_health import (
    CHECK_TYPE,
    record_nightly_memory_health,
)
from brain.platform.db.models.memory_health import MemoryHealthLog


async def test_record_nightly_memory_health_persists_inventory_row(
    async_sqlite_session_factory,
):
    sqlite3.register_adapter(dict, json.dumps)
    session = await async_sqlite_session_factory([])
    await session.execute(
        text(
            """
            CREATE TABLE memory_nodes (
                id INTEGER PRIMARY KEY,
                org_id TEXT,
                archived_at DATETIME
            )
            """
        )
    )
    await session.execute(
        text(
            """
            CREATE TABLE memory_health_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                check_type VARCHAR(50) NOT NULL,
                status VARCHAR(20) NOT NULL,
                details JSON,
                org_id TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    )
    await session.execute(
        text(
            """
            INSERT INTO memory_nodes (id, org_id, archived_at)
            VALUES (1, NULL, NULL), (2, NULL, NULL), (3, NULL, CURRENT_TIMESTAMP)
            """
        )
    )

    result = await record_nightly_memory_health(session, date(2026, 7, 23))
    count = await session.scalar(select(func.count()).select_from(MemoryHealthLog))
    row = await session.scalar(select(MemoryHealthLog))

    assert result == {
        "id": 1,
        "check_type": CHECK_TYPE,
        "status": "ok",
        "details": {
            "target_date": "2026-07-23",
            "active_memory_nodes": 2,
            "memory_system": "reconstructive",
        },
    }
    assert count == 1
    assert row is not None
    assert row.details["active_memory_nodes"] == 2
