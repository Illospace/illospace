"""Cortex router package — facade re-exporting all public names.

This package was split from a single cortex.py file for LLM workability.
All imports from ``brain.app.api.routers.cortex`` continue to work unchanged.
"""
from brain.app.api.routers.cortex._router import router  # noqa: F401

# Import submodules so their @router decorators register endpoints
import brain.app.api.routers.cortex._ideas  # noqa: F401
import brain.app.api.routers.cortex._run  # noqa: F401
import brain.app.api.routers.cortex._idea_ops  # noqa: F401
import brain.app.api.routers.cortex._analytics  # noqa: F401
import brain.app.api.routers.cortex._misc  # noqa: F401
import brain.app.api.routers.cortex._bootstrap  # noqa: F401
import brain.app.api.routers.cortex._auth_keys  # noqa: F401
import brain.app.api.routers.cortex._browser  # noqa: F401

# Re-export helpers and constants for backward compatibility
from brain.app.api.routers.cortex._helpers import (  # noqa: F401
    ALLOWED_EXTENSIONS,
    ARCHIVE_EXTENSIONS,
    DOCUMENT_EXTENSIONS,
    IMAGE_EXTENSIONS,
    MAX_VIDEO_UPLOAD_SIZE,
    MAX_UPLOAD_SIZE,
    TEXT_EXTENSIONS,
    UPLOAD_DIR,
    UPLOAD_FALLBACK_CONTENT_TYPES,
    VIDEO_EXTENSIONS,
    _IMPLICIT_FEEDBACK_RULES,
    _caller_is_service_principal,
    _create_feedback_triggers,
    _extract_mentions,
    _infer_feedback_tags,
    _parse_message_type,
    _presence_cleanup,
    _presence_get,
    _presence_join,
    _presence_leave,
    _presence_store,
    _record_implicit_feedback,
    _require_idea_for_user,
    _require_worker_principal,
    _row_to_dict,
    _rows_to_list,
    _validate_idea_org,
    _validate_idea_org_orm,
)
from brain.systems.cortex.title_generation import generate_display_title  # noqa: F401

# Re-export endpoint functions referenced by tests
from brain.app.api.routers.cortex._auth_keys import (  # noqa: F401
    VALID_PROVIDERS,
    _normalize_provider_api_key,
    _should_trust_failed_key_verification,
    _verify_provider_api_key,
    add_api_key,
    set_org_main_key,
)
from brain.app.api.routers.cortex._run import _serialize_active_runs  # noqa: F401
from brain.app.api.routers.cortex._misc import auth_status  # noqa: F401
from brain.app.api.routers.cortex import _project_context as _project_context  # noqa: F401,E402
