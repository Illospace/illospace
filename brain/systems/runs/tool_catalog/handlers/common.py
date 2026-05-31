"""Tool handler implementations for the Illo agent loop.

Contains all _handle_* functions that execute tool calls,
plus _get_tool_handlers() which builds the run map.
"""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from hashlib import sha256
import json
import logging
import os
import re as _re
import sys
import time
import uuid
import inspect

from brain.kernel import config as brain_config
from brain.systems.runs import actions as action_audit
from brain.systems.runs.evidence import normalize_tool_call_evidence
from brain.systems.runs.execution_artifacts import (
    append_execution_artifacts,
    append_run_execution_artifacts,
    load_execution_artifacts,
)
from brain.systems.runs.execution_context import (
    _agent_context,
    bind_agent_context,
)
from brain.systems.runs.project_execution_env import (
    _canonical_project_token_slug,
    _current_project_token_context,
    _current_run_target_context,
    _current_workspace_root_hint,
    _project_context_token_slugs,
)
from brain.systems.runs.tool_catalog.registry import (
    action_manifest_tool_names,
    parallel_safe_tool_names,
)

logger = logging.getLogger("agent")

_MODEL_TIERS = {"high", "medium", "low", "local"}
_MODEL_TIER_ALIASES: dict[str, str] = {}
_REASONING_EFFORTS = {"none", "low", "medium", "high", "xhigh"}

# Workspace root — configurable, defaults to project root
WORKSPACE_ROOT = str(brain_config.resolve_workspace_root(default=brain_config.BRAIN_DIR))

# Max output size for tool results (from budget config)
_MAX_RESULT_CHARS = int(os.environ.get("BUDGET_MAX_TOOL_RESULT_CHARS", "10000"))
_PARALLEL_BATCH_SAFE_TOOL_NAMES = parallel_safe_tool_names(scope="batch")
_MAX_PARALLEL_BATCH_OPERATIONS = int(os.environ.get("AGENT_PARALLEL_BATCH_MAX_OPS", "12"))
_MAX_PARALLEL_BATCH_WORKERS = int(os.environ.get("AGENT_PARALLEL_BATCH_MAX_WORKERS", "6"))
_ACTION_MANIFEST_TOOL_NAMES = action_manifest_tool_names()

_CONCRETE_BLOCKER_MARKERS = (
    "relation \"cycles\" does not exist",
    "undefinedtable",
    "table does not exist",
    "missing table",
    "missing schema",
    "schema blocker",
    "migration",
    "worker_unavailable",
    "unavailable service",
    "service unavailable",
    "startup timeout",
    "invalid x-api-key",
    "api key",
    "credential",
    "permission denied",
)

_BLOCKED_REPLY_MARKERS = (
    "blocked",
    "cannot",
    "can't",
    "couldn't",
    "could not",
    "failed",
    "error",
    "unavailable",
    "missing",
)

