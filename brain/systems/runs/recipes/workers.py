"""Worker child-run recipe."""

from __future__ import annotations

import json
import logging
from typing import Any

from brain.systems.runs.assignments import WorkerAssignment
from brain.systems.runs.context import compact_project_reference
from brain.systems.runs.domain import AgentRunArtifact, ArtifactType
from brain.systems.runs.engine import RunRecipeResult, RunRuntime
from brain.systems.runs.recipes.base import BaseRunRecipe
from brain.systems.runs.recipes.shared import project_runtime_workspace_from_ref
from brain.systems.runs.status import RunStatus
from brain.systems.runs.tools import AsyncRunToolExecutor, ToolRecord, ToolScope, wrap_tool_handlers
from brain.systems.runs.invocation import build_direct_agent_invocation, invoke_direct_agent_async
from brain.platform.providers.model_policy import get_model_for_tier
from brain.systems.runs.tool_surface import build_agent_tools, build_tool_handlers

logger = logging.getLogger(__name__)


WORKER_AGENT_INSTRUCTIONS = """## Worker Mode

You are an Illo Brain worker child run. Own the scoped objective and keep the parent run observable.

Rules:
- Work only inside the declared WorkerAssignment and ownership scope.
- Inspect relevant project/workspace context before making claims.
- Use the available tools for file reads, searches, commands, and edits so evidence is recorded.
- Do not mutate forbidden or out-of-scope files. If scope is too narrow, say exactly what approval is needed.
- Stream short progress updates while working.
- Finish with a concise result: what changed or was learned, required evidence, artifacts produced, and remaining uncertainty.
"""


WorkerScope = WorkerAssignment


def _thread_attachment_context(runtime: RunRuntime) -> dict[str, Any] | None:
    metadata_context = runtime.request.metadata.get("thread_attachment_context")
    if isinstance(metadata_context, dict):
        return metadata_context
    for container in (runtime.request.target_ref, runtime.request.workspace_ref):
        value = container.get("thread_attachment_context") if isinstance(container, dict) else None
        if isinstance(value, dict):
            return value
    return None


