"""Typed action-manifest audit payloads and persistence helpers."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from functools import wraps
from hashlib import sha256
import asyncio
import json
import logging
import os
import inspect
from typing import Any, Callable

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    field_validator,
    model_validator,
)

from brain.systems.runs.tool_catalog.metadata import ActionPolicyResult
from brain.systems.runs.tool_catalog.registry import action_policy_for_tool

logger = logging.getLogger("agent")


_ACTION_POLICY_MODE_ENV = "AGENT_ACTION_POLICY_MODE"
_LEGACY_ACTION_POLICY_MODE_ENV = "ILLO_ACTION_POLICY_MODE"
_PERMISSIVE_AUDIT_MODE = "permissive_audit"
_ENFORCE_MODE = "enforce"
_REQUIRE_APPROVAL_MODE = "require_approval"
_BLOCKING_POLICY_RESULTS = {
    ActionPolicyResult.DENY.value,
}


class ActionPolicyDecision(BaseModel):
    """Concrete decision for a side-effecting action invocation."""

    model_config = ConfigDict(extra="forbid")

    result: ActionPolicyResult
    mode: str = _PERMISSIVE_AUDIT_MODE
    risk: str
    reversibility: str
    expected_effect: str
    reason: str
    approval_required: bool = False
    approval_requirement: str = "not_required_permissive_audit"

    @property
    def should_invoke_handler(self) -> bool:
        return self.result == ActionPolicyResult.ALLOW_AUDIT

    def to_manifest_fields(self) -> dict[str, Any]:
        return {
            "risk": self.risk,
            "reversibility": self.reversibility,
            "expected_effect": self.expected_effect,
            "approval_required": self.approval_required,
            "approval_requirement": self.approval_requirement,
            "policy_result": self.result.value,
            "policy_mode": self.mode,
        }


def current_action_policy_mode() -> str:
    """Return the rollout mode for tool action policy enforcement."""
    raw_mode = (
        os.environ.get(_ACTION_POLICY_MODE_ENV)
        or os.environ.get(_LEGACY_ACTION_POLICY_MODE_ENV)
        or _default_action_policy_mode()
    )
    return _normalize_action_policy_mode(raw_mode)


def _default_action_policy_mode() -> str:
    runtime_env = (
        os.environ.get("APP_ENV")
        or os.environ.get("ENV")
        or os.environ.get("ILLO_ENV")
        or os.environ.get("ENVIRONMENT")
        or ""
    ).strip().lower()
    if os.environ.get("PYTEST_CURRENT_TEST") or runtime_env in {
        "dev",
        "development",
        "local",
        "test",
        "testing",
    }:
        return _PERMISSIVE_AUDIT_MODE
    return _ENFORCE_MODE


def _normalize_action_policy_mode(value: Any) -> str:
    normalized = str(value or "").strip().lower().replace("-", "_")
    if normalized in {"", "audit", "allow_audit", "local", "dev", "development", "permissive"}:
        return _PERMISSIVE_AUDIT_MODE
    if normalized in {_PERMISSIVE_AUDIT_MODE, _ENFORCE_MODE, "enforced", "strict"}:
        return _ENFORCE_MODE if normalized in {"enforced", "strict"} else normalized
    if normalized in {"approval", "approvals", "require_approval", "approval_required"}:
        return _REQUIRE_APPROVAL_MODE
    logger.warning("Unknown %s=%r; falling back to enforced policy", _ACTION_POLICY_MODE_ENV, value)
    return _ENFORCE_MODE


def evaluate_action_policy(
    tool_name: str,
    args: tuple = (),
    kwargs: Mapping[str, Any] | None = None,
    *,
    mode: str | None = None,
) -> ActionPolicyDecision | None:
    """Evaluate the concrete action policy for a side-effecting tool call."""
    policy = action_policy_for_tool(tool_name, args=args, kwargs=dict(kwargs or {}))
    if policy is None:
        return None

    policy_mode = _normalize_action_policy_mode(mode or current_action_policy_mode())
    risk = str(policy.get("risk") or "medium").strip() or "medium"
    reversibility = str(policy.get("reversibility") or "variable").strip() or "variable"
    expected_effect = str(policy.get("expected_effect") or tool_name).strip() or tool_name
    explicit_result = str(policy.get("default_result") or "").strip()
    deny_reason = str(policy.get("deny_reason") or "").strip()

    if policy_mode == _PERMISSIVE_AUDIT_MODE:
        return ActionPolicyDecision(
            result=ActionPolicyResult.ALLOW_AUDIT,
            mode=policy_mode,
            risk=risk,
            reversibility=reversibility,
            expected_effect=expected_effect,
            reason=(
                "permissive audit mode allowed an action that would otherwise be denied"
                if explicit_result == ActionPolicyResult.DENY.value
                else "permissive audit mode"
            ),
            approval_required=False,
            approval_requirement="not_required_permissive_audit",
        )

    if explicit_result == ActionPolicyResult.DENY.value or deny_reason:
        return ActionPolicyDecision(
            result=ActionPolicyResult.DENY,
            mode=policy_mode,
            risk=risk,
            reversibility=reversibility,
            expected_effect=expected_effect,
            reason=deny_reason or "action denied by policy",
            approval_required=False,
            approval_requirement="denied_by_policy",
        )

    if policy_mode == _REQUIRE_APPROVAL_MODE or (policy_mode == _ENFORCE_MODE and risk == "high"):
        return ActionPolicyDecision(
            result=ActionPolicyResult.ALLOW_AUDIT,
            mode=policy_mode,
            risk=risk,
            reversibility=reversibility,
            expected_effect=expected_effect,
            reason="high-risk action allowed with audit",
            approval_required=False,
            approval_requirement="not_required_autonomous_policy",
        )

    return ActionPolicyDecision(
        result=ActionPolicyResult.ALLOW_AUDIT,
        mode=policy_mode,
        risk=risk,
        reversibility=reversibility,
        expected_effect=expected_effect,
        reason="action allowed by policy",
        approval_required=False,
        approval_requirement="not_required_by_policy",
    )


class ActionTarget(RootModel[dict[str, Any]]):
    """JSON-shaped description of the concrete target a tool will affect."""

    @model_validator(mode="before")
    @classmethod
    def _coerce_mapping(cls, value: Any) -> Any:
        if isinstance(value, ActionTarget):
            return value.root
        if not isinstance(value, Mapping):
            raise TypeError("ActionTarget must be a mapping")
        return dict(value)

    def to_payload(self) -> dict[str, Any]:
        return self.model_dump()


class ActionManifestCreate(BaseModel):
    """Validated payload used to create an action-manifest audit row."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    trace_id: str | None = None
    actor: str
    actor_id: str | None = None
    actor_kind: str = "agent"
    org_id: str | None = None
    run_id: int | None = None
    tool_name: str
    target: ActionTarget
    risk: str
    reversibility: str
    expected_effect: str
    approval_required: bool = False
    approval_requirement: str = "not_required_permissive_audit"
    idempotency_key: str
    policy_result: str = ActionPolicyResult.ALLOW_AUDIT.value
    policy_mode: str = "permissive_audit"
    outcome_status: str = "started"
    outcome_error: str | None = None
    completed_at: datetime | None = None
    metadata_: dict[str, Any] = Field(
        default_factory=dict,
        validation_alias=AliasChoices("metadata_", "metadata"),
    )

    @field_validator(
        "actor",
        "actor_kind",
        "tool_name",
        "risk",
        "reversibility",
        "expected_effect",
        "approval_requirement",
        "idempotency_key",
        "policy_result",
        "policy_mode",
        "outcome_status",
    )
    @classmethod
    def _non_empty_string(cls, value: str) -> str:
        normalized = str(value).strip()
        if not normalized:
            raise ValueError("must be a non-empty string")
        return normalized

    @field_validator("idempotency_key")
    @classmethod
    def _sha256_key(cls, value: str) -> str:
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise ValueError("must be a 64-character lowercase sha256 hex digest")
        return value

    def to_db_values(self) -> dict[str, Any]:
        """Return values shaped for the existing ActionManifest SQLAlchemy model."""
        values = self.model_dump()
        if values.get("outcome_error") is None:
            values.pop("outcome_error", None)
        if values.get("completed_at") is None:
            values.pop("completed_at", None)
        return values