_MANAGE_TOOL_OPERATIONS: dict[str, dict[str, dict[str, object]]] = {
    "manage_cycle": {
        "list": {"required": [], "optional": [], "effect": "read scheduled cycles"},
        "create": {
            "required": ["name", "prompt", "timezone", "schedule_expr or run_at"],
            "optional": ["enabled", "target_idea_id", "model_override", "thinking_override", "guidance", "rationale"],
            "effect": "create a recurring cycle or one-time reminder",
        },
        "update": {
            "required": ["id"],
            "optional": ["name", "prompt", "timezone", "schedule_expr", "run_at", "enabled", "target_idea_id", "guidance", "rationale"],
            "effect": "change an existing cycle",
        },
        "delete": {"required": ["id"], "optional": [], "effect": "archive/disable a cycle"},
        "run": {"required": ["id"], "optional": [], "effect": "run a cycle immediately"},
        "add_guidance": {
            "required": ["id", "guidance"],
            "optional": ["rationale"],
            "effect": "append durable guidance for future cycle runs",
        },
        "add_output_target": {
            "required": ["id", "output_target_type"],
            "optional": ["output_target_id", "output_target_label", "output_target_config", "rationale"],
            "effect": "add a durable output target the cycle may publish to or repair",
        },
        "remove_output_target": {
            "required": ["id", "output_target_id"],
            "optional": ["rationale"],
            "effect": "deactivate a durable output target",
        },
    },
    "manage_domain": {
        "list": {"required": [], "optional": ["include_archived"], "effect": "read available domains"},
        "create_domain": {
            "required": ["name"],
            "optional": ["slug", "description", "objects", "relations"],
            "effect": "create a shared custom database",
        },
        "schema": {"required": ["domain_id"], "optional": ["include_archived"], "effect": "read a domain schema"},
        "remove_domain": {"required": ["domain_id"], "optional": ["mode"], "effect": "archive or delete a domain"},
        "add_object": {
            "required": ["domain_id", "object_key", "name"],
            "optional": ["description", "fields"],
            "effect": "add an object type to a domain",
        },
        "add_field": {
            "required": ["domain_id", "object_key", "field"],
            "optional": [],
            "effect": "add a field definition to an object type",
        },
        "add_relation_type": {
            "required": ["domain_id", "relation_type"],
            "optional": [],
            "effect": "add a relation type to a domain",
        },
        "query_records": {
            "required": ["domain_id"],
            "optional": ["object_key", "search", "limit", "include_archived"],
            "effect": "read records in a domain",
        },
        "get_record": {"required": ["domain_id", "record_id"], "optional": [], "effect": "read one record"},
        "create_record": {
            "required": ["domain_id", "object_key", "data"],
            "optional": ["title"],
            "effect": "create a record",
        },
        "update_record": {
            "required": ["domain_id", "record_id", "data_patch"],
            "optional": ["title", "expected_version"],
            "effect": "update a record",
        },
        "remove_record": {"required": ["domain_id", "record_id"], "optional": ["mode"], "effect": "archive or delete a record"},
        "link_records": {
            "required": ["domain_id", "relation_key", "source_record_id", "target_record_id"],
            "optional": ["properties"],
            "effect": "create a relation between records",
        },
        "events": {"required": ["domain_id"], "optional": ["record_id", "limit"], "effect": "read domain audit events"},
    },
    "manage_inbound": {
        "list_connections": {
            "required": [],
            "optional": ["agent_kind", "transport", "include_disabled", "limit"],
            "effect": "read configured inbound source connections",
        },
        "get_connection": {
            "required": ["connection_id"],
            "optional": [],
            "effect": "read one inbound source connection",
        },
        "create_connection": {
            "required": ["display_name"],
            "optional": [
                "agent_kind",
                "transport",
                "endpoint_url",
                "remote_agent_id",
                "remote_agent_card",
                "capabilities",
                "metadata",
            ],
            "effect": "create an external source connection that can submit inbound signals",
        },
        "update_connection": {
            "required": ["connection_id"],
            "optional": [
                "display_name",
                "status",
                "endpoint_url",
                "remote_agent_id",
                "remote_agent_card",
                "capabilities",
                "metadata",
            ],
            "effect": "update an inbound source connection",
        },
        "mint_token": {
            "required": ["connection_id"],
            "optional": ["token_name", "token_scopes", "expires_at"],
            "effect": "mint a scoped source token; defaults to signal:submit only",
        },
        "list_tokens": {
            "required": [],
            "optional": ["connection_id", "include_revoked", "limit"],
            "effect": "read source token metadata without revealing token secrets",
        },
        "get_token": {
            "required": ["token_id"],
            "optional": [],
            "effect": "read one source token metadata record without revealing the token secret",
        },
        "revoke_token": {
            "required": ["token_id"],
            "optional": [],
            "effect": "revoke a source token",
        },
        "list_policies": {
            "required": [],
            "optional": ["connection_id", "include_disabled", "limit"],
            "effect": "read source routing policies",
        },
        "get_policy": {
            "required": ["policy_id"],
            "optional": [],
            "effect": "read one source routing policy",
        },
        "create_policy": {
            "required": ["connection_id", "name", "origin_patterns"],
            "optional": [
                "priority",
                "envelope_kinds",
                "instructions",
                "schema_config",
                "allowed_actions",
                "auto_execute_actions",
                "auto_execute_min_confidence",
                "review_mode",
                "metadata",
                "enabled",
            ],
            "effect": "create deterministic matching instructions for inbound signals",
        },
        "update_policy": {
            "required": ["policy_id"],
            "optional": [
                "name",
                "enabled",
                "priority",
                "origin_patterns",
                "envelope_kinds",
                "instructions",
                "schema_config",
                "allowed_actions",
                "auto_execute_actions",
                "auto_execute_min_confidence",
                "review_mode",
                "metadata",
            ],
            "effect": "update deterministic matching instructions for inbound signals",
        },
        "list_projections": {
            "required": [],
            "optional": ["connection_id", "policy_id", "include_disabled", "limit"],
            "effect": "read Domain Projection configs for inbound signals",
        },
        "get_projection": {
            "required": ["projection_id"],
            "optional": [],
            "effect": "read one Domain Projection config",
        },
        "create_projection": {
            "required": [
                "connection_id",
                "domain_id",
                "object_key",
                "external_id_path",
                "external_id_field",
                "field_mapping",
            ],
            "optional": [
                "policy_id",
                "title_path",
                "upsert_mode",
                "validation_failure_status",
                "metadata",
                "enabled",
                "auto_allow_policy_action",
            ],
            "effect": "create a deterministic projection from matching inbound payloads into Domain records",
        },
        "update_projection": {
            "required": ["projection_id"],
            "optional": [
                "policy_id",
                "domain_id",
                "object_key",
                "enabled",
                "external_id_path",
                "external_id_field",
                "field_mapping",
                "title_path",
                "upsert_mode",
                "validation_failure_status",
                "metadata",
            ],
            "effect": "update a deterministic Domain Projection",
        },
        "list_events": {
            "required": [],
            "optional": ["connection_id", "policy_id", "status", "origin", "include_payload", "limit"],
            "effect": "read inbound event logs",
        },
        "list_attention_events": {
            "required": [],
            "optional": ["connection_id", "policy_id", "origin", "include_payload", "limit"],
            "effect": "read stuck or attention-needed inbound events across review_required, quarantined, failed, or errored statuses",
        },
        "get_event": {
            "required": ["event_id"],
            "optional": ["include_receipts", "limit"],
            "effect": "read one inbound event and optionally its decision receipts",
        },
        "list_receipts": {
            "required": [],
            "optional": ["event_id", "limit"],
            "effect": "read inbound decision receipts",
        },
        "dry_run_match": {
            "required": ["connection_id", "origin"],
            "optional": ["kind", "payload"],
            "effect": "preview policy and projection matching without storing an event",
        },
        "replay_events": {
            "required": [],
            "optional": ["event_id", "connection_id", "policy_id", "status", "origin", "include_payload", "limit"],
            "effect": "replay stored inbound events against current config without mutating workspace state",
        },
        "get_source_card": {
            "required": ["connection_id"],
            "optional": ["limit"],
            "effect": "read computed and persisted source-card summary for one inbound connection",
        },
        "refresh_source_card": {
            "required": ["connection_id"],
            "optional": ["source_purpose", "source_notes", "source_tags", "limit"],
            "effect": "refresh the persisted source-card summary on an inbound connection",
        },
    },
    "manage_skill": {
        "list": {"required": [], "optional": ["include_archived", "limit"], "effect": "read available skills"},
        "get": {"required": ["skill_id or skill_name"], "optional": [], "effect": "read one skill"},
        "create": {
            "required": ["name", "procedure"],
            "optional": [
                "description",
                "model_tier",
                "thinking_tier",
                "triggers",
                "guardrails",
                "pitfalls",
                "refinements",
                "assets",
                "create_as_package",
                "user_requested",
            ],
            "effect": "create a durable slash-routable skill",
        },
        "create_many": {
            "required": ["skills"],
            "optional": ["model_tier", "thinking_tier", "create_as_package", "user_requested"],
            "effect": "create multiple durable slash-routable skills in one tool call",
        },
        "update": {
            "required": ["skill_id or skill_name", "at least one changed field"],
            "optional": ["name", "description", "procedure", "model_tier", "thinking_tier", "triggers", "guardrails", "pitfalls", "refinements"],
            "effect": "update a skill and bump version when procedure changes",
        },
        "edit": {
            "required": ["skill_id or skill_name", "at least one changed field"],
            "optional": ["name", "description", "procedure", "model_tier", "thinking_tier", "triggers", "guardrails", "pitfalls", "refinements"],
            "effect": "alias for update",
        },
        "archive": {"required": ["skill_id or skill_name"], "optional": [], "effect": "archive a skill"},
        "delete": {"required": ["skill_id or skill_name"], "optional": [], "effect": "archive a skill"},
        "convert_to_bundle": {"required": ["skill_id or skill_name"], "optional": [], "effect": "convert a DB skill to a local bundle-backed skill"},
        "list_assets": {"required": ["skill_id or skill_name"], "optional": ["limit"], "effect": "read package assets"},
        "get_asset": {"required": ["skill_id or skill_name", "path"], "optional": ["max_chars"], "effect": "read one package asset"},
        "upsert_asset": {
            "required": ["skill_id or skill_name", "path", "content"],
            "optional": ["asset_kind", "mime_type", "loading_budget_tokens"],
            "effect": "add or replace a skill package asset",
        },
        "delete_asset": {"required": ["skill_id or skill_name", "path"], "optional": [], "effect": "delete a skill package asset"},
    },
    "manage_idea": {
        "list": {"required": [], "optional": ["status", "search", "include_archived", "limit"], "effect": "read Cortex thoughts"},
        "get": {"required": ["idea_id unless a current thread is bound"], "optional": [], "effect": "read one thought"},
        "create": {
            "required": ["title"],
            "optional": ["thread_message", "description", "status", "start_run", "parent_id", "user_id"],
            "effect": "create a Cortex thought with an Illo-authored seed message; user_id assigns the owner",
        },
        "update": {
            "required": ["idea_id unless a current thread is bound", "at least one changed field"],
            "optional": ["title", "display_title", "description", "status", "position_x", "position_y", "user_id"],
            "effect": "update thought metadata",
        },
        "archive": {"required": ["idea_id unless a current thread is bound"], "optional": [], "effect": "archive a thought"},
        "restore": {"required": ["idea_id"], "optional": [], "effect": "restore an archived thought"},
        "set_status": {"required": ["idea_id unless a current thread is bound", "status"], "optional": [], "effect": "change status"},
        "mark_read": {"required": ["idea_id unless a current thread is bound"], "optional": [], "effect": "mark a thought read"},
    },
    "manage_project": {
        "list": {"required": [], "optional": ["query", "limit", "include_inactive"], "effect": "read project context profiles, optionally filtered by project name, description, aliases, or resources"},
        "get": {"required": ["project_id"], "optional": ["include_inactive"], "effect": "read one project profile"},
        "create": {
            "required": ["slug", "name"],
            "optional": ["description", "project_context", "resources", "visibility", "shared_usernames", "metadata", "default_environment_binding_id"],
            "effect": "create a reusable project context profile",
        },
        "update": {
            "required": ["project_id"],
            "optional": ["slug", "name", "description", "project_context", "resources", "visibility", "shared_usernames", "metadata"],
            "effect": "update a project context profile",
        },
        "archive": {"required": ["project_id"], "optional": [], "effect": "archive a project profile"},
        "delete": {"required": ["project_id"], "optional": [], "effect": "archive a project profile"},
        "add_resource": {"required": ["project_id", "resource or resources"], "optional": [], "effect": "add project resources"},
        "update_resource": {"required": ["project_id", "resource_id", "resource"], "optional": [], "effect": "update a project resource"},
        "remove_resource": {"required": ["project_id", "resource_id"], "optional": [], "effect": "remove a project resource"},
        "reorder_resources": {"required": ["project_id", "resource_ids"], "optional": [], "effect": "reorder resources"},
        "attach_to_thread": {
            "required": ["project_id or project_context", "idea_id unless a current thread is bound"],
            "optional": ["environment_binding_id", "metadata"],
            "effect": "attach project context to a thought",
        },
        "search_files": {
            "required": ["query"],
            "optional": ["project_id", "paths", "glob", "limit"],
            "effect": "search files in visible Projects without loading all Project contents",
            "parameters": {
                "query": "Search text to match in files across visible Projects.",
                "limit": "Maximum search results to return.",
                "paths": "Optional file or folder paths to constrain the search.",
                "glob": "Optional file glob filter such as '**/*.md'.",
            },
        },
        "mount_reference": {
            "required": ["project_id"],
            "optional": ["paths", "path", "glob", "limit", "mount_path"],
            "effect": "expose selected files or folders from another Project as read-only reference mounts for read_file/list_files/search_files",
            "parameters": {
                "paths": "Selected file or folder paths from the source Project to expose.",
                "glob": "Optional file glob filter for selecting Project files to expose.",
                "limit": "Maximum glob-selected mounts to create.",
                "mount_path": "Workspace mount path where the read-only reference should appear.",
            },
        },
    },
    "manage_workspace_app": {
        "list": {
            "required": [],
            "optional": ["include_archived", "confirm_include_archived", "include_prototypes"],
            "effect": "read active workspace apps; archived reads require explicit user intent",
        },
        "get": {
            "required": ["app_id or key"],
            "optional": ["include_archived", "confirm_include_archived"],
            "effect": "read one active workspace app; archived reads require explicit user intent",
        },
        "create": {
            "required": ["name"],
            "optional": ["key", "description", "renderer_key", "source_kind", "source_code", "manifest", "visual_spec", "initial_state"],
            "effect": "create a generated workspace app",
        },
        "update": {
            "required": ["app_id or key"],
            "optional": ["name", "description", "renderer_key", "source_kind", "source_code", "manifest", "visual_spec"],
            "effect": "update a generated workspace app",
        },
        "archive": {"required": ["app_id or key"], "optional": [], "effect": "archive an app"},
        "restore": {
            "required": ["app_id or key", "confirm_restore_archived"],
            "optional": [],
            "effect": "restore an archived app only when the user explicitly requested restore/reopen",
        },
        "get_state": {"required": ["app_id"], "optional": ["state_key"], "effect": "read app-local state"},
        "update_state": {"required": ["app_id"], "optional": ["state_key", "data", "data_patch"], "effect": "write app-local state"},
    },
}


