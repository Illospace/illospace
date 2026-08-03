"""Brain-side meeting automation contracts."""

from brain.systems.meetings.client import (
    MeetbotClient,
    MeetbotConfigurationError,
    MeetbotServiceError,
)
from brain.systems.meetings.message import (
    MAX_TRANSCRIPT_INLINE_CHARS,
    compose_failed_meeting_run_message,
    compose_post_meeting_run_message,
)

__all__ = [
    "MAX_TRANSCRIPT_INLINE_CHARS",
    "MeetbotClient",
    "MeetbotConfigurationError",
    "MeetbotServiceError",
    "compose_failed_meeting_run_message",
    "compose_post_meeting_run_message",
]
