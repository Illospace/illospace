"""Contract tests for cortex router split.

Verify that every public import from brain.app.api.routers.cortex still works
after the module is split into submodules.
"""
from __future__ import annotations

import pytest


class TestPublicImports:
    """Every name imported across the codebase must still be importable."""

    def test_router_importable(self):
        from brain.app.api.routers.cortex import router
        assert router is not None

    def test_validate_idea_org_alias(self):
        """Legacy _validate_idea_org alias still importable."""
        from brain.app.api.routers.cortex import _validate_idea_org
        assert callable(_validate_idea_org)

    def test_validate_idea_org_orm(self):
        from brain.app.api.routers.cortex import _validate_idea_org_orm
        assert callable(_validate_idea_org_orm)

    def test_row_to_dict(self):
        from brain.app.api.routers.cortex import _row_to_dict
        assert callable(_row_to_dict)

    def test_rows_to_list(self):
        from brain.app.api.routers.cortex import _rows_to_list
        assert callable(_rows_to_list)

    def test_parse_message_type(self):
        from brain.app.api.routers.cortex import _parse_message_type
        assert callable(_parse_message_type)

    def test_extract_mentions(self):
        from brain.app.api.routers.cortex import _extract_mentions
        assert callable(_extract_mentions)

    def test_infer_feedback_tags(self):
        from brain.app.api.routers.cortex import _infer_feedback_tags
        assert callable(_infer_feedback_tags)

    def test_record_implicit_feedback(self):
        from brain.app.api.routers.cortex import _record_implicit_feedback
        assert callable(_record_implicit_feedback)

    def test_create_feedback_triggers(self):
        from brain.app.api.routers.cortex import _create_feedback_triggers
        assert callable(_create_feedback_triggers)

    def test_generate_title_gpu(self):
        from brain.app.api.routers.cortex import _generate_title_gpu
        assert callable(_generate_title_gpu)

    def test_presence_join(self):
        from brain.app.api.routers.cortex import _presence_join
        assert callable(_presence_join)

    def test_presence_leave(self):
        from brain.app.api.routers.cortex import _presence_leave
        assert callable(_presence_leave)

    def test_presence_get(self):
        from brain.app.api.routers.cortex import _presence_get
        assert callable(_presence_get)

    def test_presence_cleanup(self):
        from brain.app.api.routers.cortex import _presence_cleanup
        assert callable(_presence_cleanup)

    def test_presence_store(self):
        from brain.app.api.routers.cortex import _presence_store
        assert isinstance(_presence_store, dict)

    def test_auth_status(self):
        from brain.app.api.routers.cortex import auth_status
        assert callable(auth_status)

    def test_add_api_key(self):
        from brain.app.api.routers.cortex import add_api_key
        assert callable(add_api_key)

    def test_set_org_main_key(self):
        from brain.app.api.routers.cortex import set_org_main_key
        assert callable(set_org_main_key)

    def test_valid_providers(self):
        from brain.app.api.routers.cortex import VALID_PROVIDERS
        assert "anthropic" in VALID_PROVIDERS
        assert "openai" in VALID_PROVIDERS
        assert "google" in VALID_PROVIDERS

    def test_upload_dir(self):
        from brain.app.api.routers.cortex import UPLOAD_DIR
        assert UPLOAD_DIR is not None

    def test_allowed_extensions(self):
        from brain.app.api.routers.cortex import ALLOWED_EXTENSIONS
        assert "png" in ALLOWED_EXTENSIONS
        assert "mp4" in ALLOWED_EXTENSIONS
        assert "pdf" in ALLOWED_EXTENSIONS
        assert "pptx" in ALLOWED_EXTENSIONS
        assert "xlsx" in ALLOWED_EXTENSIONS

    def test_max_upload_size(self):
        from brain.app.api.routers.cortex import MAX_UPLOAD_SIZE
        assert MAX_UPLOAD_SIZE == 10 * 1024 * 1024

    def test_max_video_upload_size(self):
        from brain.app.api.routers.cortex import MAX_VIDEO_UPLOAD_SIZE
        assert MAX_VIDEO_UPLOAD_SIZE == 50 * 1024 * 1024

    def test_implicit_feedback_rules(self):
        from brain.app.api.routers.cortex import _IMPLICIT_FEEDBACK_RULES
        assert len(_IMPLICIT_FEEDBACK_RULES) > 0


class TestRouterEndpoints:
    """All router endpoints are still registered after the split."""

    def _get_route_set(self):
        from brain.app.api.routers.cortex import router
        return {(r.path, tuple(sorted(r.methods))) for r in router.routes if hasattr(r, 'path')}

    def test_ideas_crud_endpoints(self):
        routes = self._get_route_set()
        assert ("/api/cortex/bootstrap", ("GET",)) in routes
        assert ("/api/cortex/ideas", ("GET",)) in routes
        assert ("/api/cortex/ideas", ("POST",)) in routes
        assert ("/api/cortex/ideas/{idea_id}", ("GET",)) in routes
        assert ("/api/cortex/ideas/{idea_id}", ("PATCH",)) in routes
        assert ("/api/cortex/ideas/{idea_id}", ("PUT",)) in routes
        assert ("/api/cortex/ideas/{idea_id}", ("DELETE",)) in routes

    def test_run_endpoints(self):
        routes = self._get_route_set()
        assert ("/api/cortex/run/status", ("GET",)) in routes
        assert ("/api/cortex/run/history/{idea_id}", ("GET",)) in routes
        assert ("/api/cortex/run/{run_id}/debug", ("GET",)) in routes

    def test_auth_endpoints(self):
        routes = self._get_route_set()
        assert ("/api/cortex/auth/status", ("GET",)) in routes
        assert ("/api/cortex/auth/connect", ("POST",)) not in routes
        assert ("/api/cortex/auth/openai/oauth/start", ("POST",)) not in routes
        assert ("/api/cortex/auth/openai/oauth/exchange", ("POST",)) not in routes

    def test_key_management_endpoints(self):
        routes = self._get_route_set()
        assert ("/api/cortex/keys", ("GET",)) in routes
        assert ("/api/cortex/keys", ("POST",)) in routes
        assert ("/api/cortex/keys/default", ("PUT",)) in routes
        assert ("/api/cortex/keys/org", ("POST",)) in routes
        assert ("/api/cortex/keys/{key_id}", ("DELETE",)) in routes

    def test_analytics_endpoints(self):
        routes = self._get_route_set()
        assert ("/api/cortex/analytics", ("GET",)) in routes
        assert ("/api/cortex/ideas/{idea_id}/activity-timeline", ("GET",)) in routes

    def test_misc_endpoints(self):
        routes = self._get_route_set()
        assert ("/api/cortex/upload", ("POST",)) in routes
        assert ("/api/cortex/notify", ("POST",)) in routes
        assert ("/api/cortex/events", ("POST",)) in routes
        assert ("/api/cortex/connections", ("GET",)) in routes
        assert ("/api/cortex/connections", ("POST",)) in routes

    def test_route_table_has_no_duplicate_path_method_pairs(self):
        """Ensure the split router does not register duplicate path/method pairs."""
        from brain.app.api.routers.cortex import router
        routes = [(r.path, tuple(sorted(r.methods))) for r in router.routes if hasattr(r, "path")]
        assert len(routes) == len(set(routes))


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
