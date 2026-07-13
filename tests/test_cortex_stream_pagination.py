"""Unified Cortex stream pagination contract tests."""

from __future__ import annotations

import base64
from collections import namedtuple
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from brain.app.api.routers.cortex import _idea_ops as idea_ops


_MessageRow = namedtuple("_MessageRow", "message user_name user_color")


def _result(rows):
    return SimpleNamespace(all=lambda: rows)


def _message(row_id: int, created_at: datetime, **overrides):
    message = idea_ops.IdeaThread(**{
        "id": row_id,
        "idea_id": "idea-1",
        "created_at": created_at,
        "role": "user",
        "content": f"message-{row_id}",
        "attachments": [],
        "metadata_": {},
        "message_type": "message",
        **overrides,
    })
    return _MessageRow(message, None, None)


def _run(row_id: int, created_at: datetime, **overrides):
    return idea_ops.AgentRun(**{
        "id": row_id,
        "created_at": created_at,
        "updated_at": created_at,
        "thread_id": "idea-1",
        "profile": "fast",
        "recipe": "default",
        "status": "completed",
        "input_message": f"run-{row_id}",
        "metadata_": {},
        "completed_at": created_at,
        **overrides,
    })


def _visual(row_id: int, created_at: datetime):
    return idea_ops.VisualBlock(
        id=row_id,
        created_at=created_at,
        idea_id="idea-1",
        content_type="markdown",
        title=f"visual-{row_id}",
        content=f"content-{row_id}",
        display_mode="inline",
        run_id=None,
        position_after=None,
    )


class _ControlledSession:
    """Small PostgreSQL-query simulator for the three physical stream reads."""

    def __init__(self, messages, runs, visuals, artifacts):
        self.messages = messages
        self.runs = runs
        self.visuals = visuals
        self.artifacts = artifacts
        self.before = None
        self.limit = 0
        self.physical_query_count = 0
        self.hydrated_run_ids: set[int] = set()

    def begin_page(self, before: str | None, limit: int):
        self.before = idea_ops._decode_stream_cursor(before)
        self.limit = limit

    @staticmethod
    def _sql(stmt) -> str:
        return str(stmt.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )).lower()

    def _assert_physical_query(self, kind: str, sql: str):
        table = {
            "message": "idea_threads",
            "run": "agent_runs",
            "visual_block": "visual_blocks",
        }[kind]
        assert "count(" not in sql
        assert f"order by {table}.created_at desc, {table}.id desc" in sql
        assert f"limit {self.limit + 1}" in sql
        if kind == "run":
            assert "headless" in sql
            assert ("active_stream_roots" in sql) is (self.before is None)
            if self.before is None:
                active_root_cte = sql.split("active_stream_roots as", 1)[1]
                active_root_cte = active_root_cte.split(")\n select agent_runs.id", 1)[0]
                assert f"limit {idea_ops._STREAM_ACTIVE_ROOT_LIMIT}" in active_root_cte

        if self.before is not None:
            rank = idea_ops._STREAM_KIND_RANK[kind]
            column = f"{table}.created_at"
            assert str(self.before.created_at) in sql
            if rank < self.before.kind_rank:
                assert f"{column} <=" in sql
            elif rank > self.before.kind_rank:
                assert f"{column} <" in sql
                assert f"{table}.id <" not in sql
            else:
                assert f"{column} <" in sql
                assert f"{table}.id < {self.before.row_id}" in sql

        self.physical_query_count += 1

    def _bounded(self, kind: str, rows: list):
        rows = [
            row
            for row in rows
            if self.before is None or idea_ops._stream_candidate(kind, row).key < self.before
        ]
        rows.sort(key=lambda row: idea_ops._stream_candidate(kind, row).key, reverse=True)
        return rows[: self.limit + 1]

    async def execute(self, stmt):
        sql = self._sql(stmt)
        assert "from idea_threads" in sql
        self._assert_physical_query("message", sql)
        return _result(self._bounded("message", self.messages))

    async def scalars(self, stmt):
        sql = self._sql(stmt)
        if "recent_stream_runs" in sql:
            self._assert_physical_query("run", sql)
            visible = [run for run in self.runs if run.metadata_.get("headless") is not True]
            recent = self._bounded("run", visible)
            if self.before is None:
                active = [
                    run
                    for run in visible
                    if run.parent_run_id is None and run.status in idea_ops.OPEN_RUN_STATUS_VALUES
                ][: idea_ops._STREAM_ACTIVE_ROOT_LIMIT]
                recent = list({run.id: run for run in [*recent, *active]}.values())
            return _result(sorted(recent, key=lambda run: (run.created_at, run.id), reverse=True))
        if "from visual_blocks" in sql:
            self._assert_physical_query("visual_block", sql)
            return _result(self._bounded("visual_block", self.visuals))
        if "from agent_run_artifacts" in sql:
            compiled = stmt.compile(dialect=postgresql.dialect())
            run_ids = {
                int(run_id)
                for value in compiled.params.values()
                if isinstance(value, (list, tuple, set))
                for run_id in value
            }
            self.hydrated_run_ids.update(run_ids)
            return _result([artifact for artifact in self.artifacts if artifact.run_id in run_ids])
        if "agent_run_events" in sql or "parent_run_id in" in sql:
            return _result([])
        raise AssertionError(f"Unexpected stream query: {sql}")


