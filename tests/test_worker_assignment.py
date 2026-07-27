from __future__ import annotations

def test_worker_assignment_roundtrips_scope_and_builds_tool_scope():
    from brain.systems.runs.assignments import EvidenceRequirement, WorkerAssignment

    assignment = WorkerAssignment(
        id="execute",
        role="execute",
        objective="Update the README",
        allowed_files=("README.md", "docs/*.md"),
        forbidden_files=("docs/private.md",),
        expected_artifacts=("worker_result",),
        risk_level="low",
        evidence_requirements=(EvidenceRequirement(artifact_type="worker_result", text_contains=("README",)),),
    )

    payload = assignment.to_payload()
    restored = WorkerAssignment.from_payload(payload)
    tool_scope = restored.to_tool_scope()

    assert restored.scope_payload()["objective"] == "Update the README"
    assert restored.allows_file("docs/setup.md") is True
    assert restored.allows_file("docs/private.md") is False
    assert restored.allows_file("src/app.py") is False
    assert tool_scope.allowed_files == ("README.md", "docs/*.md")
    assert WorkerAssignment.from_payload(restored) is restored
    assert WorkerAssignment.from_payload(None, default_id="fallback").id == "fallback"


def test_assignment_from_payload_accepts_nested_scope_and_acceptance_aliases():
    from brain.systems.runs.assignments import WorkerAssignment

    assignment = WorkerAssignment.from_payload(
        {
            "assignment_id": "investigate",
            "role": "investigate",
            "scope": {
                "objective": "Collect context",
                "files": ["brain/systems/runs/*.py"],
                "risk_level": "low",
            },
            "acceptance": {
                "required_evidence": [
                    {"type": "file_observation", "min_count": 2},
                ]
            },
        }
    )

    assert assignment.id == "investigate"
    assert assignment.objective == "Collect context"
    assert assignment.allows_file("brain/systems/runs/graph.py") is True
    assert assignment.required_evidence()[0].min_count == 2
    assert assignment.required_artifact_types() == ("file_observation",)


def test_assignment_keeps_implicit_worker_result_out_of_hard_evidence_checks():
    from brain.systems.runs.assignments import EvidenceRequirement, WorkerAssignment

    assignment = WorkerAssignment(
        id="execute",
        evidence_requirements=(
            EvidenceRequirement(
                kind="artifact",
                artifact_type="worker_result",
                description="Produce the worker result summary",
            ),
            EvidenceRequirement(kind="artifact", artifact_type="file_observation"),
        ),
    )

    assert [requirement.artifact_type for requirement in assignment.required_evidence()] == ["file_observation"]
    assert assignment.required_artifact_types() == ("worker_result", "file_observation")
