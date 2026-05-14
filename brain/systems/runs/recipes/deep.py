"""Deep autonomous recipe built from AgentRun graph and child runs."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from typing import Any, Iterable

from brain.platform.providers.model_policy import get_model_for_tier
from brain.systems.personality import soul_prompt_section
from brain.systems.runs.assignments import AcceptanceCriteria, EvidenceRequirement, WorkerAssignment
from brain.systems.runs.domain import AgentRun, AgentRunArtifact, ArtifactType, RunProfile, RunRecipe
from brain.systems.runs.engine import RunRecipeResult, RunRuntime
from brain.systems.runs.events import run_event
from brain.systems.runs.graph import DeepPlan, RunEdge, RunNode
from brain.systems.runs.invocation import build_direct_agent_invocation, invoke_direct_agent_async
from brain.systems.runs.recipes.base import BaseRunRecipe
from brain.systems.runs.recipes.phase_barrier import (
    apply_phase_barrier_decision,
    phase_result_artifact,
    plan_revision_artifact,
    review_completed_phase,
)
from brain.systems.runs.recipes.scout import ScoutHandoff, scout_request
from brain.systems.runs.recipes.shared import workspace_root_from_ref
from brain.systems.runs.status import RunStatus
from brain.systems.runs.verification.evidence import (
    artifact_payloads,
    verification_evidence,
    worker_evidence_from_artifacts,
)
from brain.systems.runs.verification.gates import VerificationResult, verify_worker_evidence
from brain.systems.runs.verification.policy import VerificationMode, verification_mode_for_run

logger = logging.getLogger(__name__)


DEEP_COORDINATOR_SYNTHESIS_INSTRUCTIONS = """## Deep Coordinator Mode

You are Illo Brain in Deep mode: the coordinator who owns the final answer to the user after scoped workers finish.

