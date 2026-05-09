"""Evidence artifacts produced by verification gates."""

from __future__ import annotations

from typing import Any

from brain.systems.runs.assignments import WorkerAssignment
from brain.systems.runs.artifacts import evidence_artifact
from brain.systems.runs.domain import AgentRunArtifact


def verification_evidence(run_id: int, *, title: str, payload: dict[str, Any], root_run_id: int | None = None) -> AgentRunArtifact:
    return evidence_artifact(run_id, title=title, payload=payload, root_run_id=root_run_id)


def worker_evidence_from_artifacts(
    *,
    run_id: int,
    assignment: WorkerAssignment,
    status: str,
    artifacts: list[Any],
    output: str = "",
    node_id: str | None = None,
) -> dict[str, Any]:
    """Build the verification-facing evidence payload for one worker child run."""

    return {
        "node_id": node_id or assignment.id,
        "node_kind": "worker",
        "run_id": run_id,
        "child_run_id": run_id,
        "role": assignment.role,
        "status": status,
        "output": output,
        "artifact_count": len(artifacts),
        "assignment": assignment.to_payload(),
        "artifacts": artifact_payloads(artifacts),
        "warning": None if status == "completed" else f"worker ended with status {status}",
    }


def artifact_payloads(artifacts: Any) -> list[dict[str, Any]]:
    if not isinstance(artifacts, (list, tuple)):
        return []
    return [artifact_payload(artifact) for artifact in artifacts]


def artifact_payload(artifact: Any) -> dict[str, Any]:
    if isinstance(artifact, dict):
        artifact_type = artifact.get("artifact_type") or artifact.get("type")
        payload = artifact.get("payload")
        return {
            "id": artifact.get("id"),
            "artifact_type": str(getattr(artifact_type, "value", artifact_type) or ""),
            "title": artifact.get("title"),
            "payload": dict(payload) if isinstance(payload, dict) else {},
            "has_text": bool(str(artifact.get("text") or "").strip()),
            "text": str(artifact.get("text") or ""),
            "uri": artifact.get("uri"),
        }
    artifact_type = getattr(artifact, "artifact_type", "")
    return {
        "id": getattr(artifact, "id", None),
        "artifact_type": str(getattr(artifact_type, "value", artifact_type) or ""),
        "title": getattr(artifact, "title", None),
        "payload": dict(getattr(artifact, "payload", None) or {}),
        "has_text": bool(str(getattr(artifact, "text", None) or "").strip()),
        "text": str(getattr(artifact, "text", None) or ""),
        "uri": getattr(artifact, "uri", None),
    }


__all__ = ["artifact_payload", "artifact_payloads", "verification_evidence", "worker_evidence_from_artifacts"]
