from __future__ import annotations

import json
import os
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.kernel import config as cfg
from brain.platform.async_io import ensure_dir, run_blocking, write_text
from brain.platform.db.models.agent_run import AgentRunRow
from brain.contracts.statuses import ACTIVE_RUN_STATUS_VALUES

from .schemas import RuntimeUpdateRead
from .sidecar_queue import (
    SidecarQueue,
    acquire_start_lock,
    parse_datetime,
    read_json,
    release_start_lock,
)

_LOG_NAME = "illo-self-update.log"
_META_NAME = "illo-self-update.json"
_PID_NAME = "illo-self-update.pid"
_START_LOCK_NAME = "illo-self-update.starting"
_UPDATE_QUEUE = SidecarQueue(
    request_file_env="ILLO_SELF_UPDATE_REQUEST_FILE",
    status_file_env="ILLO_SELF_UPDATE_STATUS_FILE",
    log_path_env="ILLO_SELF_UPDATE_LOG_PATH",
    default_log_name=_LOG_NAME,
    ready_detail="Queues the update for the Compose updater sidecar.",
    queue_unavailable_label="Illospace update queue",
    waiting_detail="Illospace update is waiting for the Compose updater sidecar.",
    stale_detail="Illospace update is unavailable because the Compose updater sidecar heartbeat is stale.",
    heartbeat_file_env="ILLO_SELF_UPDATE_HEARTBEAT_FILE",
)


async def async_get_runtime_update_status(session: AsyncSession) -> RuntimeUpdateRead:
    root = _repo_root()
    request_file = _request_file()
    if request_file is not None:
        return await _async_request_update_status(session, request_file)

    state_dir = _state_dir(root)
    available, detail = _availability(root)
    metadata = _read_metadata(state_dir)
    pid = _coerce_pid(metadata.get("pid")) or _read_pid(state_dir / _PID_NAME)
    running = bool(pid and _pid_running(pid))
    started_at = parse_datetime(metadata.get("started_at"))

    return RuntimeUpdateRead(
        status="running" if running else "idle",
        available=available,
        pid=pid if running else None,
        started_at=started_at,
        active_agent_runs=await _async_active_agent_run_count(session),
        log_path=str(state_dir / _LOG_NAME),
        detail=detail,
    )