def _arg_at(args: tuple, kwargs: Mapping[str, Any], name: str, index: int, default=None):
    if name in kwargs:
        return kwargs[name]
    if len(args) > index:
        return args[index]
    return default


def _json_safe(value, *, max_string: int = 500):
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value) <= max_string:
            return value
        return f"{value[:max_string]}... (truncated, {len(value)} chars total)"
    if isinstance(value, (list, tuple)):
        return [_json_safe(item, max_string=max_string) for item in value[:20]]
    if isinstance(value, dict):
        return {
            str(key): _json_safe(val, max_string=max_string)
            for key, val in list(value.items())[:40]
        }
    return str(value)[:max_string]


def _byte_len(value) -> int:
    return len(str(value or "").encode())


def build_action_target(
    tool_name: str,
    args: tuple = (),
    kwargs: Mapping[str, Any] | None = None,
    *,
    context: Mapping[str, Any] | None = None,
) -> ActionTarget:
    """Build the typed target payload for a concrete side-effecting tool call."""
    kwargs_dict = dict(kwargs or {})
    context_dict = dict(context or {})
    if tool_name == "exec_command":
        return ActionTarget({
            "command": _json_safe(_arg_at(args, kwargs_dict, "command", 0, "")),
            "working_dir": _json_safe(_arg_at(args, kwargs_dict, "working_dir", 1)),
            "workspace": _json_safe(
                _arg_at(args, kwargs_dict, "workspace", 3)
                or kwargs_dict.get("_workspace")
            ),
        })
    if tool_name == "run_script":
        script = _arg_at(args, kwargs_dict, "script", 0, "")
        return ActionTarget({
            "description": _json_safe(_arg_at(args, kwargs_dict, "description", 1)),
            "script_chars": len(str(script or "")),
            "workspace": _json_safe(
                _arg_at(args, kwargs_dict, "workspace", 3)
                or kwargs_dict.get("_workspace")
            ),
        })
    if tool_name == "write_file":
        content = _arg_at(args, kwargs_dict, "content", 1, "")
        return ActionTarget({
            "path": _json_safe(_arg_at(args, kwargs_dict, "path", 0, "")),
            "content_bytes": _byte_len(content),
            "workspace": _json_safe(kwargs_dict.get("_workspace") or kwargs_dict.get("workspace")),
        })
    if tool_name == "edit_file":
        return ActionTarget({
            "path": _json_safe(_arg_at(args, kwargs_dict, "path", 0, "")),
            "old_text_bytes": _byte_len(_arg_at(args, kwargs_dict, "old_text", 1, "")),
            "new_text_bytes": _byte_len(_arg_at(args, kwargs_dict, "new_text", 2, "")),
            "workspace": _json_safe(kwargs_dict.get("_workspace") or kwargs_dict.get("workspace")),
        })
    if tool_name == "brain_encode":
        return ActionTarget({
            "memory_type": _json_safe(kwargs_dict.get("memory_type") or kwargs_dict.get("type")),
            "content_chars": len(str(
                kwargs_dict.get("content")
                or _arg_at(args, kwargs_dict, "content", 0, "")
                or ""
            )),
            "visibility": _json_safe(kwargs_dict.get("visibility")),
        })
    if tool_name == "browser":
        return ActionTarget({
            "action": _json_safe(_arg_at(args, kwargs_dict, "action", 0)),
            "url": _json_safe(kwargs_dict.get("url") or _arg_at(args, kwargs_dict, "url", 2)),
            "selector": _json_safe(kwargs_dict.get("selector") or _arg_at(args, kwargs_dict, "selector", 8)),
            "index": _json_safe(kwargs_dict.get("index") or _arg_at(args, kwargs_dict, "index", 14)),
            "argument_keys": sorted(kwargs_dict.keys()),
        })
    if tool_name.startswith("browser_"):
        return ActionTarget({
            "url": _json_safe(kwargs_dict.get("url") or _arg_at(args, kwargs_dict, "url", 0)),
            "selector": _json_safe(
                kwargs_dict.get("selector")
                or _arg_at(args, kwargs_dict, "selector", 0)
            ),
            "index": _json_safe(kwargs_dict.get("index") or _arg_at(args, kwargs_dict, "index", 0)),
            "argument_keys": sorted(kwargs_dict.keys()),
        })
    if tool_name in {"web_fetch", "web_search"}:
        return ActionTarget({
            "url": _json_safe(kwargs_dict.get("url") or _arg_at(args, kwargs_dict, "url", 0)),
            "query": _json_safe(kwargs_dict.get("query") or _arg_at(args, kwargs_dict, "query", 0)),
            "provider": _json_safe(kwargs_dict.get("provider")),
        })
    if tool_name in {"cortex_reply", "cortex_visual_reply"}:
        content = kwargs_dict.get("content") or _arg_at(args, kwargs_dict, "content", 0, "")
        return ActionTarget({
            "idea_id": _json_safe(context_dict.get("idea_id")),
            "content_chars": len(str(content or "")),
            "title": _json_safe(kwargs_dict.get("title")),
            "content_type": _json_safe(kwargs_dict.get("content_type")),
        })
    if tool_name in {"manage_cycle", "manage_cron_job"}:
        return ActionTarget({
            "action": _json_safe(_arg_at(args, kwargs_dict, "action", 0)),
            "name": _json_safe(kwargs_dict.get("name") or _arg_at(args, kwargs_dict, "name", 1)),
            "schedule": _json_safe(kwargs_dict.get("schedule") or kwargs_dict.get("schedule_expr")),
            "cycle_id": _json_safe(kwargs_dict.get("id")),
        })
    if tool_name == "manage_skill":
        procedure = kwargs_dict.get("procedure")
        content = kwargs_dict.get("content")
        assets = kwargs_dict.get("assets")
        return ActionTarget({
            "action": _json_safe(_arg_at(args, kwargs_dict, "action", 0)),
            "skill_id": _json_safe(kwargs_dict.get("skill_id")),
            "skill_name": _json_safe(kwargs_dict.get("skill_name")),
            "name": _json_safe(kwargs_dict.get("name")),
            "asset_path": _json_safe(kwargs_dict.get("path")),
            "procedure_chars": len(str(procedure or "")),
            "content_chars": len(str(content or "")),
            "asset_count": len(assets) if isinstance(assets, list) else None,
            "create_as_package": bool(kwargs_dict.get("create_as_package")),
        })
    if tool_name == "manage_soul":
        content = kwargs_dict.get("content") or _arg_at(args, kwargs_dict, "content", 1, "")
        reason = kwargs_dict.get("reason") or _arg_at(args, kwargs_dict, "reason", 2, "")
        return ActionTarget({
            "action": _json_safe(_arg_at(args, kwargs_dict, "action", 0)),
            "content_chars": len(str(content or "")),
            "has_reason": bool(reason),
        })
    if tool_name == "manage_workspace_app":
        source_code = kwargs_dict.get("source_code")
        manifest = kwargs_dict.get("manifest") or {}
        thumbnail = (kwargs_dict.get("visual_spec") or {}).get("thumbnail")
        return ActionTarget({
            "action": _json_safe(_arg_at(args, kwargs_dict, "action", 0)),
            "app_id": _json_safe(kwargs_dict.get("app_id")),
            "key": _json_safe(kwargs_dict.get("key")),
            "name": _json_safe(kwargs_dict.get("name")),
            "renderer_key": _json_safe(kwargs_dict.get("renderer_key")),
            "source_kind": _json_safe(kwargs_dict.get("source_kind")),
            "source_code_chars": len(str(source_code or "")),
            "state_key": _json_safe(kwargs_dict.get("state_key")),
            "contract_version": _json_safe(manifest.get("contract_version") if isinstance(manifest, dict) else None),
            "data_mode": _json_safe(
                manifest.get("data_plan", {}).get("mode")
                if isinstance(manifest, dict) and isinstance(manifest.get("data_plan"), dict)
                else None
            ),
            "thumbnail_kind": _json_safe(
                "structured"
                if isinstance(thumbnail, dict) and not (thumbnail.get("source_code") or thumbnail.get("html"))
                else "legacy_html"
                if isinstance(thumbnail, dict) and (thumbnail.get("source_code") or thumbnail.get("html"))
                else "string"
                if isinstance(thumbnail, str)
                else None
            ),
            "has_thumbnail_source": bool(isinstance(thumbnail, dict) and thumbnail.get("source_code")),
        })
    if tool_name == "test_runner":
        return ActionTarget({
            "target": _json_safe(
                kwargs_dict.get("target")
                or _arg_at(args, kwargs_dict, "target", 0)
            ),
            "pattern": _json_safe(kwargs_dict.get("pattern")),
        })
    return ActionTarget({
        "argument_keys": sorted(kwargs_dict.keys()),
        "args_count": len(args),
    })


