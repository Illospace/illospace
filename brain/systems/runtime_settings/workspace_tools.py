from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.kernel import config as cfg
from brain.platform.db.models.workspace_tool import WorkspaceToolInstallation, WorkspaceToolUserConfig

from .schemas import (
    WorkspaceToolBundleRead,
    WorkspaceToolInstallationRead,
    WorkspaceToolUserConfigRead,
    WorkspaceToolsRead,
)
from .sidecar_queue import (
    SidecarQueue,
    acquire_start_lock,
    parse_datetime,
    read_json,
    release_start_lock,
)


_WORKSPACE_TOOLS_QUEUE = SidecarQueue(
    request_file_env="ILLO_WORKSPACE_TOOLS_REQUEST_FILE",
    status_file_env="ILLO_WORKSPACE_TOOLS_STATUS_FILE",
    log_path_env="ILLO_WORKSPACE_TOOLS_LOG_PATH",
    default_log_name="illo-workspace-tools.log",
    ready_detail="Queues workspace tool installs for the host controller.",
    queue_unavailable_label="Workspace tool installer queue",
    waiting_detail="Workspace tool installation is waiting for the host controller.",
    stale_detail="Workspace tool installation is unavailable because the host controller heartbeat is stale.",
    heartbeat_file_env="ILLO_WORKSPACE_TOOLS_HEARTBEAT_FILE",
    require_heartbeat=True,
)
_DEFAULT_TOOL_ROOT = "/data/private/workspace-tools"
_SAFE_PATH_RE = re.compile(r"[^a-zA-Z0-9_.-]+")


def workspace_tool_root() -> Path:
    raw = os.getenv("ILLO_WORKSPACE_TOOLS_ROOT", _DEFAULT_TOOL_ROOT).strip() or _DEFAULT_TOOL_ROOT
    return Path(raw).resolve()


def workspace_tool_catalog() -> list[WorkspaceToolBundleRead]:
    catalog = _read_workspace_tool_catalog().get("bundles")
    if not isinstance(catalog, list):
        return []

    bundles: list[WorkspaceToolBundleRead] = []
    for item in catalog:
        if not isinstance(item, dict):
            continue
        bundle_id = str(item.get("id") or "").strip()
        name = str(item.get("name") or bundle_id).strip()
        description = str(item.get("description") or "").strip()
        if not bundle_id or not name or not description:
            continue
        bundles.append(
            WorkspaceToolBundleRead(
                id=bundle_id,
                name=name,
                description=description,
                version=_optional_text(item.get("version")),
                provided_commands=_string_list(item.get("provided_commands")),
                skill_dependencies=_string_list(item.get("skill_dependencies")),
                install_profile=_optional_text(item.get("install_profile") or item.get("installer")),
                optional=bool(item.get("optional", False)),
                metadata=item.get("metadata") if isinstance(item.get("metadata"), dict) else {},
                runtime=item.get("runtime") if isinstance(item.get("runtime"), dict) else {},
            )
        )
    return bundles