def _manage_tool_guide(tool_name: str, operation: str | None = None) -> str:
    operations = _MANAGE_TOOL_OPERATIONS.get(tool_name, {})
    requested = str(operation or "").strip().lower()
    if requested:
        detail = operations.get(requested)
        if detail is None:
            return json.dumps({
                "tool": tool_name,
                "error": f"Unknown operation: {requested}",
                "available_operations": sorted(operations),
            })
        return json.dumps({"tool": tool_name, "operation": requested, **detail}, default=str)
    return json.dumps({
        "tool": tool_name,
        "usage": "Call this tool again with action set to one of these operations. Use operation with action=help/schema for one operation.",
        "operations": operations,
    }, default=str)


def _is_missing_cycle_schema_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return (
        "undefinedtable" in message
        or 'relation "cycles" does not exist' in message
        or "relation 'cycles' does not exist" in message
    )


def _cycle_schema_missing_payload(exc: Exception) -> dict:
    return {
        "error": str(exc),
        "terminal": True,
        "blocker": "cycles_db_schema_missing",
        "instruction": (
            "The cycles database schema is missing. Do not retry or continue investigating this request. "
            "Tell the user that the server needs the cycles migration applied before cycles can be listed, "
            "created, updated, deleted, or run."
        ),
    }


def _looks_like_concrete_blocker_reply(reply: str, execution_context: str | None = None) -> bool:
    text = f"{reply}\n{execution_context or ''}".lower()
    return (
        any(marker in text for marker in _BLOCKED_REPLY_MARKERS)
        and any(marker in text for marker in _CONCRETE_BLOCKER_MARKERS)
)


