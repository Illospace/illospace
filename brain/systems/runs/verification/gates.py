"""Verification gates used by recipes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from brain.systems.runs.assignments import EvidenceRequirement, WorkerAssignment
from brain.systems.runs.verification.policy import VerificationMode


@dataclass(frozen=True)
class VerificationResult:
    mode: VerificationMode
    passed: bool
    warning: str | None = None
    details: dict[str, Any] | None = None


def verify_text_output(text: str, *, mode: VerificationMode) -> VerificationResult:
    if mode == VerificationMode.SKIP:
        return VerificationResult(mode=mode, passed=True, details={"skipped": True})
    if not text.strip():
        return VerificationResult(mode=mode, passed=False, warning="empty output", details={"characters": 0})
    return VerificationResult(mode=mode, passed=True, details={"characters": len(text.strip())})


def verify_worker_evidence(worker_results: Iterable[Mapping[str, Any]], *, mode: VerificationMode) -> VerificationResult:
    results = [dict(result) for result in worker_results]
    if mode == VerificationMode.SKIP:
        return VerificationResult(mode=mode, passed=True, details={"skipped": True, "worker_count": len(results)})
    if not results:
        return VerificationResult(mode=mode, passed=False, warning="no worker evidence", details={"worker_count": 0})

    failed = [result for result in results if _status_value(result.get("status")) != "completed"]
    missing_evidence = [result for result in results if _lacks_any_evidence(result)]
    missing_required = _missing_required_evidence(results)
    warnings: list[str] = []
    if failed:
        warnings.append(f"{len(failed)} worker run(s) failed")
    if missing_evidence:
        warnings.append(f"{len(missing_evidence)} worker run(s) lacked evidence")
    if missing_required:
        warnings.append(f"{len(missing_required)} required evidence requirement(s) missing")

    combined_output = "\n".join(str(result.get("output") or "") for result in results)
    text_gate = verify_text_output(combined_output, mode=mode)
    if not text_gate.passed and not missing_evidence:
        warnings.append(text_gate.warning or "worker output did not pass text gate")

    details = {
        "worker_count": len(results),
        "failed_worker_run_ids": [result.get("run_id") for result in failed],
        "missing_evidence_run_ids": [result.get("run_id") for result in missing_evidence],
        "missing_required_evidence": missing_required,
        "artifact_counts": {str(result.get("run_id")): int(result.get("artifact_count") or 0) for result in results},
        "text_gate": text_gate.details or {},
    }
    return VerificationResult(mode=mode, passed=not warnings, warning="; ".join(warnings) or None, details=details)


def _lacks_any_evidence(result: Mapping[str, Any]) -> bool:
    return not str(result.get("output") or "").strip() and int(result.get("artifact_count") or 0) <= 0


def _missing_required_evidence(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    missing: list[dict[str, Any]] = []
    for result in results:
        assignment_payload = result.get("assignment") or result.get("worker_assignment") or result.get("scope") or {}
        if not isinstance(assignment_payload, Mapping):
            assignment_payload = {}
        assignment = WorkerAssignment.from_payload(
            {
                "id": result.get("node_id") or result.get("role") or "worker",
                "role": result.get("role") or "worker",
                **dict(assignment_payload),
            }
        )
        artifacts = _artifact_payloads(result.get("artifacts"))
        for requirement in assignment.required_evidence():
            if not _requirement_satisfied(requirement, artifacts, result):
                missing.append(
                    {
                        "run_id": result.get("run_id") or result.get("child_run_id"),
                        "node_id": result.get("node_id"),
                        "role": assignment.role,
                        "requirement": requirement.to_payload(),
                    }
                )
    return missing


def _artifact_payloads(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    artifacts: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, Mapping):
            artifacts.append(dict(item))
    return artifacts


def _requirement_satisfied(
    requirement: EvidenceRequirement,
    artifacts: list[dict[str, Any]],
    result: Mapping[str, Any],
) -> bool:
    if requirement.is_satisfied_by(artifacts):
        return True
    if requirement.artifact_type:
        return False
    if artifacts:
        return True
    return bool(str(result.get("output") or "").strip())


def _status_value(value: Any) -> str:
    return str(getattr(value, "value", value) or "").strip().lower()


__all__ = ["VerificationResult", "verify_text_output", "verify_worker_evidence"]
