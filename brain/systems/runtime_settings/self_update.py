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

from brain.kernel import config as cfg
from brain.platform.db.models.agent_run import AgentRunRow
from brain.platform.db.repositories.unit_of_work import UnitOfWork

from .schemas import RuntimeUpdateRead

_ACTIVE_AGENT_RUN_STATUSES = ("starting", "running", "verifying")
_LOG_NAME = "illo-self-update.log"
_META_NAME = "illo-self-update.json"
_PID_NAME = "illo-self-update.pid"
_START_LOCK_NAME = "illo-self-update.starting"


def get_runtime_update_status() -> RuntimeUpdateRead:
    root = _repo_root()
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
        active_agent_runs=_active_agent_run_count(),
        log_path=str(state_dir / _LOG_NAME),
        detail=detail,
    )


def start_runtime_update(*, requested_by: str | None = None) -> RuntimeUpdateRead:
    root = _repo_root()
    available, detail = _availability(root)
    if not available:
        raise HTTPException(status_code=409, detail=detail or "Illospace self-update is unavailable.")

    state_dir = _state_dir(root)
    state_dir.mkdir(parents=True, exist_ok=True)
    existing = get_runtime_update_status()
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
        return RuntimeUpdateRead(
            **{
                **get_runtime_update_status().model_dump(),
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

        active_runs = _active_agent_run_count()
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


def _active_agent_run_count() -> int:
    try:
        with UnitOfWork() as uow:
            count = uow.session.scalar(
                select(func.count())
                .select_from(AgentRunRow)
                .where(AgentRunRow.status.in_(_ACTIVE_AGENT_RUN_STATUSES))
            )
        return int(count or 0)
    except Exception:
        return 0


def _read_metadata(state_dir: Path) -> dict[str, Any]:
    try:
        data = json.loads((state_dir / _META_NAME).read_text(encoding="utf-8"))
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
