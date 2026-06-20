"""Tests for brain.systems.cognition.dag_compaction — DAG compression engine."""

from unittest.mock import patch

import pytest

from brain.systems.cognition.dag_compaction import (
    DECISION_VERBS,
    DEPTH_PROMPTS,
    _parse_compression_result,
    compress_memories,
    deterministic_fallback,
    get_depth_prompt,
    validate_summary,
)


# ---------------------------------------------------------------------------
# Depth prompts
# ---------------------------------------------------------------------------


class TestDepthPrompts:
    def test_depth_0_exists(self):
        prompt = get_depth_prompt(0)
        assert "fresh agent" in prompt
        assert "tomorrow" in prompt

    def test_depth_1_exists(self):
        prompt = get_depth_prompt(1)
        assert "arc" in prompt

    def test_depth_2_exists(self):
        prompt = get_depth_prompt(2)
        assert "cold" in prompt
        assert "weeks" in prompt

    def test_depth_5_falls_back_to_depth_2(self):
        assert get_depth_prompt(5) == get_depth_prompt(2)

    def test_depth_100_falls_back_to_depth_2(self):
        assert get_depth_prompt(100) == DEPTH_PROMPTS[2]


# ---------------------------------------------------------------------------
# Validate summary
# ---------------------------------------------------------------------------


class TestValidateSummary:
    def test_good_summary_passes(self):
        sources = [
            "We tried three approaches. Eventually decided to use pgvector.",
            "The team selected PostgreSQL for its maturity.",
        ]
        summary = "Team decided to use PostgreSQL with pgvector."
        breadcrumbs = [{"topic": "database", "source_ids": [1, 2]}]

        assert validate_summary(summary, sources, breadcrumbs) is True

    def test_bloated_summary_rejected(self):
        """Summary longer than combined sources should fail."""
        sources = ["Short fact."]
        summary = "This is a very long summary that exceeds the original. " * 10
        breadcrumbs = [{"topic": "test", "source_ids": [1]}]

        assert validate_summary(summary, sources, breadcrumbs) is False

    def test_vacuous_summary_rejected(self):
        """Summary without any decision verbs should fail."""
        sources = [
            "We discussed various options and explored alternatives.",
            "The meeting covered many interesting topics.",
        ]
        summary = "Various options were discussed."
        breadcrumbs = [{"topic": "meeting", "source_ids": [1]}]

        assert validate_summary(summary, sources, breadcrumbs) is False

    def test_empty_summary_rejected(self):
        assert validate_summary("", ["source"], []) is False
        assert validate_summary("   ", ["source"], []) is False

    def test_short_breadcrumb_topic_rejected(self):
        sources = ["We decided to use Docker."]
        summary = "Decided to use Docker."
        breadcrumbs = [{"topic": "ab", "source_ids": [1]}]  # too short

        assert validate_summary(summary, sources, breadcrumbs) is False

    def test_empty_breadcrumbs_ok(self):
        """No breadcrumbs is fine — just no topics to validate."""
        sources = ["Long original text about the decision to use pgvector." * 3]
        summary = "Decided to use pgvector."

        assert validate_summary(summary, sources, []) is True


# ---------------------------------------------------------------------------
# Deterministic fallback
# ---------------------------------------------------------------------------


class TestDeterministicFallback:
    def test_produces_shorter_output(self):
        contents = [
            "We decided to use PostgreSQL with pgvector for embeddings.\n"
            "This was a long discussion about various database options.\n"
            "The architecture review confirmed this choice.\n"
            "Some background context about cloud providers.\n"
            "Random filler that doesn't matter much at all.",
            "The team selected FastAPI for the web layer.\n"
            "Various performance benchmarks were discussed.\n"
            "More filler content here.",
        ]

        result = deterministic_fallback(contents, max_tokens=50)

        combined_len = sum(len(c) for c in contents)
        assert len(result) < combined_len
        # Should have at most ~200 chars (50 tokens * 4)
        assert len(result) <= 250

    def test_decision_verbs_preserved(self):
        contents = [
            "Some background information.\n"
            "We decided to use Python 3.12.\n"
            "More filler content.\n"
            "The team chose pytest for testing.",
        ]

        result = deterministic_fallback(contents, max_tokens=100)

        result_lower = result.lower()
        assert "decided" in result_lower or "chose" in result_lower

    def test_empty_contents(self):
        assert deterministic_fallback([]) == ""

    def test_blank_lines_ignored(self):
        contents = ["\n\n\n"]
        assert deterministic_fallback(contents) == ""


# ---------------------------------------------------------------------------
# Parse compression result
# ---------------------------------------------------------------------------


