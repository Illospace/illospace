"""Tests for Cortex dynamic visual reply plumbing."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from brain.systems.runs.tool_definitions import (
    COORDINATOR_TOOLS,
    CORTEX_VISUAL_REPLY_TOOL,
    WORKER_TOOLS,
)


def _tool_names(tools):
    return {tool["name"] for tool in tools}


def test_cortex_visual_reply_tool_available_to_workers_and_coordinators():
    """Both direct agents and spawned workers should be able to render visual blocks."""

    assert "cortex_visual_reply" in _tool_names(COORDINATOR_TOOLS)
    assert "cortex_visual_reply" in _tool_names(WORKER_TOOLS)


def test_cortex_visual_reply_schema_preserves_supported_content_types():
    schema = CORTEX_VISUAL_REPLY_TOOL["input_schema"]
    content_types = schema["properties"]["content_type"]["enum"]

    assert content_types == ["diff", "chart", "diagram", "image", "markdown", "screenshot"]
    assert schema["properties"]["display"]["enum"] == ["inline", "canvas"]
    assert schema["required"] == ["content_type", "title", "content"]


async def test_cortex_visual_reply_persists_and_broadcasts_visual_block(monkeypatch):
    import sys
    import brain.platform.db.models.idea as idea_models
    import brain.systems.runs.tool_catalog.handlers.cortex_reply as cortex_reply
    from brain.systems.runs.execution_context import bind_agent_context

    now = datetime(2026, 4, 27, 12, 0, tzinfo=timezone.utc)
    added_blocks = []
    published = []

    class FakeVisualBlock:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)
            self.id = None
            self.created_at = None

    class FakeSession:
        async def execute(self, *_args, **_kwargs):
            return SimpleNamespace(scalar=lambda: 42)

        def add(self, block):
            block.id = 7
            block.created_at = now
            added_blocks.append(block)

        async def flush(self):
            pass

    class FakeUnitOfWork:
        def __init__(self):
            self.session = FakeSession()

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    run = SimpleNamespace(run_id=123)

    fake_events = SimpleNamespace(publish_safe=lambda event, payload: published.append((event, payload)))
    fake_uow_mod = SimpleNamespace(UnitOfWork=FakeUnitOfWork, open_unit_of_work=lambda factory: factory())
    monkeypatch.setitem(sys.modules, "brain.systems.cortex.events", fake_events)
    monkeypatch.setitem(sys.modules, "brain.platform.db.repositories.unit_of_work", fake_uow_mod)
    monkeypatch.setattr(idea_models, "VisualBlock", FakeVisualBlock)

    with bind_agent_context(idea_id="idea-1", run=run):
        result = await cortex_reply._handle_cortex_visual_reply(
            content_type="chart",
            title="Build health",
            content='{"type":"bar","data":[{"label":"passed","value":3}]}',
            display="canvas",
        )

    assert result == {
        "posted": True,
        "block_id": 7,
        "content_type": "chart",
        "display_mode": "canvas",
    }
    assert len(added_blocks) == 1
    block = added_blocks[0]
    assert block.idea_id == "idea-1"
    assert block.content_type == "chart"
    assert block.title == "Build health"
    assert block.display_mode == "canvas"
    assert block.position_after == 42
    assert block.run_id == 123

    assert published == [
        (
            "visual_reply",
            {
                "idea_id": "idea-1",
                "block": {
                    "id": 7,
                    "idea_id": "idea-1",
                    "run_id": 123,
                    "content_type": "chart",
                    "title": "Build health",
                    "content": '{"type":"bar","data":[{"label":"passed","value":3}]}',
                    "display_mode": "canvas",
                    "position_after": 42,
                    "created_at": now.isoformat(),
                },
            },
        )
    ]


@pytest.mark.asyncio
async def test_cortex_stream_includes_persisted_visual_block(monkeypatch):
    import brain.app.api.routers.cortex._idea_ops as idea_ops

    created = datetime(2026, 4, 27, 12, 30, tzinfo=timezone.utc)
    visual_block = SimpleNamespace(
        id=5,
        idea_id="idea-1",
        content_type="markdown",
        title="Summary",
        content="**Done**",
        display_mode="inline",
        run_id="99",
        position_after=12,
        created_at=created,
    )

    class FakeExecuteResult:
        def all(self):
            return []

    class FakeScalarResult:
        def __init__(self, values):
            self._values = values

        def all(self):
            return self._values

    class FakeSession:
        def __init__(self):
            self._scalar_calls = 0

        async def execute(self, *_args, **_kwargs):
            return FakeExecuteResult()

        async def scalars(self, *_args, **_kwargs):
            self._scalar_calls += 1
            values = [visual_block] if self._scalar_calls == 2 else []
            return FakeScalarResult(values)

    class FakeUnitOfWork:
        def __init__(self):
            self.session = FakeSession()

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(idea_ops, "UnitOfWork", FakeUnitOfWork)

    async def fake_require_idea(*_args, **_kwargs):
        return None

    monkeypatch.setattr(idea_ops, "_require_idea_for_user", fake_require_idea)

    page = await idea_ops.idea_unified_stream("idea-1", user={"id": "user-1"})

    assert page == {
        "idea_id": "idea-1",
        "items": [
            {
                "type": "visual_block",
                "timestamp": created.isoformat(),
                "id": "vb-5",
                "content_type": "markdown",
                "title": "Summary",
                "content": "**Done**",
                "display_mode": "inline",
                "run_id": "99",
                "position_after": 12,
            }
        ],
        "has_more": False,
        "next_before": None,
    }