async def async_start_runtime_update(
    session: AsyncSession,
    *,
    requested_by: str | None = None,
    build_no_cache: bool = False,
    worker_drain_timeout_seconds: int | None = None,
) -> RuntimeUpdateRead:
    worker_drain_timeout_seconds = _normalize_worker_drain_timeout(worker_drain_timeout_seconds)
    root = _repo_root()
    request_file = _request_file()
    if request_file is not None:
        return await _async_start_request_update(
            session,
            request_file,
            requested_by=requested_by,
            build_no_cache=build_no_cache,
            worker_drain_timeout_seconds=worker_drain_timeout_seconds,
        )

    available, detail = _availability(root)
    if not available:
        raise HTTPException(status_code=409, detail=detail or "Illospace self-update is unavailable.")

    state_dir = _state_dir(root)
    await ensure_dir(state_dir)
    existing = await async_get_runtime_update_status(session)
    if existing.status == "running":
        return RuntimeUpdateRead(
            **{
                **existing.model_dump(),
                "detail": "Illospace update is already running.",
            }
        )

    lock_path = state_dir / _START_LOCK_NAME
    lock_fd = acquire_start_lock(lock_path)
    if lock_fd is None:
        current = await async_get_runtime_update_status(session)
        return RuntimeUpdateRead(
            **{
                **current.model_dump(),
                "status": "running",
                "detail": "Illospace update is starting.",
            }
        )

    try:
        command = _update_command(root)
        started_at = datetime.now(timezone.utc)
        log_path = state_dir / _LOG_NAME
        _write_log_header(log_path, started_at, requested_by=requested_by)
        env = os.environ.copy()
        env["ILLO_SELF_UPDATE_REQUESTED_AT"] = started_at.isoformat()
        if requested_by:
            env["ILLO_SELF_UPDATE_REQUESTED_BY"] = requested_by
        if build_no_cache:
            env["ILLO_COMPOSE_BUILD_NO_CACHE"] = "1"
        if worker_drain_timeout_seconds is not None:
            env["ILLO_COMPOSE_WORKER_DRAIN_TIMEOUT_SECONDS"] = str(worker_drain_timeout_seconds)

        handle = await run_blocking(log_path.open, "ab")
        try:
            process = await run_blocking(
                subprocess.Popen,
                command,
                cwd=str(root),
                stdin=subprocess.DEVNULL,
                stdout=handle,
                stderr=subprocess.STDOUT,
                close_fds=True,
                start_new_session=True,
                env=env,
            )
        finally:
            await run_blocking(handle.close)

        metadata = {
            "pid": process.pid,
            "started_at": started_at.isoformat(),
            "requested_by": requested_by,
            "root": str(root),
        }
        await write_text(state_dir / _META_NAME, json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")
        await write_text(state_dir / _PID_NAME, f"{process.pid}\n", encoding="utf-8")

        active_runs = await _async_active_agent_run_count(session)
        detail = "Illospace update started."
        if active_runs:
            detail = (
                f"Illospace update started. {active_runs} active AgentRun(s) "
                "will drain before the worker restarts on the new code."
            )
        return RuntimeUpdateRead(
            status="running",
            available=True,
            pid=process.pid,
            started_at=started_at,
            active_agent_runs=active_runs,
            log_path=str(log_path),
            detail=detail,
        )
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Could not start Illospace update: {exc}") from exc
    finally:
        release_start_lock(lock_fd, lock_path)


def _repo_root() -> Path:
    return Path(os.getenv("ILLO_SELF_UPDATE_ROOT") or cfg.BRAIN_DIR).resolve()


def _state_dir(root: Path) -> Path:
    raw = os.getenv("ILLO_SELF_UPDATE_STATE_DIR")
    if raw:
        path = Path(raw)
        return path if path.is_absolute() else root / path
    return Path(cfg.BRAIN_LOG_DIR).resolve()


def _request_file() -> Path | None:
    return _UPDATE_QUEUE.request_file()


def _request_status_file(request_file: Path) -> Path:
    return _UPDATE_QUEUE.status_file(request_file)


def _request_log_path(request_file: Path) -> Path:
    return _UPDATE_QUEUE.log_path()


def _request_availability(request_file: Path) -> tuple[bool, str | None]:
    return _UPDATE_QUEUE.availability(request_file)


async def _async_request_update_status(session: AsyncSession, request_file: Path) -> RuntimeUpdateRead:
    available, availability_detail = _request_availability(request_file)
    status_data = _UPDATE_QUEUE.status_data(request_file)
    running = _UPDATE_QUEUE.status_is_running(request_file, status_data)
    detail = status_data.get("detail") if isinstance(status_data.get("detail"), str) else None

    return RuntimeUpdateRead(
        status="running" if running else "idle",
        available=available,
        pid=None,
        started_at=parse_datetime(status_data.get("started_at") or status_data.get("requested_at")),
        active_agent_runs=await _async_active_agent_run_count(session),
        log_path=str(_request_log_path(request_file)),
        detail=detail or availability_detail,
    )


async def _async_start_request_update(
    session: AsyncSession,
    request_file: Path,
    *,
    requested_by: str | None,
    build_no_cache: bool,
    worker_drain_timeout_seconds: int | None,
) -> RuntimeUpdateRead:
    available, detail = _request_availability(request_file)
    if not available:
        raise HTTPException(status_code=409, detail=detail or "Illospace self-update is unavailable.")

    existing = await _async_request_update_status(session, request_file)
    if existing.status == "running":
        return RuntimeUpdateRead(
            **{
                **existing.model_dump(),
                "detail": "Illospace update is already running.",
            }
        )

    lock_path = _UPDATE_QUEUE.start_lock_path(request_file)
    lock_fd = acquire_start_lock(lock_path)
    if lock_fd is None:
        current = await _async_request_update_status(session, request_file)
        return RuntimeUpdateRead(
            **{
                **current.model_dump(),
                "status": "running",
                "detail": "Illospace update is starting.",
            }
        )

    try:
        started_at = datetime.now(timezone.utc)
        payload = {
            "requested_at": started_at.isoformat(),
            "requested_by": requested_by,
        }
        if build_no_cache:
            payload["build_no_cache"] = True
        if worker_drain_timeout_seconds is not None:
            payload["worker_drain_timeout_seconds"] = worker_drain_timeout_seconds
        _UPDATE_QUEUE.write_json(request_file, payload)
        _UPDATE_QUEUE.write_json(
            _request_status_file(request_file),
            {
                **payload,
                "started_at": started_at.isoformat(),
                "status": "queued",
                "detail": "Illospace update queued for the Compose updater sidecar.",
            },
        )
        active_runs = await _async_active_agent_run_count(session)
        return RuntimeUpdateRead(
            status="running",
            available=True,
            pid=None,
            started_at=started_at,
            active_agent_runs=active_runs,
            log_path=str(_request_log_path(request_file)),
            detail="Illospace update queued for the Compose updater sidecar.",
        )
    finally:
        release_start_lock(lock_fd, lock_path)


def _availability(root: Path) -> tuple[bool, str | None]:
    if os.getenv("ILLO_SELF_UPDATE_COMMAND", "").strip():
        return True, "Self-update uses the configured update command."
    launcher = root / "illo"
    if not launcher.exists():
        return False, "Illospace update is unavailable because the ./illo launcher is missing."
    if not (root / ".git").exists():
        return False, "Illospace update is unavailable from this runtime because it is not running from a git checkout."
    return True, "Uses ./illo update to choose native/systemd or Docker Compose safely."


def _update_command(root: Path) -> list[str]:
    override = os.getenv("ILLO_SELF_UPDATE_COMMAND", "").strip()
    if override:
        try:
            command = shlex.split(override)
        except ValueError as exc:
            raise HTTPException(status_code=500, detail=f"Invalid ILLO_SELF_UPDATE_COMMAND: {exc}") from exc
        if command:
            return command
    return ["bash", str(root / "illo"), "update"]


def _normalize_worker_drain_timeout(value: int | None) -> int | None:
    if value is None:
        return None
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="worker_drain_timeout_seconds must be a positive integer.") from exc
    if normalized <= 0:
        raise HTTPException(status_code=400, detail="worker_drain_timeout_seconds must be a positive integer.")
    return normalized


async def _async_active_agent_run_count(session: AsyncSession) -> int:
    try:
        count = await session.scalar(
            select(func.count())
            .select_from(AgentRunRow)
            .where(AgentRunRow.status.in_(ACTIVE_RUN_STATUS_VALUES))
        )
        return int(count or 0)
    except Exception:
        return 0


def _read_metadata(state_dir: Path) -> dict[str, Any]:
    return read_json(state_dir / _META_NAME)


def _read_pid(path: Path) -> int | None:
    try:
        return _coerce_pid(path.read_text(encoding="utf-8").strip())
    except Exception:
        return None


def _coerce_pid(value: Any) -> int | None:
    try:
        pid = int(value)
    except Exception:
        return None
    return pid if pid > 0 else None


def _pid_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _write_log_header(log_path: Path, started_at: datetime, *, requested_by: str | None) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as handle:
        handle.write(b"\n")
        handle.write(f"=== Illospace self-update requested at {started_at.isoformat()} ===\n".encode("utf-8"))
        if requested_by:
            handle.write(f"Requested by: {requested_by}\n".encode("utf-8"))
