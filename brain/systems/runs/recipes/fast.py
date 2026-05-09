"""Fast interactive recipe."""

from __future__ import annotations

import json
import logging
from typing import Any

from brain.systems.runs.engine import RunRecipeResult, RunRuntime
from brain.systems.runs.tools import wrap_tool_handlers
from brain.systems.runs.status import RunStatus
from brain.systems.runs.invocation import build_direct_agent_invocation, invoke_direct_agent
from brain.platform.providers.model_policy import get_model_for_tier
from brain.systems.runs.tool_surface import build_agent_tools, build_tool_handlers
from brain.systems.runs.recipes.base import BaseRunRecipe
from brain.systems.runs.recipes.shared import workspace_root_from_ref
from brain.systems.personality import soul_prompt_section

logger = logging.getLogger(__name__)


FAST_AGENT_INSTRUCTIONS = """## Fast Mode

You are Illo Brain in Fast mode: one high-intelligence agent working directly with the user.

Operating rules:
- Move quickly, but keep senior engineering hygiene.
- Use the isolated workspace from the runtime when it is available.
- Preserve user changes and avoid unrelated refactors.
- Skills are optional accelerators. Load one when the user explicitly names it or when it clearly pays for itself.
- A `/skill` mention is an explicit skill command. Treat it as a signal that the user is interested in that skill and it may be relevant context; load the card, summary, or procedure only if useful.
- For onboarding/setup introductions, inspect the workspace first and distinguish configured context from things Illo can help set up next.
- Do not describe low-level implementation tools such as browser primitives, shell commands, file readers, or raw tool names as product capabilities.
- Do not build a coordinator run graph in Fast. Escalate to Deep only when autonomy, parallel workers, assignments, or long verification are genuinely useful.
- Stream useful progress through activity updates and answer in normal conversational prose.
"""

_ONBOARDING_HIDDEN_TOOL_PREFIXES = ("browser_",)


def _agent_tools_for_runtime(runtime: RunRuntime) -> list[dict]:
    tools = build_agent_tools("worker")
    metadata = runtime.request.metadata if isinstance(runtime.request.metadata, dict) else {}
    is_onboarding_intro = (
        metadata.get("origin") == "onboarding"
        or metadata.get("required_response") == "introduce_and_continue_setup"
        or metadata.get("onboarding_step") == "runtime_ready_intro"
    )
    if not is_onboarding_intro:
        return tools
    return [
        tool
        for tool in tools
        if not str(tool.get("name") or "").startswith(_ONBOARDING_HIDDEN_TOOL_PREFIXES)
    ]


def _workspace_root(workspace_ref: dict[str, Any]) -> str | None:
    return workspace_root_from_ref(workspace_ref)


def _json_block(title: str, value: Any) -> str:
    if not value:
        return ""
    return f"\n\n## {title}\n```json\n{json.dumps(value, indent=2, default=str)}\n```"


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

    def execute(self, runtime: RunRuntime) -> RunRecipeResult:
        context = runtime.context_loader.load(
            thread_id=runtime.request.thread_id,
            message=runtime.request.message,
            target_ref=runtime.request.target_ref,
            workspace_ref=runtime.request.workspace_ref,
            metadata=runtime.request.metadata,
        )
        runtime.activity("Reading context")
        workspace_root = _workspace_root(runtime.request.workspace_ref)
        model_policy = dict(runtime.request.model_policy or {})
        model = model_policy.get("model") or get_model_for_tier(
            model_policy.get("tier") or "high",
            include_provider_prefix=True,
            user_id=runtime.request.user_id,
            org_id=runtime.request.org_id,
        )
        thinking = model_policy.get("thinking") or "high"

        def _activity(label: str) -> None:
            runtime.activity(label)

        def _delta(delta: str) -> None:
            runtime.text_delta(delta)

        def _guidance() -> list[str]:
            return runtime.drain_steering()

        tool_handlers = wrap_tool_handlers(
            build_tool_handlers(workspace_root=workspace_root),
            executor=runtime.tool_executor(),
            run_id=runtime.run.id,
            root_run_id=runtime.run.root_run_id,
        )

        system_prompt = (
            soul_prompt_section()
            + "\n\n"
            + FAST_AGENT_INSTRUCTIONS
            + _json_block("Target", runtime.request.target_ref)
            + _json_block("Workspace", runtime.request.workspace_ref)
            + (f"\n\n## Context\n{context.prompt_context()}" if context.prompt_context() else "")
        )
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
            run_id=runtime.run.id,
            idea_id=None,
            tool_call_source="fast",
            on_tool_call=None,
            on_stream_activity=_activity,
            on_stream_delta=_delta,
            live_guidance_loader=_guidance,
            cancel_event=runtime.cancel_event,
            brain_context_preloaded=bool(context.prompt_context()),
            skip_harvest=True,
            metadata={
                "org_id": runtime.request.org_id,
                "profile": "fast",
                "recipe": self.name,
                "target_ref": runtime.request.target_ref,
                "workspace_ref": runtime.request.workspace_ref,
                "thread_attachment_context": _thread_attachment_context(runtime),
                "max_parallel_tool_calls": 1,
            },
        )
        try:
            result = invoke_direct_agent(spec)
        except Exception as exc:
            logger.exception("fast_recipe_failed", extra={"run_id": runtime.run.id})
            return RunRecipeResult(output=f"Fast run failed: {exc}", status=RunStatus.FAILED)

        output = str(getattr(result, "output", "") or "").strip()
        if runtime.cancel_event is not None and runtime.cancel_event.is_set():
            return RunRecipeResult(output="user_canceled", status=RunStatus.CANCELED)
        status = RunStatus.COMPLETED if getattr(result, "success", False) else RunStatus.FAILED
        if str(getattr(result, "error", "") or "") == "Cancelled by runner":
            status = RunStatus.CANCELED
        if getattr(result, "error", None) and not output:
            output = str(result.error)
        return RunRecipeResult(output=output, status=status)


__all__ = ["FastRecipe"]
