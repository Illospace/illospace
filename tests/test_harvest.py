"""Tests for brain.systems.memory.harvest - LLM-first memory extraction contract."""

import json
from unittest.mock import MagicMock, patch

from brain.systems.memory.harvest import (
    HarvestItem,
    RAW_EPISODE_CONFIDENCE,
    _fallback_raw_episode,
    _parse_response,
    _response_format,
    extract_harvest_items,
)


SAMPLE_MESSAGES = [
    {"role": "user", "content": "Use python3, not python on this machine"},
    {"role": "assistant", "content": "I'll use python3 going forward."},
    {"role": "user", "content": "We decided to use PostgreSQL with pgvector for memory storage."},
    {"role": "assistant", "content": "PostgreSQL with pgvector it is."},
]

VALID_MODEL_RESPONSE = json.dumps({
    "schema_version": 1,
    "memories": [
        {
            "content": "User prefers python3 over python on this machine",
            "kind": "preference",
            "confidence": 0.9,
            "sensitivity": "low",
            "scope": "personal",
            "expiry": None,
            "evidence": [{"message_index": 0, "role": "user", "quote": "Use python3, not python"}],
            "topic_tags": ["tooling", "python"],
        },
        {
            "content": "Team decided to use PostgreSQL with pgvector for memory storage",
            "kind": "decision",
            "confidence": 0.95,
            "sensitivity": "medium",
            "scope": "org",
            "expiry": None,
            "evidence": [{"message_index": 2, "role": "user", "quote": "We decided to use PostgreSQL with pgvector"}],
            "topic_tags": ["database", "architecture"],
        },
    ],
})


class TestExtractHarvestItems:
    @patch("brain.systems.memory.harvest._call_ollama")
    def test_ollama_extraction_uses_strict_schema(self, mock_ollama):
        mock_ollama.return_value = VALID_MODEL_RESPONSE

        items = extract_harvest_items(SAMPLE_MESSAGES, model="ollama:qwen3.5:4b")

        assert len(items) == 2
        assert items[0].content == "User prefers python3 over python on this machine"
        assert items[0].harvest_type == "preference"
        assert items[0].memory_type == "preference"
        assert items[0].confidence == 0.9
        assert items[0].scope == "personal"
        assert "python" in items[0].topic_tags
        assert items[1].harvest_type == "decision"
        assert items[1].visibility_for("org-1") == "org"
        mock_ollama.assert_called_once()

    @patch("brain.systems.memory.harvest.get_provider")
    @patch("brain.systems.memory.harvest.resolve_llm_client")
    def test_openai_extraction_uses_provider_neutral_response_format(self, mock_resolve, mock_get_provider):
        llm = MagicMock()
        llm.provider = "openai"
        llm.client = object()
        llm.build_request_headers.return_value = {"X-Test": "1"}
        mock_resolve.return_value = llm

        provider = MagicMock()
        provider.create.return_value = MagicMock(content=[MagicMock(type="text", text=VALID_MODEL_RESPONSE)])
        mock_get_provider.return_value = provider

        items = extract_harvest_items(SAMPLE_MESSAGES, model="openai/gpt-5.4", user_id="user-1", org_id="org-1")

        assert len(items) == 2
        request = provider.create.call_args.args[0]
        assert request.operation_type == "memory_extraction"
        assert request.response_format["type"] == "json_schema"
        assert request.response_format["strict"] is True
        assert request.extra_headers == {"X-Test": "1"}

    def test_empty_messages_returns_empty(self):
        assert extract_harvest_items([]) == []

    @patch("brain.systems.memory.harvest._call_ollama")
    def test_provider_unavailable_returns_low_confidence_raw_episode_only(self, mock_ollama):
        mock_ollama.return_value = None
        messages = [
            {"role": "user", "content": "I learned that connection pooling is essential for performance."},
            {"role": "assistant", "content": "Noted."},
        ]

        items = extract_harvest_items(messages, model="ollama:qwen3.5:4b")

        assert len(items) == 1
        assert items[0].raw_episode is True
        assert items[0].harvest_type == "raw_episode"
        assert items[0].memory_type == "episode"
        assert items[0].confidence == RAW_EPISODE_CONFIDENCE
        assert "connection pooling" in items[0].content

    @patch("brain.systems.memory.harvest._call_ollama")
    def test_invalid_llm_output_returns_low_confidence_raw_episode_only(self, mock_ollama):
        mock_ollama.return_value = "not json"

        items = extract_harvest_items(SAMPLE_MESSAGES, model="ollama:qwen3.5:4b")

        assert len(items) == 1
        assert items[0].raw_episode is True
        assert items[0].harvest_type == "raw_episode"

    @patch("brain.systems.memory.harvest._call_ollama")
    def test_valid_empty_extraction_does_not_create_raw_episode(self, mock_ollama):
        mock_ollama.return_value = json.dumps({"schema_version": 1, "memories": []})

        assert extract_harvest_items(SAMPLE_MESSAGES, model="ollama:qwen3.5:4b") == []