async def async_get_workspace_tools_status(
    session: AsyncSession,
    *,
    org_id: str,
    bundle_id: str | None = None,
) -> WorkspaceToolsRead:
    request_file = _WORKSPACE_TOOLS_QUEUE.request_file()
    catalog = workspace_tool_catalog()
    catalog_by_id = {bundle.id: bundle for bundle in catalog}
    normalized_bundle_id = normalize_bundle_id(bundle_id) if bundle_id else None
    if normalized_bundle_id and normalized_bundle_id not in catalog_by_id:
        raise HTTPException(status_code=400, detail=_unknown_bundle_detail(normalized_bundle_id, catalog_by_id))

    if request_file is None:
        available = False
        availability_detail = "Workspace tool installation is unavailable because no host-side installer queue is configured."
        status_data: dict[str, Any] = {}
        running = False
    else:
        available, availability_detail = _WORKSPACE_TOOLS_QUEUE.availability(request_file)
        status_data = _WORKSPACE_TOOLS_QUEUE.status_data(request_file)
        running = available and _WORKSPACE_TOOLS_QUEUE.status_is_running(request_file, status_data)

    rows = await _installation_rows(session, org_id=org_id, bundle_id=normalized_bundle_id)
    await _sync_installations_from_manifests(session, rows, org_id=org_id, catalog_by_id=catalog_by_id)
    rows = await _installation_rows(session, org_id=org_id, bundle_id=normalized_bundle_id)
    installations = [_serialize_installation(row) for row in rows]

    detail = status_data.get("detail") if isinstance(status_data.get("detail"), str) else None
    requested_bundle_id = _optional_text(status_data.get("bundle_id"))
    if normalized_bundle_id and requested_bundle_id and requested_bundle_id != normalized_bundle_id:
        running = False

    return WorkspaceToolsRead(
        status="running" if running else "idle",
        available=available,
        catalog=catalog,
        installations=installations,
        requested_bundle_id=requested_bundle_id,
        started_at=parse_datetime(status_data.get("started_at") or status_data.get("requested_at")),
        log_path=str(_WORKSPACE_TOOLS_QUEUE.log_path()),
        detail=detail or availability_detail,
    )


async def async_install_workspace_tool(
    session: AsyncSession,
    *,
    org_id: str,
    bundle_id: str,
    requested_by: str | None = None,
) -> WorkspaceToolsRead:
    bundle = require_workspace_tool_bundle(bundle_id)
    request_file = _WORKSPACE_TOOLS_QUEUE.request_file()
    if request_file is None:
        raise HTTPException(
            status_code=409,
            detail="Workspace tool installation is unavailable because no host-side installer queue is configured.",
        )

    available, detail = _WORKSPACE_TOOLS_QUEUE.availability(request_file)
    if not available:
        raise HTTPException(status_code=409, detail=detail or "Workspace tool installation is unavailable.")

    existing = await async_get_workspace_tools_status(session, org_id=org_id, bundle_id=bundle.id)
    if existing.status == "running":
        return WorkspaceToolsRead(
            **{
                **existing.model_dump(),
                "detail": "A workspace tool installation is already running.",
            }
        )

    lock_path = _WORKSPACE_TOOLS_QUEUE.start_lock_path(request_file)
    lock_fd = acquire_start_lock(lock_path)
    if lock_fd is None:
        current = await async_get_workspace_tools_status(session, org_id=org_id, bundle_id=bundle.id)
        return WorkspaceToolsRead(
            **{
                **current.model_dump(),
                "status": "running",
                "detail": "A workspace tool installation is starting.",
            }
        )

    try:
        started_at = datetime.now(timezone.utc)
        paths = workspace_tool_paths(org_id=org_id, bundle_id=bundle.id, version=bundle.version)
        payload = {
            "action": "install",
            "org_id": str(org_id),
            "bundle_id": bundle.id,
            "version": bundle.version,
            "install_root": str(paths["install_root"]),
            "current_root": str(paths["current_root"]),
            "bin_path": str(paths["bin_path"]),
            "requested_at": started_at.isoformat(),
            "requested_by": requested_by,
        }
        row = await _upsert_requested_installation(
            session,
            org_id=org_id,
            bundle=bundle,
            payload=payload,
            requested_by=requested_by,
            requested_at=started_at,
        )
        _WORKSPACE_TOOLS_QUEUE.write_json(request_file, payload)
        _WORKSPACE_TOOLS_QUEUE.write_json(
            _WORKSPACE_TOOLS_QUEUE.status_file(request_file),
            {
                **payload,
                "status": "queued",
                "detail": f"Workspace tool bundle {bundle.id} queued for installation.",
            },
        )
        await session.flush()
        status = await async_get_workspace_tools_status(session, org_id=org_id, bundle_id=bundle.id)
        if not status.installations:
            status.installations.append(_serialize_installation(row))
        return status
    finally:
        release_start_lock(lock_fd, lock_path)


