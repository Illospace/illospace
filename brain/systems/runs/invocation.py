"""Shared invocation builder for direct AgentRun calls."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from brain.platform.providers.model_policy import get_default_model
from brain.kernel.runtime.envelope import RunActor, RunEnvelope


@dataclass(frozen=True)
class DirectAgentInvocationSpec:
    """Normalized invocation contract for direct agent runs."""

    message: str
    session_id: str | None = None
    system_prompt: str = ""
    model: str | None = None
    thinking: str | None = "medium"
    tools: list[dict] | None = None
    persist_session: bool = False
    max_turns: int = 200
    workspace_root: str | None = None
    tool_handlers: dict | None = None
    cache_system_prompt: bool = True
    user_id: str | None = None
    run_id: int | None = None
    idea_id: str | None = None
    tool_call_source: str = "utility"
    on_tool_call: Callable[[str, dict, str], None] | None = None
    cancel_event: Any = None
    on_stream_activity: Callable[[str], None] | None = None
    on_stream_delta: Callable[[str], None] | None = None
    live_guidance_loader: Callable[[], list[str]] | None = None
    brain_context_preloaded: bool = False
    skip_harvest: bool = False
    metadata: dict | None = None

    def to_run_envelope(self) -> RunEnvelope:
        """Project the direct invocation into the normalized runtime envelope."""
        metadata = dict(self.metadata or {})
        model = self.model or get_default_model(
            include_provider_prefix=False,
            user_id=self.user_id,
        )
        return RunEnvelope(
            task=self.message,
            origin=metadata.get("origin") or "manual_api",
            actor=RunActor(
                kind="user" if self.user_id else "system",
                id=self.user_id,
                metadata={"tool_call_source": self.tool_call_source},
            ),
            org_id=metadata.get("org_id"),
            user_id=self.user_id,
            run_id=self.run_id,
            idea_id=self.idea_id,
            contract=metadata.get("contract") or {},
            target_context=metadata.get("target_context") or {},
            workspace_policy={
                "workspace_root": self.workspace_root,
                **(metadata.get("workspace_policy") or {}),
            },
            tool_policy={
                "tool_call_source": self.tool_call_source,
                **(metadata.get("tool_policy") or {}),
            },
            context_policy={
                "brain_context_preloaded": self.brain_context_preloaded,
                "skip_harvest": self.skip_harvest,
                **(metadata.get("context_policy") or {}),
            },
            provider_operation_type=metadata.get("provider_operation_type")
            or ("worker" if self.tool_call_source == "worker" else "coordinator"),
            budget={"max_turns": self.max_turns, **(metadata.get("budget") or {})},
            metadata=metadata,
            system_prompt=self.system_prompt,
            session_id=self.session_id,
            model=model,
            thinking=self.thinking,
            tools=self.tools or [],
            tool_handlers=self.tool_handlers,
            max_turns=self.max_turns,
            workspace_root=self.workspace_root,
            cache_system_prompt=self.cache_system_prompt,
            persist_session=self.persist_session,
            on_tool_call=self.on_tool_call,
            tool_call_source=self.tool_call_source,
            brain_context_preloaded=self.brain_context_preloaded,
            skip_harvest=self.skip_harvest,
            cancel_event=self.cancel_event,
            on_stream_activity=self.on_stream_activity,
            on_stream_delta=self.on_stream_delta,
            live_guidance_loader=self.live_guidance_loader,
        )

    def to_run_agent_kwargs(self) -> dict:
        return self.to_run_envelope().to_run_agent_kwargs()


def build_direct_agent_invocation(**kwargs) -> DirectAgentInvocationSpec:
    """Build a shared direct invocation spec."""
    return DirectAgentInvocationSpec(**kwargs)


def invoke_direct_agent(spec: DirectAgentInvocationSpec):
    """Execute a direct invocation spec through the common agent loop."""
    from brain.kernel.runtime.kernel import invoke_run_envelope

    return invoke_run_envelope(spec.to_run_envelope())
