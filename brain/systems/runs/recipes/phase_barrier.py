"""Coordinator phase-barrier review for Deep AgentRun plans."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from brain.systems.runs.assignments import WorkerAssignment
from brain.systems.runs.domain import AgentRunArtifact, ArtifactType
from brain.systems.runs.events import run_event
from brain.systems.runs.graph import DeepPlan, RunNode
from brain.systems.runs.invocation import build_direct_agent_invocation, invoke_direct_agent_async
from brain.systems.runs.recipes.shared import default_run_model, workspace_root_from_ref

logger = logging.getLogger(__name__)


PHASE_REVIEW_SYSTEM_PROMPT = """You are the Deep run coordinator.

Review the completed phase result and decide whether pending downstream worker
assignments should change. You are a project lead: preserve completed work,
adapt pending work when evidence changes the plan, and avoid churn.
If no pending worker assignments remain, review the phase and return an empty
revisions array.

Return only JSON:
{
  "summary": "short review",
  "revisions": [
    {
      "node_id": "pending-node-id",
      "reason": "why this pending assignment should change",
      "role": "optional role",
      "objective": "optional revised objective",
      "message": "optional extra guidance to include in the worker handoff",
      "assignment": {"optional": "full or partial WorkerAssignment payload"},
      "metadata": {"optional": "extra pending-node metadata"}
    }
  ]
}

