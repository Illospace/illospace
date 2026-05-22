"""Shared context loading for run recipes."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any

from brain.systems.runs.prompt_surfaces import (
    compact_target_ref,
    compact_workspace_ref,
    prompt_json_block,
)
from brain.systems.runs.skill_commands import parse_slash_skill_names


def _skill_names_from_metadata(metadata: dict[str, Any], message: str) -> list[str]:
    raw_names = metadata.get("slash_skill_names")
    names: list[str] = []
    seen: set[str] = set()
    if isinstance(raw_names, list):
        for item in raw_names:
            name = str(item or "").strip().lstrip("/")
            if name and name not in seen:
                names.append(name)
                seen.add(name)
    for name in parse_slash_skill_names(message):
        if name not in seen:
            names.append(name)
            seen.add(name)
    return names


def _request_source_from_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    for key in ("request_source", "request_source_context"):
        value = metadata.get(key)
        if isinstance(value, dict):
            return {str(k): v for k, v in value.items() if v not in (None, "", [], {})}
    return {}


@dataclass(frozen=True)
class RunContext:
    thread_id: str
    message: str
    target_ref: dict[str, Any] = field(default_factory=dict)
    workspace_ref: dict[str, Any] = field(default_factory=dict)
    thread_context: dict[str, Any] = field(default_factory=dict)
    memory: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    handoff: dict[str, Any] = field(default_factory=dict)
    request_source: dict[str, Any] = field(default_factory=dict)

    def prompt_context(self, *, include_target: bool = True, include_workspace: bool = True) -> str:
        parts: list[str] = []
        if self.thread_context:
            formatted = str(self.thread_context.get("formatted") or "").strip()
            if formatted:
                parts.append("Thread so far, before the current user message:\n" + formatted)
        if include_target and self.target_ref:
            target_block = prompt_json_block("Target", self.target_ref).strip()
            if target_block:
                parts.append(target_block)
        if include_workspace and self.workspace_ref:
            workspace_block = prompt_json_block("Workspace", self.workspace_ref).strip()
            if workspace_block:
                parts.append(workspace_block)
        if self.handoff:
            parts.append(f"Handoff: {self.handoff}")
        if self.request_source:
            parts.append(
                "Request Source:\n"
                + json.dumps(self.request_source, sort_keys=True, indent=2, default=str)
            )
        if self.skills:
            skill_tokens = ", ".join(f"/{name.lstrip('/')}" for name in self.skills)
            parts.append(
                "Slash skill command(s): "
                + skill_tokens
                + "\nThe user explicitly used these as skill commands. Treat them as a signal "
                + "that the user is interested in those skills and that they may be relevant "
                + "context. If you need more context, prefer loading the skill card or summary "
                + "before the full procedure."
            )
        if self.memory:
            parts.append("Memory:\n" + "\n".join(f"- {item}" for item in self.memory))
        return "\n\n".join(parts)


class RunContextLoader:
    def load(
        self,
        *,
        thread_id: str,
        message: str,
        target_ref: dict[str, Any] | None = None,
        workspace_ref: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> RunContext:
        metadata = dict(metadata or {})
        thread_context = metadata.get("thread_context")
        return RunContext(
            thread_id=thread_id,
            message=message,
            target_ref=dict(target_ref or {}),
            workspace_ref=dict(workspace_ref or {}),
            thread_context=dict(thread_context) if isinstance(thread_context, dict) else {},
            request_source=_request_source_from_metadata(metadata),
            skills=_skill_names_from_metadata(metadata, message),
        )


__all__ = [
    "RunContext",
    "RunContextLoader",
    "compact_target_ref",
    "compact_workspace_ref",
    "prompt_json_block",
]