async def async_check_workspace_tool(
    session: AsyncSession,
    *,
    org_id: str,
    bundle_id: str,
) -> WorkspaceToolsRead:
    bundle = require_workspace_tool_bundle(bundle_id)
    rows = await _installation_rows(session, org_id=org_id, bundle_id=bundle.id)
    if not rows:
        paths = workspace_tool_paths(org_id=org_id, bundle_id=bundle.id, version=bundle.version)
        row = WorkspaceToolInstallation(
            org_id=str(org_id),
            bundle_id=bundle.id,
            display_name=bundle.name,
            version=bundle.version,
            status="requested",
            install_root=str(paths["current_root"]),
            bin_path=str(paths["bin_path"]),
            health={},
            metadata_={},
        )
        session.add(row)
        await session.flush()
    await _sync_installations_from_manifests(
        session,
        await _installation_rows(session, org_id=org_id, bundle_id=bundle.id),
        org_id=org_id,
        catalog_by_id={bundle.id: bundle},
    )
    await session.flush()
    return await async_get_workspace_tools_status(session, org_id=org_id, bundle_id=bundle.id)


def require_workspace_tool_bundle(bundle_id: str) -> WorkspaceToolBundleRead:
    normalized = normalize_bundle_id(bundle_id)
    catalog = {bundle.id: bundle for bundle in workspace_tool_catalog()}
    bundle = catalog.get(normalized)
    if bundle is None:
        raise HTTPException(status_code=400, detail=_unknown_bundle_detail(normalized, catalog))
    return bundle


def workspace_tool_paths(*, org_id: str, bundle_id: str, version: str | None) -> dict[str, Path]:
    root = workspace_tool_root()
    org_part = _safe_path_part(org_id)
    bundle_part = _safe_path_part(bundle_id)
    version_part = _safe_path_part(version or "default")
    bundle_root = root / "orgs" / org_part / bundle_part
    install_root = bundle_root / "versions" / version_part
    current_root = bundle_root / "current"
    return {
        "bundle_root": bundle_root,
        "install_root": install_root,
        "current_root": current_root,
        "bin_path": current_root / "bin",
        "manifest_path": current_root / "illo-tool.json",
    }


def installed_workspace_tool_bin_paths(org_id: str | None) -> list[str]:
    if not org_id:
        return []
    org_root = workspace_tool_root() / "orgs" / _safe_path_part(org_id)
    if not org_root.exists():
        return []
    paths: list[str] = []
    for manifest_path in sorted(org_root.glob("*/current/illo-tool.json")):
        manifest = read_json(manifest_path)
        if str(manifest.get("status") or "").lower() != "installed":
            continue
        for raw_path in _string_list(manifest.get("path_entries")):
            path = Path(raw_path)
            if path.exists() and path.is_dir():
                paths.append(str(path))
        if not manifest.get("path_entries"):
            bin_path = manifest_path.parent / "bin"
            if bin_path.exists() and bin_path.is_dir():
                paths.append(str(bin_path))
    return _dedupe(paths)


def installed_workspace_tool_bundle_ids(org_id: str | None) -> list[str]:
    if not org_id:
        return []
    org_root = workspace_tool_root() / "orgs" / _safe_path_part(org_id)
    if not org_root.exists():
        return []
    bundle_ids: list[str] = []
    for manifest_path in sorted(org_root.glob("*/current/illo-tool.json")):
        manifest = read_json(manifest_path)
        if str(manifest.get("status") or "").lower() != "installed":
            continue
        bundle_id = _optional_text(manifest.get("bundle_id")) or manifest_path.parent.parent.name
        if bundle_id:
            bundle_ids.append(bundle_id)
    return _dedupe(bundle_ids)


async def async_get_workspace_tool_user_config(
    session: AsyncSession,
    *,
    org_id: str,
    user_id: str,
    bundle_id: str,
) -> WorkspaceToolUserConfigRead | None:
    normalized_bundle_id = normalize_bundle_id(bundle_id)
    row = await _user_config_row(
        session,
        org_id=org_id,
        user_id=user_id,
        bundle_id=normalized_bundle_id,
    )
    return _serialize_user_config(row) if row else None