@asynccontextmanager
async def _unit_of_work(session):
    yield SimpleNamespace(session=session)


@pytest.mark.asyncio
async def test_unified_stream_paginates_the_full_mixed_history(monkeypatch):
    base = datetime(2026, 7, 10, 12, tzinfo=timezone.utc)

    def at(minutes, seconds=0):
        return base + timedelta(minutes=minutes, seconds=seconds)

    active = _run(99, base, status="running", completed_at=None)
    persisted_answer = _message(
        3,
        at(3),
        role="illo",
        content="Persisted answer",
        metadata_={"run_id": 10},
    )
    session = _ControlledSession(
        messages=[_message(1, at(1)), _message(2, at(2)), persisted_answer],
        runs=[
            active,
            _run(10, at(2)),
            _run(98, at(4, 30), metadata_={"headless": True}),
            _run(11, at(5)),
        ],
        visuals=[_visual(3, at(2)), _visual(4, at(4))],
        artifacts=[idea_ops.AgentRunArtifactRow(
            id=501,
            run_id=10,
            artifact_type="final_answer",
            title=None,
            payload={},
            text="Synthetic answer",
            uri=None,
            visibility="default",
            created_at=at(2, 30),
        )],
    )
    monkeypatch.setattr(idea_ops, "UnitOfWork", lambda: _unit_of_work(session))
    monkeypatch.setattr(idea_ops, "_require_idea_for_user", AsyncMock())

    pages = []
    before = None
    while True:
        session.begin_page(before, limit=1)
        page = await idea_ops.unified_stream_payload(
            "idea-1",
            limit=1,
            before=before,
            user={"id": "user-1"},
        )
        assert set(page) == {"idea_id", "items", "has_more", "next_before"}
        pages.append(page)
        if not page["has_more"]:
            assert page["next_before"] is None
            break
        before = page["next_before"]

    assert len(pages) == 8
    assert idea_ops._decode_stream_cursor(pages[0]["next_before"]).row_id == 11
    assert len(pages[0]["items"]) == 2  # One physical item plus the out-of-band active root.
    assert [item["id"] for item in pages[0]["items"] if item["type"] == "run"] == ["99", "11"]
    assert [
        index for index, page in enumerate(pages)
        if any(item["id"] == "99" for item in page["items"])
    ] == [0, 7]
    assert all(item.get("id") != "98" for page in pages for item in page["items"])
    assert any(
        item.get("id") == "vb-3" and item.get("position_after") is None
        for page in pages for item in page["items"]
    )
    assert any(
        item.get("metadata", {}).get("synthetic_from_run_artifact") is True
        for item in pages[4]["items"]
    )
    items = [item for page in reversed(pages) for item in page["items"]]
    raw_physical_ids = [
        f"{item['type']}:{item['id']}"
        for item in items
        if item.get("metadata", {}).get("synthetic_from_run_artifact") is not True
    ]
    assert raw_physical_ids.count("run:99") == 2
    assert list(dict.fromkeys(raw_physical_ids)) == [
        "run:99",
        "message:1",
        "message:2",
        "run:10",
        "visual_block:vb-3",
        "message:3",
        "visual_block:vb-4",
        "run:11",
    ]
    assert session.hydrated_run_ids == {10, 11, 99}
    assert session.physical_query_count == len(pages) * 3


@pytest.mark.parametrize(
    "cursor",
    [
        "not-a-cursor",
        base64.urlsafe_b64encode(b"v2|2026-01-01T00:00:00+00:00|0|1").decode(),
        base64.urlsafe_b64encode(b"v1|2026-01-01T00:00:00|0|1").decode(),
        base64.urlsafe_b64encode(b"v1|2026-01-01T00:00:00+00:00|00|1").decode(),
    ],
)
def test_stream_cursor_rejects_malformed_or_unsupported_values(cursor):
    with pytest.raises(HTTPException) as exc_info:
        idea_ops._decode_stream_cursor(cursor)

    assert exc_info.value.status_code == 400
