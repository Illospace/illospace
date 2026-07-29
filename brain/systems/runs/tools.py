"""Tool execution with run-native events and artifacts."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
import fnmatch
import inspect
import json
import os
from typing import Any

from brain.platform.async_io import (
    BlockingInvocationCancelled,
    callable_uses_blocking_thread,
    invoke_maybe_async,
    mark_side_effect_started,
)
from brain.systems.runs.actions import (
    blocked_action_result,
    build_action_manifest,
    complete_action_manifest,
    record_action_manifest,
    result_failure_summary,
)
from brain.systems.runs.domain import AgentRunArtifact, ArtifactType, EventVisibility
from brain.systems.runs.events import activity_event, redact_tool_call_result, run_event
from brain.systems.runs.execution_context import bind_agent_context, current_agent_context
from brain.systems.runs.secret_mounts import (
    handler_args_with_resolved_secret_env,
    resolve_secret_env_mounts,
)
from brain.systems.runs.store import AsyncAgentRunStore
from brain.systems.runs.tool_catalog.metadata import ActionPolicyResult, is_write_side_effect_class
from brain.systems.runs.tool_catalog.registry import get_tool_registration
from brain.systems.runs.workspace_tool_runtime import (
    handler_args_with_resolved_workspace_tool_runtime,
    resolve_workspace_tool_runtime,
)


SECRET_TOOL_NAMES = frozenset({"brain_vault", "vault", "secrets"})
SENSITIVE_ARG_PARTS = ("api_key", "apikey", "authorization", "cookie", "password", "secret", "token")
FILE_OBSERVATION_TOOLS = frozenset({"file_summary", "list_files", "read_file", "search_files", "semantic_search"})
FILE_EDIT_TOOLS = frozenset({"apply_patch", "edit_file", "write_file"})
COMMAND_OUTPUT_TOOLS = frozenset({"exec_command", "run_script", "test_runner"})
CHAT_MESSAGE_TOOLS = frozenset({
    "post_chat_message",
    "post_slack_reply",
    "react_to_slack_message",
    "post_thread_discussion_reply",
    "post_ai_timeline_message",
})


async def _maybe_await(value):
    if inspect.isawaitable(value):
        return await value
    return value


def _event_stream_payload(event, row) -> dict[str, Any]:
    payload = dict(event.payload or {})
    # Backend-only attribution channel; live stream consumers never see it
    # (ref ids are raw result content with no redaction pass).
    payload.pop("result_refs", None)
    event_id = int(getattr(row, "id", 0) or 0)
    payload.update({
        "run_id": int(event.run_id),
        "root_run_id": int(getattr(row, "root_run_id", None) or event.root_run_id or event.run_id),
        "event_id": event_id,
        "run_event_id": event_id,
        "event_cursor": event_id,
        "sequence_no": int(getattr(row, "sequence_no", 0) or event.sequence_no or 0),
    })
    return payload


def redact_tool_result(tool_name: str, result: Any) -> str:
    if _tool_is_secret(tool_name):
        return redact_tool_call_result(tool_name, result)
    return _result_to_text(result)


@dataclass(frozen=True)
class ToolExecution:
    name: str
    args: dict[str, Any]
    handler: Callable[..., Any]


@dataclass(frozen=True)
class ToolScope:
    """Optional file/resource scope enforced at the runtime tool boundary."""

    allowed_files: tuple[str, ...] = ()
    forbidden_files: tuple[str, ...] = ()
    allowed_resources: tuple[str, ...] = ()
    forbidden_resources: tuple[str, ...] = ()
    require_approval_for_out_of_scope_mutation: bool = True


@dataclass(frozen=True)
class ToolRecord:
    tool_name: str
    args: dict[str, str]
    status: str
    artifact_type: ArtifactType
    side_effect: str
    result_preview: str = ""
    error: str | None = None

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "tool_name": self.tool_name,
            "args": dict(self.args),
            "status": self.status,
            "artifact_type": self.artifact_type.value,
            "side_effect": self.side_effect,
        }
        if self.result_preview:
            payload["result_preview"] = self.result_preview
        if self.error:
            payload["error"] = self.error
        return payload


class ToolScopeViolation(PermissionError):
    """Raised when a worker tool call attempts work outside its declared scope."""


class AsyncRunToolExecutor:
    def __init__(self, store: AsyncAgentRunStore, *, stream: Any = None):
        self.store = store
        self.stream = stream

    async def execute(
        self,
        run_id: int,
        tool: ToolExecution,
        *,
        root_run_id: int | None = None,
        scope: ToolScope | None = None,
        collector: list[ToolRecord] | None = None,
    ) -> Any:
        safe_args = _safe_args(tool.args)
        artifact_type = artifact_type_for_tool(tool.name)
        side_effect = classify_side_effect(tool.name)
        await self._append_event(
            activity_event(
                run_id,
                tool_activity_label(tool.name, tool.args),
                root_run_id=root_run_id,
                tool_name=tool.name,
                side_effect=side_effect,
            )
        )
        await self._append_event(
            run_event(run_id, "run.tool_started", _event_payload(tool.name, safe_args), root_run_id=root_run_id)
        )
        if callable(getattr(self.store, "commit_event_boundary", None)):
            # The handler may use an isolated UnitOfWork that writes to this
            # same run (spawn_worker is the canonical example).  Make the
            # started marker durable and release the parent transaction's row
            # and advisory locks before invoking it.
            await self._commit_event_boundary(run_id)
        manifest = None
        manifest_id = None
        try:
            enforce_tool_scope(tool.name, tool.args, scope)
            action_context = await self._action_context(run_id, root_run_id=root_run_id)
            manifest = build_action_manifest(
                tool.name,
                (),
                tool.args,
                context=action_context,
            )
            manifest_id = await _maybe_await(record_action_manifest(manifest)) if manifest else None
            policy_result = await self._apply_action_policy_gate(
                run_id,
                manifest,
                manifest_id=manifest_id,
                safe_args=safe_args,
            )
            if policy_result is not None:
                return await self._record_policy_blocked_result(
                    run_id,
                    tool,
                    safe_args,
                    artifact_type=artifact_type,
                    side_effect=side_effect,
                    result=policy_result,
                    root_run_id=root_run_id,
                    collector=collector,
                )
            secret_env = await resolve_secret_env_mounts(
                tool.name,
                tool.args.get("secret_env"),
                run_id=run_id,
                context=action_context,
            )
            workspace_tool_runtime = await resolve_workspace_tool_runtime(
                tool.name,
                tool.args,
                run_id=run_id,
                context=action_context,
            )
            handler = _runtime_policy_handler(tool.handler)
            handler_args = handler_args_with_resolved_secret_env(tool.name, tool.args, secret_env)
            handler_args = handler_args_with_resolved_workspace_tool_runtime(
                tool.name,
                handler_args,
                workspace_tool_runtime,
            )
            try:
                with bind_agent_context(_handler_context_from_action_context(action_context)):
                    mark_side_effect_started()
                    result = await invoke_maybe_async(handler, **handler_args)
            finally:
                workspace_tool_runtime.cleanup()
        except BlockingInvocationCancelled as exc:
            failure = str(exc.error) if exc.error else result_failure_summary(exc.result)
            if manifest_id:
                await _maybe_await(
                    complete_action_manifest(
                        manifest_id,
                        outcome_status="failed" if failure else "succeeded",
                        outcome_error=failure,
                    )
                )
            if failure:
                await self._append_event(
                    run_event(
                        run_id,
                        "run.tool_failed",
                        _event_payload(tool.name, safe_args, error=failure),
                        root_run_id=root_run_id,
                    )
                )
                record = ToolRecord(
                    tool_name=tool.name,
                    args=safe_args,
                    status="failed",
                    artifact_type=artifact_type,
                    side_effect=side_effect,
                    error=failure[:1000],
                )
                await self._append_artifact(
                    run_id,
                    root_run_id=root_run_id,
                    artifact_type=artifact_type,
                    title=f"{tool.name} failed",
                    payload=record.to_payload(),
                    text=failure[:4000],
                )
            else:
                safe_result = redact_tool_result(tool.name, exc.result)
                await self._append_event(
                    run_event(
                        run_id,
                        "run.tool_completed",
                        _event_payload(tool.name, safe_args, result=safe_result),
                        root_run_id=root_run_id,
                    )
                )
                record = ToolRecord(
                    tool_name=tool.name,
                    args=safe_args,
                    status="completed",
                    artifact_type=artifact_type,
                    side_effect=side_effect,
                    result_preview=safe_result[:1000],
                )
                await self._append_artifact(
                    run_id,
                    root_run_id=root_run_id,
                    artifact_type=artifact_type,
                    title=tool.name,
                    payload=record.to_payload(),
                    text=safe_result[:4000],
                )
            if collector is not None:
                collector.append(record)
            await self._commit_event_boundary(run_id)
            raise
        except asyncio.CancelledError:
            if manifest_id:
                await _maybe_await(
                    complete_action_manifest(
                        manifest_id,
                        outcome_status="indeterminate",
                        outcome_error="caller canceled before a definitive action outcome",
                    )
                )
            raise
        except Exception as exc:
            error_text = str(exc)
            if manifest_id:
                await _maybe_await(
                    complete_action_manifest(
                        manifest_id,
                        outcome_status="failed",
                        outcome_error=error_text,
                    )
                )
            await self._append_event(
                run_event(
                    run_id,
                    "run.tool_failed",
                    _event_payload(tool.name, safe_args, error=error_text),
                    root_run_id=root_run_id,
                )
            )
            record = ToolRecord(
                tool_name=tool.name,
                args=safe_args,
                status="failed",
                artifact_type=artifact_type,
                side_effect=side_effect,
                error=error_text[:1000],
            )
            if collector is not None:
                collector.append(record)
            await self._append_artifact(
                run_id,
                root_run_id=root_run_id,
                artifact_type=artifact_type,
                title=f"{tool.name} failed",
                payload=record.to_payload(),
                text=error_text[:4000],
            )
            await self._commit_event_boundary(run_id)
            raise
        failure = result_failure_summary(result)
        if manifest_id:
            await _maybe_await(
                complete_action_manifest(
                    manifest_id,
                    outcome_status="failed" if failure else "succeeded",
                    outcome_error=failure,
                )
            )
        policy_failure = _blocked_action_failure_summary(result)
        if policy_failure:
            return await self._record_policy_blocked_result(
                run_id,
                tool,
                safe_args,
                artifact_type=artifact_type,
                side_effect=side_effect,
                result=result,
                root_run_id=root_run_id,
                collector=collector,
            )
        if failure:
            await self._append_event(
                run_event(
                    run_id,
                    "run.tool_failed",
                    _event_payload(tool.name, safe_args, error=failure),
                    root_run_id=root_run_id,
                )
            )
            record = ToolRecord(
                tool_name=tool.name,
                args=safe_args,
                status="failed",
                artifact_type=artifact_type,
                side_effect=side_effect,
                error=failure[:1000],
            )
            if collector is not None:
                collector.append(record)
            await self._append_artifact(
                run_id,
                root_run_id=root_run_id,
                artifact_type=artifact_type,
                title=f"{tool.name} failed",
                payload=record.to_payload(),
                text=failure[:4000],
            )
            await self._commit_event_boundary(run_id)
            return result
        safe_result = redact_tool_result(tool.name, result)
        await self._append_event(
            run_event(
                run_id,
                "run.tool_completed",
                _event_payload(tool.name, safe_args, result=safe_result),
                root_run_id=root_run_id,
            )
        )
        record = ToolRecord(
            tool_name=tool.name,
            args=safe_args,
            status="completed",
            artifact_type=artifact_type,
            side_effect=side_effect,
            result_preview=safe_result[:1000],
        )
        if collector is not None:
            collector.append(record)
        await self._append_artifact(
            run_id,
            root_run_id=root_run_id,
            artifact_type=artifact_type,
            title=tool.name,
            payload=record.to_payload(),
            text=safe_result[:4000],
        )
        # Make the completed marker and its artifact durable before the caller
        # can wait on another session that appends to this run. PostgreSQL's
        # event-stream advisory lock is transaction-scoped; returning with this
        # transaction open can deadlock terminal settlement behind an otherwise
        # successful tool call.
        await self._commit_event_boundary(run_id)
        return result

    async def _commit_event_boundary(self, run_id: int) -> None:
        commit_event_boundary = getattr(self.store, "commit_event_boundary", None)
        if callable(commit_event_boundary):
            await commit_event_boundary(run_id)

    async def _action_context(self, run_id: int, *, root_run_id: int | None = None) -> dict[str, Any]:
        require_run = getattr(self.store, "require_run", None)
        if not callable(require_run):
            raise RuntimeError("AgentRun context unavailable: store cannot load run")
        run = await require_run(run_id)
        org_id = str(getattr(run, "org_id", "") or "").strip()
        if not org_id:
            raise RuntimeError("AgentRun missing workspace org_id")
        user_id = str(getattr(run, "user_id", "") or "").strip() or None
        thread_id = str(getattr(run, "thread_id", "") or "").strip() or None
        recipe = str(getattr(run, "recipe", "") or "").strip() or "runtime"
        resolved_root_run_id = root_run_id if root_run_id is not None else getattr(run, "root_run_id", None)
        context: dict[str, Any] = {
            "actor": f"agent-run-{run_id}",
            "actor_id": user_id,
            "actor_kind": "agent",
            "org_id": org_id,
            "run_id": int(run_id),
            "trace_id": getattr(run, "trace_id", None),
            "worker_name": recipe,
            "idea_id": thread_id,
            "root_run_id": resolved_root_run_id,
            "target_ref": dict(getattr(run, "target_ref", None) or {}),
        }
        return context

    async def _apply_action_policy_gate(
        self,
        run_id: int,
        manifest,
        *,
        manifest_id: int | None,
        safe_args: dict[str, str],
    ) -> dict[str, Any] | None:
        if manifest is None:
            return None
        if manifest.policy_result == ActionPolicyResult.DENY.value:
            result = blocked_action_result(manifest, manifest_id=manifest_id)
            if manifest_id:
                await _maybe_await(
                    complete_action_manifest(
                        manifest_id,
                        outcome_status="failed",
                        outcome_error=result["error"],
                    )
                )
            return result
        return None

    async def _record_policy_blocked_result(
        self,
        run_id: int,
        tool: ToolExecution,
        safe_args: dict[str, str],
        *,
        artifact_type: ArtifactType,
        side_effect: str,
        result: Any,
        root_run_id: int | None,
        collector: list[ToolRecord] | None,
    ) -> Any:
        policy_failure = _blocked_action_failure_summary(result) or "action blocked by policy"
        await self._append_event(
            run_event(
                run_id,
                "run.tool_failed",
                _event_payload(tool.name, safe_args, error=policy_failure),
                root_run_id=root_run_id,
            )
        )
        record = ToolRecord(
            tool_name=tool.name,
            args=safe_args,
            status="failed",
            artifact_type=artifact_type,
            side_effect=side_effect,
            result_preview=redact_tool_result(tool.name, result)[:1000],
            error=policy_failure[:1000],
        )
        if collector is not None:
            collector.append(record)
        await self._append_artifact(
            run_id,
            root_run_id=root_run_id,
            artifact_type=artifact_type,
            title=f"{tool.name} blocked",
            payload=record.to_payload(),
            text=policy_failure[:4000],
        )
        await self._commit_event_boundary(run_id)
        return result

    async def _append_event(self, event) -> None:
        row = await self.store.append_event(event)
        if self.stream is not None:
            self.stream.publish(event.event_type, _event_stream_payload(event, row))

    async def _append_artifact(
        self,
        run_id: int,
        *,
        root_run_id: int | None,
        artifact_type: ArtifactType,
        title: str,
        payload: dict[str, Any],
        text: str,
    ) -> None:
        await self.store.append_artifact(
            AgentRunArtifact(
                run_id=run_id,
                root_run_id=root_run_id,
                artifact_type=artifact_type,
                title=title,
                payload=payload,
                text=text,
                visibility=EventVisibility.PUBLIC,
            )
        )


def _handler_context_from_action_context(action_context: dict[str, Any]) -> dict[str, Any]:
    """Project persisted run context into handler-visible AgentRun context."""

    context: dict[str, Any] = {
        "org_id": action_context.get("org_id"),
        "user_id": action_context.get("actor_id"),
        "run_id": action_context.get("run_id"),
        "trace_id": action_context.get("trace_id"),
        "worker_name": action_context.get("worker_name"),
        "idea_id": action_context.get("idea_id"),
        "root_run_id": action_context.get("root_run_id"),
        "target_ref": action_context.get("target_ref"),
    }

    existing_metadata = getattr(current_agent_context(), "execution_metadata", None)
    metadata = dict(existing_metadata) if isinstance(existing_metadata, dict) else {}
    for key in ("org_id", "user_id", "run_id", "trace_id", "worker_name", "idea_id", "root_run_id"):
        metadata.pop(key, None)
        metadata[key] = context.get(key)
    context["execution_metadata"] = metadata
    return context


async def execute_tool(
    store: AsyncAgentRunStore,
    run_id: int,
    tool_name: str,
    args: dict[str, Any],
    handler: Callable[..., Any],
    *,
    root_run_id: int | None = None,
    scope: ToolScope | None = None,
    collector: list[ToolRecord] | None = None,
    stream: Any = None,
) -> Any:
    executor = AsyncRunToolExecutor(store, stream=stream)
    return await executor.execute(
        run_id,
        ToolExecution(name=tool_name, args=dict(args or {}), handler=handler),
        root_run_id=root_run_id,
        scope=scope,
        collector=collector,
    )


def wrap_tool_handlers(
    handlers: dict[str, Callable[..., Any]],
    *,
    executor: AsyncRunToolExecutor,
    run_id: int,
    root_run_id: int | None = None,
    scope: ToolScope | None = None,
    collector: list[ToolRecord] | None = None,
) -> dict[str, Callable[..., Any]]:
    """Adapt registry handlers so model tool calls pass through runtime tools."""

    wrapped: dict[str, Callable[..., Any]] = {}
    for tool_name, handler in (handlers or {}).items():
        if not callable(handler):
            wrapped[tool_name] = handler
            continue

        async def _handler(_tool_name=tool_name, _handler=handler, **kwargs):
            tool_args = dict(kwargs or {})
            handler_for_execution = _handler
            if _tool_name == "spawn_worker":
                async def _handler_with_runtime_run_id(**inner_kwargs):
                    return await _maybe_await(
                        _handler(_runtime_run_id=run_id, **inner_kwargs)
                    )

                handler_for_execution = _handler_with_runtime_run_id

            return await executor.execute(
                run_id,
                ToolExecution(name=_tool_name, args=tool_args, handler=handler_for_execution),
                root_run_id=root_run_id,
                scope=scope,
                collector=collector,
            )

        _handler.__name__ = getattr(handler, "__name__", f"runtime_{tool_name}")
        _handler._illo_marks_side_effect_start = True
        _handler._illo_uses_blocking_thread = callable_uses_blocking_thread(handler)
        wrapped[tool_name] = _handler
    return wrapped


def artifact_type_for_tool(tool_name: str) -> ArtifactType:
    name = str(tool_name or "")
    if name in FILE_OBSERVATION_TOOLS:
        return ArtifactType.FILE_OBSERVATION
    if name in FILE_EDIT_TOOLS:
        return ArtifactType.FILE_EDIT
    return ArtifactType.COMMAND_OUTPUT


def classify_side_effect(tool_name: str) -> str:
    """Return the registry side-effect class for event and artifact metadata."""
    registration = get_tool_registration(str(tool_name or ""))
    if registration is None:
        return "unknown"
    return registration.side_effect_class.value


def _classified_side_effect_is_write(side_effect: str) -> bool:
    return side_effect == "unknown" or is_write_side_effect_class(side_effect)


def tool_activity_label(tool_name: str, args: dict[str, Any] | None = None) -> str:
    args = args or {}
    target = args.get("path") or args.get("command") or args.get("pattern") or args.get("query") or args.get("url")
    if target:
        return f"Using {tool_name}: {str(target)[:120]}"
    return f"Using {tool_name}"


def enforce_tool_scope(tool_name: str, args: dict[str, Any] | None, scope: ToolScope | None) -> None:
    if scope is None:
        return
    args = args or {}
    target = _tool_target_path(tool_name, args)
    if not target:
        return
    if _matches_any(target, scope.forbidden_files):
        raise ToolScopeViolation(f"{tool_name} target is forbidden by worker scope: {target}")
    if scope.allowed_files and not _matches_any(target, scope.allowed_files):
        side_effect = classify_side_effect(tool_name)
        if _classified_side_effect_is_write(side_effect) and scope.require_approval_for_out_of_scope_mutation:
            raise ToolScopeViolation(f"{tool_name} target needs approval outside worker scope: {target}")
        raise ToolScopeViolation(f"{tool_name} target is outside worker scope: {target}")


def _event_payload(
    tool_name: str,
    args: dict[str, str],
    *,
    result: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "tool_name": tool_name,
        "tool": tool_name,
        "args": args,
        "side_effect": classify_side_effect(tool_name),
    }
    payload["is_write"] = _classified_side_effect_is_write(payload["side_effect"])
    if result is not None:
        # The stored result is a bounded PREVIEW; entity refs are extracted
        # from the FULL result first, or a big JSON result truncates into an
        # unparseable string and downstream attribution (inbound packet
        # minting, preservation evidence) goes blind to what the tool
        # actually created (illo-dev E2E finding, 2026-07-16).
        payload["result"] = result[:1000]
        try:
            from brain.systems.inbound.attribution import collect_result_refs

            refs = collect_result_refs(result, source=tool_name)
            if refs:
                payload["result_refs"] = refs
        except Exception:  # noqa: BLE001 — ref extraction may never break tool recording
            pass
    if error is not None:
        payload["error"] = error[:1000]
    return payload


def _safe_args(args: dict[str, Any]) -> dict[str, str]:
    safe: dict[str, str] = {}
    for key, value in list((args or {}).items())[:20]:
        key_text = str(key)
        if any(part in key_text.lower() for part in SENSITIVE_ARG_PARTS):
            safe[key_text] = "[redacted]"
        else:
            safe[key_text] = _result_to_text(value)[:300]
    return safe


def _tool_is_secret(tool_name: str) -> bool:
    return str(tool_name or "").lower() in SECRET_TOOL_NAMES


def _result_to_text(result: Any) -> str:
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    try:
        return json.dumps(result, default=str, sort_keys=True)
    except Exception:
        return str(result)


def _blocked_action_failure_summary(result: Any) -> str | None:
    if not isinstance(result, dict):
        return None
    policy_result = str(result.get("policy_result") or "")
    if result.get("blocked") is True and policy_result in {"deny", "require_approval"}:
        return str(result.get("error") or policy_result or "action blocked by policy")
    return None


def _runtime_policy_handler(handler: Callable[..., Any]) -> Callable[..., Any]:
    if getattr(handler, "_action_manifest_audited", False):
        unwrapped = getattr(handler, "__wrapped__", None)
        if callable(unwrapped):
            return unwrapped
    return handler


def _tool_target_path(tool_name: str, args: dict[str, Any]) -> str | None:
    if str(tool_name or "") not in FILE_OBSERVATION_TOOLS | FILE_EDIT_TOOLS:
        return None
    value = args.get("path") or args.get("file") or args.get("filename")
    return _normalize_target_path(value) if value else None


def _normalize_target_path(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return os.path.normpath(text).replace(os.sep, "/")


def _matches_any(target: str, patterns: tuple[str, ...]) -> bool:
    normalized = _normalize_target_path(target)
    basename = os.path.basename(normalized)
    for pattern in patterns:
        normalized_pattern = _normalize_target_path(pattern)
        if (
            fnmatch.fnmatch(normalized, normalized_pattern)
            or fnmatch.fnmatch(basename, normalized_pattern)
            or normalized == normalized_pattern
        ):
            return True
    return False


__all__ = [
    "AsyncRunToolExecutor",
    "SECRET_TOOL_NAMES",
    "ToolExecution",
    "ToolRecord",
    "ToolScope",
    "ToolScopeViolation",
    "artifact_type_for_tool",
    "classify_side_effect",
    "enforce_tool_scope",
    "execute_tool",
    "redact_tool_result",
    "tool_activity_label",
    "wrap_tool_handlers",
]