def _coerce_agent_run_id(value) -> int | None:
    try:
        return int(value) if value else None
    except (TypeError, ValueError):
        return None


def _current_runtime_secret_context():
    """Return the current AgentRun identity for trusted runtime secret reads."""

    from brain.systems.vault.runtime_secrets import RuntimeSecretContext

    execution_metadata = getattr(_agent_context, "execution_metadata", None)
    metadata = execution_metadata if isinstance(execution_metadata, dict) else {}
    run = getattr(_agent_context, "run", None)
    run_id = (
        getattr(_agent_context, "run_id", None)
        or getattr(run, "run_id", None)
        or metadata.get("run_id")
    )
    return RuntimeSecretContext(
        actor_user_id=str(getattr(_agent_context, "user_id", None) or metadata.get("user_id") or "").strip() or None,
        org_id=str(getattr(_agent_context, "org_id", None) or metadata.get("org_id") or "").strip() or None,
        run_id=_coerce_agent_run_id(run_id),
        idea_id=str(getattr(_agent_context, "idea_id", None) or metadata.get("idea_id") or "").strip() or None,
    )


def _current_manifest_context() -> dict:
    from brain.systems.runs.cortex.recording import trace_id_for_run_id

    run = getattr(_agent_context, "run", None)
    execution_metadata = getattr(_agent_context, "execution_metadata", {}) or {}
    user_id = getattr(_agent_context, "user_id", None) or execution_metadata.get("user_id")
    worker_name = _get_current_worker_name()
    actor_kind = "user" if user_id else "agent"
    actor = str(user_id or worker_name or "agent")
    run_id = getattr(run, "run_id", None) or execution_metadata.get("run_id")
    return {
        "actor": actor,
        "actor_id": str(user_id) if user_id else None,
        "actor_kind": actor_kind,
        "org_id": getattr(_agent_context, "org_id", None) or execution_metadata.get("org_id"),
        "run_id": run_id,
        "trace_id": execution_metadata.get("trace_id") or trace_id_for_run_id(run_id),
        "worker_name": worker_name,
        "idea_id": getattr(_agent_context, "idea_id", None) or execution_metadata.get("idea_id"),
    }


