"""Structured execution evidence for deterministic final-reply policies."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Mapping


def _structured_value(value: Any) -> Any:
    """Decode a handler's original JSON result without consulting prompt text."""

    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text or text[0] not in "[{\"":
        return value
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return value


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _searchable_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, Mapping):
        return " ".join(
            part
            for key, item in value.items()
            for part in (_searchable_text(key), _searchable_text(item))
            if part
        )
    if isinstance(value, (list, tuple, set, frozenset)):
        return " ".join(filter(None, (_searchable_text(item) for item in value)))
    return str(value)


@dataclass(frozen=True)
class ToolResultEvidence:
    """One tool attempt captured before prompt rendering or truncation."""

    tool_name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    is_error: bool = False
    result: Any = None

    @classmethod
    def capture(
        cls,
        *,
        tool_name: str,
        arguments: Mapping[str, Any] | None,
        is_error: bool,
        result: Any,
    ) -> "ToolResultEvidence":
        return cls(
            tool_name=str(tool_name or "").strip(),
            arguments=_mapping(arguments),
            is_error=bool(is_error),
            result=_structured_value(result),
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ToolResultEvidence":
        """Accept structured compatibility payloads, never rendered previews."""

        return cls.capture(
            tool_name=str(value.get("tool_name") or ""),
            arguments=value.get("arguments") if isinstance(value.get("arguments"), Mapping) else {},
            is_error=bool(value.get("is_error")),
            result=value.get("result"),
        )

    @property
    def failed(self) -> bool:
        if self.is_error:
            return True
        return isinstance(self.result, Mapping) and bool(self.result.get("error"))

    @property
    def succeeded(self) -> bool:
        return not self.failed

    def cache_payload(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "arguments": self.arguments,
            "is_error": self.is_error,
            "result": self.result,
        }


@dataclass(frozen=True)
class FinalReplyEvidence:
    """Typed evidence boundary shared by deterministic final-reply policies."""

    tool_results: tuple[ToolResultEvidence, ...] = ()
    execution_artifacts: tuple[Mapping[str, Any], ...] = ()
    worker_results: tuple[Mapping[str, Any], ...] = ()

    @classmethod
    def from_agent_context(cls, agent_context: Any) -> "FinalReplyEvidence":
        tool_results: list[ToolResultEvidence] = []
        for item in list(getattr(agent_context, "recent_tool_results", []) or []):
            if isinstance(item, ToolResultEvidence):
                tool_results.append(item)
            elif isinstance(item, Mapping):
                tool_results.append(ToolResultEvidence.from_mapping(item))

        artifacts = tuple(
            dict(item)
            for item in list(getattr(agent_context, "execution_artifacts", []) or [])
            if isinstance(item, Mapping)
        )

        run = getattr(agent_context, "run", None)
        workers: list[Mapping[str, Any]] = []
        for item in list(getattr(run, "worker_results", []) or []):
            workers.append(
                {
                    "success": bool(getattr(item, "success", False)),
                    "output": getattr(item, "output", None),
                    "evidence": getattr(item, "evidence", None),
                }
            )
        return cls(
            tool_results=tuple(tool_results),
            execution_artifacts=artifacts,
            worker_results=tuple(workers),
        )

    def results_for(self, tool_name: str) -> tuple[ToolResultEvidence, ...]:
        expected = str(tool_name or "").strip()
        return tuple(item for item in self.tool_results if item.tool_name == expected)

    def supports_terms(self, terms: list[str]) -> bool:
        if not terms:
            return False
        successful_tool_results = [
            item.cache_payload()
            for item in self.tool_results
            if item.succeeded
        ]
        successful_artifacts = [
            item
            for item in self.execution_artifacts
            if str(item.get("status") or "").strip().lower()
            not in {"error", "failed", "failure"}
        ]
        successful_workers = [
            item for item in self.worker_results if bool(item.get("success"))
        ]
        text = _searchable_text(
            {
                "tool_results": successful_tool_results,
                "execution_artifacts": successful_artifacts,
                "worker_results": successful_workers,
            }
        ).lower()
        return all(term in text for term in terms)

    def cache_fingerprint(self) -> str:
        payload = {
            "tool_results": [item.cache_payload() for item in self.tool_results],
            "execution_artifacts": self.execution_artifacts,
            "worker_results": self.worker_results,
        }
        serialized = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


__all__ = ["FinalReplyEvidence", "ToolResultEvidence"]