def build_action_manifest(
    tool_name: str,
    args: tuple = (),
    kwargs: Mapping[str, Any] | None = None,
    *,
    context: Mapping[str, Any] | None = None,
) -> ActionManifestCreate | None:
    """Build and validate an action-manifest create payload for a tool call."""
    kwargs_dict = dict(kwargs or {})
    context_dict = {
        "actor": "agent",
        "actor_id": None,
        "actor_kind": "agent",
        "org_id": None,
        "run_id": None,
        "trace_id": None,
        "worker_name": "agent",
        "idea_id": None,
        **dict(context or {}),
    }
    decision = evaluate_action_policy(tool_name, args=args, kwargs=kwargs_dict)
    if decision is None:
        return None

    target = build_action_target(tool_name, args, kwargs_dict, context=context_dict)
    key_payload = {
        "actor": context_dict["actor"],
        "run_id": context_dict["run_id"],
        "tool_name": tool_name,
        "target": target.to_payload(),
    }
    idempotency_key = sha256(
        json.dumps(key_payload, sort_keys=True, default=str).encode()
    ).hexdigest()

    return ActionManifestCreate.model_validate({
        "actor": context_dict["actor"],
        "actor_id": context_dict["actor_id"],
        "actor_kind": context_dict["actor_kind"],
        "org_id": context_dict["org_id"],
        "run_id": context_dict["run_id"],
        "trace_id": context_dict["trace_id"],
        "tool_name": tool_name,
        "target": target,
        **decision.to_manifest_fields(),
        "idempotency_key": idempotency_key,
        "outcome_status": "started",
        "metadata_": {
            "run_id": context_dict["run_id"],
            "worker_name": context_dict["worker_name"],
            "argument_keys": sorted(kwargs_dict.keys()),
            "args_count": len(args),
            "policy_reason": decision.reason,
        },
    })


