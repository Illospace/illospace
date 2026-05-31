from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from brain.kernel import config as cfg

RUNNING_QUEUE_STATUSES = {"queued", "starting", "running"}


@dataclass(frozen=True)
class SidecarQueue:
    request_file_env: str
    status_file_env: str
    log_path_env: str
    default_log_name: str
    ready_detail: str
    queue_unavailable_label: str
    waiting_detail: str
    stale_detail: str
    heartbeat_file_env: str | None = None
    fallback_heartbeat_file_env: str | None = None
    default_status_name: str = "status.json"
    require_heartbeat: bool = False

    def request_file(self) -> Path | None:
        raw = os.getenv(self.request_file_env, "").strip()
        return Path(raw).resolve() if raw else None

    def status_file(self, request_file: Path) -> Path:
        raw = os.getenv(self.status_file_env, "").strip()
        return Path(raw).resolve() if raw else request_file.with_name(self.default_status_name)

    def log_path(self) -> Path:
        raw = os.getenv(self.log_path_env, "").strip()
        if raw:
            return Path(raw).resolve()
        return Path(cfg.BRAIN_LOG_DIR).resolve() / self.default_log_name

    def heartbeat_file(self) -> Path | None:
        raw = ""
        if self.heartbeat_file_env:
            raw = os.getenv(self.heartbeat_file_env, "").strip()
        if not raw and self.fallback_heartbeat_file_env:
            raw = os.getenv(self.fallback_heartbeat_file_env, "").strip()
        return Path(raw).resolve() if raw else None

    def availability(self, request_file: Path) -> tuple[bool, str | None]:
        try:
            request_file.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return False, f"{self.queue_unavailable_label} is unavailable: {exc}"
        if not os.access(request_file.parent, os.W_OK):
            return False, f"{self.queue_unavailable_label} is not writable: {request_file.parent}"

        heartbeat_file = self.heartbeat_file()
        if heartbeat_file is None and self.require_heartbeat:
            return False, self.waiting_detail
        if heartbeat_file is not None:
            heartbeat = read_json(heartbeat_file)
            updated_at = parse_datetime(heartbeat.get("updated_at"))
            if updated_at is None:
                return False, self.waiting_detail
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - updated_at > timedelta(seconds=60):
                return False, self.stale_detail
        return True, self.ready_detail

    def status_data(self, request_file: Path) -> dict[str, Any]:
        return read_json(self.status_file(request_file))

    def status_is_running(self, request_file: Path, status_data: dict[str, Any] | None = None) -> bool:
        status_data = status_data if status_data is not None else self.status_data(request_file)
        raw_status = str(status_data.get("status") or "").strip().lower()
        return raw_status in RUNNING_QUEUE_STATUSES or request_file.exists()

    def start_lock_path(self, request_file: Path) -> Path:
        return request_file.with_name(f".{request_file.name}.starting")

    def write_json(self, path: Path, data: dict[str, Any]) -> None:
        write_json_atomic(path, data)


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(f"{path.suffix}.tmp")
    tmp_path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp_path.replace(path)


def acquire_start_lock(path: Path) -> int | None:
    try:
        return os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return None


def release_start_lock(fd: int, path: Path) -> None:
    try:
        os.close(fd)
    except OSError:
        pass
    try:
        path.unlink()
    except OSError:
        pass
