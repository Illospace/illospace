"""Tests for AgentRun worker role scopes and prompt boundaries."""

from __future__ import annotations

from types import SimpleNamespace


class TestWorkerAssignment:
    def test_worker_assignment_payload_carries_role_contract(self):
        from brain.systems.runs.assignments import EvidenceRequirement, WorkerAssignment

        assignment = WorkerAssignment(
            id="execute",
            role="developer",
            objective="Implement and test the scoped change",
            expected_artifacts=("worker_result", "command_output"),
            evidence_requirements=(EvidenceRequirement(artifact_type="worker_result"),),
            risk_level="medium",
        )
        payload = assignment.to_payload()

        assert payload["id"] == "execute"
        assert payload["role"] == "developer"
        assert payload["objective"] == "Implement and test the scoped change"
        assert payload["scope"]["expected_artifacts"] == ["worker_result", "command_output"]
        assert payload["scope"]["risk_level"] == "medium"
        assert payload["evidence_requirements"][0]["artifact_type"] == "worker_result"

class TestWorkerScope:
    def test_worker_scope_from_runtime_metadata(self):
        from brain.systems.runs.recipes.workers import worker_scope_from_runtime

        runtime = SimpleNamespace(
            request=SimpleNamespace(
                message="Do the work",
                metadata={
                    "worker_assignment": {
                        "id": "inspect-readme",
                        "objective": "Inspect README setup steps",
                        "allowed_files": ["README.md"],
                        "forbidden_files": ["secrets.env"],
                        "expected_artifacts": ["worker_result"],
                        "risk_level": "low",
                    }
                },
                target_ref={},
            )
        )

        scope = worker_scope_from_runtime(runtime)

        assert scope.objective == "Inspect README setup steps"
        assert scope.id == "inspect-readme"
        assert scope.allowed_files == ("README.md",)
        assert scope.forbidden_files == ("secrets.env",)
        assert scope.expected_artifacts == ("worker_result",)
        assert scope.risk_level == "low"

    def test_worker_prompt_includes_assignment_scope(self):
        from brain.systems.runs.assignments import WorkerAssignment
        from brain.systems.runs.recipes.workers import build_worker_prompt

        prompt = build_worker_prompt(
            WorkerAssignment(
                id="review-agent-run-artifacts",
                objective="Review the AgentRun child artifacts",
                allowed_files=("brain/systems/runs/store.py",),
                expected_artifacts=("worker_result",),
                risk_level="medium",
            ),
            target_ref={"kind": "test"},
            workspace_ref={"workspace_root": "/tmp/work"},
            context="Relevant context.",
        )

        assert "Worker Assignment" in prompt
        assert "Agent Soul" not in prompt
        assert "Review the AgentRun child artifacts" in prompt
        assert "brain/systems/runs/store.py" in prompt

    def test_worker_prompt_carries_the_soul_only_when_a_person_reads_the_result(self):
        from brain.systems.runs.assignments import WorkerAssignment
        from brain.systems.runs.recipes.workers import build_worker_prompt

        assignment = WorkerAssignment(
            id="summarize-findings",
            objective="Summarize the findings for the requester",
            expected_artifacts=("worker_result",),
            risk_level="low",
        )

        headless = build_worker_prompt(
            assignment,
            target_ref={"kind": "test"},
            workspace_ref={"workspace_root": "/tmp/work"},
        )
        user_facing = build_worker_prompt(
            assignment,
            target_ref={"kind": "test", "required_response_tool": "post_slack_reply"},
            workspace_ref={"workspace_root": "/tmp/work"},
        )

        assert "Agent Soul" not in headless
        assert "Agent Soul" in user_facing
        assert "You are a teammate writing to people" in user_facing
