"""Server-side action runner for generated workspace apps.

Apps can declare workflow actions in their manifest and call them through the
iframe bridge. This module validates the declaration and dispatches only to
registered server executors; generated app code never receives credentials and
never runs arbitrary connector code in the browser.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Callable, Mapping

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from brain.platform.db.models.workspace_app import WorkspaceApp, WorkspaceAppVersion
from brain.systems.workspace_apps.contracts import ACTION_EFFECTS, ACTION_EXECUTOR_TYPES, ACTION_KINDS
from brain.systems.workspace_apps.service import (
    WorkspaceAppError,
    a_active_version,
    a_get_app,
    active_version,
    get_app,
)


class WorkspaceAppActionError(WorkspaceAppError):
    """Base error for app action execution."""


class WorkspaceAppActionNotDeclared(WorkspaceAppActionError):
    """Raised when an app calls an action not declared in its manifest."""


class WorkspaceAppActionExecutorMissing(WorkspaceAppActionError):
    """Raised when an action is declared but no approved executor can run it."""


class WorkspaceAppActionContractError(WorkspaceAppActionError):
    """Raised when an action declaration or payload violates action boundaries."""


@dataclass(frozen=True)
class WorkspaceAppActionContext:
    session: Session | AsyncSession
    org_id: str
    user_id: str | None
    app: WorkspaceApp
    version: WorkspaceAppVersion
    action_key: str
    declaration: dict[str, Any]


WorkspaceAppActionExecutor = Callable[[WorkspaceAppActionContext, dict[str, Any]], Mapping[str, Any] | None]

_EXECUTORS: dict[str, WorkspaceAppActionExecutor] = {}
_BUILTIN_EXECUTOR_KEYS = {"generic.http"}
_SECRET_KEY_RE = re.compile(
    r"(?:^|[_-])(?:token|secret|password|api[_-]?key|authorization|bearer|client[_-]?secret|private[_-]?key)(?:$|[_-])",
    re.IGNORECASE,
)
_SECRET_VALUE_RE = re.compile(r"\b(?:ghp_|github_pat_|sk-|xox[baprs]-)[A-Za-z0-9_=-]{8,}")


def register_workspace_app_action_executor(key: str, executor: WorkspaceAppActionExecutor) -> None:
    """Register a product-owned executor for manifest-declared actions."""

    normalized = str(key or "").strip()
    if not normalized:
        raise ValueError("executor key is required")
    _EXECUTORS[normalized] = executor


def unregister_workspace_app_action_executor(key: str) -> None:
    _EXECUTORS.pop(str(key or "").strip(), None)


def action_declarations(manifest: Mapping[str, Any] | None) -> dict[str, Any]:
    manifest_dict = dict(manifest) if isinstance(manifest, Mapping) else {}
    actions = manifest_dict.get("actions")
    if actions is None:
        action_plan = manifest_dict.get("action_plan")
        if isinstance(action_plan, Mapping):
            actions = action_plan.get("actions")
    return dict(actions) if isinstance(actions, Mapping) else {}


def validate_action_declaration(action_key: str, declaration: Mapping[str, Any]) -> dict[str, Any]:
    action = dict(declaration) if isinstance(declaration, Mapping) else {}
    if not action:
        raise WorkspaceAppActionContractError(f"Workspace action '{action_key}' must be an object")

    kind = str(action.get("kind") or "connector").strip()
    if kind not in ACTION_KINDS:
        raise WorkspaceAppActionContractError(
            f"Workspace action '{action_key}' kind must be one of: {', '.join(sorted(ACTION_KINDS))}"
        )

    effects = [str(effect).strip() for effect in action.get("effects", []) if str(effect).strip()]
    if not effects:
        raise WorkspaceAppActionContractError(f"Workspace action '{action_key}' must declare effects")
    invalid_effects = sorted(set(effects) - ACTION_EFFECTS)
    if invalid_effects:
        raise WorkspaceAppActionContractError(
            f"Workspace action '{action_key}' declares unsupported effect(s): {', '.join(invalid_effects)}"
        )

    executor = action.get("executor")
    if executor is not None:
        executor_obj = dict(executor) if isinstance(executor, Mapping) else {}
        executor_type = str(executor_obj.get("type") or "").strip()
        if executor_type not in ACTION_EXECUTOR_TYPES:
            raise WorkspaceAppActionContractError(
                f"Workspace action '{action_key}' executor.type must be one of: {', '.join(sorted(ACTION_EXECUTOR_TYPES))}"
            )
        if executor_type == "registered" and not str(executor_obj.get("key") or "").strip():
            raise WorkspaceAppActionContractError(
                f"Workspace action '{action_key}' executor.key is required for registered executors"
            )

    forbidden = _forbidden_secret_paths(action)
    if forbidden:
        raise WorkspaceAppActionContractError(
            f"Workspace action '{action_key}' declaration contains secret-like field(s): {', '.join(forbidden)}"
        )

    return action


def run_workspace_app_action(
    session: Session,
    *,
    org_id: str,
    app_id: str,
    action_key: str,
    payload: Mapping[str, Any] | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    """Validate and execute one manifest-declared workspace app action."""

    normalized_key = str(action_key or "").strip()
    if not normalized_key:
        raise WorkspaceAppActionContractError("actions.run(actionKey) requires an action key")

    app = get_app(session, org_id=org_id, app_id=app_id)
    version = active_version(session, app.id)
    if version is None:
        raise WorkspaceAppActionContractError("Workspace app has no active version")

    actions = action_declarations(version.manifest or {})
    raw_declaration = actions.get(normalized_key)
    if raw_declaration is None:
        raise WorkspaceAppActionNotDeclared(f"Workspace action '{normalized_key}' is not declared in this app manifest")

    declaration = validate_action_declaration(normalized_key, raw_declaration)
    payload_dict = dict(payload) if isinstance(payload, Mapping) else {}
    forbidden_payload_paths = _forbidden_secret_paths(payload_dict)
    if forbidden_payload_paths:
        raise WorkspaceAppActionContractError(
            f"Workspace action '{normalized_key}' payload must not contain raw credentials: "
            + ", ".join(forbidden_payload_paths)
        )

    executor = declaration.get("executor")
    executor_obj = dict(executor) if isinstance(executor, Mapping) else {}
    executor_type = str(executor_obj.get("type") or "deferred").strip()
    executor_key = str(executor_obj.get("key") or "").strip()
    if executor_type != "registered" or not executor_key:
        raise WorkspaceAppActionExecutorMissing(_missing_executor_message(normalized_key))

    registered = _EXECUTORS.get(executor_key) or _builtin_executor(executor_key)
    if registered is None:
        raise WorkspaceAppActionExecutorMissing(
            f"Workspace action '{normalized_key}' uses executor '{executor_key}', but no approved executor is registered."
        )

    context = WorkspaceAppActionContext(
        session=session,
        org_id=org_id,
        user_id=user_id,
        app=app,
        version=version,
        action_key=normalized_key,
        declaration=declaration,
    )
    result = registered(context, payload_dict) or {}
    return {
        "ok": True,
        "action_key": normalized_key,
        "status": "completed",
        "effects": [str(effect) for effect in declaration.get("effects", [])],
        "connector_keys": _connector_keys(declaration),
        "result": dict(result) if isinstance(result, Mapping) else {"value": result},
    }


async def async_run_workspace_app_action(
    session: AsyncSession,
    *,
    org_id: str,
    app_id: str,
    action_key: str,
    payload: Mapping[str, Any] | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    """Validate and execute one manifest-declared workspace app action."""

    normalized_key = str(action_key or "").strip()
    if not normalized_key:
        raise WorkspaceAppActionContractError("actions.run(actionKey) requires an action key")

    app = await a_get_app(session, org_id=org_id, app_id=app_id)
    version = await a_active_version(session, app.id)
    if version is None:
        raise WorkspaceAppActionContractError("Workspace app has no active version")

    actions = action_declarations(version.manifest or {})
    raw_declaration = actions.get(normalized_key)
    if raw_declaration is None:
        raise WorkspaceAppActionNotDeclared(f"Workspace action '{normalized_key}' is not declared in this app manifest")

    declaration = validate_action_declaration(normalized_key, raw_declaration)
    payload_dict = dict(payload) if isinstance(payload, Mapping) else {}
    forbidden_payload_paths = _forbidden_secret_paths(payload_dict)
    if forbidden_payload_paths:
        raise WorkspaceAppActionContractError(
            f"Workspace action '{normalized_key}' payload must not contain raw credentials: "
            + ", ".join(forbidden_payload_paths)
        )

    executor = declaration.get("executor")
    executor_obj = dict(executor) if isinstance(executor, Mapping) else {}
    executor_type = str(executor_obj.get("type") or "deferred").strip()
    executor_key = str(executor_obj.get("key") or "").strip()
    if executor_type != "registered" or not executor_key:
        raise WorkspaceAppActionExecutorMissing(_missing_executor_message(normalized_key))

    registered = _EXECUTORS.get(executor_key)
    if registered is None:
        raise WorkspaceAppActionExecutorMissing(
            f"Workspace action '{normalized_key}' uses executor '{executor_key}', but no approved executor is registered."
        )

    context = WorkspaceAppActionContext(
        session=session,
        org_id=org_id,
        user_id=user_id,
        app=app,
        version=version,
        action_key=normalized_key,
        declaration=declaration,
    )
    result = registered(context, payload_dict) or {}
    return {
        "ok": True,
        "action_key": normalized_key,
        "status": "completed",
        "effects": [str(effect) for effect in declaration.get("effects", [])],
        "connector_keys": _connector_keys(declaration),
        "result": dict(result) if isinstance(result, Mapping) else {"value": result},
    }


def _missing_executor_message(action_key: str) -> str:
    return (
        f"Workspace action '{action_key}' is declared, but no server-side action executor is registered yet. "
        "Use Domain APIs in-app, or add an approved connector/action executor for external systems."
    )


def _builtin_executor(key: str) -> WorkspaceAppActionExecutor | None:
    if key not in _BUILTIN_EXECUTOR_KEYS:
        return None
    from brain.systems.workspace_apps.generic_http import execute_generic_http_action

    return execute_generic_http_action


def _connector_keys(declaration: Mapping[str, Any]) -> list[str]:
    connectors = declaration.get("connectors")
    if not isinstance(connectors, list):
        return []
    keys: list[str] = []
    for connector in connectors:
        if isinstance(connector, str) and connector.strip():
            keys.append(connector.strip())
        elif isinstance(connector, Mapping):
            key = str(connector.get("key") or connector.get("provider") or "").strip()
            if key:
                keys.append(key)
    return keys


def _forbidden_secret_paths(value: Any, prefix: str = "") -> list[str]:
    paths: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key)
            path = f"{prefix}.{key_text}" if prefix else key_text
            if _SECRET_KEY_RE.search(key_text):
                paths.append(path)
            paths.extend(_forbidden_secret_paths(item, path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            paths.extend(_forbidden_secret_paths(item, f"{prefix}[{index}]"))
    elif isinstance(value, str) and _SECRET_VALUE_RE.search(value):
        paths.append(prefix or "<value>")
    return paths


__all__ = [
    "WorkspaceAppActionContractError",
    "WorkspaceAppActionContext",
    "WorkspaceAppActionError",
    "WorkspaceAppActionExecutorMissing",
    "WorkspaceAppActionNotDeclared",
    "async_run_workspace_app_action",
    "register_workspace_app_action_executor",
    "run_workspace_app_action",
    "unregister_workspace_app_action_executor",
]
