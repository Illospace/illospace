"""Slash skill command parsing for AgentRun context."""

from __future__ import annotations

from typing import Any, Iterator, TypedDict


class SlashSkillCommand(TypedDict):
    name: str
    token: str
    start: int
    end: int


def _is_command_char(char: str | None) -> bool:
    if not char:
        return False
    code = ord(char)
    return (
        48 <= code <= 57
        or 65 <= code <= 90
        or 97 <= code <= 122
        or char in {"_", "-"}
    )


def _is_boundary_before(message: str, index: int) -> bool:
    return index == 0 or message[index - 1].isspace()


def iter_slash_skill_commands(message: str | None) -> Iterator[SlashSkillCommand]:
    """Yield slash skill tokens, skipping path-like ``/name/...`` text."""

    text = message or ""
    index = 0
    while index < len(text):
        if text[index] != "/" or not _is_boundary_before(text, index):
            index += 1
            continue

        end = index + 1
        if not _is_command_char(text[end] if end < len(text) else None):
            index += 1
            continue
        while end < len(text) and _is_command_char(text[end]):
            end += 1

        if end < len(text) and text[end] == "/":
            index = end + 1
            continue

        name = text[index + 1 : end]
        yield {"name": name, "token": text[index:end], "start": index, "end": end}
        index = end


def parse_slash_skill_names(message: str | None) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for command in iter_slash_skill_commands(message):
        name = command["name"]
        if name in seen:
            continue
        names.append(name)
        seen.add(name)
    return names


def annotate_metadata_with_slash_skill_commands(
    metadata: dict[str, Any] | None,
    message: str | None,
) -> dict[str, Any]:
    payload = dict(metadata or {})
    names = parse_slash_skill_names(message)
    if not names:
        return payload

    payload["slash_skill_names"] = names
    payload["slash_skill_commands"] = [
        {
            "name": name,
            "token": f"/{name}",
            "kind": "skill_command",
            "source": "user_message",
        }
        for name in names
    ]
    return payload


__all__ = [
    "SlashSkillCommand",
    "annotate_metadata_with_slash_skill_commands",
    "iter_slash_skill_commands",
    "parse_slash_skill_names",
]