class WorkerRecipe(BaseRunRecipe):
    name = "worker"

    async def execute(self, runtime: RunRuntime) -> RunRecipeResult:
        assignment = worker_assignment_from_runtime(runtime)
        context = runtime.context_loader.load(
            thread_id=runtime.request.thread_id,
            message=runtime.request.message,
            target_ref=runtime.request.target_ref,
            workspace_ref=runtime.request.workspace_ref,
            metadata=runtime.request.metadata,
        )
        project_workspace = project_runtime_workspace_from_ref(runtime.request.workspace_ref)
        workspace_root = project_workspace.workspace_root
        await runtime.activity(
            "Reading worker assignment",
            assignment_id=assignment.id,
            role=assignment.role,
            objective=assignment.objective,
            risk_level=assignment.risk_level,
        )

        tool_records: list[ToolRecord] = []
        executor = AsyncRunToolExecutor(runtime.store, stream=runtime.stream)
        handlers = wrap_tool_handlers(
            build_tool_handlers(
                workspace_root=workspace_root,
                allowed_workspaces=project_workspace.allowed_workspaces,
            ),
            executor=executor,
            run_id=runtime.run.id,
            root_run_id=runtime.run.root_run_id,
            scope=_tool_scope_from_assignment(assignment),
            collector=tool_records,
        )
        model_policy = dict(runtime.request.model_policy or {})
        model = model_policy.get("model") or get_model_for_tier(
            model_policy.get("tier") or "high",
            include_provider_prefix=True,
            user_id=runtime.request.user_id,
            org_id=runtime.request.org_id,
        )
        thinking = model_policy.get("thinking") or "high"
        streamed_output = False

        async def _activity(label: str) -> None:
            await runtime.activity(label)

        async def _record_delta(delta: str) -> None:
            nonlocal streamed_output
            streamed_output = True
            await runtime.text_delta(delta)

        _delta = _record_delta

        async def _guidance() -> list[str]:
            return await runtime.drain_steering()

        prompt_context = context.prompt_context()
        system_prompt = build_worker_prompt(
            assignment,
            target_ref=runtime.request.target_ref,
            workspace_ref=runtime.request.workspace_ref,
            context=prompt_context,
            evidence_so_far=runtime.request.metadata.get("evidence") or runtime.request.metadata.get("parent_evidence"),
        )
        await runtime.activity("Starting scoped worker", workspace_root=workspace_root)
        spec = build_direct_agent_invocation(
            message=runtime.request.message,
            system_prompt=system_prompt,
            session_id=f"agent-run-{runtime.run.id}-worker",
            model=str(model),
            thinking=str(thinking),
            tools=build_agent_tools("worker"),
            tool_handlers=handlers,
            persist_session=True,
            workspace_root=workspace_root,
            user_id=runtime.request.user_id,
            org_id=runtime.request.org_id,
            run_id=runtime.run.id,
            idea_id=None,
            tool_call_source="worker",
            on_tool_call=None,
            on_stream_activity=_activity,
            on_stream_delta=_delta,
            live_guidance_loader=_guidance,
            brain_context_preloaded=bool(prompt_context or runtime.request.target_ref or runtime.request.workspace_ref),
            skip_harvest=True,
            metadata={
                "profile": str(runtime.request.normalized_profile.value),
                "recipe": self.name,
                "execution_provenance": runtime.request.metadata,
                "parent_run_id": runtime.run.parent_run_id,
                "root_run_id": runtime.run.root_run_id,
                "worker_assignment": assignment.to_payload(),
                "target_ref": runtime.request.target_ref,
                "workspace_ref": runtime.request.workspace_ref,
                "thread_attachment_context": _thread_attachment_context(runtime),
                "headless": bool(runtime.request.metadata.get("headless")),
                "tool_policy": runtime.request.metadata.get("tool_policy")
                if isinstance(runtime.request.metadata.get("tool_policy"), dict)
                else {},
            },
        )
        try:
            result = await invoke_direct_agent_async(spec)
        except Exception as exc:
            logger.exception("worker_recipe_failed", extra={"run_id": runtime.run.id})
            output = f"Worker failed: {exc}"
            status = RunStatus.FAILED
            post_completion_tasks = ()
        else:
            output = str(getattr(result, "output", "") or "").strip()
            status = RunStatus.COMPLETED if getattr(result, "success", False) else RunStatus.FAILED
            if getattr(result, "error", None) and not output:
                output = str(result.error)
            post_completion_tasks = tuple(getattr(result, "post_completion_tasks", ()) or ())
        if output and not streamed_output:
            await runtime.text_delta(output)
        worker_result = worker_result_artifact(
            runtime.run.id,
            assignment=assignment,
            output=output,
            status=status,
            tool_records=tool_records,
            root_run_id=runtime.run.root_run_id,
        )
        return RunRecipeResult(
            output=output,
            status=status,
            artifacts=(worker_result,),
            post_completion_tasks=post_completion_tasks,
        )


def worker_assignment_from_runtime(runtime: RunRuntime) -> WorkerAssignment:
    payload = _assignment_payload(runtime.request.metadata, runtime.request.target_ref)
    objective = str(
        payload.get("objective")
        or runtime.request.metadata.get("objective")
        or runtime.request.target_ref.get("objective")
        or runtime.request.message
    ).strip()
    role = str(payload.get("role") or runtime.request.metadata.get("worker_role") or "worker")
    assignment_id = str(
        payload.get("id")
        or payload.get("node_id")
        or runtime.request.metadata.get("parent_node_id")
        or role
    )
    return WorkerAssignment.from_payload(
        {
            **payload,
            "id": assignment_id,
            "role": role,
            "objective": objective or runtime.request.message,
        }
    )


def worker_scope_from_runtime(runtime: RunRuntime) -> WorkerAssignment:
    return worker_assignment_from_runtime(runtime)


