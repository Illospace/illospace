from __future__ import annotations

import pytest


def test_agent_run_request_normalizes_profile_and_recipe():
    from brain.systems.runs.domain import AgentRunRequest, RunProfile, RunRecipe

    fast = AgentRunRequest(thread_id="thread-1", message="Read README")
    deep = AgentRunRequest(thread_id="thread-1", message="Build it", profile="deep")
    worker = AgentRunRequest(thread_id="thread-1", message="Do slice", recipe="worker")

    assert fast.normalized_profile == RunProfile.FAST
    assert fast.normalized_recipe == RunRecipe.FAST
    assert deep.normalized_profile == RunProfile.DEEP
    assert deep.normalized_recipe == RunRecipe.DEEP
    assert worker.normalized_recipe == RunRecipe.WORKER


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


def test_steering_inbox_appends_guidance_without_canceling():
    from brain.systems.runs.steering import SteeringInbox, SteeringMessage

    inbox = SteeringInbox()
    inbox.append(SteeringMessage(run_id=9, content="  use the existing API  ", user_id="u1"))

    messages = inbox.drain(9)

    assert [message.content for message in messages] == ["use the existing API"]
    assert inbox.drain(9) == []