def _build_action_manifest(
    tool_name: str,
    args: tuple,
    kwargs: dict,
) -> action_audit.ActionManifestCreate | None:
    return action_audit.build_action_manifest(
        tool_name,
        args,
        kwargs,
        context=_patched_private("_current_manifest_context", _current_manifest_context)(),
    )


def _wrap_action_manifest_audit(tool_name: str, handler):
    return action_audit.wrap_action_manifest_audit(
        tool_name,
        handler,
        context_factory=lambda: _patched_private(
            "_current_manifest_context",
            _current_manifest_context,
        )(),
    )


def _wrap_brain_encode(original_fn):
    """Wrap brain_encode to handle the 'type' → 'memory_type' rename."""
    async def wrapper(**kwargs):
        if "type" in kwargs:
            kwargs["memory_type"] = kwargs.pop("type")
        kwargs.setdefault("user_id", getattr(_agent_context, "user_id", None))
        kwargs.setdefault("org_id", getattr(_agent_context, "org_id", None))
        kwargs.setdefault("idea_id", getattr(_agent_context, "idea_id", None))
        kwargs.setdefault("session_id", getattr(_agent_context, "session_id", None))
        run = getattr(_agent_context, "run", None)
        execution_metadata = getattr(_agent_context, "execution_metadata", None)
        run_id = getattr(run, "run_id", None)
        if not run_id and isinstance(execution_metadata, dict):
            run_id = execution_metadata.get("run_id")
        kwargs.setdefault("run_id", run_id)
        if kwargs.get("user_id") and not kwargs.get("org_id"):
            try:
                from brain.platform.db.repositories.unit_of_work import UnitOfWork
                from brain.platform.db.models.org import User
                async with UnitOfWork() as uow:
                    user = await uow.session.get(User, kwargs["user_id"])
                    if user and getattr(user, "org_id", None):
                        kwargs["org_id"] = user.org_id
            except Exception:
                pass
        if "visibility" not in kwargs:
            kwargs["visibility"] = "org" if kwargs.get("org_id") else "private"
        result = original_fn(**kwargs)
        if inspect.isawaitable(result):
            return await result
        return result
    return wrapper