def _coerce_manifest_create(
    manifest: ActionManifestCreate | Mapping[str, Any],
) -> ActionManifestCreate:
    if isinstance(manifest, ActionManifestCreate):
        return manifest
    return ActionManifestCreate.model_validate(manifest)


def _run_action_manifest_coro(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    return coro


async def _record_action_manifest_async(manifest: ActionManifestCreate | Mapping[str, Any] | None) -> int | None:
    """Persist a validated action manifest, returning its row id when recorded."""
    if not manifest:
        return None
    try:
        from brain.platform.db.models.run import ActionManifest
        from brain.platform.db.repositories.unit_of_work import UnitOfWork

        create = _coerce_manifest_create(manifest)
        async with UnitOfWork() as uow:
            row = ActionManifest.from_create(create)
            uow.session.add(row)
            await uow.session.flush()
            return row.id
    except Exception:
        logger.debug("Failed to record action manifest", exc_info=True)
        return None


def record_action_manifest(manifest: ActionManifestCreate | Mapping[str, Any] | None):
    return _run_action_manifest_coro(_record_action_manifest_async(manifest))


async def _complete_action_manifest_async(
    manifest_id: int | None,
    *,
    outcome_status: str,
    outcome_error: str | None = None,
) -> None:
    """Mark an action manifest complete without exposing DB details to handlers."""
    if not manifest_id:
        return
    try:
        from brain.platform.db.models.run import ActionManifest
        from brain.platform.db.repositories.unit_of_work import UnitOfWork

        async with UnitOfWork() as uow:
            row = await uow.session.get(ActionManifest, manifest_id)
            if not row:
                return
            row.outcome_status = outcome_status
            row.outcome_error = (outcome_error or "")[:2000] or None
            row.completed_at = datetime.now(timezone.utc)
    except Exception:
        logger.debug("Failed to complete action manifest", exc_info=True)


def complete_action_manifest(
    manifest_id: int | None,
    *,
    outcome_status: str,
    outcome_error: str | None = None,
):
    return _run_action_manifest_coro(
        _complete_action_manifest_async(
            manifest_id,
            outcome_status=outcome_status,
            outcome_error=outcome_error,
        )
    )


def result_failure_summary(result) -> str | None:
    payload = result
    if isinstance(result, str):
        try:
            payload = json.loads(result)
        except Exception:
            return None
    if isinstance(payload, dict):
        if payload.get("blocked") is True:
            return str(payload.get("error") or "blocked")
        if payload.get("error"):
            return str(payload.get("error"))
        exit_code = payload.get("exit_code")
        if isinstance(exit_code, int) and exit_code != 0:
            return str(payload.get("stderr") or f"exit_code={exit_code}")[:2000]
    return None


def action_manifest_blocks_handler(manifest: ActionManifestCreate | Mapping[str, Any] | None) -> bool:
    if not manifest:
        return False
    create = _coerce_manifest_create(manifest)
    return create.policy_result in _BLOCKING_POLICY_RESULTS


def blocked_action_result(
    manifest: ActionManifestCreate | Mapping[str, Any],
    *,
    manifest_id: int | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    create = _coerce_manifest_create(manifest)
    reason = str((create.metadata_ or {}).get("policy_reason") or create.approval_requirement)
    message = error or (
        f"Action denied by policy: {reason}"
        if create.policy_result == ActionPolicyResult.DENY.value
        else f"Action requires approval before execution: {reason}"
    )
    payload: dict[str, Any] = {
        "ok": False,
        "blocked": True,
        "error": message,
        "tool_name": create.tool_name,
        "policy_result": create.policy_result,
        "policy_mode": create.policy_mode,
        "approval_required": create.approval_required,
        "approval_requirement": create.approval_requirement,
        "risk": create.risk,
        "expected_effect": create.expected_effect,
    }
    if manifest_id:
        payload["action_manifest_id"] = manifest_id
    return payload


def wrap_action_manifest_audit(
    tool_name: str,
    handler: Callable[..., Any],
    *,
    context_factory: Callable[[], Mapping[str, Any]],
) -> Callable[..., Any]:
    """Wrap a side-effecting tool handler with action-manifest audit and policy gates."""
    if getattr(handler, "_action_manifest_audited", False):
        return handler

    @wraps(handler)
    async def _invoke(*args, **kwargs):
        manifest_id = None
        try:
            manifest = build_action_manifest(
                tool_name,
                args,
                kwargs,
                context=context_factory(),
            )
            if manifest:
                manifest_id = record_action_manifest(manifest)
                if inspect.isawaitable(manifest_id):
                    manifest_id = await manifest_id
            if action_manifest_blocks_handler(manifest):
                blocked_result = blocked_action_result(manifest, manifest_id=manifest_id)
                if manifest_id:
                    completion = complete_action_manifest(
                        manifest_id,
                        outcome_status="failed",
                        outcome_error=blocked_result["error"],
                    )
                    if inspect.isawaitable(completion):
                        await completion
                return blocked_result
        except Exception:
            logger.debug("Failed to build action manifest", exc_info=True)

        try:
            result = handler(*args, **kwargs)
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:
            if manifest_id:
                completion = complete_action_manifest(
                    manifest_id,
                    outcome_status="failed",
                    outcome_error=str(exc),
                )
                if inspect.isawaitable(completion):
                    await completion
            raise

        failure = result_failure_summary(result)
        if manifest_id:
            completion = complete_action_manifest(
                manifest_id,
                outcome_status="failed" if failure else "succeeded",
                outcome_error=failure,
            )
            if inspect.isawaitable(completion):
                await completion
        return result

    def wrapper(*args, **kwargs):
        awaitable = _invoke(*args, **kwargs)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            with asyncio.Runner() as runner:
                return runner.run(awaitable)
        return awaitable

    wrapper._action_manifest_audited = True
    return wrapper
