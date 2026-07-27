"""Focused tests for call identity, progress tracking, and loop detectors."""

from __future__ import annotations

import json

from brain.systems.runs.direct_loop.loop_control import RunControlPolicy
from brain.systems.runs.direct_loop.loop_guard import (
    ExactRepeatDetector,
    LoopTrigger,
    Progress,
    ProgressObservation,
    ProgressTracker,
    ToolCallIdentity,
    tool_call_identity,
    tool_call_target_key,
)
from brain.systems.runs.direct_loop.tool_execution import ResolvedToolCall
from brain.systems.runs.tool_catalog.metadata import ToolCallIdentitySpec
from brain.systems.runs.tool_catalog.registry import get_tool_registration


def _resolved_read_call(
    tool_name: str,
    tool_input: dict,
    result: dict,
    *,
    block_id: str = "replay",
) -> ResolvedToolCall:
    return ResolvedToolCall(
        block_id=block_id,
        tool_name=tool_name,
        tool_input=tool_input,
        result_text=json.dumps(result, sort_keys=True),
        result_value=result,
    )


class TestIdentityProjection:
    def test_cycle_9_query_paraphrases_share_the_declared_target(self):
        queries = [
            "Promotion readiness workspace evidence during the scheduled review window",
            "Promotion readiness workspace evidence for scheduled window",
            (
                "Promotion readiness workspace evidence in the scheduled review "
                "window, including relevant thread/Cycle activity"
            ),
        ]
        inputs = [
            {
                "query": query,
                "time_window": "custom",
                "start_at": "2026-07-01T14:00:00Z",
                "end_at": "2026-07-01T15:00:00Z",
                "person": "reviewer@example.com",
            }
            for query in queries
        ]

        identities = [
            tool_call_identity("read_team_activity", tool_input)
            for tool_input in inputs
        ]

        assert len({identity.exact_key for identity in identities}) == 3
        assert len({identity.target_key for identity in identities}) == 1
        assert tool_call_target_key(
            "read_team_activity",
            {**inputs[0], "person": "different-reviewer@example.com"},
        ) != identities[0].target_key

    def test_catalog_projection_omits_only_tool_declared_volatile_fields(self):
        first_summary = tool_call_target_key(
            "summarize_file_for_task",
            {
                "path": "brain/agent.py",
                "question": "Where does the loop stop?",
                "focus": "Termination behavior",
            },
        )
        second_summary = tool_call_target_key(
            "summarize_file_for_task",
            {
                "path": "brain/agent.py",
                "question": "Explain how this loop exits",
                "focus": "Control-flow edges",
            },
        )

        assert first_summary == second_summary
        assert tool_call_target_key(
            "web_search",
            {"query": "first independently meaningful search"},
        ) != tool_call_target_key(
            "web_search",
            {"query": "second independently meaningful search"},
        )

    def test_identity_spec_is_typed_and_absent_from_permission_payload(self):
        registration = get_tool_registration("read_team_activity")

        assert registration is not None
        assert isinstance(registration.identity_spec, ToolCallIdentitySpec)
        assert registration.identity_spec.volatile_fields == (
            "cursor",
            "query",
            "search",
        )
        assert "identity_spec" not in registration.to_permission_payload()


class TestProgressTracking:
    def test_tracker_emits_new_changed_and_unchanged_observations(self):
        tracker = ProgressTracker()
        identity = ToolCallIdentity(exact_key="exact", target_key="target")

        new_target = tracker.observe(identity, "result-a")
        changed = tracker.observe(identity, "result-b")
        unchanged = tracker.observe(identity, "result-b")

        assert new_target.progress is Progress.NEW_TARGET
        assert changed.progress is Progress.CHANGED
        assert unchanged.progress is Progress.UNCHANGED
        assert all(
            observation.identity is identity
            for observation in (new_target, changed, unchanged)
        )


