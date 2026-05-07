"""Lease-backed warm resource pools with cold-path fallback."""
from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
import tempfile
from functools import lru_cache
from dataclasses import dataclass
from datetime import datetime, timedelta

from brain.kernel.common.time import utcnow as _shared_utcnow
from pathlib import Path
from typing import Any

from brain.kernel.common.env import env_flag as _shared_env_flag
from brain.kernel.common.env import env_int as _shared_env_int

from brain.systems.cortex.resources.leases import LeaseDecision, ResourceLeaseManager
from brain.systems.cortex.resources.telemetry import (
    build_browser_resource_summary,
    build_workspace_resource_summary,
)
from brain.platform.db.models.resource_pool import BrowserPoolEntry, WorkspacePoolEntry
from brain.platform.db.repositories.unit_of_work import UnitOfWork

logger = logging.getLogger(__name__)

_SUPPORTED_WORKSPACE_MODES = {"cold", "copy", "reflink", "snapshot"}
_SUPPORTED_BROWSER_MODES = {"cold", "fresh"}
_WORKSPACE_STATUS_READY = {"ready", "warm", "leased"}
_BROWSER_STATUS_READY = {"ready", "warm", "leased"}
_WORKSPACE_ADVANCED_MODES = {"reflink", "snapshot"}


def _env_flag(name: str, default: str = "false") -> bool:
    return _shared_env_flag(
        name,
        default=default.strip().lower() in {"1", "true", "yes", "on"},
        true_only=True,
    )


def _utcnow() -> datetime:
    return _shared_utcnow()


def _env_int(name: str, default: str) -> int:
    return _shared_env_int(name, default, minimum=1)


@lru_cache(maxsize=32)
def _probe_workspace_clone_support(probe_root: str) -> bool:
    root = Path(probe_root)
    try:
        root.mkdir(parents=True, exist_ok=True)
    except Exception:
        return False

    command = ["cp", "-cR"] if platform.system() == "Darwin" else ["cp", "--reflink=always", "-R"]
    try:
        with tempfile.TemporaryDirectory(dir=root) as tmpdir:
            source = Path(tmpdir) / "source"
            source.mkdir()
            (source / "README.md").write_text("clone-probe\n")
            (source / "nested").mkdir()
            (source / "nested" / "payload.txt").write_text("probe\n")
            target = Path(tmpdir) / "target"
            result = subprocess.run(
                [*command, f"{source}/.", str(target)],
                capture_output=True,
                text=True,
                timeout=20,
            )
            return result.returncode == 0 and (target / "README.md").exists() and (target / "nested" / "payload.txt").exists()
    except Exception:
        return False


@dataclass(frozen=True)
class PoolPlan:
    """Outcome of a warm-pool lookup."""

    resource_kind: str
    mode: str
    warm_start_used: bool
    reason: str | None
    pool_key: str | None = None
    pool_entry_id: int | None = None
    lease_token: str | None = None
    summary: dict[str, Any] | None = None
    lease: LeaseDecision | None = None
    resource_path: str | None = None
    cleanup_required: bool = False