async def async_set_workspace_tool_user_config(
    session: AsyncSession,
    *,
    org_id: str,
    user_id: str,
    bundle_id: str,
    preferences: dict[str, Any] | None = None,
    credential_refs: dict[str, Any] | None = None,
) -> WorkspaceToolUserConfigRead:
    bundle = require_workspace_tool_bundle(bundle_id)
    row = await _user_config_row(
        session,
        org_id=org_id,
        user_id=user_id,
        bundle_id=bundle.id,
    )
    now = datetime.now(timezone.utc)
    if row is None:
        row = WorkspaceToolUserConfig(
            org_id=str(org_id),
            user_id=str(user_id),
            bundle_id=bundle.id,
            preferences={},
            credential_refs={},
        )
        session.add(row)
    if preferences is not None:
        if not isinstance(preferences, dict):
            raise HTTPException(status_code=400, detail="preferences must be an object.")
        row.preferences = dict(preferences)
    if credential_refs is not None:
        if not isinstance(credential_refs, dict):
            raise HTTPException(status_code=400, detail="credential_refs must be an object.")
        row.credential_refs = dict(credential_refs)
    row.updated_at = now
    await session.flush()
    return _serialize_user_config(row)


def normalize_bundle_id(value: str | None) -> str:
    normalized = str(value or "").strip().lower().replace("_", "-")
    if not normalized:
        raise HTTPException(status_code=400, detail="bundle_id is required.")
    return normalized


async def _installation_rows(
    session: AsyncSession,
    *,
    org_id: str,
    bundle_id: str | None,
) -> list[WorkspaceToolInstallation]:
    stmt = (
        select(WorkspaceToolInstallation)
        .where(WorkspaceToolInstallation.org_id == str(org_id))
        .order_by(WorkspaceToolInstallation.bundle_id.asc())
    )
    if bundle_id:
        stmt = stmt.where(WorkspaceToolInstallation.bundle_id == bundle_id)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def _user_config_row(
    session: AsyncSession,
    *,
    org_id: str,
    user_id: str,
    bundle_id: str,
) -> WorkspaceToolUserConfig | None:
    stmt = (
        select(WorkspaceToolUserConfig)
        .where(
            WorkspaceToolUserConfig.org_id == str(org_id),
            WorkspaceToolUserConfig.user_id == str(user_id),
            WorkspaceToolUserConfig.bundle_id == str(bundle_id),
        )
        .limit(1)
    )
    return (await session.scalars(stmt)).first()


async def _upsert_requested_installation(
    session: AsyncSession,
    *,
    org_id: str,
    bundle: WorkspaceToolBundleRead,
    payload: dict[str, Any],
    requested_by: str | None,
    requested_at: datetime,
) -> WorkspaceToolInstallation:
    rows = await _installation_rows(session, org_id=org_id, bundle_id=bundle.id)
    row = rows[0] if rows else None
    if row is None:
        row = WorkspaceToolInstallation(org_id=str(org_id), bundle_id=bundle.id, display_name=bundle.name)
        session.add(row)
    row.display_name = bundle.name
    row.version = bundle.version
    row.status = "queued"
    row.install_root = str(payload["current_root"])
    row.bin_path = str(payload["bin_path"])
    row.requested_by_user_id = requested_by
    row.requested_at = requested_at
    row.last_error = None
    row.install_request = dict(payload)
    row.metadata_ = {"install_profile": bundle.install_profile}
    row.updated_at = requested_at
    return row