class TestExactRepeatDetector:
    def test_changing_progress_resets_exact_repetition(self):
        detector = ExactRepeatDetector(break_threshold=3)
        identity = ToolCallIdentity(exact_key="exact", target_key="target")

        assert detector.observe(
            ProgressObservation(identity, "a", Progress.NEW_TARGET)
        ) is None
        assert detector.observe(
            ProgressObservation(identity, "a", Progress.UNCHANGED)
        ) is None
        assert detector.observe(
            ProgressObservation(identity, "b", Progress.CHANGED)
        ) is None
        assert detector.observe(
            ProgressObservation(identity, "b", Progress.UNCHANGED)
        ) is None

    def test_byte_identical_repeats_still_trip_the_exact_detector(self):
        policy = RunControlPolicy(
            exact_repeat_threshold=5,
            semantic_stall_threshold=100,
            unchanged_result_threshold=100,
        )
        tool_input = {
            "action": "pull_request_checks",
            "repo": "Illospace/illospace",
            "sha": "abc123",
        }

        for poll in range(5):
            decision = policy.observe_tool_result(
                _resolved_read_call(
                    "read_github_source",
                    tool_input,
                    {"status": "pending"},
                    block_id=f"poll-{poll}",
                )
            )
            if poll < 4:
                assert decision.termination is None

        assert policy.termination is not None
        assert policy.termination.trigger is LoopTrigger.EXACT_REPEAT

    def test_reminder_comes_from_detector_state(self):
        detector = ExactRepeatDetector(break_threshold=5, warn_threshold=3)
        identity = ToolCallIdentity(exact_key="exact", target_key="target")

        for count in range(3):
            detector.observe(
                ProgressObservation(
                    identity,
                    "same",
                    Progress.NEW_TARGET if count == 0 else Progress.UNCHANGED,
                )
            )

        message = detector.reminder_message()
        assert message is not None
        assert message["role"] == "user"
        assert "cd` does not persist" in message["content"]


class TestSemanticNoProgressDetector:
    def test_paraphrased_team_activity_calls_trip_after_ten_stalled_targets(self):
        policy = RunControlPolicy(unchanged_result_threshold=100)
        targets = [f"thread-{index}" for index in range(10)]

        for target in targets:
            policy.observe_tool_result(
                _resolved_read_call(
                    "read_team_activity",
                    {"idea_id": target, "query": f"Initial evidence for {target}"},
                    {"items": [{"thread_id": target}]},
                )
            )
        assert policy.termination is None

        for index, target in enumerate(targets, start=1):
            decision = policy.observe_tool_result(
                _resolved_read_call(
                    "read_team_activity",
                    {
                        "idea_id": target,
                        "query": f"Paraphrased evidence request {index}",
                    },
                    {"items": [{"thread_id": target}]},
                )
            )
            if index < 10:
                assert decision.termination is None

        assert policy.termination is not None
        assert policy.termination.trigger is LoopTrigger.SEMANTIC_NO_PROGRESS

    def test_structurally_distinct_targets_do_not_trip(self):
        policy = RunControlPolicy()

        for index in range(20):
            policy.observe_tool_result(
                _resolved_read_call(
                    "read_team_activity",
                    {
                        "idea_id": f"thread-{index}",
                        "query": f"Inspect target {index}",
                        "search": f"ticket {index}",
                    },
                    {"items": []},
                )
            )

        assert policy.termination is None


class TestUnchangedResultDetector:
    def test_unchanged_poll_result_trips_on_fourth_call(self):
        policy = RunControlPolicy()
        tool_input = {
            "action": "pull_request_checks",
            "repo": "Illospace/illospace",
            "sha": "abc123",
        }

        for poll in range(4):
            decision = policy.observe_tool_result(
                _resolved_read_call(
                    "read_github_source",
                    tool_input,
                    {"status": "pending"},
                    block_id=f"poll-{poll}",
                )
            )
            if poll < 3:
                assert decision.termination is None

        assert policy.termination is not None
        assert policy.termination.trigger is LoopTrigger.UNCHANGED_RESULT


class TestLegitimateProgress:
    def test_changing_poll_results_suppress_every_loop_detector(self):
        policy = RunControlPolicy()
        tool_input = {
            "action": "pull_request_checks",
            "repo": "Illospace/illospace",
            "sha": "abc123",
        }

        for poll in range(20):
            decision = policy.observe_tool_result(
                _resolved_read_call(
                    "read_github_source",
                    tool_input,
                    {"status": "pending", "completed_checks": poll},
                )
            )
            assert decision.termination is None

        assert policy.termination is None

    def test_paginated_reads_with_new_results_do_not_trip(self):
        policy = RunControlPolicy()

        for page in range(20):
            decision = policy.observe_tool_result(
                _resolved_read_call(
                    "read_team_activity",
                    {
                        "query": "Promotion readiness evidence",
                        "time_window": "week",
                        "cursor": f"opaque-page-{page}",
                    },
                    {
                        "items": [{"event_id": page}],
                        "next_cursor": f"opaque-page-{page + 1}",
                    },
                )
            )
            assert decision.termination is None

        assert policy.termination is None

    def test_progress_heuristics_do_not_apply_to_write_capable_tools(self):
        policy = RunControlPolicy()

        for index in range(20):
            policy.observe_tool_result(
                _resolved_read_call(
                    "manage_cycle",
                    {
                        "action": "create",
                        "name": "Review reminder",
                        "prompt": f"Review wording {index}",
                    },
                    {"ok": True},
                )
            )

        assert policy.termination is None
