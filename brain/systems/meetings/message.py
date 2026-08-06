"""Run-message composition for completed and failed meeting sessions."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from brain.kernel.common.coercion import coerce_datetime


# A meeting at the two-hour session cap yields roughly 120k characters of
# captions. Budget for the whole thing: a truncated transcript silently drops
# the decisions made late in the meeting, which are the tickets this run exists
# to file.
MAX_TRANSCRIPT_INLINE_CHARS = 120_000


def compose_post_meeting_run_message(
    payload: Mapping[str, Any],
    transcript_text: str,
    *,
    inline_budget: int = MAX_TRANSCRIPT_INLINE_CHARS,
    source_truncated: bool = False,
) -> str:
    """Compose bounded transcript context followed by the standing workflow."""

    transcript_path = str(payload.get("transcript_md_path") or "transcript.md").strip()
    inline, truncated = _bounded_inline(
        transcript_text,
        budget=inline_budget,
        source_truncated=source_truncated,
    )
    lines = [
        "A meeting transcript is ready for post-meeting follow-up.",
        "",
        *_meeting_header(payload),
        "",
        "## Transcript",
        inline or "(No caption text was recorded.)",
    ]
    if truncated:
        lines.extend(
            [
                "",
                f"Transcript truncated; full transcript at {transcript_path}",
            ]
        )
    lines.extend(
        [
            "",
            "## Required sequence",
            "1. Post a concise meeting summary in the originating thread.",
            "2. Ask clarifying questions where decisions or owners are unclear.",
            "3. Announce the tickets you will file, including each proposed title and owner.",
            (
                "4. File the announced tickets with create_github_issue and "
                "add_github_sub_issue, following your standing triage playbook."
            ),
        ]
    )
    return "\n".join(lines)


def compose_failed_meeting_run_message(payload: Mapping[str, Any]) -> str:
    """Compose a short visible failure-report task without the transcript pipeline."""

    error = str(payload.get("error") or payload.get("warning") or "unknown failure").strip()
    return "\n".join(
        [
            "A meetbot session failed.",
            "",
            *_meeting_header(payload),
            f"Failure: {error}",
            "",
            (
                "Post a short failure report in the originating Slack thread. State what failed "
                "and the next useful operator action. Do not run the transcript-to-ticket pipeline."
            ),
        ]
    )


def compose_degraded_meeting_run_message(payload: Mapping[str, Any]) -> str:
    """Compose a visible capture-failure task for an empty terminal transcript."""

    return "\n".join(
        [
            "A meeting ended, but transcript capture is degraded.",
            "",
            *_meeting_header(payload),
            "",
            "No caption lines were captured, so no summary, decisions, or action items can be recovered.",
            (
                "Likely causes: the wrong meeting, the bot was never admitted, or captions were off."
            ),
            "",
            (
                "Post a capture-failure report in the originating Slack thread. Name the meeting "
                "URL, participant count, caption-line count, and likely causes. Do not ask "
                "clarifying questions or run the transcript-to-ticket pipeline."
            ),
        ]
    )


def compose_meeting_health_warning_message(payload: Mapping[str, Any]) -> str:
    """Compose the immediate Slack task for one stale active session."""

    try:
        participant_count = max(0, int(payload.get("participant_count") or 0))
    except (TypeError, ValueError):
        participant_count = 0
    try:
        caption_lines = max(0, int(payload.get("caption_lines") or 0))
    except (TypeError, ValueError):
        caption_lines = 0
    return "\n".join(
        [
            "An active meetbot session needs operator attention.",
            "",
            "## Meeting session health",
            f"URL: {str(payload.get('meeting_url') or '').strip()}",
            f"Session status: {str(payload.get('status') or '').strip()}",
            f"Participants observed: {participant_count}",
            f"Caption lines: {caption_lines}",
            f"Health warning: {str(payload.get('warning') or '').strip()}",
            "Likely causes: the wrong meeting, the bot was never admitted, or captions are off.",
            "",
            (
                "Post a concise warning in the originating Slack thread now. Include the meeting "
                "URL, both observed counts, and the likely causes. Do not wait for the meeting to end."
            ),
        ]
    )


def _meeting_header(payload: Mapping[str, Any]) -> list[str]:
    participants = [
        str(item).strip()
        for item in payload.get("participants") or []
        if str(item or "").strip()
    ]
    try:
        caption_lines = max(0, int(payload.get("caption_lines") or 0))
    except (TypeError, ValueError):
        caption_lines = 0
    return [
        "## Meeting",
        f"URL: {str(payload.get('meeting_url') or '').strip()}",
        f"Duration: {_duration(payload.get('started_at'), payload.get('ended_at'))}",
        f"Participants: {', '.join(participants) if participants else 'None reported'}",
        f"Caption lines: {caption_lines}",
    ]


def _duration(started_at: Any, ended_at: Any) -> str:
    start = coerce_datetime(started_at, utc=True)
    end = coerce_datetime(ended_at, utc=True)
    if not isinstance(start, datetime) or not isinstance(end, datetime) or end < start:
        return "Unknown"
    seconds = int((end - start).total_seconds())
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {seconds}s"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def _bounded_inline(
    transcript_text: str,
    *,
    budget: int,
    source_truncated: bool,
) -> tuple[str, bool]:
    normalized = str(transcript_text or "").replace("\x00", "").strip()
    limit = max(0, int(budget))
    if len(normalized) <= limit:
        return normalized, bool(source_truncated)
    return normalized[:limit].rstrip(), True


__all__ = [
    "MAX_TRANSCRIPT_INLINE_CHARS",
    "compose_degraded_meeting_run_message",
    "compose_failed_meeting_run_message",
    "compose_meeting_health_warning_message",
    "compose_post_meeting_run_message",
]
