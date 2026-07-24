"""Typed registry for direct/headless work-intake targets."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class DirectHeadlessTarget:
    """A registered direct run target with a stable required thread id."""

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


_REGISTERED_DIRECT_TARGET_KINDS: set[str] = set()


def register_direct_target_kind(kind: str) -> None:
    """Register a target kind for the shared direct/headless request builder."""

    normalized_kind = str(kind or "").strip()
    if not normalized_kind:
        raise ValueError("Direct/headless target kind is required")
    _REGISTERED_DIRECT_TARGET_KINDS.add(normalized_kind)


def unregister_direct_target_kind(kind: str) -> None:
    """Remove a registration, primarily for isolated extension tests."""

    _REGISTERED_DIRECT_TARGET_KINDS.discard(str(kind or "").strip())


def resolve_direct_target(target: Mapping[str, Any]) -> DirectHeadlessTarget | None:
    """Normalize a registered target or return ``None`` for another route."""

    kind = str(target.get("kind") or "").strip()
    if kind not in _REGISTERED_DIRECT_TARGET_KINDS:
        return None
    return DirectHeadlessTarget.from_mapping(target)


register_direct_target_kind("app_report")
register_direct_target_kind("external_agent_headless_ask")
register_direct_target_kind("inbound_submission")


__all__ = [
    "DirectHeadlessTarget",
    "register_direct_target_kind",
    "resolve_direct_target",
    "unregister_direct_target_kind",
]
