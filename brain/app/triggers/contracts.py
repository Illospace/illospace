"""Product-native trigger contracts for Illo.

External systems may become adapters later, but the runtime contract is Illo's:
source/event/actor/target/payload/policy normalize before run admission.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from brain.app.api.authorization import PrincipalIdentity

ILLO_NATIVE_SOURCES = frozenset({
    "cortex",
    "cycle",
    "memory",
    "skill",
    "verifier",
    "approval",
    "product",
    "internal",
    "chat",
})

ILLO_NATIVE_EVENTS = frozenset({
    "cortex.thread_reply",
    "cortex.idea_created",
    "cortex.thought_state_changed",
    "cycle.due_run",
    "memory.review_due",
    "skill.gap_threshold",
    "verifier.repair_required",
    "approval.received",
    "product.object_changed",
    "internal.manual",
    "chat.room_message_mention",
    "chat.room_thread_mention",
})


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return str(value)


def stable_idempotency_key(
    *,
    source: str,
    event_type: str,
    org_id: str,
    target: Mapping[str, Any],
    payload: Mapping[str, Any] | None = None,
) -> str:
    basis = {
        "source": source,
        "event_type": event_type,
        "org_id": org_id,
        "target": _jsonable(dict(target or {})),
        "payload": _jsonable(dict(payload or {})),
    }
    raw = json.dumps(basis, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]


def _actor_payload(actor: PrincipalIdentity | None) -> dict[str, Any] | None:
    if actor is None:
        return None
    return {
        "id": actor.id,
        "principal_type": actor.principal_type,
        "role": actor.role,
        "name": actor.name,
        "email": actor.email,
        "org_id": actor.org_id,
        "internal": actor.internal,
        "permissions": sorted(actor.permissions),
        "metadata": _jsonable(dict(actor.metadata or {})),
    }


@dataclass(frozen=True)
class IlloTrigger:
    """Normalized trigger before it reaches run/scheduler admission."""

    source: str
    event_type: str
    actor: PrincipalIdentity | None
    org_id: str
    target: Mapping[str, Any]
    payload: Mapping[str, Any]
    idempotency_key: str
    policy: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        source = (self.source or "").strip()
        event_type = (self.event_type or "").strip()
        org_id = (self.org_id or "").strip()
        if source not in ILLO_NATIVE_SOURCES:
            raise ValueError(f"Unsupported Illo trigger source: {source}")
        if event_type not in ILLO_NATIVE_EVENTS:
            raise ValueError(f"Unsupported Illo trigger event_type: {event_type}")
        if not org_id:
            raise ValueError("IlloTrigger.org_id is required")
        if self.actor and self.actor.org_id and self.actor.org_id != org_id:
            raise ValueError("IlloTrigger actor org_id must match trigger org_id")
        if not self.idempotency_key:
            raise ValueError("IlloTrigger.idempotency_key is required")
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "event_type", event_type)
        object.__setattr__(self, "org_id", org_id)
        object.__setattr__(self, "target", dict(self.target or {}))
        object.__setattr__(self, "payload", dict(self.payload or {}))
        object.__setattr__(self, "policy", dict(self.policy or {}))

    def to_payload(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "event_type": self.event_type,
            "actor": _actor_payload(self.actor),
            "org_id": self.org_id,
            "target": _jsonable(dict(self.target or {})),
            "payload": _jsonable(dict(self.payload or {})),
            "idempotency_key": self.idempotency_key,
            "policy": _jsonable(dict(self.policy or {})),
        }


@dataclass(frozen=True)
class TriggerRouteResult:
    """Result of routing a normalized trigger."""

    ok: bool
    route: str
    run_id: int | None = None
    scheduler_run_id: int | None = None
    skipped_reason: str | None = None

    def to_response(self) -> dict[str, Any]:
        payload = {"ok": self.ok}
        if self.run_id is not None:
            payload["run_id"] = self.run_id
        if self.scheduler_run_id is not None:
            payload["scheduler_run_id"] = self.scheduler_run_id
        if self.skipped_reason:
            payload["skipped_reason"] = self.skipped_reason
        return payload
