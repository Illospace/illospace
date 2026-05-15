"""Contract tests for cortex router split.

Verify that every public import from brain.app.api.routers.cortex still works
after the module is split into submodules.
"""
from __future__ import annotations

class TestBehavioralContracts:
    """Key behavioral contracts still work."""

    def test_row_to_dict_with_none(self):
        from brain.app.api.routers.cortex import _row_to_dict
        assert _row_to_dict(None) is None

    def test_row_to_dict_with_mapping(self):
        from brain.app.api.routers.cortex import _row_to_dict
        import uuid
        from datetime import datetime

        class FakeRow:
            class _mapping:
                pass

            def __init__(self):
                self._mapping = {"id": uuid.UUID("12345678-1234-5678-1234-567812345678"),
                                 "ts": datetime(2024, 1, 1)}

        row = FakeRow()
        result = _row_to_dict(row)
        assert result["id"] == "12345678-1234-5678-1234-567812345678"
        assert isinstance(result["ts"], str)

    def test_parse_message_type_trigger(self):
        from brain.app.api.routers.cortex import _parse_message_type
        assert _parse_message_type("Hey @illo do this") == "trigger"

    def test_parse_message_type_plain_user_message_triggers(self):
        from brain.app.api.routers.cortex import _parse_message_type
        assert _parse_message_type("Just chatting") == "trigger"

    def test_parse_message_type_agent_response(self):
        from brain.app.api.routers.cortex import _parse_message_type
        assert _parse_message_type("anything", role="assistant") == "agent_response"

    def test_extract_mentions_basic(self):
        from brain.app.api.routers.cortex import _extract_mentions
        assert _extract_mentions("Hey @alice and @bob") == ["alice", "bob"]

    def test_infer_feedback_tags_detects_memory(self):
        from brain.app.api.routers.cortex import _infer_feedback_tags
        tags = _infer_feedback_tags("Illo does not remember anything")
        assert "memory_failure" in tags

    def test_infer_feedback_tags_clean(self):
        from brain.app.api.routers.cortex import _infer_feedback_tags
        tags = _infer_feedback_tags("Great job on that feature!")
        assert tags == []

    def test_presence_roundtrip(self):
        from brain.app.api.routers.cortex import _presence_join, _presence_get, _presence_leave, _presence_store
        _presence_store.clear()

        _presence_join("test-idea", "user-1", "Alice", "#f00")
        viewers = _presence_get("test-idea")
        assert len(viewers) == 1
        assert viewers[0]["name"] == "Alice"

        _presence_leave("test-idea", "user-1")
        viewers = _presence_get("test-idea")
        assert len(viewers) == 0
