"""Typed routing for direct/headless work-intake targets.

Legacy per-kind thread-id derivation was deliberately dropped. Direct/headless
producers must now supply the stable ``thread_id`` required by the target contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class DirectHeadlessTarget:
    """A supported direct run target with a stable required thread id."""

    kind: str
    thread_id: str
    value: Mapping[str, Any]

    @classmethod
    def from_mapping(cls, target: Mapping[str, Any]) -> "DirectHeadlessTarget":
        kind = str(target.get("kind") or "").strip()
        thread_id = str(target.get("thread_id") or "").strip()
        if not thread_id:
            raise ValueError(f"{kind or 'Direct/headless'} target requires thread_id")
        return cls(kind=kind, thread_id=thread_id, value=dict(target))


DIRECT_TARGET_KINDS: frozenset[str] = frozenset(
    {
        "app_report",
        "external_agent_headless_ask",
        "inbound_submission",
        "knowledge_distillation",
    }
)


def resolve_direct_target(target: Mapping[str, Any]) -> DirectHeadlessTarget | None:
    """Normalize a registered target or return ``None`` for another route."""

    kind = str(target.get("kind") or "").strip()
    if kind not in DIRECT_TARGET_KINDS:
        return None
    return DirectHeadlessTarget.from_mapping(target)


__all__ = [
    "DIRECT_TARGET_KINDS",
    "DirectHeadlessTarget",
    "resolve_direct_target",
]