def _wrap_brain_recall(original_fn):
    """Inject run viewer context into recall tool calls."""
    async def wrapper(**kwargs):
        execution_metadata = getattr(_agent_context, "execution_metadata", None)
        if kwargs.get("user_id") is None:
            kwargs["user_id"] = getattr(_agent_context, "user_id", None)
        if kwargs.get("org_id") is None:
            kwargs["org_id"] = getattr(_agent_context, "org_id", None)
        if isinstance(execution_metadata, dict):
            if kwargs.get("user_id") is None:
                kwargs["user_id"] = execution_metadata.get("user_id")
            if kwargs.get("org_id") is None:
                kwargs["org_id"] = execution_metadata.get("org_id")
        result = original_fn(**kwargs)
        if inspect.isawaitable(result):
            result = await result
        _patched_private("_record_tool_evidence", _record_tool_evidence)(
            "brain_recall",
            kwargs,
            result,
        )
        return result
    return wrapper


def _invoke_with_threadlocal_context(handler, args: dict, threadlocal_context: dict) -> object:
    """Invoke a handler in another thread while preserving AgentRun-local context."""
    with bind_agent_context(threadlocal_context):
        return handler(**args)


def _normalize_workspace_entry(entry) -> dict[str, str] | None:
    """Normalize a workspace entry into {name, path}."""
    if isinstance(entry, str) and entry.strip():
        expanded = os.path.realpath(os.path.expanduser(entry.strip()))
        if os.path.exists(expanded) and not os.path.isdir(expanded):
            return None
        return {"name": os.path.basename(expanded), "path": expanded}
    if isinstance(entry, dict):
        raw_path = str(entry.get("path", "")).strip()
        if not raw_path:
            return None
        expanded = os.path.realpath(os.path.expanduser(raw_path))
        if os.path.exists(expanded) and not os.path.isdir(expanded):
            return None
        name = str(entry.get("name") or os.path.basename(expanded)).strip()
        return {"name": name, "path": expanded}
    return None


def _build_workspace_registry(
    workspace_root: str | None = None,
    allowed_workspaces: list[str | dict] | None = None,
) -> list[dict[str, str]]:
    """Return a deduplicated list of accessible workspace roots."""
    registry: list[dict[str, str]] = []
    seen_paths: set[str] = set()

    for raw in ([workspace_root] if workspace_root else []) + list(allowed_workspaces or []):
        item = _normalize_workspace_entry(raw)
        if not item or item["path"] in seen_paths:
            continue
        registry.append(item)
        seen_paths.add(item["path"])

    return registry


def _select_workspace(
    workspace: str | None,
    workspace_root: str | None = None,
    allowed_workspaces: list[str | dict] | None = None,
) -> str | None:
    """Resolve an explicit workspace selector against the allowed workspace set."""
    registry = _build_workspace_registry(workspace_root, allowed_workspaces)
    default_root = registry[0]["path"] if registry else None
    if not workspace:
        return default_root
    if not registry:
        return None

    requested = workspace.strip()
    requested_path = os.path.realpath(os.path.expanduser(requested))
    path_matches = [item for item in registry if item["path"] == requested_path]
    if path_matches:
        return path_matches[0]["path"]

    name_matches = [item for item in registry if item["name"] == requested or os.path.basename(item["path"]) == requested]
    if len(name_matches) == 1:
        return name_matches[0]["path"]
    if len(name_matches) > 1:
        options = ", ".join(sorted({item["path"] for item in name_matches}))
        raise ValueError(f"Workspace selector '{workspace}' is ambiguous. Matches: {options}")

    options = ", ".join(sorted(item["name"] for item in registry)) or "(none)"
    raise ValueError(f"Workspace '{workspace}' is not accessible in this run. Available: {options}")


