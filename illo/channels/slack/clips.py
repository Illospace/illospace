"""Use Slack's own transcript. Never send a private clip to another service."""

import asyncio


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


async def read_clips(client, files: list[dict], timeout: float, poll: float) -> list[str] | None:
    """Bound the total wait for all clips. None means at least one clip is unreadable."""
    clips = [
        file
        for file in files
        if (
            file.get("mimetype", "").startswith(("audio/", "video/"))
            or file.get("is_audio")
            or file.get("subtype") == "slack_video"
        )
    ]
    result = []
    try:
        async with asyncio.timeout(timeout):
            for file in clips:
                text = transcript(file)
                while not text:
                    response = await client.files_info(file=file["id"])
                    file = response["file"]
                    text = transcript(file)
                    if (
                        isinstance(file.get("transcription"), dict)
                        and file["transcription"].get("status") == "failed"
                    ):
                        return None
                    if not text:
                        await asyncio.sleep(poll)
                result.append(text)
    except Exception:
        return None
    return result