def build_worker_prompt(
    assignment: WorkerAssignment,
    *,
    target_ref: dict[str, Any],
    workspace_ref: dict[str, Any],
    context: str = "",
    evidence_so_far: Any = None,
) -> str:
    context_block = context
    if not context_block:
        context_parts = []
        if target_ref:
            context_parts.append("Target:\n" + compact_project_reference(target_ref))
        if workspace_ref:
            context_parts.append("Workspace:\n" + compact_project_reference(workspace_ref))
        context_block = "\n\n".join(context_parts)
    return (
        WORKER_AGENT_INSTRUCTIONS
        + _json_block("Worker Assignment", assignment.to_payload())
        + _json_block("Parent Evidence", evidence_so_far)
        + (f"\n\n## Context\n{context_block}" if context_block else "")
    )


def worker_result_artifact(
    run_id: int,
    *,
    assignment: WorkerAssignment,
    output: str,
    status: RunStatus,
    tool_records: list[ToolRecord],
    root_run_id: int | None,
) -> AgentRunArtifact:
    payload = {
        "status": status.value,
        "assignment": assignment.to_payload(),
        "scope": _assignment_scope_payload(assignment),
        "evidence_requirements": [requirement.to_payload() for requirement in assignment.required_evidence()],
        "evidence": {
            "tool_calls": [record.to_payload() for record in tool_records],
            "tool_names": [record.tool_name for record in tool_records],
            "artifact_types": sorted({record.artifact_type.value for record in tool_records}),
        },
    }
    return AgentRunArtifact(
        run_id=run_id,
        root_run_id=root_run_id,
        artifact_type=ArtifactType.WORKER_RESULT,
        title=_worker_result_title(assignment),
        payload=payload,
        text=output,
    )


def _assignment_payload(metadata: dict[str, Any], target_ref: dict[str, Any]) -> dict[str, Any]:
    for source in (
        metadata.get("worker_assignment"),
        metadata.get("assignment"),
        metadata.get("worker_scope"),
        metadata.get("scope"),
        target_ref.get("worker_assignment"),
        target_ref.get("assignment"),
        target_ref.get("worker_scope"),
        target_ref.get("scope"),
    ):
        if isinstance(source, dict):
            return dict(source)
    return {}


def _tool_scope_from_assignment(assignment: WorkerAssignment) -> ToolScope:
    return ToolScope(
        allowed_files=assignment.allowed_files,
        forbidden_files=assignment.forbidden_files,
        allowed_resources=assignment.allowed_resources,
        forbidden_resources=assignment.forbidden_resources,
    )


def _string_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    if isinstance(value, dict):
        value = value.values()
    try:
        return tuple(str(item).strip() for item in value if str(item).strip())
    except TypeError:
        return (str(value).strip(),) if str(value).strip() else ()


def _json_block(title: str, value: Any) -> str:
    if not value:
        return ""
    return f"\n\n## {title}\n```json\n{json.dumps(value, indent=2, default=str)}\n```"


def _assignment_scope_payload(assignment: WorkerAssignment) -> dict[str, Any]:
    expected_artifacts = assignment.expected_artifacts or tuple(
        requirement.artifact_type for requirement in assignment.required_evidence() if requirement.artifact_type
    )
    return {
        "objective": assignment.objective,
        "allowed_files": list(assignment.allowed_files),
        "forbidden_files": list(assignment.forbidden_files),
        "allowed_resources": list(assignment.allowed_resources),
        "forbidden_resources": list(assignment.forbidden_resources),
        "expected_artifacts": list(expected_artifacts),
        "risk_level": assignment.risk_level,
    }


def _worker_result_title(assignment: WorkerAssignment) -> str:
    objective = " ".join(assignment.objective.split())
    return f"Worker result: {objective[:80]}" if objective else "Worker result"


__all__ = [
    "WorkerRecipe",
    "WorkerScope",
    "build_worker_prompt",
    "worker_assignment_from_runtime",
    "worker_result_artifact",
    "worker_scope_from_runtime",
]