def _get_current_run_id() -> str | None:
    """Get the current run_id from AgentRun context."""
    run = getattr(_agent_context, "run", None)
    return run.run_id if run else None


def _get_current_worker_name() -> str:
    """Get the current worker/skill name from AgentRun context."""
    worker_name = getattr(_agent_context, "worker_name", None)
    if worker_name:
        return worker_name
    run = getattr(_agent_context, "run", None)
    return run.skill_used or "coordinator" if run else "unknown"


def _coerce_issue_number(value: object) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _attach_issue_number(artifact: dict, issue_number: int | None) -> dict:
    normalized = dict(artifact)
    if (
        issue_number is not None
        and normalized.get("issue_number") is None
        and normalized.get("type") in {"branch", "pr", "merge"}
    ):
        normalized["issue_number"] = issue_number
    return normalized


def _persist_execution_artifacts(artifacts: list[dict], run_id: int | None = None) -> None:
    """Attach normalized execution artifacts to the active AgentRun context."""
    if not artifacts:
        return

    from brain.systems.runs.cortex.recording import trace_id_for_run_id

    run = getattr(_agent_context, "run", None)
    execution_metadata = getattr(_agent_context, "execution_metadata", {}) or {}
    run_id = run_id or getattr(run, "run_id", None) or execution_metadata.get("run_id")
    trace_id = execution_metadata.get("trace_id") or trace_id_for_run_id(run_id)

    normalized_artifacts = [dict(artifact) for artifact in artifacts]

    existing = getattr(_agent_context, "execution_artifacts", None)
    if existing is None:
        _agent_context.execution_artifacts = []
        existing = _agent_context.execution_artifacts

    for artifact in normalized_artifacts:
        if artifact not in existing:
            existing.append(artifact)


async def _persist_execution_artifacts_async(artifacts: list[dict], run_id: int | None = None) -> None:
    """Persist normalized execution artifacts to AgentRun context and run state."""
    if not artifacts:
        return

    from brain.systems.runs.cortex.recording import trace_id_for_run_id

    run = getattr(_agent_context, "run", None)
    execution_metadata = getattr(_agent_context, "execution_metadata", {}) or {}
    run_id = run_id or getattr(run, "run_id", None) or execution_metadata.get("run_id")
    trace_id = execution_metadata.get("trace_id") or trace_id_for_run_id(run_id)

    normalized_artifacts = [dict(artifact) for artifact in artifacts]
    _persist_execution_artifacts(normalized_artifacts, run_id=run_id)

    if run_id:
        await append_run_execution_artifacts(run_id=run_id, artifacts=normalized_artifacts)

    execution_id = execution_metadata.get("execution_id")
    if execution_id:
        provenance = {
            "execution_id": execution_id,
            "run_id": execution_metadata.get("run_id", run_id),
            "trace_id": trace_id,
            "session_id": execution_metadata.get("session_id"),
            "node_id": execution_metadata.get("node_id") or execution_metadata.get("step_id"),
        }
        await append_execution_artifacts(
            execution_id=execution_id,
            provenance=provenance,
            artifacts=[
                {**artifact, "provenance": provenance}
                for artifact in normalized_artifacts
            ],
        )


def _current_evidence_provenance() -> dict:
    """Return compact execution provenance for evidence ledger records."""
    run = getattr(_agent_context, "run", None)
    execution_metadata = getattr(_agent_context, "execution_metadata", {}) or {}
    return {
        "run_id": getattr(run, "run_id", None) or execution_metadata.get("run_id"),
        "execution_id": execution_metadata.get("execution_id"),
        "worker_id": execution_metadata.get("worker_id"),
        "node_id": execution_metadata.get("node_id") or execution_metadata.get("step_id"),
        "skill": execution_metadata.get("skill_name"),
        "session_id": execution_metadata.get("session_id") or getattr(_agent_context, "session_id", None),
    }


def _record_tool_evidence(tool_name: str, args: dict | None, result: object) -> None:
    """Persist backend-owned evidence records for a tool call."""
    provenance = {
        key: value
        for key, value in _current_evidence_provenance().items()
        if value not in (None, "", {})
    }
    records = normalize_tool_call_evidence(
        tool_name,
        args if isinstance(args, dict) else {},
        result,
        provenance=provenance,
    )
    if not records:
        return
    _patched_private(
        "_persist_execution_artifacts",
        _persist_execution_artifacts,
    )(records, run_id=provenance.get("run_id"))