Rules:
- Answer the user's original request directly in natural conversational prose.
- Use worker results as evidence. Do not invent changes, commands, files, or verification that are not in the synthesis payload.
- Surface the useful outcome first, then mention verification, blockers, or uncertainty when they matter.
- Do not expose internal JSON, run graph mechanics, or worker chatter unless it helps the user understand the result.
- Keep it concise and concrete.
"""


def _root_run_id(runtime: RunRuntime) -> int:
    return runtime.run.root_run_id or runtime.run.id


def _status_value(status: Any) -> str:
    return str(getattr(status, "value", status) or "")


def _truncate(value: str, *, limit: int = 900) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _scout_from_payload(payload: dict[str, Any] | None) -> ScoutHandoff:
    payload = payload if isinstance(payload, dict) else {}
    reasons = payload.get("reasons") if isinstance(payload.get("reasons"), list) else []
    return ScoutHandoff(
        summary=str(payload.get("summary") or "Scout completed."),
        should_escalate=bool(payload.get("should_escalate")),
        reasons=tuple(str(reason) for reason in reasons),
    )


class DeepRecipe(BaseRunRecipe):
    name = "deep"

    async def execute(self, runtime: RunRuntime) -> RunRecipeResult:
        await runtime.activity("Starting Deep AgentRun graph")
        try:
            plan = await self._plan(runtime)
            node_results = await self._execute_plan(runtime, plan)
            verify_payload = node_results.get("verify")
            verification = self._verification_result(verify_payload)
            verify_status = str((verify_payload or {}).get("status") or "")
            if verification is None and verify_status != RunStatus.COMPLETED.value:
                output = f"Deep verification failed: verify node ended with status {verify_status or 'missing'}"
                await runtime.text_delta(output)
                return RunRecipeResult(output=output, status=RunStatus.FAILED)
            if verification is not None and not verification.passed:
                output = f"Deep verification failed: {verification.warning or 'gate did not pass'}"
                await runtime.text_delta(output)
                return RunRecipeResult(output=output, status=RunStatus.FAILED)
            output = str((node_results.get("synthesize") or {}).get("output") or "").strip()
            if not output:
                output = self._synthesize_output(node_results)
        except Exception as exc:
            logger.exception("deep_recipe_failed", extra={"run_id": runtime.run.id})
            return RunRecipeResult(output=f"Deep run failed: {exc}", status=RunStatus.FAILED)
        await runtime.text_delta(output)
        return RunRecipeResult(output=output, status=RunStatus.COMPLETED)

    async def _plan(self, runtime: RunRuntime) -> DeepPlan:
        payload = await runtime.run_step("plan", lambda: self._plan_uncached(runtime))
        return DeepPlan.from_payload(payload)

    async def _plan_uncached(self, runtime: RunRuntime) -> dict[str, Any]:
        await runtime.activity("Planning Deep run graph", step="plan")
        assignments = self._configured_assignments(runtime.request.metadata) or self._default_assignments(runtime.request.message)
        nodes: list[RunNode] = [
            RunNode(id="scout", role="scout", objective="Scout request", recipe="scout", kind="scout")
        ]
        worker_nodes = [
            RunNode(
                id=assignment.id,
                role=assignment.role,
                objective=assignment.objective,
                recipe="worker",
                kind="worker",
                assignment=assignment,
                depends_on=_assignment_depends_on(assignment),
                metadata={"assignment": assignment.to_payload()},
            )
            for assignment in assignments
        ]
        nodes.extend(worker_nodes)
        nodes.extend(
            [
                RunNode(
                    id="verify",
                    role="verify",
                    objective="Verify worker evidence",
                    recipe="verification",
                    kind="verification",
                ),
                RunNode(
                    id="synthesize",
                    role="synthesize",
                    objective="Synthesize final answer",
                    recipe="synthesis",
                    kind="synthesis",
                ),
            ]
        )
        edges = [
            *(RunEdge("scout", node.id) for node in worker_nodes),
            *(RunEdge(node.id, "verify") for node in worker_nodes),
            RunEdge("verify", "synthesize"),
        ]
        plan = DeepPlan(
            nodes=tuple(nodes),
            edges=tuple(edges),
            objective=runtime.request.message,
            metadata={"request": runtime.request.message},
        )
        payload = plan.to_payload()
        await runtime.store.append_artifact(
            AgentRunArtifact(
                run_id=runtime.run.id,
                root_run_id=_root_run_id(runtime),
                artifact_type=ArtifactType.DEEP_PLAN,
                title="Deep plan",
                payload=payload,
            )
        )
        for wave_index, wave in enumerate(plan.waves, start=1):
            for node in wave:
                await self._record_node_event(runtime, "planned", node, status="planned", wave=wave_index)
                if _node_kind(node) == "worker":
                    assignment = _assignment_for_node(node)
                    await runtime.store.append_event(
                        run_event(
                            runtime.run.id,
                            "run.worker_planned",
                            {
                                "node_id": node.id,
                                "assignment": assignment.to_payload(),
                                **_worker_event_fields(assignment),
                            },
                            root_run_id=_root_run_id(runtime),
                            producer="deep",
                        )
                    )
        return payload

    async def _execute_plan(self, runtime: RunRuntime, plan: DeepPlan) -> dict[str, dict[str, Any]]:
        node_results: dict[str, dict[str, Any]] = {}
        plan_state = plan
        wave_index = 0
        while True:
            ready = _ready_wave(plan_state)
            if not ready:
                blocked = _blocked_pending_nodes(plan_state)
                if not blocked:
                    break
                for node in blocked:
                    payload = await runtime.run_step(
                        node.id,
                        lambda node=node: self._skip_node_uncached(
                            runtime,
                            node,
                            plan_state.dependency_ids(node.id),
                        ),
                    )
                    node_results[node.id] = dict(payload or {})
                    plan_state = plan_state.with_node_status(node.id, "skipped")
                continue

            wave_index += 1
            completed_nodes: list[RunNode] = []
            await runtime.store.append_event(
                run_event(
                    runtime.run.id,
                    "run.graph_wave_started",
                    {"wave": wave_index, "nodes": [node.id for node in ready]},
                    root_run_id=_root_run_id(runtime),
                    producer="deep",
                )
            )
            for planned_node in ready:
                node = plan_state.require_node(planned_node.id)
                payload = await runtime.run_step(
                    node.id,
                    lambda node=node: self._execute_node_uncached(runtime, node, node_results),
                )
                node_results[node.id] = dict(payload or {})
                status = str((payload or {}).get("status") or RunStatus.COMPLETED.value)
                run_id = (payload or {}).get("child_run_id") or (payload or {}).get("run_id")
                try:
                    run_id = int(run_id) if run_id is not None else None
                except (TypeError, ValueError):
                    run_id = None
                plan_state = plan_state.with_node_status(node.id, status, run_id=run_id)
                completed_node = plan_state.require_node(node.id)
                await self._record_phase_result(runtime, completed_node, dict(payload or {}))
                if completed_node.is_terminal:
                    completed_nodes.append(completed_node)
            for completed_node in completed_nodes:
                if _node_kind(completed_node) != "synthesis":
                    plan_state = await self._review_phase_barrier(runtime, plan_state, completed_node, node_results)
            await runtime.store.append_event(
                run_event(
                    runtime.run.id,
                    "run.graph_wave_completed",
                    {
                        "wave": wave_index,
                        "nodes": [node.id for node in ready],
                        "statuses": {node.id: (node_results.get(node.id) or {}).get("status") for node in ready},
                    },
                    root_run_id=_root_run_id(runtime),
                    producer="deep",
                )
            )
        return node_results

    async def _record_phase_result(self, runtime: RunRuntime, node: RunNode, payload: dict[str, Any]) -> None:
        await runtime.run_step(
            f"phase_result:{node.id}",
            lambda: self._record_phase_result_uncached(runtime, node, payload),
        )

    async def _record_phase_result_uncached(self, runtime: RunRuntime, node: RunNode, payload: dict[str, Any]) -> dict[str, Any]:
        artifact = phase_result_artifact(
            run_id=runtime.run.id,
            root_run_id=_root_run_id(runtime),
            node=node,
            payload=payload,
        )
        await runtime.store.append_artifact(artifact)
        await runtime.store.append_event(
            run_event(
                runtime.run.id,
                "run.phase_result_recorded",
                {
                    "node_id": node.id,
                    "node_kind": _node_kind(node),
                    "status": payload.get("status"),
                    "artifact_type": str(getattr(artifact.artifact_type, "value", artifact.artifact_type)),
                },
                root_run_id=_root_run_id(runtime),
                producer="deep",
            )
        )
        return {"node_id": node.id, "status": payload.get("status"), "artifact_type": str(getattr(artifact.artifact_type, "value", artifact.artifact_type))}

    async def _review_phase_barrier(
        self,
        runtime: RunRuntime,
        plan: DeepPlan,
        node: RunNode,
        node_results: dict[str, dict[str, Any]],
    ) -> DeepPlan:
        payload = await runtime.run_step(
            f"phase_barrier:{node.id}",
            lambda: self._review_phase_barrier_uncached(runtime, plan, node, node_results),
        )
        if isinstance(payload, dict) and isinstance(payload.get("plan"), dict):
            return DeepPlan.from_payload(payload.get("plan"))
        return plan

    async def _review_phase_barrier_uncached(
        self,
        runtime: RunRuntime,
        plan: DeepPlan,
        node: RunNode,
        node_results: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        decision = await review_completed_phase(runtime, plan, node, node_results)
        revised_plan, applied = apply_phase_barrier_decision(plan, decision)
        if applied:
            await runtime.store.append_event(
                run_event(
                    runtime.run.id,
                    "run.plan_revised",
                    {
                        "after_node_id": node.id,
                        "summary": decision.summary,
                        "applied": list(applied),
                        "updated_node_ids": [
                            item["node_id"] for item in applied if item.get("status") == "applied"
                        ],
                        "ignored_node_ids": [
                            item["node_id"] for item in applied if item.get("status") == "ignored"
                        ],
                    },
                    root_run_id=_root_run_id(runtime),
                    producer="deep",
                )
            )
            await runtime.store.append_artifact(
                plan_revision_artifact(
                    run_id=runtime.run.id,
                    root_run_id=_root_run_id(runtime),
                    node=node,
                    decision=decision,
                    applied=applied,
                    plan=revised_plan,
                )
            )
        return {
            "node_id": node.id,
            "decision": decision.to_payload(),
            "applied": list(applied),
            "plan": revised_plan.to_payload(),
        }

    async def _execute_node_uncached(
        self,
        runtime: RunRuntime,
        node: RunNode,
        node_results: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        node_kind = _node_kind(node)
        await runtime.activity(f"Running Deep node: {node.id}", step=node.id, node_kind=node_kind)
        await self._record_node_event(runtime, "started", node, status=RunStatus.RUNNING.value)
        try:
            if node_kind == "scout":
                payload = await self._run_scout_node(runtime, node)
            elif node_kind == "worker":
                payload = await self._run_worker_node(runtime, node, node_results)
            elif node_kind == "verification":
                payload = await self._run_verification_node(runtime, node, node_results)
            elif node_kind == "synthesis":
                payload = await self._run_synthesis_node(runtime, node, node_results)
            else:
                raise ValueError(f"Unsupported Deep node kind: {node_kind}")
        except Exception as exc:
            await self._record_node_event(runtime, "failed", node, status=RunStatus.FAILED.value, error=str(exc))
            raise
        status = str(payload.get("status") or RunStatus.COMPLETED.value)
        event_status = "completed" if status == RunStatus.COMPLETED.value else "failed"
        await self._record_node_event(runtime, event_status, node, status=status, result=payload)
        return payload

    async def _skip_node_uncached(self, runtime: RunRuntime, node: RunNode, blocked_by: Iterable[str]) -> dict[str, Any]:
        blocked = tuple(str(item) for item in blocked_by)
        payload = {
            "node_id": node.id,
            "node_kind": _node_kind(node),
            "status": "skipped",
            "blocked_by": list(blocked),
        }
        await self._record_node_event(runtime, "skipped", node, status="skipped", blocked_by=list(blocked))
        return payload

    async def _run_scout_node(self, runtime: RunRuntime, node: RunNode) -> dict[str, Any]:
        child = await runtime.run_child(
            recipe=RunRecipe.SCOUT,
            message=runtime.request.message,
            step_key=f"node:{node.id}",
            profile=RunProfile.FAST,
            metadata={"node_id": node.id, "node_kind": _node_kind(node)},
        )
        scout = scout_request(runtime.request.message)
        payload = {
            "node_id": node.id,
            "node_kind": _node_kind(node),
            "status": _status_value(getattr(child, "status", RunStatus.COMPLETED)) or RunStatus.COMPLETED.value,
            "child_run_id": child.id,
            **scout.to_payload(),
        }
        await runtime.store.append_artifact(
            AgentRunArtifact(
                run_id=runtime.run.id,
                root_run_id=_root_run_id(runtime),
                artifact_type="scout_handoff",
                title="Scout handoff",
                payload=payload,
            )
        )
        return payload

    async def _run_worker_node(
        self,
        runtime: RunRuntime,
        node: RunNode,
        node_results: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        assignment = _assignment_for_node(node)
        scout = _scout_from_payload(node_results.get("scout"))
        metadata = {
            **dict(runtime.request.metadata or {}),
            "parent_node_id": node.id,
            "worker_role": assignment.role,
            "worker_assignment": assignment.to_payload(),
            "evidence_requirements": [requirement.to_payload() for requirement in assignment.evidence_requirements],
        }
        child = await runtime.create_child_run(
            recipe=RunRecipe.WORKER,
            message=self._worker_message(runtime, scout, assignment),
            step_key=f"node:{node.id}",
            profile=RunProfile.DEEP,
            metadata=metadata,
        )
        await runtime.store.append_event(
            run_event(
                runtime.run.id,
                "run.worker_started",
                {
                    "node_id": node.id,
                    "child_run_id": child.id,
                    "assignment": assignment.to_payload(),
                    **_worker_event_fields(assignment),
                },
                root_run_id=_root_run_id(runtime),
                producer="deep",
            )
        )
        completed = await self._run_existing_child(runtime, child)
        artifacts = await _child_artifacts(runtime.store, child.id)
        output = _child_output(artifacts)
        status = _status_value(getattr(completed, "status", RunStatus.FAILED))
        event_type = "run.worker_completed" if status == RunStatus.COMPLETED.value else "run.worker_failed"
        await runtime.store.append_event(
            run_event(
                runtime.run.id,
                event_type,
                {
                    "node_id": node.id,
                    "child_run_id": child.id,
                    "status": status,
                    "artifact_count": len(artifacts),
                    "assignment": assignment.to_payload(),
                    **_worker_event_fields(assignment),
                },
                root_run_id=_root_run_id(runtime),
                producer="deep",
            )
        )
        return worker_evidence_from_artifacts(
            run_id=child.id,
            assignment=assignment,
            status=status,
            artifacts=artifacts,
            output=output,
            node_id=node.id,
        )

    async def _run_verification_node(
        self,
        runtime: RunRuntime,
        node: RunNode,
        node_results: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        mode = verification_mode_for_run(runtime.run.profile, runtime.request.metadata)
        worker_evidence = [
            result for result in node_results.values() if result.get("node_kind") == "worker" or result.get("assignment")
        ]
        result = verify_worker_evidence(worker_evidence, mode=mode)
        payload = {
            "node_id": node.id,
            "node_kind": _node_kind(node),
            "status": RunStatus.COMPLETED.value if result.passed else RunStatus.FAILED.value,
            "mode": result.mode.value,
            "passed": result.passed,
            "warning": result.warning,
            "details": result.details or {},
        }
        await runtime.store.append_artifact(
            verification_evidence(
                runtime.run.id,
                title="Deep verification report",
                payload=payload,
                root_run_id=_root_run_id(runtime),
            )
        )
        await runtime.store.append_event(
            run_event(
                runtime.run.id,
                "run.verification_passed" if result.passed else "run.verification_failed",
                payload,
                root_run_id=_root_run_id(runtime),
                producer="deep",
            )
        )
        return payload

    async def _run_synthesis_node(
        self,
        runtime: RunRuntime,
        node: RunNode,
        node_results: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        output = await self._synthesize_with_coordinator(runtime, node_results)
        return {"node_id": node.id, "node_kind": _node_kind(node), "status": RunStatus.COMPLETED.value, "output": output}

    async def _synthesize_with_coordinator(self, runtime: RunRuntime, node_results: dict[str, dict[str, Any]]) -> str:
        fallback = self._synthesize_output(node_results)
        model, thinking = _coordinator_model_and_thinking(runtime)
        system_prompt = soul_prompt_section() + "\n\n" + DEEP_COORDINATOR_SYNTHESIS_INSTRUCTIONS
        payload = _coordinator_synthesis_payload(runtime, node_results)
        await runtime.store.append_event(
            run_event(
                runtime.run.id,
                "run.coordinator_synthesis_started",
                {"worker_count": len(payload.get("workers") or [])},
                root_run_id=_root_run_id(runtime),
                producer="deep",
            )
        )
        try:
            spec = build_direct_agent_invocation(
                message=json.dumps(payload, indent=2, default=str),
                system_prompt=system_prompt,
                session_id=f"agent-run-{runtime.run.id}-synthesis",
                model=str(model),
                thinking=str(thinking),
                tools=[],
                tool_handlers={},
                persist_session=False,
                max_turns=2,
                workspace_root=workspace_root_from_ref(runtime.request.workspace_ref),
                user_id=runtime.request.user_id,
                run_id=runtime.run.id,
                idea_id=None,
                tool_call_source="coordinator",
                brain_context_preloaded=True,
                skip_harvest=True,
                metadata={
                    "org_id": runtime.request.org_id,
                    "profile": str(runtime.request.normalized_profile.value),
                    "recipe": "deep_synthesis",
                    "root_run_id": runtime.run.root_run_id,
                    "provider_operation_type": "coordinator",
                },
            )
            result = await invoke_direct_agent_async(spec)
            success = bool(getattr(result, "success", False))
            output = str(getattr(result, "output", "") or "").strip()
            used_fallback = False
            if not success or not output:
                output = fallback
                used_fallback = True
            await runtime.store.append_event(
                run_event(
                    runtime.run.id,
                    "run.coordinator_synthesis_completed",
                    {
                        "used_fallback": used_fallback,
                        "success": success,
                    },
                    root_run_id=_root_run_id(runtime),
                    producer="deep",
                )
            )
            return output
        except Exception as exc:
            logger.warning("deep_coordinator_synthesis_failed run_id=%s error=%s", runtime.run.id, exc)
            await runtime.store.append_event(
                run_event(
                    runtime.run.id,
                    "run.coordinator_synthesis_failed",
                    {"error": str(exc)},
                    root_run_id=_root_run_id(runtime),
                    producer="deep",
                )
            )
            return fallback

    def _synthesize_output(self, node_results: dict[str, dict[str, Any]]) -> str:
        scout = _scout_from_payload(node_results.get("scout"))
        worker_rows = _worker_synthesis_rows(node_results)
        lines = ["Deep completed using native AgentRun workers.", "", f"Scout: {scout.summary}", "", "Worker results:"]
        for row in worker_rows:
            detail = _truncate(str(row.get("output") or "")) or str(row.get("warning") or "No worker output recorded.")
            lines.append(f"- {row.get('role')} (run {row.get('run_id')}, {row.get('status')}): {detail}")
        return "\n".join(lines).strip()

    def _verification_result(self, payload: dict[str, Any] | None) -> VerificationResult | None:
        if not isinstance(payload, dict) or "passed" not in payload:
            return None
        try:
            mode = VerificationMode(str(payload.get("mode") or VerificationMode.BLOCKING.value))
        except Exception:
            mode = VerificationMode.BLOCKING
        return VerificationResult(
            mode=mode,
            passed=bool(payload.get("passed")),
            warning=str(payload["warning"]) if payload.get("warning") else None,
            details=dict(payload.get("details") or {}),
        )

    async def _run_existing_child(self, runtime: RunRuntime, child: AgentRun) -> AgentRun:
        if runtime.engine is None:
            raise RuntimeError("RunRuntime cannot execute child runs without an engine")
        return await runtime.engine.run_existing(child.id)

    def _configured_assignments(self, metadata: dict[str, Any]) -> list[WorkerAssignment] | None:
        configured = metadata.get("worker_assignments") or metadata.get("deep_workers")
        if not isinstance(configured, list):
            return None
        assignments: list[WorkerAssignment] = []
        used_ids: set[str] = set()
        for index, item in enumerate(configured[:4], start=1):
            if not isinstance(item, dict):
                continue
            objective = str(item.get("objective") or item.get("task") or "").strip()
            if not objective:
                continue
            role = str(item.get("role") or f"worker-{index}").strip()
            node_id = str(item.get("id") or item.get("node_id") or _node_id("worker", role, index)).strip()
            depends_on = _string_tuple(item.get("depends_on") or item.get("dependencies"))
            item_metadata = dict(item.get("metadata") or {})
            if depends_on:
                item_metadata["depends_on"] = list(depends_on)
            assignment = WorkerAssignment.from_payload({
                **item,
                "id": _unique_node_id(node_id, used_ids),
                "role": role,
                "objective": objective,
                "metadata": item_metadata,
            })
            used_ids.add(assignment.id)
            assignments.append(assignment)
        return assignments or None

    def _default_assignments(self, message: str) -> list[WorkerAssignment]:
        base = message.strip()
        return [
            WorkerAssignment(
                id="investigate",
                role="investigate",
                objective=f"Gather context, constraints, and evidence for this request: {base}",
                evidence_requirements=(
                    EvidenceRequirement(
                        description="Produce the worker result summary",
                        artifact_type="worker_result",
                    ),
                    EvidenceRequirement(
                        description="Capture relevant file or command evidence when available",
                        required=False,
                        artifact_type="file_observation",
                    ),
                ),
                acceptance_criteria=AcceptanceCriteria(
                    checklist=("Relevant context and uncertainty are explicit.",),
                ),
                risk_level="low",
            ),
            WorkerAssignment(
                id="execute",
                role="execute",
                objective=f"Do the primary scoped work for this request and report concrete results: {base}",
                evidence_requirements=(
                    EvidenceRequirement(
                        description="Produce the worker result summary",
                        artifact_type="worker_result",
                    ),
                ),
                acceptance_criteria=AcceptanceCriteria(
                    checklist=("Concrete changes, findings, or blockers are reported with evidence.",),
                ),
                risk_level="medium",
            ),
        ]

    def _worker_message(self, runtime: RunRuntime, scout: ScoutHandoff, assignment: WorkerAssignment) -> str:
        requirements = ", ".join(
            requirement.artifact_type or requirement.description for requirement in assignment.required_evidence()
        )
        barrier_guidance = str(assignment.metadata.get("phase_barrier_message") or "").strip()
        barrier_reason = str(assignment.metadata.get("phase_barrier_reason") or "").strip()
        barrier_block = ""
        if barrier_guidance or barrier_reason:
            barrier_block = (
                "\n"
                f"Coordinator revision reason: {barrier_reason or 'updated after prior phase output'}\n"
                f"Coordinator revision guidance: {barrier_guidance or assignment.objective}\n"
            )
        return (
            f"Parent Deep run {runtime.run.id}\n"
            f"Worker assignment: {assignment.id}\n"
            f"Role: {assignment.role}\n"
            f"Objective: {assignment.objective}\n"
            f"Scout summary: {scout.summary}\n"
            f"{barrier_block}"
            f"Required evidence: {requirements or 'worker_result'}"
        )

    async def _record_node_event(
        self,
        runtime: RunRuntime,
        event_status: str,
        node: RunNode,
        *,
        status: str,
        **payload: Any,
    ) -> None:
        await runtime.store.append_event(
            run_event(
                runtime.run.id,
                f"run.node_{event_status}",
                {
                    "node_id": node.id,
                    "node_kind": _node_kind(node),
                    "status": status,
                    **payload,
                },
                root_run_id=_root_run_id(runtime),
                producer="deep",
            )
        )


def _worker_event_fields(assignment: WorkerAssignment) -> dict[str, Any]:
    return {
        "role": assignment.role,
        "objective": assignment.objective,
        "risk_level": assignment.risk_level,
        "evidence_requirements": [requirement.to_payload() for requirement in assignment.required_evidence()],
        "acceptance_criteria": assignment.acceptance_criteria.to_payload(),
    }


async def _child_artifacts(store: Any, child_run_id: int) -> list[Any]:
    if not hasattr(store, "list_artifacts"):
        return []
    artifacts = store.list_artifacts(child_run_id)
    if hasattr(artifacts, "__await__"):
        artifacts = await artifacts
    return list(artifacts)


def _child_output(artifacts: list[Any]) -> str:
    latest_final = ""
    latest_worker = ""
    for artifact in artifacts:
        artifact_type = _artifact_type(artifact)
        text = str((artifact.get("text") if isinstance(artifact, dict) else getattr(artifact, "text", None)) or "")
        if artifact_type == "final_answer" and text:
            latest_final = text
        elif artifact_type == "worker_result" and text:
            latest_worker = text
    return latest_final or latest_worker


def _coordinator_model_and_thinking(runtime: RunRuntime) -> tuple[str, str]:
    model_policy = dict(runtime.request.model_policy or {})
    model = model_policy.get("coordinator_model") or model_policy.get("model")
    if not model:
        model = get_model_for_tier(
            model_policy.get("coordinator_tier") or model_policy.get("tier") or "high",
            include_provider_prefix=True,
            user_id=runtime.request.user_id,
            org_id=runtime.request.org_id,
        )
    thinking = model_policy.get("coordinator_thinking") or model_policy.get("thinking") or "high"
    return str(model), str(thinking)


def _coordinator_synthesis_payload(runtime: RunRuntime, node_results: dict[str, dict[str, Any]]) -> dict[str, Any]:
    scout = _scout_from_payload(node_results.get("scout"))
    verification = dict(node_results.get("verify") or {})
    return {
        "task": runtime.request.message,
        "run_id": runtime.run.id,
        "scout": scout.to_payload(),
        "verification": {
            "status": verification.get("status"),
            "passed": verification.get("passed"),
            "warning": verification.get("warning"),
            "details": verification.get("details") or {},
        },
        "workers": _worker_synthesis_rows(node_results),
    }


def _worker_synthesis_rows(node_results: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for result in node_results.values():
        if not result.get("assignment"):
            continue
        assignment = _assignment_from_worker_result(result)
        rows.append(
            {
                "node_id": result.get("node_id"),
                "run_id": result.get("run_id") or result.get("child_run_id"),
                "role": assignment.role,
                "status": result.get("status"),
                "objective": assignment.objective,
                "output": _truncate(str(result.get("output") or ""), limit=4000),
                "warning": result.get("warning"),
                "evidence": _synthesis_artifact_summaries(result.get("artifacts")),
            }
        )
    return rows


def _assignment_from_worker_result(result: Mapping[str, Any]) -> WorkerAssignment:
    assignment_payload = result.get("assignment") if isinstance(result.get("assignment"), Mapping) else {}
    return WorkerAssignment.from_payload(
        {"id": str(result.get("node_id") or "worker"), **dict(assignment_payload)}
    )


def _synthesis_artifact_summaries(value: Any) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for artifact in artifact_payloads(value)[:8]:
        payload = artifact.get("payload") if isinstance(artifact.get("payload"), Mapping) else {}
        summaries.append(
            {
                "artifact_type": artifact.get("artifact_type"),
                "title": artifact.get("title"),
                "has_text": bool(artifact.get("has_text")),
                "text": _truncate(str(artifact.get("text") or ""), limit=1600),
                "payload": _truncate(json.dumps(payload, sort_keys=True, default=str), limit=1600) if payload else "",
                "uri": artifact.get("uri"),
            }
        )
    return summaries


def _artifact_type(artifact: Any) -> str:
    if isinstance(artifact, dict):
        artifact_type = artifact.get("artifact_type") or artifact.get("type") or ""
        return str(getattr(artifact_type, "value", artifact_type) or "")
    artifact_type = getattr(artifact, "artifact_type", "")
    return str(getattr(artifact_type, "value", artifact_type) or "")


def _ready_wave(plan: DeepPlan) -> tuple[RunNode, ...]:
    """Return all currently ready nodes so parallel graph waves remain intact."""
    ready = list(plan.ready_nodes())
    order = _plan_order(plan)
    for node in plan.pending_nodes():
        if _node_kind(node) != "verification" or node in ready:
            continue
        dependencies = tuple(plan.require_node(dependency) for dependency in plan.dependency_ids(node.id))
        if dependencies and all(dependency.is_terminal for dependency in dependencies):
            ready.append(node)
    ready.sort(key=lambda node: order[node.id])
    return tuple(ready)


def _blocked_pending_nodes(plan: DeepPlan) -> tuple[RunNode, ...]:
    blocked: list[RunNode] = []
    order = _plan_order(plan)
    for node in plan.pending_nodes():
        if _node_kind(node) == "verification":
            continue
        dependencies = tuple(plan.require_node(dependency) for dependency in plan.dependency_ids(node.id))
        if any(dependency.is_failed or dependency.status == "skipped" for dependency in dependencies):
            blocked.append(node)
    blocked.sort(key=lambda node: order[node.id])
    return tuple(blocked)


def _plan_order(plan: DeepPlan) -> dict[str, tuple[int, int]]:
    return {node.id: (int(node.wave or 0), index) for index, node in enumerate(plan.nodes)}


def _assignment_depends_on(assignment: WorkerAssignment) -> tuple[str, ...]:
    return _string_tuple(assignment.metadata.get("depends_on") or assignment.metadata.get("dependencies"))


def _node_kind(node: RunNode) -> str:
    kind = str(getattr(node, "kind", None) or "").strip().lower()
    recipe = str(getattr(node, "recipe", None) or "").strip().lower()
    if kind == "worker" and recipe in {"scout", "verification", "synthesis"}:
        return recipe
    return kind or recipe or str(getattr(node, "role", None) or "worker").strip().lower()


def _assignment_for_node(node: RunNode) -> WorkerAssignment:
    assignment = getattr(node, "assignment", None)
    if isinstance(assignment, WorkerAssignment):
        return assignment
    if isinstance(assignment, Mapping):
        return WorkerAssignment.from_payload(assignment, default_id=str(node.id or "worker"))
    metadata = getattr(node, "metadata", None)
    metadata_assignment = metadata.get("assignment") if isinstance(metadata, Mapping) else None
    if isinstance(metadata_assignment, Mapping):
        return WorkerAssignment.from_payload(metadata_assignment, default_id=str(node.id or "worker"))
    return WorkerAssignment.from_payload(
        {
            "id": str(getattr(node, "id", "") or "worker"),
            "node_id": str(getattr(node, "id", "") or "worker"),
            "role": str(getattr(node, "role", "") or "worker"),
            "objective": str(getattr(node, "objective", "") or ""),
        }
    )


def _node_id(prefix: str, role: str, index: int) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(role or "").lower()).strip("-")
    return f"{prefix}-{slug or index}"


def _unique_node_id(candidate: str, used: set[str]) -> str:
    if candidate not in used:
        return candidate
    suffix = 2
    while f"{candidate}-{suffix}" in used:
        suffix += 1
    return f"{candidate}-{suffix}"


def _string_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        text = value.strip()
        return (text,) if text else ()
    if isinstance(value, Mapping):
        value = value.values()
    try:
        return tuple(str(item).strip() for item in value if str(item).strip())
    except TypeError:
        text = str(value).strip()
        return (text,) if text else ()


__all__ = ["DeepRecipe"]
