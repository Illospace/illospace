"""Tests for pipelines/nightly_dream.py"""
import json
import os
import sys
from datetime import date
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 1))))
from brain.jobs.pipelines.nightly_dream import (
    build_dream_prompt,
    gather_random_old_memories,
    store_dream_memories,
)


@pytest.mark.requires_db
async def test_random_old_memories_accepts_date_bound_parameter(
    unit_of_work_for_session,
    monkeypatch,
):
    """The dream query must resolve against real PostgreSQL with a `date` bind.

    The parameter is sent untyped, so Postgres infers its type from the
    expression it appears in. Before the fix the query read
    `created_at::date < :target_date - INTERVAL '7 days'`, which inferred
    `:target_date` as `interval` and failed with
    `operator does not exist: date < interval` — every night for 23 days.

    `:target_date - 7` fails the same way one type over
    (`date < integer`), so only an explicit `CAST(... AS date)` is correct.
    SQLite accepts all three spellings, which is why this test must run
    against PostgreSQL to mean anything.
    """
    monkeypatch.setattr(
        "brain.jobs.pipelines.nightly_dream.UnitOfWork",
        unit_of_work_for_session,
    )

    memories = await gather_random_old_memories(date(2026, 8, 10))

    assert isinstance(memories, list)


class TestBuildDreamPrompt:
    def test_prompt_includes_memories(self):
        today = [{"memory_type": "lesson", "salience": 8, "content": "Test memory", "id": 1}]
        old = [{"memory_type": "episode", "created_date": "2026-01-01", "content": "Old memory", "id": 2}]
        prompt = build_dream_prompt(today, old, date(2026, 3, 4))
        assert "Test memory" in prompt
        assert "Old memory" in prompt
        assert "2026-03-04" in prompt
        assert "connections" in prompt

    def test_prompt_under_token_limit(self):
        # With reasonable inputs, prompt should be compact
        today = [{"memory_type": "lesson", "salience": 5, "content": "x" * 150, "id": i} for i in range(10)]
        old = [{"memory_type": "episode", "created_date": "2026-01-01", "content": "y" * 150, "id": i} for i in range(10, 20)]
        prompt = build_dream_prompt(today, old, date(2026, 3, 4))
        # Rough estimate: 500 tokens ~ 2000 chars for prompt template
        assert len(prompt) < 10000


class TestStoreDreamMemories:
    async def test_dry_run_no_db(self):
        dream = {
            "connections": [
                {"insight": "A connects to B", "why_it_matters": "synergy"}
            ],
            "counterfactual": {"scenario": "what if X", "potential_outcome": "Y"}
        }
        stored = await store_dream_memories(dream, date(2026, 3, 4), dry_run=True)
        assert stored == 2  # 1 connection + 1 counterfactual

    async def test_empty_dream(self):
        stored = await store_dream_memories({}, date(2026, 3, 4), dry_run=True)
        assert stored == 0
