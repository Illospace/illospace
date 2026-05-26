"""Shared context loading for run recipes."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any

from brain.systems.runs.skill_commands import parse_slash_skill_names

PROMPT_REFERENCE_CHAR_LIMIT = 12_000
PROMPT_SCALAR_CHAR_LIMIT = 1_200
PROMPT_LIST_ITEM_LIMIT = 24
PROMPT_DICT_ITEM_LIMIT = 48

PromptPath = tuple[str, ...]

PROJECT_REFERENCE_HEAVY_PATHS: tuple[PromptPath, ...] = (
    ("project_context_snapshot", "resources", "*", "content"),
    ("project_context_snapshot", "resources", "*", "uploaded_files"),
    ("project_context_snapshot", "resources", "*", "materialization", "imports"),
    ("project_context_snapshot", "resources", "*", "materialization", "root_versions"),
    ("project_runtime_context", "project_context_snapshot", "resources", "*", "content"),
    ("project_runtime_context", "project_context_snapshot", "resources", "*", "uploaded_files"),
    ("project_runtime_context", "project_context_snapshot", "resources", "*", "materialization", "imports"),
    ("project_runtime_context", "project_context_snapshot", "resources", "*", "materialization", "root_versions"),
    ("project_context_materialization", "imports"),
    ("project_context_materialization", "root_versions"),
)

HANDOFF_REFERENCE_HEAVY_PATHS: tuple[PromptPath, ...] = (
    ("content",),
    ("file_contents",),
    ("files",),
    ("result",),
    ("result_preview",),
    ("stderr",),
    ("stdout",),
    ("text",),
)

_COUNT_KEYS = (
    "file_count",
    "path_count",
    "resource_count",
    "root_file_count",
    "root_path_count",
    "draft_file_count",
    "draft_path_count",
    "project_root_file_count",
    "project_root_path_count",
    "project_draft_file_count",
    "project_draft_path_count",
    "seed_resource_count",
    "changed_file_count",
)


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


def _compact_heavy_value(value: Any) -> dict[str, Any]:
    summary: dict[str, Any] = {"omitted": "large value omitted from prompt context"}
    if isinstance(value, dict):
        summary["key_count"] = len(value)
        for key in _COUNT_KEYS:
            if key in value:
                summary[key] = value.get(key)
        if value:
            summary["keys"] = [str(key) for key in list(value.keys())[:PROMPT_LIST_ITEM_LIMIT]]
    elif isinstance(value, list):
        summary["item_count"] = len(value)
    else:
        text = str(value or "")
        summary["char_count"] = len(text)
    return summary


def _truncate_scalar(value: Any, limit: int = PROMPT_SCALAR_CHAR_LIMIT) -> Any:
    if not isinstance(value, str):
        return value
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 38)].rstrip() + f"... [truncated {len(value) - limit} chars]"


def _path_matches(pattern: PromptPath, path: PromptPath) -> bool:
    if len(pattern) != len(path):
        return False
    return all(expected == "*" or expected == actual for expected, actual in zip(pattern, path))


def _path_should_compact(path: PromptPath, heavy_paths: tuple[PromptPath, ...]) -> bool:
    return any(_path_matches(pattern, path) for pattern in heavy_paths)


def _compact_prompt_value(
    value: Any,
    *,
    heavy_paths: tuple[PromptPath, ...],
    path: PromptPath = (),
    depth: int = 0,
) -> Any:
    if depth >= 6:
        if isinstance(value, dict):
            return {"omitted": "nested object omitted from prompt context", "key_count": len(value)}
        if isinstance(value, list):
            return {"omitted": "nested list omitted from prompt context", "item_count": len(value)}
        return _truncate_scalar(value, 240)

    if _path_should_compact(path, heavy_paths):
        return _compact_heavy_value(value)

    if isinstance(value, dict):
        compact: dict[str, Any] = {}
        for index, (raw_key, raw_item) in enumerate(value.items()):
            if index >= PROMPT_DICT_ITEM_LIMIT:
                compact["omitted_keys_count"] = len(value) - PROMPT_DICT_ITEM_LIMIT
                break
            key = str(raw_key)
            compact[key] = _compact_prompt_value(
                raw_item,
                heavy_paths=heavy_paths,
                path=(*path, key),
                depth=depth + 1,
            )
        return compact

    if isinstance(value, list):
        compact_items = [
            _compact_prompt_value(item, heavy_paths=heavy_paths, path=(*path, "*"), depth=depth + 1)
            for item in value[:PROMPT_LIST_ITEM_LIMIT]
        ]
        if len(value) > PROMPT_LIST_ITEM_LIMIT:
            compact_items.append({"omitted_items_count": len(value) - PROMPT_LIST_ITEM_LIMIT})
        return compact_items

    return _truncate_scalar(value)


def compact_prompt_reference(
    value: dict[str, Any],
    *,
    heavy_paths: tuple[PromptPath, ...],
    char_limit: int = PROMPT_REFERENCE_CHAR_LIMIT,
) -> str:
    compact = _compact_prompt_value(value, heavy_paths=heavy_paths)
    rendered = json.dumps(compact, sort_keys=True, indent=2, default=str)
    if len(rendered) <= char_limit:
        return rendered
    return rendered[: max(0, char_limit - 47)].rstrip() + f"\n... [prompt reference truncated to {char_limit} chars]"


def compact_project_reference(value: dict[str, Any], *, char_limit: int = PROMPT_REFERENCE_CHAR_LIMIT) -> str:
    return compact_prompt_reference(value, heavy_paths=PROJECT_REFERENCE_HEAVY_PATHS, char_limit=char_limit)


def compact_handoff_reference(value: dict[str, Any], *, char_limit: int = PROMPT_REFERENCE_CHAR_LIMIT) -> str:
    return compact_prompt_reference(value, heavy_paths=HANDOFF_REFERENCE_HEAVY_PATHS, char_limit=char_limit)


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

    def prompt_context(self) -> str:
        parts: list[str] = []
        if self.thread_context:
            formatted = str(self.thread_context.get("formatted") or "").strip()
            if formatted:
                parts.append("Thread so far, before the current user message:\n" + formatted)
        if self.target_ref:
            parts.append("Target:\n" + compact_project_reference(self.target_ref))
        if self.workspace_ref:
            parts.append("Workspace:\n" + compact_project_reference(self.workspace_ref))
        if self.handoff:
            parts.append("Handoff:\n" + compact_handoff_reference(self.handoff))
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


__all__ = ["RunContext", "RunContextLoader", "compact_project_reference"]
