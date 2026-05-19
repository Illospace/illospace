"""Public presentation helpers for AgentRun work events.

These helpers intentionally produce a small, user-facing projection from
durable run events. They are used for live UI and API snapshots; trace exports
continue to read the raw durable event rows for debugging.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlparse


SENSITIVE_ARG_PARTS = {
    "api_key",
    "apikey",
    "authorization",
    "auth",
    "bearer",
    "credential",
    "github_token",
    "key",
    "password",
    "pat",
    "secret",
    "token",
}

SENSITIVE_TEXT_PATTERNS = [
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{12,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
]

SECRET_TOOL_NAMES = {"brain_vault", "vault", "secrets"}
SAFE_RESULT_TOOL_NAMES = {"vault_secret_prompt"}
PUBLIC_ARG_ALLOWLIST = {
    "access_level",
    "action",
    "category",
    "cycle_id",
    "description",
    "end_index",
    "id",
    "limit",
    "max_chars",
    "mode",
    "path",
    "pattern",
    "query",
    "repo",
    "repository",
    "section",
    "start_index",
    "task",
    "url",
}


def public_tool_event_payload(payload: dict[str, Any] | None, event_type: str = "") -> dict[str, Any]:
    """Return a browser-safe projection of a durable tool event payload."""

    raw = dict(payload or {})
    tool_name = _text(raw.get("tool_name") or raw.get("tool") or "tool") or "tool"
    args = raw.get("args") if isinstance(raw.get("args"), dict) else {}
    status = _status_for_event(event_type, raw.get("status"))
    display = public_tool_display(tool_name, args, status=status)

    public = {
        key: value
        for key, value in raw.items()
        if key not in {"args", "result", "result_preview"} and not _is_sensitive_key(str(key).lower())
    }
    public["tool_name"] = tool_name
    public["tool"] = tool_name
    public["args"] = public_tool_args(args)
    public["tool_display"] = display
    public["display"] = display
    public["display_label"] = display["label"]

    if raw.get("error") is not None:
        public["error"] = _redact_text(_text(raw.get("error")) or "")[:500]

    result = raw.get("result")
    if result is not None and (tool_name in SAFE_RESULT_TOOL_NAMES or _is_safe_prompt_result(tool_name, result)):
        public["result"] = result
    elif result is not None:
        preview = _redact_text(_text(result) or "")
        if preview:
            public["result_preview"] = _clip(preview, 140)

    return public


def public_tool_args(args: dict[str, Any] | None) -> dict[str, str]:
    """Keep only low-risk, useful args for UI behaviors and labels."""

    public: dict[str, str] = {}
    for key, value in list((args or {}).items())[:20]:
        key_text = str(key)
        key_norm = key_text.lower()
        if key_norm not in PUBLIC_ARG_ALLOWLIST:
            continue
        if _is_sensitive_key(key_norm):
            continue
        text = _safe_url_target(str(value)) if key_norm == "url" else _redact_text(_text(value) or "")
        if not text:
            continue
        public[key_text] = _clip(text, 120)
    return public


def public_tool_display(
    tool_name: str,
    args: dict[str, Any] | None = None,
    *,
    status: str | None = None,
) -> dict[str, Any]:
    tool = _text(tool_name) or "tool"
    normalized = tool.lower()
    args = args or {}
    target = _tool_target(normalized, args)
    label = _tool_label(normalized, args, target)
    display: dict[str, Any] = {
        "kind": _tool_kind(normalized),
        "tool": tool,
        "icon": "🔧",
        "label": label,
        "status": status or "running",
        "sensitive": normalized in SECRET_TOOL_NAMES or _args_contain_sensitive_text(args),
    }
    if target:
        display["target"] = target
    detail = _tool_detail(normalized, args)
    if detail:
        display["detail"] = detail
    return display


def _status_for_event(event_type: str, fallback: Any) -> str:
    if event_type.endswith("tool_failed"):
        return "failed"
    if event_type.endswith("tool_completed"):
        return "completed"
    if event_type.endswith("tool_started"):
        return "running"
    value = _text(fallback)
    return value or "running"


def _tool_kind(tool: str) -> str:
    if tool in {"run_script", "exec_command"} or "command" in tool or "shell" in tool:
        return "command"
    if tool in {"read_file", "view_file", "open_file", "read_thread_messages"} or tool.startswith("read_"):
        return "read"
    if tool in {"edit_file", "apply_patch", "write_file", "create_file"} or any(part in tool for part in ("edit", "patch", "write")):
        return "write"
    if "search" in tool or tool in {"brain_skills", "skill_asset", "skill_view"}:
        return "search"
    if tool in SECRET_TOOL_NAMES or tool.startswith("vault_"):
        return "credential"
    return "tool"


def _tool_label(tool: str, args: dict[str, Any], target: str | None) -> str:
    description = _clean_sentence(args.get("description") or args.get("task"))
    if description:
        return _clip(description, 96)

    if tool == "vault_inventory":
        category = _text(args.get("category"))
        if category and category != "general":
            return f"Checked available {category} credentials"
        return "Checked available credentials"
    if tool == "brain_vault":
        return "Read a stored credential"
    if tool == "vault_secret_prompt":
        return "Requested a missing credential"
    if tool == "brain_skills":
        return "Looked up relevant skills"
    if tool in {"read_thread_messages"}:
        query = _text(args.get("query"))
        return f"Searched previous messages for {query}" if query else "Read previous messages"
    if tool in {"run_script", "exec_command"} or "command" in tool or "shell" in tool:
        return _command_label(args) or "Ran a command"
    if tool in {"read_file", "view_file", "open_file"} or tool.startswith("read_"):
        return f"Read {target}" if target else "Read context"
    if tool in {"edit_file", "apply_patch"} or "edit" in tool or "patch" in tool:
        return f"Edited {target}" if target else "Edited files"
    if tool in {"write_file", "create_file"} or "write" in tool:
        return f"Wrote {target}" if target else "Wrote files"
    if "search" in tool:
        return f"Searched for {target}" if target else "Searched"
    if target:
        return f"{_humanize_tool_name(tool)} {target}"
    return _humanize_tool_name(tool)


def _command_label(args: dict[str, Any]) -> str | None:
    command = _text(args.get("command") or args.get("cmd") or args.get("script"))
    if not command:
        return None
    compact = " ".join(command.split())
    lowered = compact.lower()
    if "gh auth status" in lowered:
        return "Checked GitHub CLI authentication"
    if "api.github.com" in lowered or "gh repo" in lowered or "gh api" in lowered:
        return "Queried GitHub repository metadata"
    if re.search(r"\bgit\s+status\b", lowered):
        return "Checked repository status"
    if re.search(r"\b(rg|grep)\b", lowered):
        return "Searched the workspace"
    if re.search(r"\b(cat|sed|nl)\b", lowered):
        return "Read workspace files"
    if len(compact) <= 48 and not _args_contain_sensitive_text({"command": compact}):
        return f"Ran {compact}"
    return "Ran a command"


def _tool_target(tool: str, args: dict[str, Any]) -> str | None:
    for key in ("path", "file_path", "filename", "repo", "repository", "query", "pattern", "url"):
        value = _text(args.get(key))
        if not value:
            continue
        if key == "url":
            return _safe_url_target(value)
        return _clip(_redact_text(value), 80)
    return None


def _tool_detail(tool: str, args: dict[str, Any]) -> str | None:
    reason = _clean_sentence(args.get("reason"))
    if reason and tool not in SECRET_TOOL_NAMES:
        return _clip(reason, 120)
    category = _text(args.get("category"))
    if category:
        return f"Category: {category}"
    return None


def _safe_url_target(value: str) -> str:
    try:
        parsed = urlparse(value)
    except Exception:
        return _clip(_redact_text(value), 80)
    host = parsed.hostname or ""
    if host:
        try:
            port_value = parsed.port
        except ValueError:
            port_value = None
        port = f":{port_value}" if port_value else ""
        path = parsed.path.rstrip("/")
        suffix = path if len(path) <= 36 else f"…{path[-35:]}"
        return f"{host}{port}{suffix}"
    if parsed.path:
        return _clip(_redact_text(parsed.path.rstrip("/")), 80)
    return _clip(_redact_text(value), 80)


def _humanize_tool_name(value: str) -> str:
    words = [part for part in re.split(r"[_\-\s]+", value) if part]
    if not words:
        return "Used a tool"
    return " ".join(word.capitalize() for word in words)


def _clean_sentence(value: Any) -> str | None:
    text = _text(value)
    if not text:
        return None
    text = _redact_text(" ".join(text.split())).strip(" .")
    return text or None


def _is_sensitive_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", str(key).lower()).strip("_")
    if not normalized:
        return False
    parts = {part for part in normalized.split("_") if part}
    if normalized in SENSITIVE_ARG_PARTS or parts.intersection(SENSITIVE_ARG_PARTS):
        return True
    return normalized.endswith(("_token", "_secret", "_password", "_credential", "_api_key", "_apikey"))


def _args_contain_sensitive_text(args: dict[str, Any] | None) -> bool:
    try:
        text = json.dumps(args or {}, default=str)
    except Exception:
        text = str(args or {})
    return _redact_text(text) != text


def _is_safe_prompt_result(tool_name: str, result: Any) -> bool:
    if str(result).strip() == "[secret redacted]":
        return True
    if tool_name != "brain_vault":
        return False
    try:
        parsed = json.loads(result) if isinstance(result, str) else result
    except Exception:
        return False
    return (
        isinstance(parsed, dict)
        and parsed.get("error") == "Vault grant required before this agent can read the secret"
    )


def _redact_text(text: str) -> str:
    redacted = text
    for pattern in SENSITIVE_TEXT_PATTERNS:
        redacted = pattern.sub("[redacted]", redacted)
    return redacted


def _clip(text: str, limit: int) -> str:
    clean = " ".join(str(text or "").split())
    if len(clean) <= limit:
        return clean
    return clean[: max(0, limit - 1)].rstrip() + "…"


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


__all__ = [
    "public_tool_args",
    "public_tool_display",
    "public_tool_event_payload",
]
