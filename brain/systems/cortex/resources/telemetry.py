"""Telemetry builders for warm resource planning."""
from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from brain.kernel.common.time import utcnow as _shared_utcnow

from brain.systems.cortex.events import publish_safe
from brain.platform.db.models.run import AgentRun
from brain.platform.db.repositories.unit_of_work import UnitOfWork

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return _shared_utcnow()


def _clean_mapping(value: Mapping[str, Any] | None) -> dict[str, Any]:
    return dict(value or {})


def build_workspace_resource_summary(
    *,
    mode: str = "cold",
    warm_start_used: bool = False,
    reason: str | None = None,
    worktree_path: str | None = None,
    branch: str | None = None,
    repo_root: str | None = None,
    base_commit: str | None = None,
    runtime_fingerprint: str | None = None,
    pool_key: str | None = None,
    pool_entry_id: int | None = None,
    lease_token: str | None = None,
) -> dict[str, Any]:
    """Return a cold-compatible workspace resource telemetry payload."""
    return {
        "workspace": {
            "mode": mode,
            "warm_start_used": bool(warm_start_used),
            "reason": reason,
            "worktree_path": worktree_path,
            "branch": branch,
            "repo_root": repo_root,
            "base_commit": base_commit,
            "runtime_fingerprint": runtime_fingerprint,
            "pool_key": pool_key,
            "pool_entry_id": pool_entry_id,
            "lease_token": lease_token,
            "observed_at": _utcnow().isoformat(),
        }
    }


def build_browser_resource_summary(
    *,
    mode: str = "cold",
    warm_start_used: bool = False,
    reason: str | None = None,
    profile_key: str | None = None,
    browser_version: str | None = None,
    context_mode: str | None = None,
    pool_key: str | None = None,
    pool_entry_id: int | None = None,
    lease_token: str | None = None,
) -> dict[str, Any]:
    """Return a cold-compatible browser resource telemetry payload."""
    return {
        "browser": {
            "mode": mode,
            "warm_start_used": bool(warm_start_used),
            "reason": reason,
            "profile_key": profile_key,
            "browser_version": browser_version,
            "context_mode": context_mode,
            "pool_key": pool_key,
            "pool_entry_id": pool_entry_id,
            "lease_token": lease_token,
            "observed_at": _utcnow().isoformat(),
        }
    }


async def record_run_resource_telemetry(
    run_id: int | None,
    summary: Mapping[str, Any],
) -> dict[str, Any]:
    """Persist additive resource telemetry on the run row."""
    normalized = _clean_mapping(summary)
    if not run_id:
        return normalized

    try:
        async with UnitOfWork() as uow:
            run = await uow.session.get(AgentRun, run_id)
            if not run:
                return normalized

            merged = dict(run.resource_summary or {})
            for key, value in normalized.items():
                if isinstance(value, dict) and isinstance(merged.get(key), dict):
                    merged[key] = {**merged[key], **value}
                else:
                    merged[key] = value

            run.resource_summary = merged
            workspace = merged.get("workspace") if isinstance(merged.get("workspace"), dict) else {}
            if isinstance(workspace, dict):
                if "mode" in workspace:
                    run.workspace_mode = workspace.get("mode")
                if "warm_start_used" in workspace:
                    run.warm_start_used = bool(workspace.get("warm_start_used"))

        publish_safe("resource_telemetry", {
            "run_id": run_id,
            "summary": merged,
        })
        return merged
    except Exception as exc:
        logger.debug("Failed to persist resource telemetry for run %s: %s", run_id, exc)
        return normalized