async def _sync_installations_from_manifests(
    session: AsyncSession,
    rows: list[WorkspaceToolInstallation],
    *,
    org_id: str,
    catalog_by_id: dict[str, WorkspaceToolBundleRead],
) -> None:
    by_bundle = {row.bundle_id: row for row in rows}
    for bundle_id, bundle in catalog_by_id.items():
        paths = workspace_tool_paths(org_id=org_id, bundle_id=bundle_id, version=bundle.version)
        manifest = read_json(paths["manifest_path"])
        if not manifest:
            continue
        row = by_bundle.get(bundle_id)
        if row is None:
            row = WorkspaceToolInstallation(
                org_id=str(org_id),
                bundle_id=bundle_id,
                display_name=bundle.name,
            )
            session.add(row)
            by_bundle[bundle_id] = row
        row.display_name = str(manifest.get("name") or bundle.name)
        row.version = _optional_text(manifest.get("version")) or bundle.version
        row.status = _manifest_status(manifest)
        row.install_root = _optional_text(manifest.get("install_root")) or str(paths["current_root"])
        row.bin_path = _optional_text(manifest.get("bin_path")) or str(paths["bin_path"])
        row.installed_at = parse_datetime(manifest.get("installed_at")) or row.installed_at
        row.checked_at = parse_datetime(manifest.get("checked_at")) or row.checked_at
        row.last_error = _optional_text(manifest.get("last_error"))
        row.health = manifest.get("health") if isinstance(manifest.get("health"), dict) else {}
        row.metadata_ = manifest.get("metadata") if isinstance(manifest.get("metadata"), dict) else row.metadata_
        row.updated_at = datetime.now(timezone.utc)


def _serialize_installation(row: WorkspaceToolInstallation) -> WorkspaceToolInstallationRead:
    return WorkspaceToolInstallationRead(
        id=str(row.id) if row.id is not None else None,
        bundle_id=str(row.bundle_id),
        display_name=str(row.display_name),
        version=row.version,
        status=_coerce_status(row.status),
        install_root=row.install_root,
        bin_path=row.bin_path,
        requested_by_user_id=row.requested_by_user_id,
        requested_at=row.requested_at,
        installed_at=row.installed_at,
        checked_at=row.checked_at,
        last_error=row.last_error,
        health=row.health or {},
        metadata=row.metadata_ or {},
    )


def _serialize_user_config(row: WorkspaceToolUserConfig) -> WorkspaceToolUserConfigRead:
    return WorkspaceToolUserConfigRead(
        id=str(row.id) if row.id is not None else None,
        org_id=str(row.org_id),
        user_id=str(row.user_id),
        bundle_id=str(row.bundle_id),
        preferences=row.preferences or {},
        credential_refs=row.credential_refs or {},
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _read_workspace_tool_catalog() -> dict[str, Any]:
    catalog_path = Path(cfg.BRAIN_DIR) / "deploy" / "compose" / "workspace-tools.json"
    try:
        data = json.loads(catalog_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _manifest_status(manifest: dict[str, Any]) -> str:
    return _coerce_status(str(manifest.get("status") or "installed"))


def _coerce_status(status: str | None) -> str:
    normalized = str(status or "").strip().lower()
    if normalized in {"requested", "queued", "installing", "installed", "failed", "removed"}:
        return normalized
    return "failed" if normalized == "error" else "requested"


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        values = value.replace(",", " ").split()
    elif isinstance(value, (list, tuple, set)):
        values = list(value)
    else:
        values = []
    return [str(item).strip() for item in values if str(item or "").strip()]


def _safe_path_part(value: str | None) -> str:
    cleaned = _SAFE_PATH_RE.sub("-", str(value or "").strip()).strip(".-")
    if not cleaned:
        raise HTTPException(status_code=400, detail="Workspace tool path component cannot be empty.")
    return cleaned[:160]


def _dedupe(values: list[str]) -> list[str]:
    deduped: list[str] = []
    for value in values:
        if value not in deduped:
            deduped.append(value)
    return deduped


def _unknown_bundle_detail(bundle_id: str, catalog: dict[str, WorkspaceToolBundleRead]) -> str:
    allowed = ", ".join(sorted(catalog)) or "<none configured>"
    return f"Unknown workspace tool bundle: {bundle_id}. Allowed: {allowed}."
