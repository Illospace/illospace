"""Tests for Cortex-to-Brain encoding on thought archive."""
import json
import pytest
from unittest.mock import AsyncMock, patch, MagicMock


def _make_idea_row(title="Test idea", display_title=None, agent_details=None):
    row = {
        "id": "abc12345-0000-0000-0000-000000000000",
        "title": title,
        "display_title": display_title,
        "agent_details": agent_details or [],
        "encoded_at": None,
        "user_id": "user-1",
        "org_id": "org-1",
    }
    return row


def _make_thread_rows(roles=None):
    if roles is None:
        return []
    return [{"role": r, "content": f"Message from {r}"} for r in roles]


def _make_uow(idea_row, thread_rows):
    """Build a mock UnitOfWork for encode_thought_to_brain.

    The function does two execute() calls inside the first UnitOfWork context:
    1. SELECT ... FROM ideas -> mappings().first() -> idea_row
    2. SELECT ... FROM idea_threads -> mappings().all() -> thread_rows

    There may be a second UnitOfWork context for the encoded_at update,
    so we return a factory that produces fresh mocks.
    """
    mock_session = MagicMock()

    # First execute: idea lookup
    idea_exec = MagicMock()
    idea_exec.mappings.return_value.first.return_value = idea_row

    # Second execute: thread lookup
    thread_exec = MagicMock()
    thread_exec.mappings.return_value.all.return_value = thread_rows

    mock_session.execute = AsyncMock(side_effect=[idea_exec, thread_exec])

    mock_uow = MagicMock()
    mock_uow.__aenter__ = AsyncMock(return_value=mock_uow)
    mock_uow.__aexit__ = AsyncMock(return_value=False)
    mock_uow.session = mock_session
    return mock_uow


def _make_uow_factory(idea_row, thread_rows):
    """Return a callable that produces UnitOfWork mocks.

    First call returns the main UoW (with idea + thread queries).
    Subsequent calls return a simple UoW for the encoded_at update.
    """
    main_uow = _make_uow(idea_row, thread_rows)
    calls = [0]

    def factory():
        calls[0] += 1
        if calls[0] == 1:
            return main_uow
        # Subsequent UoW contexts (encoded_at update)
        update_uow = MagicMock()
        update_uow.__aenter__ = AsyncMock(return_value=update_uow)
        update_uow.__aexit__ = AsyncMock(return_value=False)
        update_uow.session.execute = AsyncMock()
        return update_uow

    return factory


def _make_gpu_client(content: str):
    """Return a mock gpu_client whose generate() returns content."""
    client = MagicMock()
    client.generate.return_value = content
    return client


@patch("brain.platform.gpu_client.get_client")
@patch("brain.app.cli.memory.add_memory", new_callable=AsyncMock)
async def test_encode_with_replies_high_salience(mock_add_mem, mock_get_client):
    """Thought with replies should encode with higher salience."""
    from brain.systems.cortex.encode import encode_thought_to_brain as _encode_thought_to_brain

    idea_row = _make_idea_row(display_title="Important Discussion")
    thread_rows = _make_thread_rows(["user", "user", "assistant"])

    mock_get_client.return_value = _make_gpu_client(
        "1. The team decided to use PostgreSQL for storage\n2. Type: decision\n3. Salience: 7"
    )
    mock_add_mem.return_value = {"id": 1}

    with patch("brain.systems.cortex.encode.UnitOfWork", side_effect=_make_uow_factory(idea_row, thread_rows)):
        await _encode_thought_to_brain("abc12345-0000-0000-0000-000000000000")

    mock_add_mem.assert_called_once()
    call_kwargs = mock_add_mem.call_args[1]
    assert call_kwargs["memory_type"] == "decision"
    assert call_kwargs["salience"] >= 5.0
    assert call_kwargs["source"] == "cortex"
    assert "cortex" in call_kwargs["tags"]
    assert "[Cortex: Important Discussion]" in call_kwargs["content"]


@patch("brain.platform.gpu_client.get_client")
@patch("brain.app.cli.memory.add_memory", new_callable=AsyncMock)
async def test_encode_with_agent_work(mock_add_mem, mock_get_client):
    """Thought with agent work should encode as task_completed."""
    from brain.systems.cortex.encode import encode_thought_to_brain as _encode_thought_to_brain

    idea_row = _make_idea_row(
        title="Fix login bug",
        agent_details=[{"agent": "coder", "status": "completed"}],
    )
    thread_rows = _make_thread_rows(["user", "assistant"])

    mock_get_client.return_value = _make_gpu_client(
        "1. Fixed the login redirect bug by updating OAuth callback\n2. Type: task_completed\n3. Salience: 8"
    )
    mock_add_mem.return_value = {"id": 2}

    with patch("brain.systems.cortex.encode.UnitOfWork", side_effect=_make_uow_factory(idea_row, thread_rows)):
        await _encode_thought_to_brain("abc12345-0000-0000-0000-000000000000")

    mock_add_mem.assert_called_once()
    call_kwargs = mock_add_mem.call_args[1]
    assert call_kwargs["memory_type"] == "task_completed"
    assert call_kwargs["salience"] >= 6.0


