from __future__ import annotations

import json
import os
import shlex
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.kernel import config as cfg
from brain.platform.db.models.agent_run import AgentRunRow

from .schemas import RuntimeUpdateRead

_ACTIVE_AGENT_RUN_STATUSES = ("starting", "running", "verifying")
_LOG_NAME = "illo-self-update.log"
_META_NAME = "illo-self-update.json"
_PID_NAME = "illo-self-update.pid"
_START_LOCK_NAME = "illo-self-update.starting"
_REQUEST_RUNNING_STATUSES = {"queued", "starting", "running"}


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
    started_at = _parse_datetime(metadata.get("started_at"))

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
) -> RuntimeUpdateRead:
    root = _repo_root()
    request_file = _request_file()
    if request_file is not None:
        return await _async_start_request_update(session, request_file, requested_by=requested_by)

    available, detail = _availability(root)
    if not available:
        raise HTTPException(status_code=409, detail=detail or "Illospace self-update is unavailable.")

    state_dir = _state_dir(root)
    state_dir.mkdir(parents=True, exist_ok=True)
    existing = await async_get_runtime_update_status(session)
    if existing.status == "running":
        return RuntimeUpdateRead(
            **{
                **existing.model_dump(),
                "detail": "Illospace update is already running.",
            }
        )

    lock_path = state_dir / _START_LOCK_NAME
    lock_fd = _acquire_start_lock(lock_path)
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

        with log_path.open("ab") as handle:
            process = subprocess.Popen(
                command,
                cwd=str(root),
                stdin=subprocess.DEVNULL,
                stdout=handle,
                stderr=subprocess.STDOUT,
                close_fds=True,
                start_new_session=True,
                env=env,
            )

        metadata = {
            "pid": process.pid,
            "started_at": started_at.isoformat(),
            "requested_by": requested_by,
            "root": str(root),
        }
        (state_dir / _META_NAME).write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")
        (state_dir / _PID_NAME).write_text(f"{process.pid}\n", encoding="utf-8")

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
        _release_start_lock(lock_fd, lock_path)


def _repo_root() -> Path:
    return Path(os.getenv("ILLO_SELF_UPDATE_ROOT") or cfg.BRAIN_DIR).resolve()


def _state_dir(root: Path) -> Path:
    raw = os.getenv("ILLO_SELF_UPDATE_STATE_DIR")
    if raw:
        path = Path(raw)
        return path if path.is_absolute() else root / path
    return Path(cfg.BRAIN_LOG_DIR).resolve()


def _request_file() -> Path | None:
    raw = os.getenv("ILLO_SELF_UPDATE_REQUEST_FILE", "").strip()
    return Path(raw).resolve() if raw else None


def _request_status_file(request_file: Path) -> Path:
    raw = os.getenv("ILLO_SELF_UPDATE_STATUS_FILE", "").strip()
    return Path(raw).resolve() if raw else request_file.with_name("status.json")


def _request_log_path(request_file: Path) -> Path:
    raw = os.getenv("ILLO_SELF_UPDATE_LOG_PATH", "").strip()
    if raw:
        return Path(raw).resolve()
    return Path(cfg.BRAIN_LOG_DIR).resolve() / _LOG_NAME


def _request_heartbeat_file(request_file: Path) -> Path | None:
    raw = os.getenv("ILLO_SELF_UPDATE_HEARTBEAT_FILE", "").strip()
    return Path(raw).resolve() if raw else None


def _request_availability(request_file: Path) -> tuple[bool, str | None]:
    try:
        request_file.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return False, f"Illospace update queue is unavailable: {exc}"
    if not os.access(request_file.parent, os.W_OK):
        return False, f"Illospace update queue is not writable: {request_file.parent}"
    heartbeat_file = _request_heartbeat_file(request_file)
    if heartbeat_file is not None:
        heartbeat = _read_json(heartbeat_file)
        updated_at = _parse_datetime(heartbeat.get("updated_at"))
        if updated_at is None:
            return False, "Illospace update is waiting for the Compose updater sidecar."
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - updated_at > timedelta(seconds=60):
            return False, "Illospace update is unavailable because the Compose updater sidecar heartbeat is stale."
    return True, "Queues the update for the Compose updater sidecar."


async def _async_request_update_status(session: AsyncSession, request_file: Path) -> RuntimeUpdateRead:
    available, availability_detail = _request_availability(request_file)
    status_data = _read_json(_request_status_file(request_file))
    raw_status = str(status_data.get("status") or "").strip().lower()
    running = raw_status in _REQUEST_RUNNING_STATUSES or request_file.exists()
    detail = status_data.get("detail") if isinstance(status_data.get("detail"), str) else None

    return RuntimeUpdateRead(
        status="running" if running else "idle",
        available=available,
        pid=None,
        started_at=_parse_datetime(status_data.get("started_at") or status_data.get("requested_at")),
        active_agent_runs=await _async_active_agent_run_count(session),
        log_path=str(_request_log_path(request_file)),
        detail=detail or availability_detail,
    )


async def _async_start_request_update(
    session: AsyncSession,
    request_file: Path,
    *,
    requested_by: str | None,
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

    lock_path = request_file.with_name(f".{request_file.name}.starting")
    lock_fd = _acquire_start_lock(lock_path)
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
        _write_json_atomic(request_file, payload)
        _write_json_atomic(
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
        _release_start_lock(lock_fd, lock_path)


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


async def _async_active_agent_run_count(session: AsyncSession) -> int:
    try:
        count = await session.scalar(
            select(func.count())
            .select_from(AgentRunRow)
            .where(AgentRunRow.status.in_(_ACTIVE_AGENT_RUN_STATUSES))
        )
        return int(count or 0)
    except Exception:
        return 0


def _read_metadata(state_dir: Path) -> dict[str, Any]:
    return _read_json(state_dir / _META_NAME)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


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


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _write_log_header(log_path: Path, started_at: datetime, *, requested_by: str | None) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as handle:
        handle.write(b"\n")
        handle.write(f"=== Illospace self-update requested at {started_at.isoformat()} ===\n".encode("utf-8"))
        if requested_by:
            handle.write(f"Requested by: {requested_by}\n".encode("utf-8"))


def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(f"{path.suffix}.tmp")
    tmp_path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp_path.replace(path)


def _acquire_start_lock(path: Path) -> int | None:
    try:
        return os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return None


def _release_start_lock(fd: int, path: Path) -> None:
    try:
        os.close(fd)
    except OSError:
        pass
    try:
        path.unlink()
    except OSError:
        pass