class TestParseCompressionResult:
    def test_extracts_breadcrumbs(self):
        result = (
            "Summary of the work: we decided to use pgvector.\n"
            "Expand for details about: [database choice, pgvector setup, migration plan]"
        )

        content, breadcrumbs = _parse_compression_result(result, [10, 20, 30])

        assert "decided" in content
        assert "Expand for details about" not in content
        assert len(breadcrumbs) == 3
        assert breadcrumbs[0]["topic"] == "database choice"
        assert breadcrumbs[1]["topic"] == "pgvector setup"
        assert breadcrumbs[2]["topic"] == "migration plan"

    def test_source_ids_distributed(self):
        result = "Summary. Expand for details about: [topic A, topic B]"
        _, breadcrumbs = _parse_compression_result(result, [1, 2, 3, 4])

        # Source IDs should be distributed across topics
        all_ids = []
        for bc in breadcrumbs:
            all_ids.extend(bc["source_ids"])
        assert len(all_ids) > 0

    def test_no_breadcrumb_line(self):
        result = "Just a plain summary with no breadcrumb marker."
        content, breadcrumbs = _parse_compression_result(result, [1, 2])

        assert content == "Just a plain summary with no breadcrumb marker."
        assert breadcrumbs == []

    def test_empty_result(self):
        content, breadcrumbs = _parse_compression_result("", [1])
        assert content == ""
        assert breadcrumbs == []


# ---------------------------------------------------------------------------
# Compress memories — escalation
# ---------------------------------------------------------------------------


class TestCompressMemories:
    SOURCES = [
        "We decided to use PostgreSQL with pgvector for memory storage.",
        "The team selected FastAPI for the API layer.",
    ]
    SOURCE_IDS = [1, 2]

    @patch("brain.systems.cognition.dag_compaction._call_model")
    def test_normal_succeeds(self, mock_call):
        mock_call.return_value = (
            "Team decided PostgreSQL + pgvector for storage, selected FastAPI for API.\n"
            "Expand for details about: [database, API framework]"
        )

        result = compress_memories(self.SOURCES, self.SOURCE_IDS, depth=0)

        assert result["level"] == "normal"
        assert result["model_used"] == "openai/gpt-5.5"
        assert "decided" in result["content"].lower()
        assert len(result["breadcrumbs"]) == 2
        # Only one call needed (normal pass succeeded)
        assert mock_call.call_count == 1

    @patch("brain.systems.cognition.dag_compaction._call_model")
    def test_escalation_to_aggressive(self, mock_call):
        """When normal fails validation, escalates to aggressive."""
        # First call returns something too long (fails validation)
        bloated = "x " * 1000  # way longer than sources
        good = (
            "Decided PostgreSQL + pgvector.\n"
            "Expand for details about: [database choice]"
        )
        mock_call.side_effect = [bloated, good]

        result = compress_memories(self.SOURCES, self.SOURCE_IDS, depth=0)

        assert result["level"] == "aggressive"
        assert mock_call.call_count == 2

    @patch("brain.systems.cognition.dag_compaction._call_model")
    def test_escalation_to_deterministic(self, mock_call):
        """When both LLM passes fail, falls back to deterministic."""
        mock_call.return_value = None  # model unavailable

        result = compress_memories(self.SOURCES, self.SOURCE_IDS, depth=1)

        assert result["level"] == "deterministic"
        assert result["model_used"] == "deterministic"
        assert len(result["content"]) > 0
        assert len(result["breadcrumbs"]) == 2  # one per source

    @patch("brain.systems.cognition.dag_compaction._call_model")
    def test_vacuous_normal_and_aggressive_fall_through(self, mock_call):
        """Both LLM outputs lack decision verbs → deterministic."""
        vacuous = "Things happened and stuff was discussed."
        mock_call.return_value = vacuous

        result = compress_memories(self.SOURCES, self.SOURCE_IDS, depth=0)

        assert result["level"] == "deterministic"

    @patch("brain.systems.cognition.dag_compaction._call_model")
    def test_depth_affects_prompt(self, mock_call):
        """Different depths should produce different prompts."""
        mock_call.return_value = (
            "Decided to use pgvector.\n"
            "Expand for details about: [storage]"
        )

        compress_memories(self.SOURCES, self.SOURCE_IDS, depth=0)
        call_0_prompt = mock_call.call_args_list[0][0][0]

        mock_call.reset_mock()
        compress_memories(self.SOURCES, self.SOURCE_IDS, depth=2)
        call_2_prompt = mock_call.call_args_list[0][0][0]

        # Depth 0 prompt has "tomorrow", depth 2 has "weeks"
        assert "tomorrow" in call_0_prompt
        assert "weeks" in call_2_prompt
