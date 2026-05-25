"""Fast interactive recipe."""

from __future__ import annotations

import logging
from typing import Any

from brain.systems.runs.engine import RunRecipeResult, RunRuntime, cancel_event_is_set
from brain.systems.runs.tools import wrap_tool_handlers
from brain.systems.runs.status import RunStatus
from brain.systems.runs.invocation import build_direct_agent_invocation, invoke_direct_agent_async
from brain.platform.providers.model_policy import get_model_for_tier
from brain.systems.runs.tool_surface import build_agent_tools, build_tool_handlers
from brain.systems.runs.recipes.base import BaseRunRecipe
from brain.systems.runs.recipes.shared import project_runtime_workspace_from_ref
from brain.systems.personality import agent_profile_prompt_section, soul_prompt_section

logger = logging.getLogger(__name__)


FAST_RUNTIME_PROMPT = """## Fast Runtime Recipe

This run is the interactive single-agent path. Handle the user's request in one
continuous conversation when it can be answered, inspected, or completed with
short feedback loops.

Runtime rules:
- Treat the provided Target, Workspace, Context, attachments, memory, and live steering as the current run state.
- Use later live steering to adjust the current run without discarding useful progress.
- Prefer the smallest complete action that satisfies the request now; leave larger follow-up work explicit.
- Fast may spawn scoped workers. If an independent investigation, implementation slice, verification pass, duplicate search, or bug/blocker report can safely progress in parallel while you continue the user-facing run, use spawn_worker.
- Use headless=true for internal blocker or bug-report workers that do not need user input and should not create visible thread content.
- Do not spawn a worker when delegation overhead is larger than doing the step directly, when write scopes would overlap unsafely, or when your final answer depends on a multi-wave verified synthesis.
- Use Deep when the request needs heavy verification, dependency-ordered worker waves, internal follow-ups, or formal synthesis across worker results.
- Before your first tool call on work that needs inspection, edits, or more than a moment, write one brief task-specific assistant sentence that says what you are about to do.
- Make that opening natural to the request; do not use canned acknowledgements.
- Keep progress updates brief and meaningful when work takes more than a moment.
- Before finalizing, use the Agent Profile's Final Reply Presenter rules. Include evidence, blockers, or uncertainty only when they change what the user should do next.
"""

_FAST_HIDDEN_TOOL_NAMES = {"cortex_reply", "cortex_visual_reply"}


def build_fast_system_prompt(prompt_context: str = "") -> str:
    sections = [
        soul_prompt_section(),
        agent_profile_prompt_section(),
        FAST_RUNTIME_PROMPT,
    ]
    if prompt_context:
        sections.append(f"## Context\n{prompt_context}")
    return "\n\n".join(section for section in sections if section.strip())


def _disabled_tool_names(runtime: RunRuntime) -> set[str]:
    policy = runtime.request.metadata.get("tool_policy")
    if not isinstance(policy, dict):
        return set()
    raw_names = policy.get("disabled_tools") or policy.get("blocked_tools") or []
    if isinstance(raw_names, str):
        raw_names = [raw_names]
    if not isinstance(raw_names, list):
        return set()
    return {str(name).strip() for name in raw_names if str(name or "").strip()}


def _agent_tools_for_runtime(runtime: RunRuntime) -> list[dict]:
    hidden = _FAST_HIDDEN_TOOL_NAMES | _disabled_tool_names(runtime)
    return [
        tool
        for tool in build_agent_tools("coordinator")
        if str(tool.get("name") or "") not in hidden
    ]


def _thread_attachment_context(runtime: RunRuntime) -> dict[str, Any] | None:
    metadata_context = runtime.request.metadata.get("thread_attachment_context")
    if isinstance(metadata_context, dict):
        return metadata_context
    for container in (runtime.request.target_ref, runtime.request.workspace_ref):
        value = container.get("thread_attachment_context") if isinstance(container, dict) else None
        if isinstance(value, dict):
            return value
    return None


