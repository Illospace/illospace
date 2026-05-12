"""Workspace app orchestration tool handlers."""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from typing import Any

from brain.systems.runs.tool_catalog.handlers.common import *
from brain.systems.workspace_apps.compiler import (
    compile_workspace_app_input,
    contract_repair_guidance,
    service_error_guidance,
)


def _workspace_app_context() -> tuple[str | None, str | None]:
    execution_metadata = getattr(_agent_context, "execution_metadata", {}) or {}
    org_id = getattr(_agent_context, "org_id", None) or execution_metadata.get("org_id")
    user_id = getattr(_agent_context, "user_id", None) or execution_metadata.get("user_id")
    return org_id, user_id


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_uuid(value: Any, field: str) -> tuple[str | None, dict[str, str] | None]:
    text = _optional_text(value)
    if text is None:
        return None, None
    try:
        return str(uuid.UUID(text)), None
    except (TypeError, ValueError, AttributeError):
        return None, {"error": f"{field} must be a valid UUID when provided"}


def _optional_bool(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off", ""}:
        return False
    return default


def _optional_mapping(value: Any, field: str) -> tuple[dict[str, Any] | None, dict[str, str] | None]:
    if value is None:
        return None, None
    if isinstance(value, Mapping):
        return dict(value), None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None, None
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            return None, {"error": f"{field} must be an object or valid JSON object string: {exc.msg}"}
        if isinstance(parsed, Mapping):
            return dict(parsed), None
    return None, {"error": f"{field} must be an object when provided"}


def _handle_manage_workspace_app(
    action: str,
    operation: str | None = None,
    app_id: str | None = None,
    key: str | None = None,
    name: str | None = None,
    description: str | None = None,
    renderer_key: str | None = None,
    source_kind: str | None = None,
    source_code: str | None = None,
    manifest: dict | None = None,
    visual_spec: dict | None = None,
    metadata: dict | None = None,
    anchor_user_id: str | None = None,
    initial_state: dict | None = None,
    state_key: str = "default",
    data: dict | None = None,
    data_patch: dict | None = None,
    include_archived: bool = False,
    include_prototypes: bool = False,
    confirm_restore_archived: bool = False,
) -> str:
    action = str(action or "").strip().lower()
    if action in {"help", "schema"}:
        return _manage_tool_guide("manage_workspace_app", operation)

    app_id = _optional_text(app_id)
    key = _optional_text(key)
    name = _optional_text(name)
    description = _optional_text(description)
    renderer_key = _optional_text(renderer_key)
    source_kind = _optional_text(source_kind)
    state_key = _optional_text(state_key) or "default"
    include_archived = _optional_bool(include_archived)
    include_prototypes = _optional_bool(include_prototypes)
    confirm_restore_archived = _optional_bool(confirm_restore_archived)
    anchor_user_id, uuid_error = _optional_uuid(anchor_user_id, "anchor_user_id")
    if uuid_error:
        return json.dumps(uuid_error)

    manifest, mapping_error = _optional_mapping(manifest, "manifest")
    if mapping_error:
        return json.dumps(mapping_error)
    visual_spec, mapping_error = _optional_mapping(visual_spec, "visual_spec")
    if mapping_error:
        return json.dumps(mapping_error)
    metadata, mapping_error = _optional_mapping(metadata, "metadata")
    if mapping_error:
        return json.dumps(mapping_error)
    initial_state, mapping_error = _optional_mapping(initial_state, "initial_state")
    if mapping_error:
        return json.dumps(mapping_error)
    data, mapping_error = _optional_mapping(data, "data")
    if mapping_error:
        return json.dumps(mapping_error)
    data_patch, mapping_error = _optional_mapping(data_patch, "data_patch")
    if mapping_error:
        return json.dumps(mapping_error)

    from brain.platform.db.repositories.unit_of_work import UnitOfWork
    from brain.systems.workspace_apps.service import (
        WorkspaceAppContractError,
        WorkspaceAppError,
        archive_app,
        create_app,
        get_app,
        get_state,
        list_apps,
        restore_app,
        serialize_app,
        serialize_apps,
        serialize_state,
        update_app,
        update_state,
    )
    from brain.systems.workspace_apps.events import publish_workspace_app_change

    org_id, user_id = _workspace_app_context()
    if not org_id:
        return json.dumps({"error": "manage_workspace_app requires an org-scoped run"})

    actor_id = str(user_id) if user_id else None

    try:
        with UnitOfWork() as uow:
            if action == "list":
                apps = serialize_apps(
                    uow.session,
                    list_apps(
                        uow.session,
                        org_id,
                        include_archived=include_archived,
                        include_prototypes=include_prototypes,
                    ),
                )
                return json.dumps({"apps": apps}, default=str)

            if action == "create":
                if not name:
                    return json.dumps({"error": "create requires: name"})
                compiled = compile_workspace_app_input(
                    action="create",
                    name=name,
                    key=key,
                    renderer_key=renderer_key,
                    source_kind=source_kind,
                    source_code=source_code,
                    manifest=manifest,
                    visual_spec=visual_spec,
                    metadata=metadata,
                    initial_state=initial_state,
                )
                app = create_app(
                    uow.session,
                    org_id=org_id,
                    key=key,
                    name=name,
                    description=description,
                    renderer_key=compiled.renderer_key,
                    source_kind=compiled.source_kind,
                    source_code=compiled.source_code,
                    manifest=compiled.manifest or {},
                    visual_spec=compiled.visual_spec or {},
                    metadata=compiled.metadata or {},
                    created_by_user_id=actor_id,
                    anchor_user_id=anchor_user_id or actor_id,
                    initial_state=initial_state,
                    state_key=state_key,
                )
                serialized = serialize_app(uow.session, app)
                uow.commit()
                publish_workspace_app_change(org_id=org_id, action="create", app=serialized)
                payload = {"app": serialized}
                if compiled.repairs:
                    payload["compiler_repairs"] = list(compiled.repairs)
                return json.dumps(payload, default=str)

            if action == "get":
                app = get_app(uow.session, org_id, app_id, key=key, include_archived=include_archived)
                return json.dumps({"app": serialize_app(uow.session, app)}, default=str)

            if action == "update":
                if not app_id and not key:
                    return json.dumps({"error": "update requires: app_id or key"})
                compiled = compile_workspace_app_input(
                    action="update",
                    name=name,
                    key=key,
                    renderer_key=renderer_key,
                    source_kind=source_kind,
                    source_code=source_code,
                    manifest=manifest,
                    visual_spec=visual_spec,
                    metadata=metadata,
                )
                app = update_app(
                    uow.session,
                    org_id=org_id,
                    app_id=app_id,
                    key=key,
                    name=name,
                    description=description,
                    renderer_key=compiled.renderer_key,
                    source_kind=compiled.source_kind,
                    source_code=compiled.source_code,
                    manifest=compiled.manifest,
                    visual_spec=compiled.visual_spec,
                    metadata=compiled.metadata,
                    anchor_user_id=anchor_user_id,
                    updated_by_user_id=actor_id,
                )
                serialized = serialize_app(uow.session, app)
                uow.commit()
                publish_workspace_app_change(org_id=org_id, action="update", app=serialized)
                payload = {"app": serialized}
                if compiled.repairs:
                    payload["compiler_repairs"] = list(compiled.repairs)
                return json.dumps(payload, default=str)

            if action == "archive":
                if not app_id and not key:
                    return json.dumps({"error": "archive requires: app_id or key"})
                result = archive_app(uow.session, org_id=org_id, app_id=app_id, key=key)
                uow.commit()
                archived = result.get("archived", {})
                publish_workspace_app_change(
                    org_id=org_id,
                    action="archive",
                    app_id=archived.get("id") or app_id,
                    key=archived.get("key") or key,
                )
                return json.dumps(result, default=str)

            if action == "restore":
                if not app_id and not key:
                    return json.dumps({"error": "restore requires: app_id or key"})
                if not confirm_restore_archived:
                    return json.dumps(
                        {
                            "error": (
                                "restore requires confirm_restore_archived=true. "
                                "Use restore only when the user explicitly asks to restore an archived app; "
                                "for build/create requests, create a new app or update an active app instead."
                            )
                        }
                    )
                app = restore_app(uow.session, org_id=org_id, app_id=app_id, key=key)
                serialized = serialize_app(uow.session, app)
                uow.commit()
                publish_workspace_app_change(org_id=org_id, action="restore", app=serialized)
                return json.dumps({"app": serialized}, default=str)

            if action == "get_state":
                if not app_id:
                    return json.dumps({"error": "get_state requires: app_id"})
                state = get_state(
                    uow.session,
                    org_id=org_id,
                    app_id=app_id,
                    key=state_key,
                    user_id=actor_id,
                )
                return json.dumps({"state": serialize_state(state)}, default=str)

            if action == "update_state":
                if not app_id:
                    return json.dumps({"error": "update_state requires: app_id"})
                state = update_state(
                    uow.session,
                    org_id=org_id,
                    app_id=app_id,
                    key=state_key,
                    data=data,
                    data_patch=data_patch,
                    user_id=actor_id,
                )
                return json.dumps({"state": serialize_state(state)}, default=str)

            return json.dumps({"error": f"Unknown action: {action}"})
    except WorkspaceAppContractError as exc:
        return json.dumps(
            {
                "error": str(exc),
                "contract_validation": exc.report,
                "repair_guidance": contract_repair_guidance(exc.report),
            }
        )
    except WorkspaceAppError as exc:
        payload = {"error": str(exc)}
        guidance = service_error_guidance(str(exc))
        if guidance:
            payload["repair_guidance"] = guidance
        return json.dumps(payload)
    except Exception as exc:
        logger.exception("manage_workspace_app failed: %s", exc)
        return json.dumps({"error": str(exc)})


__all__ = [name for name in globals() if not name.startswith("__")]