@patch("brain.platform.gpu_client.get_client")
async def test_encode_projects_scoped_legacy_run_failures_before_memory_extraction(mock_get_client):
    from brain.systems.cortex.encode import encode_thought_to_brain
    from brain.systems.runs.failures import UPSTREAM_FAILED_RUN_MESSAGE

    idea_id = "abc12345-0000-0000-0000-000000000000"
    raw_diagnostic = "legacy provider failure secret=request-token"
    idea_row = _make_idea_row(title="Mixed legacy thread")
    thread_rows = [
        {
            "role": "user",
            "content": "User-authored context stays unchanged.",
            "metadata": {"run_id": 999},
            "message_type": "message",
        },
        {
            "role": "illo",
            "content": raw_diagnostic,
            "metadata": {"run_id": 7},
            "message_type": "agent_response",
        },
        {
            "role": "assistant",
            "content": "Completed assistant answer stays unchanged.",
            "metadata": {"created_by_run_id": 8},
            "message_type": "agent_response",
        },
    ]
    client = _make_gpu_client("SKIP")
    mock_get_client.return_value = client
    failure_lookup = AsyncMock(return_value={
        7: {
            "status": "failed",
            "category": "upstream",
            "message": UPSTREAM_FAILED_RUN_MESSAGE,
        }
    })

    with (
        patch("brain.systems.cortex.encode.UnitOfWork", side_effect=_make_uow_factory(idea_row, thread_rows)),
        patch(
            "brain.systems.cortex.encode.public_failures_for_run_ids",
            failure_lookup,
            create=True,
        ),
    ):
        await encode_thought_to_brain(idea_id)

    failure_lookup.assert_awaited_once()
    assert failure_lookup.await_args.args[1] == {7, 8}
    assert failure_lookup.await_args.kwargs == {
        "thread_id": idea_id,
        "org_id": "org-1",
    }
    prompt = client.generate.call_args.kwargs["prompt"]
    assert UPSTREAM_FAILED_RUN_MESSAGE in prompt
    assert raw_diagnostic not in prompt
    assert "User-authored context stays unchanged." in prompt
    assert "Completed assistant answer stays unchanged." in prompt


@patch("brain.platform.gpu_client.get_client")
@patch("brain.app.cli.memory.add_memory", new_callable=AsyncMock)
async def test_encode_no_interaction_low_salience(mock_add_mem, mock_get_client):
    """Thought with no interaction should encode with low salience."""
    from brain.systems.cortex.encode import encode_thought_to_brain as _encode_thought_to_brain

    idea_row = _make_idea_row(title="Random thought")
    thread_rows = []

    mock_get_client.return_value = _make_gpu_client(
        "1. User noted a random thought about architecture\n2. Type: lesson\n3. Salience: 3"
    )
    mock_add_mem.return_value = {"id": 3}

    with patch("brain.systems.cortex.encode.UnitOfWork", side_effect=_make_uow_factory(idea_row, thread_rows)):
        await _encode_thought_to_brain("abc12345-0000-0000-0000-000000000000")

    mock_add_mem.assert_called_once()
    call_kwargs = mock_add_mem.call_args[1]
    assert call_kwargs["salience"] <= 5.0


@patch("brain.platform.gpu_client.get_client")
@patch("brain.app.cli.memory.add_memory", new_callable=AsyncMock)
async def test_encode_skip_no_memory(mock_add_mem, mock_get_client):
    """When Qwen returns SKIP, no memory should be created."""
    from brain.systems.cortex.encode import encode_thought_to_brain as _encode_thought_to_brain

    idea_row = _make_idea_row(title="Empty thought")
    thread_rows = []

    mock_get_client.return_value = _make_gpu_client("SKIP")

    with patch("brain.systems.cortex.encode.UnitOfWork", side_effect=_make_uow_factory(idea_row, thread_rows)):
        await _encode_thought_to_brain("abc12345-0000-0000-0000-000000000000")

    mock_add_mem.assert_not_called()


@patch("brain.platform.gpu_client.get_client")
async def test_gpu_client_uses_correct_params(mock_get_client):
    """Verify gpu_client.generate is called with think=False, temp 0.3, max_tokens=100."""
    from brain.systems.cortex.encode import encode_thought_to_brain as _encode_thought_to_brain

    idea_row = _make_idea_row()
    thread_rows = []

    client = _make_gpu_client("SKIP")
    mock_get_client.return_value = client

    with patch("brain.systems.cortex.encode.UnitOfWork", side_effect=_make_uow_factory(idea_row, thread_rows)):
        await _encode_thought_to_brain("abc12345-0000-0000-0000-000000000000")

    client.generate.assert_called_once()
    call_kwargs = client.generate.call_args[1]
    assert call_kwargs["think"] is False
    assert call_kwargs["temperature"] == 0.3
    assert call_kwargs["max_tokens"] == 100