class FastRecipe(BaseRunRecipe):
    name = "fast"

    async def execute(self, runtime: RunRuntime) -> RunRecipeResult:
        context = runtime.context_loader.load(
            thread_id=runtime.request.thread_id,
            message=runtime.request.message,
            target_ref=runtime.request.target_ref,
            workspace_ref=runtime.request.workspace_ref,
            metadata=runtime.request.metadata,
        )
        await runtime.activity("Reading context")
        project_workspace = project_runtime_workspace_from_ref(runtime.request.workspace_ref)
        workspace_root = project_workspace.workspace_root
        model_policy = dict(runtime.request.model_policy or {})
        model = model_policy.get("model") or get_model_for_tier(
            model_policy.get("tier") or "high",
            include_provider_prefix=True,
            user_id=runtime.request.user_id,
            org_id=runtime.request.org_id,
        )
        thinking = model_policy.get("thinking") or "high"

        async def _activity(label: str) -> None:
            await runtime.activity(label)

        async def _delta(delta: str) -> None:
            await runtime.text_delta(delta)

        async def _guidance() -> list[str]:
            return await runtime.drain_steering()

        disabled_tools = _disabled_tool_names(runtime)
        raw_tool_handlers = build_tool_handlers(
            workspace_root=workspace_root,
            allowed_workspaces=project_workspace.allowed_workspaces,
        )
        if disabled_tools:
            raw_tool_handlers = {
                name: handler for name, handler in raw_tool_handlers.items() if name not in disabled_tools
            }
        tool_handlers = wrap_tool_handlers(
            raw_tool_handlers,
            executor=runtime.tool_executor(),
            run_id=runtime.run.id,
            root_run_id=runtime.run.root_run_id,
        )

        prompt_context = context.prompt_context()
        system_prompt = build_fast_system_prompt(prompt_context)
        spec = build_direct_agent_invocation(
            message=context.message,
            system_prompt=system_prompt,
            session_id=f"agent-run-{runtime.run.id}",
            model=str(model),
            thinking=str(thinking),
            tools=_agent_tools_for_runtime(runtime),
            tool_handlers=tool_handlers,
            persist_session=True,
            workspace_root=workspace_root,
            user_id=runtime.request.user_id,
            org_id=runtime.request.org_id,
            run_id=runtime.run.id,
            idea_id=None,
            tool_call_source="fast",
            on_tool_call=None,
            on_stream_activity=_activity,
            on_stream_delta=_delta,
            live_guidance_loader=_guidance,
            cancel_event=runtime.cancel_event,
            brain_context_preloaded=bool(prompt_context),
            skip_harvest=True,
            metadata={
                "profile": "fast",
                "recipe": self.name,
                "org_id": runtime.request.org_id or runtime.request.metadata.get("org_id"),
                "user_id": runtime.request.user_id or runtime.request.metadata.get("user_id"),
                "execution_provenance": runtime.request.metadata,
                "target_ref": runtime.request.target_ref,
                "workspace_ref": runtime.request.workspace_ref,
                "thread_attachment_context": _thread_attachment_context(runtime),
                "max_parallel_tool_calls": 4,
            },
        )
        try:
            result = await invoke_direct_agent_async(spec)
        except Exception as exc:
            logger.exception("fast_recipe_failed", extra={"run_id": runtime.run.id})
            return RunRecipeResult(output=f"Fast run failed: {exc}", status=RunStatus.FAILED)

        output = str(getattr(result, "output", "") or "").strip()
        if await cancel_event_is_set(runtime.cancel_event):
            return RunRecipeResult(output="user_canceled", status=RunStatus.CANCELED)
        status = RunStatus.COMPLETED if getattr(result, "success", False) else RunStatus.FAILED
        if str(getattr(result, "error", "") or "") == "Cancelled by runner":
            status = RunStatus.CANCELED
        if getattr(result, "error", None) and not output:
            output = str(result.error)
        return RunRecipeResult(
            output=output,
            status=status,
            post_completion_tasks=tuple(getattr(result, "post_completion_tasks", ()) or ()),
        )


__all__ = ["FAST_RUNTIME_PROMPT", "FastRecipe", "build_fast_system_prompt"]
