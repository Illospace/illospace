from __future__ import annotations

import pytest


def test_agent_run_request_normalizes_profile_and_recipe():
    from brain.systems.runs.domain import AgentRunRequest, RunProfile, RunRecipe

    fast = AgentRunRequest(org_id="org-1", thread_id="thread-1", message="Read README")
    deep = AgentRunRequest(org_id="org-1", thread_id="thread-1", message="Build it", profile="deep")
    worker = AgentRunRequest(org_id="org-1", thread_id="thread-1", message="Do slice", recipe="worker")
    historical_deep = AgentRunRequest(
        org_id="org-1",
        thread_id="thread-1",
        message="Historical deep run",
        profile="deep",
        recipe="deep",
    )
    historical_scout = AgentRunRequest(
        org_id="org-1",
        thread_id="thread-1",
        message="Historical scout run",
        recipe="scout",
    )

    assert fast.normalized_profile == RunProfile.FAST
    assert fast.normalized_recipe == RunRecipe.FAST
    assert deep.normalized_profile == RunProfile.DEEP
    assert deep.normalized_recipe == RunRecipe.FAST
    assert worker.normalized_recipe == RunRecipe.WORKER
    assert historical_deep.normalized_recipe == RunRecipe.DEEP
    assert historical_scout.normalized_recipe == RunRecipe.SCOUT


def test_agent_run_request_requires_workspace_org_id():
    from brain.systems.runs.domain import AgentRunRequest

    with pytest.raises(ValueError, match="workspace org_id"):
        AgentRunRequest(thread_id="thread-1", message="Read README", org_id=" ")


def test_non_deep_verification_policy_preserves_explicit_metadata_modes():
    from brain.systems.runs.domain import RunProfile
    from brain.systems.runs.verification import VerificationMode, verification_mode_for_run

    assert verification_mode_for_run(RunProfile.FAST, {}) is VerificationMode.LIGHTWEIGHT
    assert verification_mode_for_run(RunProfile.FAST, {"verification": "blocking"}) is VerificationMode.BLOCKING
    assert verification_mode_for_run(RunProfile.FAST, {"strict": True}) is VerificationMode.BLOCKING
    assert verification_mode_for_run(RunProfile.FAST, {"verification": "skip"}) is VerificationMode.SKIP


def test_run_status_transitions_are_single_source_of_truth():
    from brain.systems.runs.status import RunStatus, RunTransitionError, ensure_run_transition

    assert ensure_run_transition(RunStatus.QUEUED, RunStatus.STARTING) == (
        RunStatus.QUEUED,
        RunStatus.STARTING,
    )
    assert ensure_run_transition("running", "completed") == (
        RunStatus.RUNNING,
        RunStatus.COMPLETED,
    )
    with pytest.raises(RunTransitionError):
        ensure_run_transition("completed", "running")


def test_run_event_and_artifact_helpers_are_run_native():
    from brain.systems.runs.artifacts import final_answer_artifact
    from brain.systems.runs.domain import ArtifactType
    from brain.systems.runs.events import activity_event, text_delta_event

    activity = activity_event(7, "Reading README.md", root_run_id=7)
    delta = text_delta_event(7, "hello", root_run_id=7)
    artifact = final_answer_artifact(7, "Done", root_run_id=7)

    assert activity.event_type == "run.activity"
    assert activity.payload == {"label": "Reading README.md"}
    assert delta.event_type == "run.text_delta"
    assert artifact.normalized_type == ArtifactType.FINAL_ANSWER
    assert artifact.text == "Done"
    assert ArtifactType("verifier_evidence") is ArtifactType.VERIFIER_EVIDENCE


def test_steering_inbox_appends_guidance_without_canceling():
    from brain.systems.runs.steering import SteeringInbox, SteeringMessage

    inbox = SteeringInbox()
    inbox.append(SteeringMessage(run_id=9, content="  use the existing API  ", user_id="u1"))

    messages = inbox.drain(9)

    assert [message.content for message in messages] == ["use the existing API"]
    assert inbox.drain(9) == []
