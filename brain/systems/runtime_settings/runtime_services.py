from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from brain.kernel import config as cfg

from .schemas import RuntimeServiceRead, RuntimeServicesRead
from .sidecar_queue import (
    SidecarQueue,
    acquire_start_lock,
    parse_datetime,
    release_start_lock,
)

_RUNTIME_SERVICES_QUEUE = SidecarQueue(
    request_file_env="ILLO_RUNTIME_SERVICES_REQUEST_FILE",
    status_file_env="ILLO_RUNTIME_SERVICES_STATUS_FILE",
    log_path_env="ILLO_RUNTIME_SERVICES_LOG_PATH",
    default_log_name="illo-runtime-services.log",
    ready_detail="Queues runtime service operations for the host controller.",
    queue_unavailable_label="Runtime service queue",
    waiting_detail="Runtime service management is waiting for the host controller.",
    stale_detail="Runtime service management is unavailable because the host controller heartbeat is stale.",
    heartbeat_file_env="ILLO_RUNTIME_SERVICES_HEARTBEAT_FILE",
    require_heartbeat=True,
)


async def async_get_runtime_services_status() -> RuntimeServicesRead:
    request_file = _RUNTIME_SERVICES_QUEUE.request_file()
    services = runtime_service_catalog()
    if request_file is None:
        return RuntimeServicesRead(
            status="idle",
            available=False,
            services=services,
            log_path=str(_RUNTIME_SERVICES_QUEUE.log_path()),
            detail="Runtime service management is unavailable because no host-side service queue is configured.",
        )

    available, availability_detail = _RUNTIME_SERVICES_QUEUE.availability(request_file)
    status_data = _RUNTIME_SERVICES_QUEUE.status_data(request_file)
    detail = status_data.get("detail") if isinstance(status_data.get("detail"), str) else None

    running = available and _RUNTIME_SERVICES_QUEUE.status_is_running(request_file, status_data)

    return RuntimeServicesRead(
        status="running" if running else "idle",
        available=available,
        services=services,
        requested_services=_coerce_service_list(status_data.get("services")),
        started_at=parse_datetime(status_data.get("started_at") or status_data.get("requested_at")),
        log_path=str(_RUNTIME_SERVICES_QUEUE.log_path()),
        detail=(detail if available else None) or availability_detail,
    )


async def async_restart_runtime_services(
    services: list[str] | tuple[str, ...] | str | None,
    *,
    requested_by: str | None = None,
) -> RuntimeServicesRead:
    request_file = _RUNTIME_SERVICES_QUEUE.request_file()
    if request_file is None:
        raise HTTPException(
            status_code=409,
            detail="Runtime service management is unavailable because no host-side service queue is configured.",
        )

    available, detail = _RUNTIME_SERVICES_QUEUE.availability(request_file)
    if not available:
        raise HTTPException(status_code=409, detail=detail or "Runtime service management is unavailable.")

    requested_services = normalize_runtime_service_ids(services)
    existing = await async_get_runtime_services_status()
    if existing.status == "running":
        return RuntimeServicesRead(
            **{
                **existing.model_dump(),
                "detail": "A runtime service operation is already running.",
            }
        )

    lock_path = _RUNTIME_SERVICES_QUEUE.start_lock_path(request_file)
    lock_fd = acquire_start_lock(lock_path)
    if lock_fd is None:
        current = await async_get_runtime_services_status()
        return RuntimeServicesRead(
            **{
                **current.model_dump(),
                "status": "running",
                "detail": "A runtime service operation is starting.",
            }
        )

    try:
        started_at = datetime.now(timezone.utc)
        payload = {
            "action": "restart",
            "services": requested_services,
            "requested_at": started_at.isoformat(),
            "requested_by": requested_by,
        }
        _RUNTIME_SERVICES_QUEUE.write_json(request_file, payload)
        _RUNTIME_SERVICES_QUEUE.write_json(
            _RUNTIME_SERVICES_QUEUE.status_file(request_file),
            {
                **payload,
                "started_at": started_at.isoformat(),
                "status": "queued",
                "detail": "Runtime service restart queued for the host controller.",
            },
        )
        return RuntimeServicesRead(
            status="running",
            available=True,
            services=runtime_service_catalog(),
            requested_services=requested_services,
            started_at=started_at,
            log_path=str(_RUNTIME_SERVICES_QUEUE.log_path()),
            detail="Runtime service restart queued for the host controller.",
        )
    finally:
        release_start_lock(lock_fd, lock_path)


def runtime_service_catalog() -> list[RuntimeServiceRead]:
    services = _read_runtime_service_catalog().get("services")
    if not isinstance(services, list):
        return []

    catalog: list[RuntimeServiceRead] = []
    for item in services:
        if not isinstance(item, dict):
            continue
        service_id = str(item.get("id") or "").strip()
        name = str(item.get("name") or service_id).strip()
        description = str(item.get("description") or "").strip()
        if not service_id or not name or not description:
            continue
        catalog.append(
            RuntimeServiceRead(
                id=service_id,
                name=name,
                description=description,
                restartable=bool(item.get("restartable", True)),
                optional=bool(item.get("optional", False)),
            )
        )
    return catalog


def normalize_runtime_service_ids(services: list[str] | tuple[str, ...] | str | None) -> list[str]:
    values = _coerce_service_list(services)
    if not values:
        raise HTTPException(status_code=400, detail="services must include at least one runtime service id.")

    known = {service.id for service in runtime_service_catalog()}
    expanded: list[str] = []
    unknown: list[str] = []
    for value in values:
        normalized = _normalize_service_id(value)
        if normalized == "all":
            expanded.append("all")
        elif normalized in known:
            expanded.append(normalized)
        else:
            unknown.append(str(value))

    if unknown:
        allowed = ", ".join(["all", *sorted(known)])
        raise HTTPException(
            status_code=400,
            detail=f"Unknown runtime service id(s): {', '.join(unknown)}. Allowed: {allowed}.",
        )
    return _dedupe_preserving_order(expanded)


def _read_runtime_service_catalog() -> dict[str, Any]:
    catalog_path = Path(cfg.BRAIN_DIR) / "deploy" / "compose" / "runtime-services.json"
    try:
        data = json.loads(catalog_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _coerce_service_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw_values = value.replace(",", " ").split()
    elif isinstance(value, (list, tuple, set)):
        raw_values = list(value)
    else:
        raw_values = [value]
    return [str(item).strip() for item in raw_values if str(item).strip()]


def _normalize_service_id(value: str) -> str:
    return str(value or "").strip().lower().replace("-", "_")


def _dedupe_preserving_order(values: list[str]) -> list[str]:
    deduped: list[str] = []
    for value in values:
        if value not in deduped:
            deduped.append(value)
    return deduped
