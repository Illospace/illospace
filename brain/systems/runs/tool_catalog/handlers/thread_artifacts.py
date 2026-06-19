"""Tool handler for thread-scoped interactive artifacts."""
from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from brain.systems.runs.tool_catalog.handlers.common import *


def _artifact_context() -> tuple[str | None, str | None, str | None]:
    execution_metadata = getattr(_agent_context, "execution_metadata", {}) or {}
    org_id = getattr(_agent_context, "org_id", None) or execution_metadata.get("org_id")
    user_id = getattr(_agent_context, "user_id", None) or execution_metadata.get("user_id")

    thread_id = getattr(_agent_context, "idea_id", None) or execution_metadata.get("idea_id")
    run = getattr(_agent_context, "run", None)
    if not thread_id and run is not None:
        thread_id = getattr(run, "thread_id", None)
    if not thread_id:
        target_ref = execution_metadata.get("target_ref")
        if isinstance(target_ref, Mapping):
            thread_id = target_ref.get("thread_id") or target_ref.get("idea_id")
    return (
        str(org_id) if org_id else None,
        str(user_id) if user_id else None,
        str(thread_id) if thread_id else None,
    )


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _optional_bool(value: Any, *, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
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


async def _handle_publish_thread_artifact(
    title: str,
    source_code: str,
    thread_id: str | None = None,
    description: str | None = None,
    artifact_kind: str | None = None,
    app_id: str | None = None,
    key: str | None = None,
    update_existing: bool = True,
    manifest: dict | None = None,
    visual_spec: dict | None = None,
    metadata: dict | None = None,
    initial_state: dict | None = None,
    **_ignored: Any,
) -> str:
    from brain.platform.db.repositories.unit_of_work import UnitOfWork
    from brain.systems.cortex.thread_artifacts import ThreadArtifactError, publish_thread_artifact_app
    from brain.systems.workspace_apps.events import publish_workspace_app_change
    from brain.systems.workspace_apps.service import WorkspaceAppContractError, WorkspaceAppError
    from brain.systems.workspace_apps.compiler import contract_repair_guidance, service_error_guidance

    org_id, user_id, current_thread_id = _artifact_context()
    target_thread_id = _optional_text(thread_id) or current_thread_id
    if not org_id:
        return json.dumps({"error": "publish_thread_artifact could not access this workspace context"})
    if not target_thread_id:
        return json.dumps({"error": "publish_thread_artifact requires thread_id when no current Thread is bound"})

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

    try:
        async with UnitOfWork() as uow:
            result = await publish_thread_artifact_app(
                uow.session,
                org_id=org_id,
                user_id=user_id,
                thread_id=target_thread_id,
                title=title,
                description=description,
                artifact_kind=artifact_kind,
                source_code=source_code,
                app_id=app_id,
                key=key,
                update_existing=_optional_bool(update_existing, default=True),
                manifest=manifest,
                visual_spec=visual_spec,
                metadata=metadata,
                initial_state=initial_state,
            )
            await uow.commit()
            publish_workspace_app_change(org_id=org_id, action=result["action"], app=result["app"])
            return json.dumps(result, default=str)
    except WorkspaceAppContractError as exc:
        return json.dumps(
            {
                "error": str(exc),
                "contract_validation": exc.report,
                "repair_guidance": contract_repair_guidance(exc.report),
            },
            default=str,
        )
    except WorkspaceAppError as exc:
        payload: dict[str, Any] = {"error": str(exc)}
        guidance = service_error_guidance(str(exc))
        if guidance:
            payload["repair_guidance"] = guidance
        return json.dumps(payload, default=str)
    except ThreadArtifactError as exc:
        return json.dumps({"error": str(exc)}, default=str)
    except Exception as exc:
        logger.exception("publish_thread_artifact failed: %s", exc)
        return json.dumps({"error": str(exc)}, default=str)


__all__ = [name for name in globals() if not name.startswith("__")]