class TestParseResponse:
    def test_plain_json(self):
        items = _parse_response(VALID_MODEL_RESPONSE)
        assert len(items) == 2

    def test_markdown_code_fence(self):
        wrapped = f"```json\n{VALID_MODEL_RESPONSE}\n```"
        items = _parse_response(wrapped)
        assert len(items) == 2
        assert items[0].harvest_type == "preference"

    def test_legacy_array_response_is_invalid(self):
        legacy = json.dumps([
            {"content": "User prefers python3", "harvest_type": "preference", "confidence": 0.9},
        ])
        assert _parse_response(legacy) == []

    def test_invalid_harvest_type_is_rejected_not_reclassified(self):
        data = json.dumps({
            "schema_version": 1,
            "memories": [{
                "content": "something useful enough",
                "kind": "BOGUS",
                "confidence": 0.5,
                "sensitivity": "low",
                "scope": "personal",
                "expiry": None,
                "evidence": [{"message_index": 0, "role": "user", "quote": "something"}],
                "topic_tags": [],
            }],
        })
        assert _parse_response(data) == []

    def test_confidence_must_be_in_schema_bounds(self):
        data = json.dumps({
            "schema_version": 1,
            "memories": [{
                "content": "confidence cannot be over one",
                "kind": "lesson",
                "confidence": 5.0,
                "sensitivity": "low",
                "scope": "personal",
                "expiry": None,
                "evidence": [{"message_index": 0, "role": "user", "quote": "over"}],
                "topic_tags": [],
            }],
        })
        assert _parse_response(data) == []

    def test_pii_policy_narrows_visibility(self):
        data = json.dumps({
            "schema_version": 1,
            "memories": [{
                "content": "Alex's contact email is alex@example.com",
                "kind": "fact",
                "confidence": 0.8,
                "sensitivity": "low",
                "scope": "org",
                "expiry": None,
                "evidence": [{"message_index": 0, "role": "user", "quote": "alex@example.com"}],
                "topic_tags": ["contact"],
            }],
        })

        item = _parse_response(data)[0]

        assert item.sensitivity == "high"
        assert item.visibility_for("org-1") == "private"

    def test_secret_policy_redacts_content_and_evidence(self):
        data = json.dumps({
            "schema_version": 1,
            "memories": [{
                "content": "The deploy token is sk-abc123456789abcdef",
                "kind": "fact",
                "confidence": 0.8,
                "sensitivity": "low",
                "scope": "personal",
                "expiry": None,
                "evidence": [{"message_index": 0, "role": "user", "quote": "sk-abc123456789abcdef"}],
                "topic_tags": ["deploy"],
            }],
        })

        item = _parse_response(data)[0]

        assert "sk-" not in item.content
        assert item.evidence[0]["quote"] == "[REDACTED_SECRET]"
        assert item.sensitivity == "restricted"

    def test_expiry_must_be_iso8601(self):
        data = json.dumps({
            "schema_version": 1,
            "memories": [{
                "content": "Temporary staging password expires next Friday",
                "kind": "fact",
                "confidence": 0.8,
                "sensitivity": "restricted",
                "scope": "personal",
                "expiry": "next Friday",
                "evidence": [{"message_index": 0, "role": "user", "quote": "expires next Friday"}],
                "topic_tags": [],
            }],
        })
        assert _parse_response(data) == []

    def test_response_format_is_strict(self):
        response_format = _response_format()
        schema = response_format["schema"]

        assert response_format["type"] == "json_schema"
        assert response_format["name"] == "MemoryExtractionPayload"
        assert response_format["strict"] is True
        assert schema["additionalProperties"] is False
        assert schema["required"] == list(schema["properties"].keys())


class TestRawFallback:
    def test_raw_fallback_does_not_semantically_classify_lesson_text(self):
        text = "user: I learned that caching improves latency significantly."
        items = _fallback_raw_episode(text, reason="provider_unavailable")

        assert len(items) == 1
        assert isinstance(items[0], HarvestItem)
        assert items[0].raw_episode is True
        assert items[0].harvest_type == "raw_episode"
        assert items[0].memory_type == "episode"

    def test_empty_text_returns_empty(self):
        assert _fallback_raw_episode("", reason="provider_unavailable") == []
