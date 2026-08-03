"""Meeting automation tool schemas."""

from __future__ import annotations


MEETING_TOOLS = [
    {
        "name": "join_meeting",
        "description": (
            "Ask the configured Illo meetbot to join one Google Meet as a visible, "
            "muted notetaker. Returns the honest lobby, admitted, captions-flowing, "
            "ended, or failed state after a brief status poll."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "meeting_url": {
                    "type": "string",
                    "description": "Google Meet URL in meet.google.com/abc-defg-hij form.",
                },
                "display_name": {
                    "type": "string",
                    "description": "Optional visible participant name. Meetbot uses its configured default when omitted.",
                },
            },
            "required": ["meeting_url"],
        },
    },
    {
        "name": "meeting_status",
        "description": (
            "Read the current meetbot session state, caption count, transcript path, "
            "warning, and error without claiming that page load means admission."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "Session ID returned by join_meeting.",
                },
            },
            "required": ["session_id"],
        },
    },
    {
        "name": "leave_meeting",
        "description": "Ask meetbot to leave the active meeting and finalize its transcript.",
        "input_schema": {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "Session ID returned by join_meeting.",
                },
            },
            "required": ["session_id"],
        },
    },
    {
        "name": "send_meeting_chat",
        "description": "Post visible text into the Google Meet in-call chat for an active meetbot session.",
        "input_schema": {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "Session ID returned by join_meeting.",
                },
                "text": {
                    "type": "string",
                    "description": "Text to post visibly in the meeting chat.",
                },
            },
            "required": ["session_id", "text"],
        },
    },
]


__all__ = ["MEETING_TOOLS"]
