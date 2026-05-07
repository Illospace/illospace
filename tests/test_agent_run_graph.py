from __future__ import annotations

import pytest


def test_deep_plan_computes_deterministic_waves_and_ready_nodes():
    from brain.systems.runs.graph import DeepPlan, RunEdge, RunNode

    plan = DeepPlan(
        nodes=(
            RunNode(id="synthesize"),
            RunNode(id="execute"),
            RunNode(id="scout"),
            RunNode(id="review"),
        ),
        edges=(
            RunEdge(source="execute", target="review"),
            RunEdge(source="scout", target="execute"),
            RunEdge(source="review", target="synthesize"),
        ),
    )

    assert plan.node_ids == ("scout", "execute", "review", "synthesize")
    assert plan.wave_index == {"scout": 0, "execute": 1, "review": 2, "synthesize": 3}
    assert [[node.id for node in wave] for wave in plan.waves] == [
        ["scout"],
        ["execute"],
        ["review"],
        ["synthesize"],
    ]
    assert [[node.id for node in wave] for wave in plan.waves()] == [
        ["scout"],
        ["execute"],
        ["review"],
        ["synthesize"],
    ]
    assert plan.ready_node_ids() == ("scout",)

    after_scout = plan.with_node_status("scout", "completed", run_id=101)

    assert after_scout.require_node("scout").run_id == 101
    assert after_scout.status_for("scout") == "completed"
    assert after_scout.ready_node_ids() == ("execute",)
    assert after_scout.dependency_ids("review") == ("execute",)
    assert after_scout.dependent_ids("execute") == ("review",)


def test_deep_plan_detects_missing_dependencies_and_cycles():
    from brain.systems.runs.graph import (
        DeepPlan,
        RunEdge,
        RunGraphCycleError,
        RunGraphMissingDependencyError,
        RunNode,
    )

    with pytest.raises(RunGraphMissingDependencyError):
        DeepPlan(nodes=(RunNode(id="execute"),), edges=(RunEdge(source="scout", target="execute"),))

    with pytest.raises(RunGraphCycleError):
        DeepPlan(
            nodes=(RunNode(id="a"), RunNode(id="b")),
            edges=(RunEdge(source="a", target="b"), RunEdge(source="b", target="a")),
        )


def test_deep_plan_serializes_and_loads_worker_payloads():
    from brain.systems.runs.assignments import WorkerAssignment
    from brain.systems.runs.graph import DeepPlan

    payload = {
        "plan_id": "plan-1",
        "summary": "Do the work",
        "workers": [
            {"role": "execute", "objective": "Make the change", "expected_artifacts": ["worker_result"]},
            {"role": "verify", "objective": "Check the change", "depends_on": ["execute"]},
        ],
    }

    plan = DeepPlan.from_payload(payload)
    roundtrip = DeepPlan.from_payload(plan.to_payload())

    assert plan.id == "plan-1"
    assert plan.require_node("execute").metadata["expected_artifacts"] == ["worker_result"]
    assert plan.dependency_ids("verify") == ("execute",)
    assert roundtrip.to_payload() == plan.to_payload()

    assigned = DeepPlan(
        nodes=(
            {
                "id": "assigned",
                "kind": "worker",
                "title": "Assigned worker",
                "assignment": WorkerAssignment(id="assigned", objective="Do it").to_payload(),
            },
        )
    )
    assert assigned.require_node("assigned").kind == "worker"
    assert assigned.require_node("assigned").title == "Assigned worker"
    assert assigned.require_node("assigned").assignment.objective == "Do it"


def test_deep_plan_marks_dependents_blocked_by_failed_nodes():
    from brain.systems.runs.graph import DeepPlan, RunEdge, RunNode

    plan = DeepPlan(
        nodes=(RunNode(id="scout"), RunNode(id="execute"), RunNode(id="verify")),
        edges=(RunEdge(source="scout", target="execute"), RunEdge(source="execute", target="verify")),
    )
    plan = plan.with_node_status("scout", "completed").with_node_status("execute", "failed")

    assert plan.blocked_node_ids() == ("verify",)
    assert plan.ready_node_ids() == ()