def _wrap_tool_evidence(tool_name: str, handler):
    """Wrap a handler so successful backend tool output becomes evidence."""
    async def wrapper(*args, **kwargs):
        result = handler(*args, **kwargs)
        if inspect.isawaitable(result):
            result = await result
        evidence_args = dict(kwargs)
        if args:
            evidence_args["_args"] = list(args)
        _patched_private("_record_tool_evidence", _record_tool_evidence)(
            tool_name,
            evidence_args,
            result,
        )
        return result
    return wrapper


def _record_execution_artifacts(command: str, result: dict) -> None:
    """Persist structured execution artifacts for provenance questions."""
    exit_code = result.get("exit_code")
    stdout = result.get("stdout", "") or ""
    artifacts: list[dict] = []

    if exit_code == 0:
        if m := _re.search(r"\bgit\s+checkout\s+-b\s+([^\s]+)", command):
            artifacts.append({"type": "branch", "branch": m.group(1), "status": "created"})
        if m := _re.search(r"\bgit\s+switch\s+-c\s+([^\s]+)", command):
            artifacts.append({"type": "branch", "branch": m.group(1), "status": "created"})
        if m := _re.search(r"\bgit\s+worktree\s+add\b.*?\s+-b\s+([^\s]+)", command):
            artifacts.append({"type": "branch", "branch": m.group(1), "status": "created"})
        if (
            _re.search(r"\bgit\s+commit\b", command)
            and (m := _re.search(r"\[([^\s]+)\s+([0-9a-f]{7,40})\]\s+([^\n]+)", stdout))
        ):
            artifacts.append({
                "type": "commit",
                "branch": m.group(1),
                "sha": m.group(2),
                "summary": m.group(3).strip(),
            })
        if m := _re.search(r"\bgit\s+push\s+origin\s+([^\s]+)", command):
            artifacts.append({"type": "push", "remote": "origin", "branch": m.group(1), "status": "pushed"})

        pr_match = _re.search(r"https://github\.com/[^\s]+/pull/(\d+)", stdout)
        if pr_match:
            if (
                _re.search(r"\bgh\s+pr\s+merge\b", command)
                or _re.search(r"\b(?:merged|merge(?:d)?)\s+(?:a\s+)?(?:pr|pull request)\b", stdout, _re.I)
            ):
                artifacts.append({
                    "type": "merge",
                    "url": pr_match.group(0),
                    "number": int(pr_match.group(1)),
                    "status": "merged",
                })
            elif (
                _re.search(r"\bgh\s+pr\s+create\b", command)
                or _re.search(r"\b(?:created?|opened|submitted|raised)\s+(?:a\s+)?(?:pr|pull request)\b", stdout, _re.I)
            ):
                artifacts.append({
                    "type": "pr",
                    "url": pr_match.group(0),
                    "number": int(pr_match.group(1)),
                    "status": "created",
                })

        issue_match = _re.search(r"https://github\.com/[^\s]+/issues/(\d+)", stdout)
        if issue_match and (
            _re.search(r"\bgh\s+issue\s+create\b", command)
            or _re.search(r"\b(?:created?|opened|filed)\s+(?:a\s+)?issue\b", stdout, _re.I)
        ):
            artifacts.append({
                "type": "issue",
                "url": issue_match.group(0),
                "number": int(issue_match.group(1)),
                "status": "created",
            })

        if _looks_like_test_command(command):
            artifacts.append({
                "type": "test_run",
                "command": command,
                "status": "passed" if exit_code == 0 else "failed",
                "exit_code": exit_code,
                "summary": (stdout or result.get("stderr", "") or "")[:500],
            })

    _patched_private(
        "_persist_execution_artifacts",
        _persist_execution_artifacts,
    )(artifacts)


def _looks_like_test_command(command: str) -> bool:
    lowered = (command or "").lower()
    return any(
        hint in lowered
        for hint in (
            "pytest",
            "python -m pytest",
            "unittest",
            "npm test",
            "pnpm test",
            "yarn test",
            "go test",
            "cargo test",
            "make test",
        )
    )


def _require_run_id(tool_name: str) -> tuple[str | None, str | None]:
    """Return (run_id, error_json). If error_json is set, return it immediately."""
    run_id = _patched_private("_get_current_run_id", _get_current_run_id)()
    if not run_id:
        return None, json.dumps({"error": f"No active AgentRun — {tool_name} requires a run context"})
    return run_id, None


def _patched_private(name: str, default):
    """Resolve private patch points exposed by brain.systems.runs.tool_handlers."""
    facade = sys.modules.get("brain.systems.runs.tool_handlers")
    if facade is None:
        return default
    return getattr(facade, name, default)


def _patched_workspace_root() -> str:
    return _patched_private("WORKSPACE_ROOT", WORKSPACE_ROOT)


_VALID_SECTIONS = frozenset({"findings", "decisions", "open_questions", "resources", "handoffs"})


__all__ = [name for name in globals() if not name.startswith("__")]