class ResourcePoolManager:
    """Own resource acquisition, release, and reclaim decisions."""

    def __init__(self, *, lease_manager: ResourceLeaseManager | None = None):
        self.lease_manager = lease_manager or ResourceLeaseManager()
        self.workspace_enabled = _env_flag("CORTEX_WARM_WORKSPACE_POOL_ENABLED")
        self.browser_enabled = _env_flag("CORTEX_BROWSER_POOL_ENABLED")
        self.workspace_mode = (os.environ.get("CORTEX_WORKSPACE_DELTA_MODE", "cold") or "cold").strip().lower()
        self.browser_mode = (os.environ.get("CORTEX_BROWSER_CONTEXT_MODE", "fresh") or "fresh").strip().lower()
        self.workspace_ttl_seconds = _env_int("CORTEX_WARM_WORKSPACE_POOL_TTL_SEC", "600")
        self.browser_ttl_seconds = _env_int("CORTEX_BROWSER_POOL_TTL_SEC", "600")
        self.pool_root = Path(os.environ.get("CORTEX_WARM_POOL_ROOT", tempfile.gettempdir()))

    def workspace_pool_key(
        self,
        *,
        repo_root: str | None,
        base_commit: str | None,
        runtime_fingerprint: str | None,
        mode: str | None = None,
    ) -> str | None:
        selected_mode = (mode or self.workspace_mode or "cold").strip().lower()
        if selected_mode not in _SUPPORTED_WORKSPACE_MODES:
            return None
        if not repo_root or not base_commit or not runtime_fingerprint:
            return None
        return "|".join((str(repo_root), str(base_commit), str(runtime_fingerprint), selected_mode))

    def _workspace_mode_supported(self, mode: str, probe_root: str | None) -> bool:
        if mode == "cold" or mode == "copy":
            return True
        if mode not in _WORKSPACE_ADVANCED_MODES:
            return False
        return _probe_workspace_clone_support(str(Path(probe_root or self.pool_root).resolve()))

    def _resolve_workspace_mode(
        self,
        requested_mode: str | None,
        *,
        probe_root: str | None,
        allow_warm_reuse: bool,
    ) -> str:
        selected_mode = (requested_mode or self.workspace_mode or "cold").strip().lower()
        if not allow_warm_reuse:
            return "cold"
        if selected_mode == "cold":
            selected_mode = "copy"
        if selected_mode not in _SUPPORTED_WORKSPACE_MODES:
            selected_mode = "copy"
        if selected_mode in _WORKSPACE_ADVANCED_MODES and not self._workspace_mode_supported(selected_mode, probe_root):
            return "copy"
        if not self._workspace_mode_supported(selected_mode, probe_root):
            return "cold"
        return selected_mode

    def browser_pool_key(
        self,
        *,
        profile_key: str | None,
        browser_version: str | None,
        context_mode: str | None = None,
    ) -> str | None:
        selected_mode = (context_mode or self.browser_mode or "fresh").strip().lower()
        if selected_mode not in _SUPPORTED_BROWSER_MODES:
            return None
        if not profile_key or not browser_version:
            return None
        return "|".join((str(profile_key), str(browser_version), selected_mode))

    def plan_workspace(
        self,
        *,
        repo_root: str | None,
        base_commit: str | None,
        runtime_fingerprint: str | None,
        mode: str | None = None,
        allow_warm_reuse: bool = False,
        run_id: int | None = None,
        worker_id: str | None = None,
        target_path: str | None = None,
    ) -> PoolPlan:
        probe_root = str(Path(target_path).parent) if target_path else repo_root
        pool_key = self.workspace_pool_key(
            repo_root=repo_root,
            base_commit=base_commit,
            runtime_fingerprint=runtime_fingerprint,
            mode=self._resolve_workspace_mode(mode, probe_root=probe_root, allow_warm_reuse=allow_warm_reuse),
        )
        selected_mode = self._resolve_workspace_mode(
            mode,
            probe_root=probe_root,
            allow_warm_reuse=allow_warm_reuse,
        )

        if not self.workspace_enabled:
            reason = "warm workspace pool disabled"
            return self._workspace_cold_plan(repo_root, base_commit, runtime_fingerprint, pool_key, reason)
        if not allow_warm_reuse:
            reason = "warm workspace reuse not enabled in this slice"
            return self._workspace_cold_plan(repo_root, base_commit, runtime_fingerprint, pool_key, reason)

        with UnitOfWork() as uow:
            entry = (
                uow.session.query(WorkspacePoolEntry)
                .filter(
                    WorkspacePoolEntry.pool_key == pool_key,
                    WorkspacePoolEntry.status.in_(sorted(_WORKSPACE_STATUS_READY)),
                )
                .order_by(WorkspacePoolEntry.last_used_at.asc().nullsfirst(), WorkspacePoolEntry.created_at.asc())
                .first()
            )

        if not entry:
            return self._workspace_cold_plan(
                repo_root,
                base_commit,
                runtime_fingerprint,
                pool_key,
                "no safe warm workspace candidate available",
            )

        valid, reason = self._validate_workspace_entry(entry)
        if not valid:
            self._destroy_workspace_entry(entry, reason or "workspace pool entry failed validation")
            return self._workspace_cold_plan(
                repo_root,
                base_commit,
                runtime_fingerprint,
                pool_key,
                "no safe warm workspace candidate available",
            )

        lease = self.lease_manager.acquire_lease(
            "workspace_pool_entry",
            str(entry.id),
            owner_run_id=run_id,
            owner_worker_id=worker_id,
            ttl_seconds=self.workspace_ttl_seconds,
        )
        if not lease.acquired or not lease.lease_token:
            return self._workspace_cold_plan(
                repo_root,
                base_commit,
                runtime_fingerprint,
                pool_key,
                lease.reason or "active lease exists",
            )

        workspace_path = self._workspace_handoff_path(
            repo_root=repo_root,
            base_commit=base_commit,
            runtime_fingerprint=runtime_fingerprint,
            run_id=run_id,
            target_path=target_path,
        )
        try:
            self._materialize_workspace_entry(entry, workspace_path, mode=selected_mode)
        except Exception as exc:
            logger.warning("Warm workspace handoff failed for pool entry %s: %s", entry.id, exc)
            self.lease_manager.release_lease(lease.lease_token, release_reason="handoff_failed")
            self._destroy_workspace_entry(entry, f"warm handoff failed: {exc}")
            return self._workspace_cold_plan(
                repo_root,
                base_commit,
                runtime_fingerprint,
                pool_key,
                "warm workspace handoff failed",
            )

        self._mark_workspace_entry_leased(entry.id, lease.lease_token, workspace_path, selected_mode)
        summary = build_workspace_resource_summary(
            mode=selected_mode,
            warm_start_used=True,
            reason="warm workspace pool hit",
            worktree_path=str(workspace_path),
            branch=f"run/{run_id}" if run_id is not None else None,
            repo_root=repo_root,
            base_commit=base_commit,
            runtime_fingerprint=runtime_fingerprint,
            pool_key=pool_key,
            pool_entry_id=entry.id,
            lease_token=lease.lease_token,
        )
        return PoolPlan(
            resource_kind="workspace",
            mode=selected_mode,
            warm_start_used=True,
            reason="warm workspace pool hit",
            pool_key=pool_key,
            pool_entry_id=entry.id,
            lease_token=lease.lease_token,
            summary=summary,
            lease=lease,
            resource_path=str(workspace_path),
            cleanup_required=True,
        )

    def acquire_workspace(
        self,
        *,
        repo_root: str | None,
        base_commit: str | None,
        runtime_fingerprint: str | None,
        mode: str | None = None,
        allow_warm_reuse: bool = False,
        run_id: int | None = None,
        worker_id: str | None = None,
        target_path: str | None = None,
    ) -> PoolPlan:
        return self.plan_workspace(
            repo_root=repo_root,
            base_commit=base_commit,
            runtime_fingerprint=runtime_fingerprint,
            mode=mode,
            allow_warm_reuse=allow_warm_reuse,
            run_id=run_id,
            worker_id=worker_id,
            target_path=target_path,
        )

    def plan_browser(
        self,
        *,
        profile_key: str | None,
        browser_version: str | None,
        context_mode: str | None = None,
        allow_warm_reuse: bool = False,
        owner_run_id: int | None = None,
        owner_worker_id: str | None = None,
    ) -> PoolPlan:
        pool_key = self.browser_pool_key(
            profile_key=profile_key,
            browser_version=browser_version,
            context_mode=context_mode,
        )
        selected_mode = (context_mode or self.browser_mode or "fresh").strip().lower()
        if selected_mode not in _SUPPORTED_BROWSER_MODES:
            selected_mode = "fresh"

        if not self.browser_enabled:
            reason = "warm browser pool disabled"
            return self._browser_cold_plan(profile_key, browser_version, selected_mode, pool_key, reason)
        if not allow_warm_reuse:
            reason = "warm browser reuse not enabled in this slice"
            return self._browser_cold_plan(profile_key, browser_version, selected_mode, pool_key, reason)

        with UnitOfWork() as uow:
            entry = (
                uow.session.query(BrowserPoolEntry)
                .filter(
                    BrowserPoolEntry.profile_key == profile_key,
                    BrowserPoolEntry.browser_version == browser_version,
                    BrowserPoolEntry.context_mode == selected_mode,
                    BrowserPoolEntry.status.in_(sorted(_BROWSER_STATUS_READY)),
                )
                .order_by(BrowserPoolEntry.last_used_at.asc().nullsfirst(), BrowserPoolEntry.created_at.asc())
                .first()
            )

        if not entry:
            return self._browser_cold_plan(
                profile_key,
                browser_version,
                selected_mode,
                pool_key,
                "no safe warm browser candidate available",
            )

        valid, reason = self._validate_browser_entry(entry)
        if not valid:
            self._destroy_browser_entry(entry, reason or "browser pool entry failed validation")
            return self._browser_cold_plan(
                profile_key,
                browser_version,
                selected_mode,
                pool_key,
                "no safe warm browser candidate available",
            )

        lease = self.lease_manager.acquire_lease(
            "browser_pool_entry",
            str(entry.id),
            owner_run_id=owner_run_id,
            owner_worker_id=owner_worker_id,
            ttl_seconds=self.browser_ttl_seconds,
        )
        if not lease.acquired or not lease.lease_token:
            return self._browser_cold_plan(
                profile_key,
                browser_version,
                selected_mode,
                pool_key,
                lease.reason or "active lease exists",
            )

        self._mark_browser_entry_leased(entry.id, lease.lease_token, selected_mode)
        summary = build_browser_resource_summary(
            mode=selected_mode,
            warm_start_used=True,
            reason="warm browser pool hit",
            profile_key=profile_key,
            browser_version=browser_version,
            context_mode=selected_mode,
            pool_key=pool_key,
            pool_entry_id=entry.id,
            lease_token=lease.lease_token,
        )
        return PoolPlan(
            resource_kind="browser",
            mode=selected_mode,
            warm_start_used=True,
            reason="warm browser pool hit",
            pool_key=pool_key,
            pool_entry_id=entry.id,
            lease_token=lease.lease_token,
            summary=summary,
            lease=lease,
            cleanup_required=True,
        )

    def acquire_browser(
        self,
        *,
        profile_key: str | None,
        browser_version: str | None,
        context_mode: str | None = None,
        allow_warm_reuse: bool = False,
        owner_run_id: int | None = None,
        owner_worker_id: str | None = None,
    ) -> PoolPlan:
        return self.plan_browser(
            profile_key=profile_key,
            browser_version=browser_version,
            context_mode=context_mode,
            allow_warm_reuse=allow_warm_reuse,
            owner_run_id=owner_run_id,
            owner_worker_id=owner_worker_id,
        )

    def release_workspace(
        self,
        lease_token: str | None,
        *,
        pool_entry_id: int | None = None,
        release_reason: str = "released",
    ) -> bool:
        if not lease_token:
            return False
        released = self.lease_manager.release_lease(lease_token, release_reason=release_reason)
        self._finalize_workspace_entry(pool_entry_id, lease_token, release_reason)
        return released

    def release_browser(
        self,
        lease_token: str | None,
        *,
        pool_entry_id: int | None = None,
        release_reason: str = "released",
    ) -> bool:
        if not lease_token:
            return False
        released = self.lease_manager.release_lease(lease_token, release_reason=release_reason)
        self._finalize_browser_entry(pool_entry_id, lease_token, release_reason)
        return released

    def reclaim_workspace(self) -> int:
        reclaimed = self.lease_manager.reclaim_expired(resource_type="workspace_pool_entry")
        self._reconcile_expired_workspace_entries()
        return reclaimed

    def reclaim_browser(self) -> int:
        reclaimed = self.lease_manager.reclaim_expired(resource_type="browser_pool_entry")
        self._reconcile_expired_browser_entries()
        return reclaimed

    def _workspace_cold_plan(
        self,
        repo_root: str | None,
        base_commit: str | None,
        runtime_fingerprint: str | None,
        pool_key: str | None,
        reason: str,
    ) -> PoolPlan:
        return PoolPlan(
            resource_kind="workspace",
            mode="cold",
            warm_start_used=False,
            reason=reason,
            pool_key=pool_key,
            summary=build_workspace_resource_summary(
                mode="cold",
                warm_start_used=False,
                reason=reason,
                repo_root=repo_root,
                base_commit=base_commit,
                runtime_fingerprint=runtime_fingerprint,
                pool_key=pool_key,
            ),
        )

    def _browser_cold_plan(
        self,
        profile_key: str | None,
        browser_version: str | None,
        context_mode: str,
        pool_key: str | None,
        reason: str,
    ) -> PoolPlan:
        return PoolPlan(
            resource_kind="browser",
            mode="cold",
            warm_start_used=False,
            reason=reason,
            pool_key=pool_key,
            summary=build_browser_resource_summary(
                mode="cold",
                warm_start_used=False,
                reason=reason,
                profile_key=profile_key,
                browser_version=browser_version,
                context_mode=context_mode,
                pool_key=pool_key,
            ),
        )

    def _workspace_handoff_path(
        self,
        *,
        repo_root: str | None,
        base_commit: str | None,
        runtime_fingerprint: str | None,
        run_id: int | None,
        target_path: str | None,
    ) -> Path:
        if target_path:
            return Path(target_path)
        run_part = str(run_id) if run_id is not None else "shared"
        safe_runtime = (runtime_fingerprint or "runtime").replace(os.sep, "-").replace(":", "-")
        safe_commit = (base_commit or "commit")[:12]
        root = Path(repo_root or self.pool_root)
        return root / ".warm-pools" / f"{run_part}-{safe_commit}-{safe_runtime}"

    def _validate_workspace_entry(self, entry: WorkspacePoolEntry) -> tuple[bool, str | None]:
        base_path = Path(entry.base_path)
        if not base_path.exists():
            return False, "workspace pool entry base path is missing"
        if entry.pool_key and entry.pool_key != self.workspace_pool_key(
            repo_root=entry.repo_root,
            base_commit=entry.base_commit,
            runtime_fingerprint=entry.runtime_fingerprint,
            mode=entry.mode,
        ):
            return False, "workspace pool key mismatch"

        try:
            status = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=str(base_path),
                capture_output=True,
                text=True,
                timeout=15,
            )
            if status.returncode != 0:
                return False, f"workspace pool entry validation failed: {status.stderr.strip() or status.stdout.strip()}"
            if status.stdout.strip():
                return False, "workspace pool entry is dirty"
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(base_path),
                capture_output=True,
                text=True,
                timeout=15,
            )
            if head.returncode != 0:
                return False, f"workspace pool entry validation failed: {head.stderr.strip() or head.stdout.strip()}"
            if entry.base_commit and head.stdout.strip() != entry.base_commit.strip():
                return False, "workspace pool entry commit mismatch"
        except Exception as exc:
            return False, f"workspace pool entry validation failed: {exc}"
        return True, None

    def _validate_browser_entry(self, entry: BrowserPoolEntry) -> tuple[bool, str | None]:
        if entry.context_mode not in _SUPPORTED_BROWSER_MODES:
            return False, "browser pool entry context mode is unsupported"
        if entry.status not in _BROWSER_STATUS_READY:
            return False, "browser pool entry is not ready"
        return True, None

    def _materialize_workspace_entry(self, entry: WorkspacePoolEntry, target_path: Path, *, mode: str) -> None:
        source = Path(entry.base_path)
        if target_path.exists():
            shutil.rmtree(target_path, ignore_errors=True)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        if mode not in _SUPPORTED_WORKSPACE_MODES:
            mode = "copy"
        if mode in _WORKSPACE_ADVANCED_MODES:
            clone_command = ["cp", "-cR"] if platform.system() == "Darwin" else ["cp", "--reflink=always", "-R"]
            result = subprocess.run(
                [*clone_command, f"{source}/.", str(target_path)],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "workspace clone failed")
            return
        shutil.copytree(source, target_path, dirs_exist_ok=False)

    def _destroy_workspace_entry(self, entry: WorkspacePoolEntry, reason: str) -> None:
        try:
            shutil.rmtree(entry.base_path, ignore_errors=True)
        except Exception:
            pass
        try:
            with UnitOfWork() as uow:
                row = uow.session.get(WorkspacePoolEntry, entry.id)
                if row:
                    row.status = "destroyed"
                    row.health = {**(row.health or {}), "destroyed_reason": reason, "destroyed_at": _utcnow().isoformat()}
                    row.ttl_expires_at = _utcnow()
        except Exception:
            logger.debug("Failed to mark workspace pool entry %s destroyed", entry.id)

    def _destroy_browser_entry(self, entry: BrowserPoolEntry, reason: str) -> None:
        try:
            with UnitOfWork() as uow:
                row = uow.session.get(BrowserPoolEntry, entry.id)
                if row:
                    row.status = "destroyed"
                    row.health = {**(row.health or {}), "destroyed_reason": reason, "destroyed_at": _utcnow().isoformat()}
                    row.ttl_expires_at = _utcnow()
        except Exception:
            logger.debug("Failed to mark browser pool entry %s destroyed", entry.id)

    def _mark_workspace_entry_leased(self, entry_id: int, lease_token: str, workspace_path: Path, mode: str) -> None:
        try:
            with UnitOfWork() as uow:
                row = uow.session.get(WorkspacePoolEntry, entry_id)
                if row:
                    row.status = "leased"
                    row.last_used_at = _utcnow()
                    row.ttl_expires_at = _utcnow() + timedelta(seconds=self.workspace_ttl_seconds)
                    row.health = {
                        **(row.health or {}),
                        "last_lease_token": lease_token,
                        "last_workspace_path": str(workspace_path),
                        "last_mode": mode,
                    }
        except Exception:
            logger.debug("Failed to mark workspace pool entry %s leased", entry_id)

    def _mark_browser_entry_leased(self, entry_id: int, lease_token: str, mode: str) -> None:
        try:
            with UnitOfWork() as uow:
                row = uow.session.get(BrowserPoolEntry, entry_id)
                if row:
                    row.status = "leased"
                    row.last_used_at = _utcnow()
                    row.ttl_expires_at = _utcnow() + timedelta(seconds=self.browser_ttl_seconds)
                    row.health = {
                        **(row.health or {}),
                        "last_lease_token": lease_token,
                        "last_mode": mode,
                    }
        except Exception:
            logger.debug("Failed to mark browser pool entry %s leased", entry_id)

    def _finalize_workspace_entry(self, pool_entry_id: int | None, lease_token: str, release_reason: str) -> None:
        if pool_entry_id is None:
            return
        try:
            with UnitOfWork() as uow:
                row = uow.session.get(WorkspacePoolEntry, pool_entry_id)
                if row:
                    row.last_used_at = _utcnow()
                    row.health = {
                        **(row.health or {}),
                        "last_release_reason": release_reason,
                        "last_lease_token": lease_token,
                    }
                    row.status = "ready" if release_reason not in {"suspicious", "destroyed"} else row.status
        except Exception:
            logger.debug("Failed to finalize workspace pool entry %s", pool_entry_id)

    def _finalize_browser_entry(self, pool_entry_id: int | None, lease_token: str, release_reason: str) -> None:
        if pool_entry_id is None:
            return
        try:
            with UnitOfWork() as uow:
                row = uow.session.get(BrowserPoolEntry, pool_entry_id)
                if row:
                    row.last_used_at = _utcnow()
                    row.health = {
                        **(row.health or {}),
                        "last_release_reason": release_reason,
                        "last_lease_token": lease_token,
                    }
                    row.status = "ready" if release_reason not in {"suspicious", "destroyed"} else row.status
        except Exception:
            logger.debug("Failed to finalize browser pool entry %s", pool_entry_id)

    def _reconcile_expired_workspace_entries(self) -> None:
        try:
            now = _utcnow()
            with UnitOfWork() as uow:
                rows = (
                    uow.session.query(WorkspacePoolEntry)
                    .filter(
                        WorkspacePoolEntry.status == "leased",
                        WorkspacePoolEntry.ttl_expires_at.isnot(None),
                        WorkspacePoolEntry.ttl_expires_at <= now,
                    )
                    .all()
                )
                for row in rows:
                    row.status = "ready"
                    row.health = {**(row.health or {}), "reclaimed_at": now.isoformat(), "reclaimed_reason": "expired"}
        except Exception:
            logger.debug("Failed to reconcile expired workspace entries")

    def _reconcile_expired_browser_entries(self) -> None:
        try:
            now = _utcnow()
            with UnitOfWork() as uow:
                rows = (
                    uow.session.query(BrowserPoolEntry)
                    .filter(
                        BrowserPoolEntry.status == "leased",
                        BrowserPoolEntry.ttl_expires_at.isnot(None),
                        BrowserPoolEntry.ttl_expires_at <= now,
                    )
                    .all()
                )
                for row in rows:
                    row.status = "ready"
                    row.health = {**(row.health or {}), "reclaimed_at": now.isoformat(), "reclaimed_reason": "expired"}
        except Exception:
            logger.debug("Failed to reconcile expired browser entries")


