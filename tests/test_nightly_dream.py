"""Tests for pipelines/nightly_dream.py"""
import json
import os
import sys
from datetime import date
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 1))))
from brain.jobs.pipelines.nightly_dream import build_dream_prompt, store_dream_memories


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
    def test_dry_run_no_db(self):
        dream = {
            "connections": [
                {"insight": "A connects to B", "why_it_matters": "synergy"}
            ],
            "counterfactual": {"scenario": "what if X", "potential_outcome": "Y"}
        }
        stored = store_dream_memories(dream, date(2026, 3, 4), dry_run=True)
        assert stored == 2  # 1 connection + 1 counterfactual

    def test_empty_dream(self):
        stored = store_dream_memories({}, date(2026, 3, 4), dry_run=True)
        assert stored == 0