Use an empty revisions array when the remaining plan should stand.
"""


@dataclass(frozen=True)
class PhasePlanRevision:
    node_id: str
    reason: str = ""
    role: str | None = None
    objective: str | None = None
    message: str | None = None
    assignment: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "PhasePlanRevision":
        assignment = payload.get("assignment")
        metadata = payload.get("metadata")
        return cls(
            node_id=str(payload.get("node_id") or payload.get("id") or "").strip(),
            reason=str(payload.get("reason") or "").strip(),
            role=_optional_text(payload.get("role")),
            objective=_optional_text(payload.get("objective")),
            message=_optional_text(payload.get("message") or payload.get("guidance")),
            assignment=dict(assignment) if isinstance(assignment, Mapping) else {},
            metadata=dict(metadata) if isinstance(metadata, Mapping) else {},
        )

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"node_id": self.node_id, "reason": self.reason}
        if self.role:
            payload["role"] = self.role
        if self.objective:
            payload["objective"] = self.objective
        if self.message:
            payload["message"] = self.message
        if self.assignment:
            payload["assignment"] = dict(self.assignment)
        if self.metadata:
            payload["metadata"] = dict(self.metadata)
        return payload


@dataclass(frozen=True)
class PhaseBarrierDecision:
    summary: str = ""
    revisions: tuple[PhasePlanRevision, ...] = ()
    raw_output: str = ""

    @classmethod
    def no_change(cls, summary: str = "No pending plan changes.") -> "PhaseBarrierDecision":
        return cls(summary=summary, revisions=())

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any], *, raw_output: str = "") -> "PhaseBarrierDecision":
        revisions = payload.get("revisions")
        if not isinstance(revisions, list):
            revisions = []
        return cls(
            summary=str(payload.get("summary") or "").strip(),
            revisions=tuple(
                revision
                for item in revisions
                if isinstance(item, Mapping)
                for revision in (PhasePlanRevision.from_payload(item),)
                if revision.node_id
            ),
            raw_output=raw_output,
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "revisions": [revision.to_payload() for revision in self.revisions],
            "raw_output": self.raw_output,
        }


async def review_completed_phase(
    runtime: Any,
    plan: DeepPlan,
    node: RunNode,
    node_results: Mapping[str, dict[str, Any]],
) -> PhaseBarrierDecision:
    """Ask the Deep coordinator to review a completed phase and revise pending work."""
    metadata = dict(getattr(runtime.request, "metadata", {}) or {})
    if metadata.get("disable_phase_barrier_review") is True:
        return PhaseBarrierDecision.no_change("Phase barrier review disabled for this run.")

    payload = _review_payload(plan, node, node_results)
    model_policy = dict(getattr(runtime.request, "model_policy", {}) or {})
    model = model_policy.get("coordinator_model") or model_policy.get("model")
    if not model:
        model = await default_run_model(
            user_id=getattr(runtime.request, "user_id", None),
            org_id=getattr(runtime.request, "org_id", None),
        )
    thinking = model_policy.get("coordinator_thinking") or model_policy.get("thinking") or "high"

    await runtime.store.append_event(
        run_event(
            runtime.run.id,
            "run.phase_review_started",
            {"node_id": node.id, "completed_node_id": node.id, "pending_nodes": _pending_node_ids(plan)},
            root_run_id=runtime.run.root_run_id,
            producer="deep",
        )
    )
    try:
        spec = build_direct_agent_invocation(
            message=json.dumps(payload, indent=2, default=str),
            system_prompt=PHASE_REVIEW_SYSTEM_PROMPT,
            session_id=f"agent-run-{runtime.run.id}-phase-review-{node.id}",
            model=str(model),
            thinking=str(thinking),
            tools=[],
            tool_handlers={},
            persist_session=False,
            max_turns=2,
            workspace_root=workspace_root_from_ref(getattr(runtime.request, "workspace_ref", {}) or {}),
            user_id=getattr(runtime.request, "user_id", None),
            org_id=getattr(runtime.request, "org_id", None),
            run_id=runtime.run.id,
            tool_call_source="coordinator",
            brain_context_preloaded=True,
            skip_harvest=True,
            metadata={
                "profile": str(getattr(runtime.run.profile, "value", runtime.run.profile)),
                "recipe": "deep_phase_review",
                "root_run_id": runtime.run.root_run_id,
                "phase_node_id": node.id,
            },
        )
        result = await invoke_direct_agent_async(spec)
        raw_output = str(getattr(result, "output", "") or "").strip()
        parsed = _parse_json_object(raw_output)
        decision = PhaseBarrierDecision.from_payload(parsed or {}, raw_output=raw_output)
    except Exception as exc:
        logger.warning("deep_phase_barrier_review_failed run_id=%s node_id=%s error=%s", runtime.run.id, node.id, exc)
        decision = PhaseBarrierDecision.no_change(f"Phase review failed open: {exc}")
    await runtime.store.append_event(
        run_event(
            runtime.run.id,
            "run.phase_review_completed",
            {"node_id": node.id, "completed_node_id": node.id, **decision.to_payload()},
            root_run_id=runtime.run.root_run_id,
            producer="deep",
        )
    )
    return decision


def apply_phase_barrier_decision(plan: DeepPlan, decision: PhaseBarrierDecision) -> tuple[DeepPlan, tuple[dict[str, Any], ...]]:
    """Apply coordinator revisions to pending worker nodes only."""
    if not decision.revisions:
        return plan, ()

    nodes = list(plan.nodes)
    applied: list[dict[str, Any]] = []
    for revision in decision.revisions:
        index = next((idx for idx, node in enumerate(nodes) if node.id == revision.node_id), None)
        if index is None:
            applied.append({"node_id": revision.node_id, "status": "ignored", "reason": "node_not_found"})
            continue
        node = nodes[index]
        if not node.is_pending:
            applied.append({"node_id": node.id, "status": "ignored", "reason": "node_not_pending"})
            continue
        if _node_kind(node) != "worker":
            applied.append({"node_id": node.id, "status": "ignored", "reason": "node_not_worker"})
            continue

        updated = _revise_worker_node(node, revision)
        nodes[index] = updated
        applied.append(
            {
                "node_id": node.id,
                "status": "applied",
                "reason": revision.reason,
                "before": _node_revision_snapshot(node),
                "after": _node_revision_snapshot(updated),
            }
        )

    if not any(item.get("status") == "applied" for item in applied):
        return plan, tuple(applied)
    return (
        DeepPlan(
            nodes=tuple(nodes),
            edges=plan.edges,
            id=plan.id,
            objective=plan.objective,
            metadata={**dict(plan.metadata), "last_revision_summary": decision.summary},
        ),
        tuple(applied),
    )


def phase_result_artifact(
    *,
    run_id: int,
    root_run_id: int | None,
    node: RunNode,
    payload: Mapping[str, Any],
) -> AgentRunArtifact:
    text = str(payload.get("output") or payload.get("summary") or payload.get("warning") or "").strip()
    return AgentRunArtifact(
        run_id=run_id,
        root_run_id=root_run_id,
        artifact_type=ArtifactType.PHASE_RESULT,
        title=f"Phase result: {node.id}",
        payload={
            "node_id": node.id,
            "node_kind": _node_kind(node),
            "node": node.to_payload(),
            "result": dict(payload),
            "output": dict(payload),
        },
        text=text or None,
    )


def plan_revision_artifact(
    *,
    run_id: int,
    root_run_id: int | None,
    node: RunNode,
    decision: PhaseBarrierDecision,
    applied: tuple[dict[str, Any], ...],
    plan: DeepPlan,
) -> AgentRunArtifact:
    return AgentRunArtifact(
        run_id=run_id,
        root_run_id=root_run_id,
        artifact_type=ArtifactType.DEEP_PLAN_REVISION,
        title=f"Plan revision after: {node.id}",
        payload={
            "node_id": node.id,
            "decision": decision.to_payload(),
            "applied": list(applied),
            "plan": plan.to_payload(),
        },
        text=decision.summary or None,
    )


def _review_payload(plan: DeepPlan, node: RunNode, node_results: Mapping[str, dict[str, Any]]) -> dict[str, Any]:
    completed_result = dict(node_results.get(node.id) or {})
    return {
        "objective": plan.objective,
        "completed_phase": {
            "node": node.to_payload(),
            "result": completed_result,
        },
        "completed_results": {
            node_id: result
            for node_id, result in node_results.items()
            if plan.require_node(node_id).is_terminal
        },
        "pending_nodes": [
            node.to_payload()
            for node in plan.pending_nodes()
            if _node_kind(node) == "worker"
        ],
        "plan": plan.to_payload(),
        "instructions": (
            "Revise only pending worker nodes. Do not alter completed nodes. "
            "If the completed phase learned a better downstream plan, update pending objectives."
        ),
    }


def _revise_worker_node(node: RunNode, revision: PhasePlanRevision) -> RunNode:
    assignment = _assignment_for_node(node)
    assignment_payload = assignment.to_payload()
    assignment_patch = dict(revision.assignment)
    if revision.role:
        assignment_patch["role"] = revision.role
    if revision.objective:
        assignment_patch["objective"] = revision.objective
    revision_metadata = dict(assignment_payload.get("metadata") or {})
    if isinstance(assignment_patch.get("metadata"), Mapping):
        revision_metadata.update(dict(assignment_patch.get("metadata") or {}))
    if revision.reason:
        revision_metadata["phase_barrier_reason"] = revision.reason
    if revision.message:
        revision_metadata["phase_barrier_message"] = revision.message
    if revision_metadata:
        assignment_patch["metadata"] = revision_metadata
    next_assignment = WorkerAssignment.from_payload(
        {
            **assignment_payload,
            **assignment_patch,
            "id": assignment_payload.get("id") or node.id,
            "node_id": assignment_payload.get("node_id") or node.id,
        },
        default_id=node.id,
    )
    metadata = dict(node.metadata)
    if revision.metadata:
        metadata.update(revision.metadata)
    metadata.setdefault("phase_barrier_revisions", [])
    metadata["phase_barrier_revisions"] = [
        *list(metadata.get("phase_barrier_revisions") or []),
        revision.to_payload(),
    ]
    return replace(
        node,
        role=revision.role or next_assignment.role or node.role,
        objective=revision.objective or next_assignment.objective or node.objective,
        assignment=next_assignment,
        metadata=metadata,
    )


def _assignment_for_node(node: RunNode) -> WorkerAssignment:
    assignment = getattr(node, "assignment", None)
    if isinstance(assignment, WorkerAssignment):
        return assignment
    if isinstance(assignment, Mapping):
        return WorkerAssignment.from_payload(assignment, default_id=node.id)
    metadata = getattr(node, "metadata", None)
    metadata_assignment = metadata.get("assignment") if isinstance(metadata, Mapping) else None
    if isinstance(metadata_assignment, Mapping):
        return WorkerAssignment.from_payload(metadata_assignment, default_id=node.id)
    return WorkerAssignment.from_payload(
        {
            "id": node.id,
            "node_id": node.id,
            "role": node.role,
            "objective": node.objective,
        },
        default_id=node.id,
    )


def _node_revision_snapshot(node: RunNode) -> dict[str, Any]:
    assignment = _assignment_for_node(node)
    return {
        "node_id": node.id,
        "role": node.role,
        "objective": node.objective,
        "assignment": assignment.to_payload(),
    }


def _pending_node_ids(plan: DeepPlan) -> list[str]:
    return [node.id for node in plan.pending_nodes()]


def _node_kind(node: RunNode) -> str:
    kind = str(getattr(node, "kind", None) or "").strip().lower()
    recipe = str(getattr(node, "recipe", None) or "").strip().lower()
    if kind == "worker" and recipe in {"scout", "verification", "synthesis"}:
        return recipe
    return kind or recipe or str(getattr(node, "role", None) or "worker").strip().lower()


def _parse_json_object(raw: str) -> dict[str, Any] | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


__all__ = [
    "PhaseBarrierDecision",
    "PhasePlanRevision",
    "apply_phase_barrier_decision",
    "phase_result_artifact",
    "plan_revision_artifact",
    "review_completed_phase",
]
