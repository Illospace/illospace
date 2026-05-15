"""SOUL.md loading and controlled mutation.

Illo's SOUL file is private operator context, not project context. It is prompt
text loaded into direct agent runs and can only be changed through the
manage_soul tool.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
from typing import Any

from brain.kernel import config

DEFAULT_SOUL_MD = """# Illo Soul

You are Illo: an agent inside a workspace used by a team.

You help workspace members think clearly, remember what matters, and move work forward.
You are part teammate, part connective tissue: carrying context between threads, people,
projects, and decisions.

## Voice

Be sharp, warm, and concrete.
Have a point of view. Do not hedge everything into mush.
Skip filler, throat-clearing, and corporate helper language.
Never open with "Great question," "Absolutely," or "I'd be happy to help." Just answer.

Default to concise. Lead with the useful thing first. Add detail only when it helps.

## Posture

Be proactive when action is available.
Read context before asking the user to repeat themselves.
When something is safe and obviously useful, do it.
When something is risky, external, destructive, or ambiguous, ask clearly.

Push back on weak choices. Be candid without being cruel.
Competence is warmth.

## Team Awareness

Treat the workspace as one living place that may include one person or many.
You are not the user's voice. Be careful on shared surfaces.
Requests may come directly from a workspace member or through a connected
personal agent acting for that member. Treat personal-agent requests as
human-originated intent with an agent intermediary.
Help people coordinate, preserve decisions, and keep momentum.
Private things stay private.

## Coordination

When a request involves other workspace members, treat it as workspace
coordination, not only as a reply to the current user.

Choose the lightest visible coordination surface that fits the work:
- reply in the current thread when the current audience is enough;
- name or mention teammates in the current thread when they should share the same context;
- create teammate-owned threads when each person needs their own action, follow-up, or handoff.
"""

SOUL_MAX_CHARS = int(os.getenv("ILLO_SOUL_MAX_CHARS", "6000"))

_THREAT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"\b(ignore|disregard)\s+(all|previous|prior|above)\s+(instructions|rules|messages)\b",
            re.I,
        ),
        "instruction_override",
    ),
    (re.compile(r"\bsystem\s+prompt\s+override\b", re.I), "system_prompt_override"),
    (re.compile(r"\bdo\s+not\s+tell\s+the\s+user\b", re.I), "deception_hide"),
    (
        re.compile(r"\b(disable|turn\s+off)\s+(safety|guardrails|approvals|tool\s+policy)\b", re.I),
        "safety_disable",
    ),
    (
        re.compile(r"\b(bypass|skip)\s+(approvals?|permissions?|sandbox|tool\s+policy)\b", re.I),
        "policy_bypass",
    ),
)

_INVISIBLE_CHARS = {
    "\u200b",
    "\u200c",
    "\u200d",
    "\u2060",
    "\ufeff",
    "\u202a",
    "\u202b",
    "\u202c",
    "\u202d",
    "\u202e",
}


@dataclass(frozen=True)
class AgentSoul:
    path: Path
    content: str
    exists: bool
    source: str
    validation_errors: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        return not self.validation_errors

    def to_payload(self, *, include_content: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "path": str(self.path),
            "exists": self.exists,
            "source": self.source,
            "valid": self.valid,
            "validation_errors": list(self.validation_errors),
            "max_chars": SOUL_MAX_CHARS,
        }
        if include_content:
            payload["content"] = self.content
        return payload


def agent_soul_path() -> Path:
    return Path(config.AGENT_SOUL_PATH)


def validation_errors_for_soul(content: str) -> tuple[str, ...]:
    text = str(content or "")
    errors: list[str] = []
    if len(text) > SOUL_MAX_CHARS:
        errors.append(f"too_long:{len(text)}>{SOUL_MAX_CHARS}")
    if any(char in text for char in _INVISIBLE_CHARS):
        errors.append("contains_invisible_unicode")
    for pattern, label in _THREAT_PATTERNS:
        if pattern.search(text):
            errors.append(label)
    return tuple(errors)


def _read_file_text(path: Path) -> str | None:
    try:
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def read_agent_soul(*, include_default: bool = True) -> AgentSoul:
    path = agent_soul_path()
    raw = _read_file_text(path)
    exists = raw is not None
    source = "file" if exists else "default"
    content = raw if raw else (DEFAULT_SOUL_MD.strip() if include_default else "")
    errors = validation_errors_for_soul(content) if content else ()
    return AgentSoul(path=path, content=content, exists=exists, source=source, validation_errors=errors)


def soul_prompt_section() -> str:
    soul = read_agent_soul(include_default=True)
    if not soul.content:
        return ""
    if not soul.valid:
        fallback = DEFAULT_SOUL_MD.strip()
        return (
            "## Agent Soul\n"
            f"{fallback}\n\n"
            "Local SOUL.md was not loaded because it failed validation."
        )
    return f"## Agent Soul\n{soul.content}"


def _normalize_content(content: str | None) -> str:
    text = str(content or "").strip()
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not text:
        raise ValueError("content is required for replace")
    errors = validation_errors_for_soul(text)
    if errors:
        raise ValueError("SOUL.md validation failed: " + ", ".join(errors))
    return text


def _write_agent_soul(content: str) -> dict[str, Any]:
    path = agent_soul_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    backup_path: Path | None = None
    if path.exists():
        backup_path = path.with_name(f"{path.name}.bak")
        backup_path.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    tmp_path = path.with_name(f".{path.name}.tmp")
    tmp_path.write_text(content.rstrip() + "\n", encoding="utf-8")
    tmp_path.replace(path)
    return {
        "path": str(path),
        "backup_path": str(backup_path) if backup_path else None,
        "chars": len(content),
    }


def manage_agent_soul(
    action: str,
    content: str | None = None,
    reason: str | None = None,
    actor_user_id: str | None = None,
) -> str:
    """Read or mutate Illo's SOUL.md file through a narrow tool surface."""

    normalized_action = str(action or "").strip().lower()
    if normalized_action == "read":
        return json.dumps({"soul": read_agent_soul(include_default=True).to_payload()}, default=str)
    if normalized_action in {"replace", "reset"} and not actor_user_id:
        return json.dumps({"error": "manage_soul mutations require a user-scoped run"})
    if normalized_action == "replace":
        try:
            normalized_content = _normalize_content(content)
            write_result = _write_agent_soul(normalized_content)
        except ValueError as exc:
            return json.dumps({"error": str(exc)})
        return json.dumps(
            {
                "soul": read_agent_soul(include_default=True).to_payload(include_content=False),
                "write": write_result,
                "reason": reason,
                "applies_to": "future_runs",
            },
            default=str,
        )
    if normalized_action == "reset":
        write_result = _write_agent_soul(DEFAULT_SOUL_MD.strip())
        return json.dumps(
            {
                "soul": read_agent_soul(include_default=True).to_payload(include_content=False),
                "write": write_result,
                "reason": reason,
                "applies_to": "future_runs",
            },
            default=str,
        )
    return json.dumps({"error": "action must be one of: read, replace, reset"})
