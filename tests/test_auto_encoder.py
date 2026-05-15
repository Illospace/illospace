"""Tests for brain.systems.memory.encoder raw episode capture."""

from unittest.mock import AsyncMock, patch

from brain.systems.memory.encoder import (
    ExtractedLesson,
    auto_encode_agent_run,
    auto_encode_session,
    extract_lessons,
)


class TestExtractLessons:
    def test_captures_raw_episode_without_semantic_type_detection(self):
        text = "I learned that the database needs indexing for performance."
        lessons = extract_lessons(text, source="session")

        assert lessons == [
            ExtractedLesson(
                content=text,
                lesson_type="raw_episode",
                salience=3.0,
                source="session",
            )
        ]

    def test_does_not_detect_fix_pattern(self):
        text = "The fix is to add a retry loop around the API call."
        lessons = extract_lessons(text, source="session")

        assert len(lessons) == 1
        assert lessons[0].lesson_type == "raw_episode"

    def test_does_not_detect_correction_pattern(self):
        text = "No, actually the endpoint returns JSON not XML format."
        lessons = extract_lessons(text, source="session")

        assert len(lessons) == 1
        assert lessons[0].lesson_type == "raw_episode"

    def test_ignores_short_text(self):
        assert extract_lessons("I learned ok.", source="session") == []

    def test_empty_text(self):
        assert extract_lessons("", source="session") == []


class TestAutoEncode:
    @patch("brain.systems.memory.encoder.add_memory")
    async def test_session_dry_run(self, mock_add):
        results = await auto_encode_session("The fix is to use connection pooling properly.", dry_run=True)

        assert len(results) == 1
        assert results[0]["dry_run"] is True
        assert results[0]["type"] == "raw_episode"
        mock_add.assert_not_called()

    @patch("brain.systems.memory.encoder.add_memory", new_callable=AsyncMock)
    async def test_session_encodes_raw_episode(self, mock_add):
        mock_add.return_value = {"id": 1}
        results = await auto_encode_session("The fix is to use connection pooling properly.")

        assert len(results) == 1
        mock_add.assert_awaited()
        call_kwargs = mock_add.call_args.kwargs
        assert call_kwargs["memory_type"] == "episode"
        assert call_kwargs["tags"] == ["raw_episode"]

    @patch("brain.systems.memory.encoder.add_memory", new_callable=AsyncMock)
    async def test_agent_run_encodes_raw_episode(self, mock_add):
        mock_add.return_value = {"id": 2}
        results = await auto_encode_agent_run("Found that the root cause was a race condition in the worker.")

        assert len(results) == 1
        mock_add.assert_awaited()
        assert mock_add.call_args.kwargs["memory_type"] == "episode"
