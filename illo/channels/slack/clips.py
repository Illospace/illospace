"""Prefer Slack transcripts. Another service requires an explicit workspace choice."""

import asyncio
import time
from collections.abc import Awaitable, Callable


def transcript(file: dict) -> str | None:
    value = file.get("transcription") or file.get("transcript")
    if isinstance(value, str):
        return value or None
    if isinstance(value, dict) and value.get("status") not in {"pending", "processing", "failed"}:
        preview = value.get("preview", {})
        if preview.get("content") and not preview.get("has_more"):
            return preview["content"]
        return (
            value.get("text")
            or "\n".join(segment.get("text", "") for segment in value.get("segments", []))
            or None
        )
    return None


def pending(file: dict) -> bool:
    value = file.get("transcription") or file.get("transcript")
    return isinstance(value, dict) and value.get("status") in {"pending", "processing"}


async def read_clips(
    client,
    files: list[dict],
    timeout: float,
    poll: float,
    fallback: Callable[[str], Awaitable[str | None]] | None = None,
) -> list[str] | None:
    """Poll only unfinished Slack transcripts. Bound the total wait for all clips."""
    clips = [
        file
        for file in files
        if (
            file.get("mimetype", "").startswith(("audio/", "video/"))
            or file.get("is_audio")
            or file.get("subtype") in {"slack_video", "slack_audio"}
        )
    ]
    result = []
    try:
        deadline = time.monotonic() + timeout
        for original in clips:
            file = original
            text = transcript(file)
            if not text and pending(file):
                async with asyncio.timeout(max(0, deadline - time.monotonic())):
                    while not text and pending(file):
                        response = await client.files_info(file=original["id"])
                        file = response["file"]
                        text = transcript(file)
                        if not text and pending(file):
                            await asyncio.sleep(poll)
            if not text and fallback:
                text = await fallback(original["id"])
            if not text:
                return None
            result.append(text)
    except Exception:
        # Do not log provider responses, private URLs, or clip text.
        return None
    return result
