"""Normalized runtime envelope for Illo agent invocations.

The envelope is the product-level contract around a model run. It keeps the
origin, actor, tenant, run/cycle binding, policies, and budget in one
portable object.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class RunOrigin(StrEnum):
    """Product surface that originated a runtime invocation."""

    CORTEX_THREAD = "cortex_thread"
    CYCLE = "cycle"
    WORKER_NODE = "worker_node"
    INTERNAL_EVENT = "internal_event"
    MANUAL_API = "manual_api"


class RunActorKind(StrEnum):
    """Actor category that caused a runtime invocation."""

    SYSTEM = "system"
    USER = "user"
    WORKER = "worker"
    CYCLE = "cycle"
    SERVICE = "service"
    UNKNOWN = "unknown"


def _coerce_enum(enum_cls, value: Any, default):
    try:
        return enum_cls(value)
    except Exception:
        return default


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return str(value)


def _stable_digest(value: Mapping[str, Any], *, length: int = 24) -> str:
    payload = json.dumps(_jsonable(value), sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:length]


def _trace_id_for(
    *,
    run_id: int | None,
    scheduler_run_id: int | None,
    invocation_id: str,
) -> str:
    if run_id is not None:
        return f"run:{run_id}"
    if scheduler_run_id is not None:
        return f"scheduler-run:{scheduler_run_id}"
    return f"run:{invocation_id}"


@dataclass(frozen=True)
class RunActor:
    """Actor that caused the run."""

    kind: RunActorKind | str = RunActorKind.SYSTEM
    id: str | None = None
    display_name: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "kind",
            _coerce_enum(RunActorKind, self.kind, RunActorKind.UNKNOWN),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "kind": str(self.kind),
            "id": self.id,
            "display_name": self.display_name,
            "metadata": _jsonable(dict(self.metadata or {})),
        }


@dataclass(frozen=True)
class _MappingPolicy:
    """Typed wrapper for policy blocks that are still persisted as JSON."""

    payload: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, value: "_MappingPolicy | Mapping[str, Any] | None") -> "_MappingPolicy":
        if isinstance(value, cls):
            return value
        if isinstance(value, _MappingPolicy):
            return cls(payload=value.to_payload())
        if isinstance(value, Mapping):
            return cls(payload=dict(value))
        return cls()

    def to_payload(self) -> dict[str, Any]:
        return _jsonable(dict(self.payload or {}))


@dataclass(frozen=True)
class RunContract(_MappingPolicy):
    """Typed runtime contract block."""


@dataclass(frozen=True)
class TargetContext(_MappingPolicy):
    """Typed target/workspace binding block."""


@dataclass(frozen=True)
class WorkspacePolicy(_MappingPolicy):
    """Typed workspace policy block."""


@dataclass(frozen=True)
class ToolPolicy(_MappingPolicy):
    """Typed tool policy block."""


@dataclass(frozen=True)
class ContextPolicy(_MappingPolicy):
    """Typed context selection policy block."""


@dataclass(frozen=True)
class RunBudget(_MappingPolicy):
    """Typed runtime budget block."""


@dataclass
class RunEnvelope:
    """One normalized entry point for an Illo agent run."""

    task: str
    origin: RunOrigin | str = RunOrigin.MANUAL_API
    actor: RunActor | Mapping[str, Any] | None = None
    org_id: str | None = None
    user_id: str | None = None
    run_id: int | None = None
    scheduler_run_id: int | None = None
    idea_id: str | None = None
    contract: RunContract | Mapping[str, Any] = field(default_factory=RunContract)
    target_context: TargetContext | Mapping[str, Any] = field(default_factory=TargetContext)
    workspace_policy: WorkspacePolicy | Mapping[str, Any] = field(default_factory=WorkspacePolicy)
    tool_policy: ToolPolicy | Mapping[str, Any] = field(default_factory=ToolPolicy)
    context_policy: ContextPolicy | Mapping[str, Any] = field(default_factory=ContextPolicy)
    provider_operation_type: str | None = None
    budget: RunBudget | Mapping[str, Any] = field(default_factory=RunBudget)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    invocation_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    trace_id: str | None = None

    # Direct-agent invocation fields.
    system_prompt: str = ""
    session_id: str | None = None
    model: str | None = None
    thinking: str | None = "medium"
    tools: list[dict] | None = None
    tool_handlers: dict | None = None
    max_turns: int = 200
    timeout_sec: int | None = None
    cache_system_prompt: bool = True
    persist_session: bool = True
    on_tool_call: Callable[[str, dict, str], None] | None = None
    workspace_root: str | None = None
    brain_context_preloaded: bool = False
    tool_call_source: str = "runner"
    cancel_event: Any = None
    on_stream_activity: Callable[[str], None] | None = None
    on_stream_delta: Callable[[str], None] | None = None
    live_guidance_loader: Callable[[], list[str]] | None = None
    skip_harvest: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "origin",
            _coerce_enum(RunOrigin, self.origin, RunOrigin.MANUAL_API),
        )
        object.__setattr__(self, "contract", RunContract.from_mapping(self.contract))
        object.__setattr__(self, "target_context", TargetContext.from_mapping(self.target_context))
        object.__setattr__(self, "workspace_policy", WorkspacePolicy.from_mapping(self.workspace_policy))
        object.__setattr__(self, "tool_policy", ToolPolicy.from_mapping(self.tool_policy))
        object.__setattr__(self, "context_policy", ContextPolicy.from_mapping(self.context_policy))
        object.__setattr__(self, "budget", RunBudget.from_mapping(self.budget))
        if self.trace_id is None:
            self.trace_id = _trace_id_for(
                run_id=self.run_id,
                scheduler_run_id=self.scheduler_run_id,
                invocation_id=self.invocation_id,
            )
        if isinstance(self.actor, Mapping):
            object.__setattr__(
                self,
                "actor",
                RunActor(
                    kind=str(self.actor.get("kind") or self.actor.get("type") or "unknown"),
                    id=str(self.actor.get("id")) if self.actor.get("id") is not None else None,
                    display_name=(
                        str(self.actor.get("display_name"))
                        if self.actor.get("display_name") is not None
                        else None
                    ),
                    metadata={
                        key: value
                        for key, value in self.actor.items()
                        if key not in {"kind", "type", "id", "display_name"}
                    },
                ),
            )

    @classmethod
    def from_run_agent_kwargs(
        cls,
        *,
        message: str,
        origin: RunOrigin | str = RunOrigin.MANUAL_API,
        actor: RunActor | Mapping[str, Any] | None = None,
        org_id: str | None = None,
        scheduler_run_id: int | None = None,
        contract: Mapping[str, Any] | None = None,
        target_context: Mapping[str, Any] | None = None,
        workspace_policy: Mapping[str, Any] | None = None,
        tool_policy: Mapping[str, Any] | None = None,
        context_policy: Mapping[str, Any] | None = None,
        provider_operation_type: str | None = None,
        budget: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
        **kwargs: Any,
    ) -> "RunEnvelope":
        """Build an envelope from direct-agent keyword arguments."""
        metadata_payload = dict(metadata or {})
        if org_id is None:
            org_id = metadata_payload.get("org_id")
        return cls(
            task=message,
            origin=origin,
            actor=actor,
            org_id=org_id,
            user_id=kwargs.get("user_id"),
            run_id=kwargs.get("run_id"),
            scheduler_run_id=scheduler_run_id,
            idea_id=kwargs.get("idea_id"),
            contract=RunContract.from_mapping(contract or metadata_payload.get("contract")),
            target_context=TargetContext.from_mapping(target_context or metadata_payload.get("target_context")),
            workspace_policy=WorkspacePolicy.from_mapping(
                workspace_policy or metadata_payload.get("workspace_policy")
            ),
            tool_policy=ToolPolicy.from_mapping(tool_policy or metadata_payload.get("tool_policy")),
            context_policy=ContextPolicy.from_mapping(context_policy or metadata_payload.get("context_policy")),
            provider_operation_type=provider_operation_type or metadata_payload.get("provider_operation_type"),
            budget=RunBudget.from_mapping(budget or metadata_payload.get("budget")),
            metadata=metadata_payload,
            system_prompt=kwargs.get("system_prompt") or "",
            session_id=kwargs.get("session_id"),
            model=kwargs.get("model"),
            thinking=kwargs.get("thinking"),
            tools=kwargs.get("tools"),
            tool_handlers=kwargs.get("tool_handlers"),
            max_turns=int(kwargs.get("max_turns") or 200),
            timeout_sec=kwargs.get("timeout_sec"),
            cache_system_prompt=bool(kwargs.get("cache_system_prompt", True)),
            persist_session=bool(kwargs.get("persist_session", True)),
            on_tool_call=kwargs.get("on_tool_call"),
            workspace_root=kwargs.get("workspace_root"),
            brain_context_preloaded=bool(kwargs.get("brain_context_preloaded", False)),
            tool_call_source=kwargs.get("tool_call_source") or "runner",
            cancel_event=kwargs.get("cancel_event"),
            on_stream_activity=kwargs.get("on_stream_activity"),
            on_stream_delta=kwargs.get("on_stream_delta"),
            live_guidance_loader=kwargs.get("live_guidance_loader"),
            skip_harvest=bool(kwargs.get("skip_harvest", False)),
        )

    def to_payload(self) -> dict[str, Any]:
        """Return a deterministic, serializable envelope payload."""
        actor_payload = self.actor.to_payload() if isinstance(self.actor, RunActor) else None
        payload = {
            "invocation_id": self.invocation_id,
            "trace_id": self.trace_id,
            "origin": str(self.origin),
            "actor": actor_payload,
            "org_id": self.org_id,
            "user_id": self.user_id,
            "run_id": self.run_id,
            "scheduler_run_id": self.scheduler_run_id,
            "idea_id": self.idea_id,
            "task": self.task,
            "contract": self.contract.to_payload(),
            "target_context": self.target_context.to_payload(),
            "workspace_policy": self.workspace_policy.to_payload(),
            "tool_policy": self.tool_policy.to_payload(),
            "context_policy": self.context_policy.to_payload(),
            "provider_operation_type": self.provider_operation_type,
            "budget": self.budget.to_payload(),
            "metadata": _jsonable(dict(self.metadata or {})),
            "runtime": {
                "session_id": self.session_id,
                "model": self.model,
                "thinking": self.thinking,
                "persist_session": self.persist_session,
                "cache_system_prompt": self.cache_system_prompt,
                "max_turns": self.max_turns,
                "workspace_root": self.workspace_root,
                "brain_context_preloaded": self.brain_context_preloaded,
                "tool_call_source": self.tool_call_source,
                "skip_harvest": self.skip_harvest,
                "tool_names": [
                    str(tool.get("name"))
                    for tool in self.tools or []
                    if isinstance(tool, Mapping) and tool.get("name")
                ],
            },
        }
        payload["digest"] = _stable_digest(payload)
        return payload

    @property
    def digest(self) -> str:
        return str(self.to_payload()["digest"])

    def to_metadata(self) -> dict[str, Any]:
        payload = self.to_payload()
        return {
            "runtime_envelope": payload,
            "runtime_run_id": self.run_id,
            "runtime_invocation_id": self.invocation_id,
            "runtime_trace_id": self.trace_id,
            "runtime_origin": str(self.origin),
            "runtime_envelope_digest": payload["digest"],
        }

    def to_run_agent_kwargs(self) -> dict[str, Any]:
        """Convert to the existing run_agent keyword surface."""
        metadata = dict(self.metadata or {})
        metadata.update(self.to_metadata())
        if self.org_id and not metadata.get("org_id"):
            metadata["org_id"] = self.org_id
        if self.provider_operation_type and not metadata.get("provider_operation_type"):
            metadata["provider_operation_type"] = self.provider_operation_type
        return {
            "message": self.task,
            "system_prompt": self.system_prompt,
            "session_id": self.session_id,
            "model": self.model,
            "thinking": self.thinking,
            "tools": self.tools,
            "tool_handlers": self.tool_handlers,
            "max_turns": self.max_turns,
            "timeout_sec": self.timeout_sec,
            "cache_system_prompt": self.cache_system_prompt,
            "persist_session": self.persist_session,
            "on_tool_call": self.on_tool_call,
            "workspace_root": self.workspace_root,
            "brain_context_preloaded": self.brain_context_preloaded,
            "run_id": self.run_id,
            "idea_id": self.idea_id,
            "tool_call_source": self.tool_call_source,
            "cancel_event": self.cancel_event,
            "on_stream_activity": self.on_stream_activity,
            "on_stream_delta": self.on_stream_delta,
            "live_guidance_loader": self.live_guidance_loader,
            "user_id": self.user_id,
            "org_id": self.org_id,
            "skip_harvest": self.skip_harvest,
            "metadata": metadata,
        }


@dataclass(frozen=True)
class RunResult:
    """Structured result projected from an AgentResult."""

    run_id: int | None
    invocation_id: str
    trace_id: str | None
    origin: RunOrigin | str
    success: bool
    output: str
    session_id: str
    tokens_input: int = 0
    tokens_output: int = 0
    tokens_cache_read: int = 0
    tokens_cache_creation: int = 0
    duration_sec: int = 0
    tool_calls: tuple[str, ...] = ()
    error: str | None = None
    envelope_digest: str | None = None
    agent_result: Any = None

    @classmethod
    def from_agent_result(cls, envelope: RunEnvelope, agent_result: Any) -> "RunResult":
        return cls(
            run_id=envelope.run_id,
            invocation_id=envelope.invocation_id,
            trace_id=envelope.trace_id,
            origin=envelope.origin,
            success=bool(getattr(agent_result, "success", False)),
            output=str(getattr(agent_result, "output", "") or ""),
            session_id=str(getattr(agent_result, "session_id", "") or ""),
            tokens_input=int(getattr(agent_result, "tokens_input", 0) or 0),
            tokens_output=int(getattr(agent_result, "tokens_output", 0) or 0),
            tokens_cache_read=int(getattr(agent_result, "tokens_cache_read", 0) or 0),
            tokens_cache_creation=int(getattr(agent_result, "tokens_cache_creation", 0) or 0),
            duration_sec=int(getattr(agent_result, "duration_sec", 0) or 0),
            tool_calls=tuple(getattr(agent_result, "tool_calls", []) or []),
            error=getattr(agent_result, "error", None),
            envelope_digest=envelope.digest,
            agent_result=agent_result,
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "trace_id": self.trace_id,
            "origin": str(self.origin),
            "success": self.success,
            "output": self.output,
            "session_id": self.session_id,
            "tokens_input": self.tokens_input,
            "tokens_output": self.tokens_output,
            "tokens_cache_read": self.tokens_cache_read,
            "tokens_cache_creation": self.tokens_cache_creation,
            "duration_sec": self.duration_sec,
            "tool_calls": list(self.tool_calls),
            "error": self.error,
            "envelope_digest": self.envelope_digest,
        }
