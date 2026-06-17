"""Shared run metadata keys for the original human message."""
from __future__ import annotations

HUMAN_MESSAGE_METADATA_KEY = "human_message"
INTROSPECTION_MESSAGE_METADATA_KEY = "introspection_message"
INTROSPECTION_MESSAGE_METADATA_KEYS = (
    INTROSPECTION_MESSAGE_METADATA_KEY,
    HUMAN_MESSAGE_METADATA_KEY,
)


def extract_latest_user_intent(message: str | None) -> str:
    """Extract the latest user request from coordinator task wrappers when present."""
    text = (message or "").strip()
    marker = "Latest user message:"
    if marker in text:
        return text.split(marker, 1)[1].strip() or text
    if text.startswith("[Idea:") and "\n\n" in text:
        return text.split("\n\n", 1)[1].strip() or text
    return text
