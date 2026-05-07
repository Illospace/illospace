"""Tenant-safe AgentRun visibility helpers for Cortex."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RunReadScope:
    org_id: str | None = None
    unrestricted: bool = False

    @classmethod
    def all_orgs(cls) -> "RunReadScope":
        return cls(unrestricted=True)

    @classmethod
    def for_org(cls, org_id: str | None) -> "RunReadScope":
        return cls(org_id=str(org_id) if org_id else None)


def run_belongs_to_scope(_session: Any, run: Any, scope: RunReadScope) -> bool:
    if scope.unrestricted:
        return True
    return bool(scope.org_id) and str(getattr(run, "org_id", "") or "") == str(scope.org_id)


__all__ = ["RunReadScope", "run_belongs_to_scope"]