class WorkspacePoolManager(ResourcePoolManager):
    """Backward-compatible wrapper for workspace-specific callers."""

    def plan(
        self,
        *,
        repo_root: str | None,
        base_commit: str | None,
        runtime_fingerprint: str | None,
        mode: str | None = None,
        allow_warm_reuse: bool = False,
        run_id: int | None = None,
        worker_id: str | None = None,
        target_path: str | None = None,
    ) -> PoolPlan:
        return self.plan_workspace(
            repo_root=repo_root,
            base_commit=base_commit,
            runtime_fingerprint=runtime_fingerprint,
            mode=mode,
            allow_warm_reuse=allow_warm_reuse,
            run_id=run_id,
            worker_id=worker_id,
            target_path=target_path,
        )


class BrowserPoolManager(ResourcePoolManager):
    """Backward-compatible wrapper for browser-specific callers."""

    def plan(
        self,
        *,
        profile_key: str | None,
        browser_version: str | None,
        context_mode: str | None = None,
        allow_warm_reuse: bool = False,
        owner_run_id: int | None = None,
        owner_worker_id: str | None = None,
    ) -> PoolPlan:
        return self.plan_browser(
            profile_key=profile_key,
            browser_version=browser_version,
            context_mode=context_mode,
            allow_warm_reuse=allow_warm_reuse,
            owner_run_id=owner_run_id,
            owner_worker_id=owner_worker_id,
        )
