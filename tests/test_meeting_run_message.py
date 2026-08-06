from __future__ import annotations

from brain.systems.meetings.message import (
    compose_degraded_meeting_run_message,
    compose_failed_meeting_run_message,
    compose_meeting_health_warning_message,
    compose_post_meeting_run_message,
)


def _payload(**overrides):
    return {
        "session_id": "session-1",
        "meeting_url": "https://meet.google.com/abc-defg-hij",
        "status": "ended",
        "transcript_md_path": "brain/uploads/meetings/session-1/transcript.md",
        "started_at": "2026-08-03T15:00:00Z",
        "ended_at": "2026-08-03T16:02:03Z",
        "caption_lines": 42,
        "participants": ["Reda", "Axel"],
        **overrides,
    }


def test_post_meeting_message_keeps_complete_transcript_and_sequences_work():
    message = compose_post_meeting_run_message(
        _payload(),
        "**Reda**: We decided to ship it.",
        inline_budget=200,
    )

    assert "Duration: 1h 2m 3s" in message
    assert "Participants: Reda, Axel" in message
    assert "Caption lines: 42" in message
    assert "**Reda**: We decided to ship it." in message
    assert "Transcript truncated" not in message
    assert "1. Post a concise meeting summary" in message
    assert "2. Ask clarifying questions" in message
    assert "3. Announce the tickets" in message
    assert "4. File the announced tickets with create_github_issue and add_github_sub_issue" in message
    assert "standing triage playbook" in message


def test_post_meeting_message_truncates_loudly_with_full_path_pointer():
    message = compose_post_meeting_run_message(
        _payload(),
        "abcdefghij",
        inline_budget=5,
    )

    assert "abcde" in message
    assert "fghij" not in message
    assert (
        "Transcript truncated; full transcript at "
        "brain/uploads/meetings/session-1/transcript.md"
    ) in message


def test_source_truncation_is_declared_even_when_excerpt_fits_budget():
    message = compose_post_meeting_run_message(
        _payload(),
        "bounded excerpt",
        inline_budget=100,
        source_truncated=True,
    )

    assert "bounded excerpt" in message
    assert "Transcript truncated; full transcript at" in message


def test_failed_meeting_message_skips_ticket_pipeline():
    message = compose_failed_meeting_run_message(
        _payload(status="failed", error="Host denied admission")
    )

    assert "Host denied admission" in message
    assert "Post a short failure report" in message
    assert "Required sequence" not in message
    assert "create_github_issue" not in message


def test_zero_caption_meeting_message_reports_degraded_capture_without_questions():
    message = compose_degraded_meeting_run_message(
        _payload(caption_lines=0, participants=[]),
    )

    assert "capture is degraded" in message
    assert "Caption lines: 0" in message
    assert "wrong meeting" in message
    assert "never admitted" in message
    assert "captions were off" in message
    assert "Post a capture-failure report" in message
    assert "Ask clarifying questions" not in message
    assert "create_github_issue" not in message


def test_health_warning_names_url_counts_and_likely_causes():
    message = compose_meeting_health_warning_message(
        {
            "meeting_url": "https://meet.google.com/abc-defg-hij",
            "status": "lobby",
            "participant_count": 0,
            "caption_lines": 0,
            "warning": "The session is stale.",
        }
    )

    assert "https://meet.google.com/abc-defg-hij" in message
    assert "Participants observed: 0" in message
    assert "Caption lines: 0" in message
    assert "wrong meeting" in message
    assert "never admitted" in message
    assert "captions are off" in message
