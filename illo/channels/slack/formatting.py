"""Convert common Markdown without changing code blocks."""

import re


def mrkdwn(text: str) -> str:
    parts = re.split(r"(```[\s\S]*?```|`[^`\n]+`)", text)
    for index in range(0, len(parts), 2):
        part = parts[index]
        part = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", r"<\2|\1>", part)
        part = re.sub(r"(?m)^#{1,6}\s+(.+)$", r"*\1*", part)
        part = re.sub(r"\*\*(.+?)\*\*", r"*\1*", part)
        part = re.sub(r"~~(.+?)~~", r"~\1~", part)
        parts[index] = part
    return "".join(parts)


def chunks(text: str, limit: int = 3500) -> list[str]:
    """Keep each message below Slack's text limit, including code fences."""
    remaining = mrkdwn(text)
    result = []
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit - 4)
        if cut < limit // 2:
            cut = limit - 4
        part, remaining = remaining[:cut], remaining[cut:].lstrip("\n")
        if part.count("```") % 2:
            part += "\n```"
            remaining = "```\n" + remaining
        result.append(part)
    result.append(remaining or " ")
    return result
