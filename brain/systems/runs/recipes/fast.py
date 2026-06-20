"""Fast interactive recipe."""

from __future__ import annotations

import logging
from typing import Any

from brain.systems.runs.engine import RunRecipeResult, RunRuntime, cancel_event_is_set
from brain.systems.runs.tools import wrap_tool_handlers
from brain.systems.runs.status import RunStatus
from brain.systems.runs.invocation import build_direct_agent_invocation, invoke_direct_agent_async
from brain.systems.runs.tool_surface import build_agent_tools, build_tool_handlers
from brain.systems.runs.recipes.base import BaseRunRecipe
from brain.systems.runs.recipes.shared import default_run_model, project_runtime_workspace_from_ref
from brain.systems.runs.recipes.surface_guidance import response_surface_guidance
from brain.systems.runs.tool_policy import disabled_tool_names_from_metadata
from brain.systems.personality import agent_profile_prompt_section, soul_prompt_section

logger = logging.getLogger(__name__)


FAST_RUNTIME_PROMPT = """## Fast Runtime Recipe

This run is the interactive single-agent path. Handle the user's request in one
continuous conversation when it can be answered, inspected, or completed with
short feedback loops.

Runtime rules:
- Treat the provided Target, Workspace, Context, attachments, memory, and live steering as the current run state.
- Use later live steering to adjust the current run without discarding useful progress.
- Triage first: decide whether this is a direct answer, a short interactive task, or work that needs durable/parallel delegation.
- Prefer the smallest complete action that satisfies the request now, but do not disappear into long work before giving the user a timely model-authored update on the originating surface.
- Fast should spawn scoped workers. If an independent investigation, implementation slice, verification pass, duplicate search, or bug/blocker report can safely progress in parallel while you continue the user-facing run, use spawn_worker.
- Use headless=true for internal blocker or bug-report workers that do not need user input and should not create visible thread content.
- Do not spawn a worker when delegation overhead is larger than doing the step directly, when write scopes would overlap unsafely, or when your final answer depends on a multi-wave verified synthesis.
- Use Deep when the request needs heavy verification, dependency-ordered worker waves, internal follow-ups, or formal synthesis across worker results.
- Before your first tool call on work that needs inspection, edits, or more than a moment, write one brief task-specific assistant sentence that says what you are about to do.
- Make that opening natural to the request; do not use canned acknowledgements.
- Keep progress updates brief and meaningful when work takes more than a moment.
- Do not simulate a Deep coordinator graph inside Fast. If the request needs parallel workers, long verification, or durable delegation, make that boundary explicit and prepare a clean handoff.
- Before finalizing, use the Agent Profile's Final Reply Presenter rules. Include evidence, blockers, or uncertainty only when they change what the user should do next.
"""

_FAST_HIDDEN_TOOL_NAMES = {"cortex_reply", "cortex_visual_reply"}
_THREAD_DISCUSSION_REPLY_TOOL = "post_thread_discussion_reply"
_THREAD_DISCUSSION_SURFACE = "thread_discussion"
_THREAD_DISCUSSION_THREAD_PREFIX = "thread-discussion:"


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
    return disabled_tool_names_from_metadata(runtime.request.metadata)


def _runtime_context_maps(runtime: RunRuntime):
    for container in (
        getattr(runtime.request, "metadata", None),
        getattr(runtime.request, "target_ref", None),
    ):
        if isinstance(container, dict):
            yield container


def _runtime_originated_from_thread_discussion(runtime: RunRuntime) -> bool:
    for container in _runtime_context_maps(runtime):
        if container.get("kind") == _THREAD_DISCUSSION_SURFACE:
            return True
        if container.get("originating_surface") == _THREAD_DISCUSSION_SURFACE:
            return True
        if container.get("triggering_surface") == _THREAD_DISCUSSION_SURFACE:
            return True
        if container.get("source_surface") == _THREAD_DISCUSSION_SURFACE:
            return True
        if isinstance(container.get("discussion_trigger"), dict):
            return True
    thread_id = str(getattr(runtime.request, "thread_id", "") or "")
    return thread_id.startswith(_THREAD_DISCUSSION_THREAD_PREFIX)


def _agent_tools_for_runtime(runtime: RunRuntime) -> list[dict]:
    hidden = _FAST_HIDDEN_TOOL_NAMES | _disabled_tool_names(runtime)
    if not _runtime_originated_from_thread_discussion(runtime):
        hidden.add(_THREAD_DISCUSSION_REPLY_TOOL)
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
        model = model_policy.get("model") or await default_run_model(
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

        surface_guidance = response_surface_guidance(
            target_ref=runtime.request.target_ref,
            metadata=runtime.request.metadata,
        )
        prompt_context = "\n\n".join(
            section for section in (surface_guidance, context.prompt_context()) if section.strip()
        )
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
