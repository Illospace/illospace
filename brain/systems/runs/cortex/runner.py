"""Small AgentRun runner loop for Cortex."""

from __future__ import annotations

import logging
import os
import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import time
from typing import Any
import uuid

from sqlalchemy import func, select

from brain.kernel import config as brain_config
from brain.systems.runs.cancel import RunCancelToken
from brain.systems.runs.engine import AgentRunEngine
from brain.systems.runs.events import activity_event, run_event
from brain.systems.runs.recipes import default_recipes
from brain.systems.runs.status import RunStatus, TERMINAL_RUN_STATUSES, coerce_run_status
from brain.systems.runs.store import AgentRunStore
from brain.systems.runs.stream import RunStream
from brain.systems.runs.ui_events import run_event_to_ui_message
from brain.systems.cortex.events import publish_live_safe, publish_safe
from brain.systems.cortex.project_context.materializer import materialize_project_context_workspaces
from brain.platform.db.models.agent_run import AgentRunEventRow, AgentRunRow
from brain.platform.db.models.idea import Idea, IdeaStateLog
from brain.platform.db.repositories.unit_of_work import UnitOfWork, run_sync_with_unit_of_work

logger = logging.getLogger(__name__)

_stop_event = threading.Event()
_runner_lock = threading.Lock()
_runner_supervisor_thread: threading.Thread | None = None
_runner_slots: list[tuple[threading.Thread, threading.Event]] = []
_runner_thread_index = 0
_poll_interval_sec = 0.5
_runner_reconcile_interval_sec = 2.0
_stale_reconcile_interval_sec = 30.0
_default_runner_concurrency = 4
_max_runner_concurrency = 32
_default_heartbeat_interval_sec = 10.0
_default_stale_run_sec = 300.0
_last_stale_reconcile_monotonic = 0.0

_PROCESS_ACTIVE_STATUS_VALUES = tuple(
    status.value for status in (RunStatus.STARTING, RunStatus.RUNNING, RunStatus.VERIFYING)
)


def _run_db(fn, /, *args: Any, **kwargs: Any):
    """Run sync ORM worker code through the asyncpg-backed UnitOfWork bridge."""

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(run_sync_with_unit_of_work(fn, *args, **kwargs))
    raise RuntimeError("Cortex runner DB bridge cannot be called from a running event loop")


def _coerce_concurrency(value: Any, *, default: int | None = None) -> int | None:
    try:
        if value is None or value == "":
            return default
        return max(1, min(_max_runner_concurrency, int(value)))
    except (TypeError, ValueError):
        return default


def _coerce_float(value: Any, *, default: float, minimum: float) -> float:
    try:
        if value is None or value == "":
            return default
        return max(float(minimum), float(value))
    except (TypeError, ValueError):
        return default


def _runner_heartbeat_interval_seconds() -> float:
    return _coerce_float(
        os.getenv("ILLO_AGENT_RUN_HEARTBEAT_INTERVAL_SECONDS"),
        default=_default_heartbeat_interval_sec,
        minimum=1.0,
    )


def _stale_run_seconds() -> float:
    interval = _runner_heartbeat_interval_seconds()
    return max(
        interval * 3,
        _coerce_float(
            os.getenv("ILLO_AGENT_RUN_STALE_SECONDS"),
            default=_default_stale_run_sec,
            minimum=interval * 2,
        ),
    )


def _runner_concurrency() -> int:
    return _coerce_concurrency(
        os.getenv("ILLO_AGENT_RUNNER_CONCURRENCY"),
        default=_default_runner_concurrency,
    ) or _default_runner_concurrency


def _active_runner_threads() -> list[threading.Thread]:
    return [thread for thread, stop_event in _runner_slots if thread.is_alive() and not stop_event.is_set()]


def _int_value(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _project_live_event(session, event_type: str, payload: dict[str, Any]) -> None:
    run_id = _int_value(payload.get("run_id"))
    if run_id is None:
        return
    run = session.get(AgentRunRow, run_id)
    if run is None:
        return
    event_id = _int_value(payload.get("run_event_id") or payload.get("event_id") or payload.get("event_cursor")) or 0
    sequence_no = _int_value(payload.get("sequence_no")) or 0
    event = SimpleNamespace(
        id=event_id,
        event_type=event_type,
        payload=dict(payload or {}),
        run_id=run_id,
        root_run_id=_int_value(payload.get("root_run_id")) or run.root_run_id or run_id,
        sequence_no=sequence_no,
        created_at=None,
    )
    message = run_event_to_ui_message(event, run=run, org_id=str(run.org_id) if run.org_id else None)
    if not message:
        return
    message_type = message.pop("type")
    publish_live_safe(message_type, message)


def _live_stream_sink(session):
    def _sink(event_type: str, payload: dict[str, Any]) -> None:
        try:
            _project_live_event(session, event_type, payload)
        except Exception:
            logger.debug("agent_run_live_event_failed", exc_info=True)

    return _sink


def _drain_steering_in_isolated_uow(run_id: int):
    def _drain():
        with UnitOfWork() as uow:
            return AgentRunStore(uow.session).drain_steering(int(run_id))

    return _run_db(_drain)


def _engine_for_session(session) -> AgentRunEngine:
    return AgentRunEngine(
        session,
        recipes=default_recipes(),
        stream=RunStream(_live_stream_sink(session)),
        auto_commit_events=True,
        cancel_event_factory=lambda run_id: RunCancelToken(run_id),
        durable_steering_drain=_drain_steering_in_isolated_uow,
    )


_TERMINAL_RUN_IDEA_STATUS = {
    "completed": "unread_reply",
    "failed": "failed",
    "canceled": "failed",
}
_PROTECTED_IDEA_STATUSES = {"archived", "resolved"}


def _settle_idea_for_terminal_root_run(session, run_id: int) -> dict[str, Any] | None:
    run = session.get(AgentRunRow, int(run_id))
    if run is None or run.parent_run_id is not None:
        return None
    target_status = _TERMINAL_RUN_IDEA_STATUS.get(str(run.status or ""))
    if not target_status or not run.thread_id:
        return None
    idea = session.get(Idea, str(run.thread_id))
    if idea is None:
        return None
    old_status = str(idea.status or "")
    if old_status in _PROTECTED_IDEA_STATUSES or old_status == target_status:
        return None

    idea.status = target_status
    idea.updated_at = datetime.now(timezone.utc)
    session.add(IdeaStateLog(
        idea_id=str(idea.id),
        from_state=old_status,
        to_state=target_status,
        trigger=f"agent_run_{run.status}",
    ))
    session.flush()
    return {
        "idea_id": str(idea.id),
        "old_status": old_status,
        "new_status": target_status,
        "run_id": int(run.id),
    }


def _run_has_project_context(run: AgentRunRow | None) -> bool:
    if run is None:
        return False
    target_ref = run.target_ref if isinstance(run.target_ref, dict) else {}
    workspace_ref = run.workspace_ref if isinstance(run.workspace_ref, dict) else {}
    return bool(
        isinstance(target_ref.get("project_context_snapshot"), dict)
        or isinstance(workspace_ref.get("project_context_snapshot"), dict)
        or isinstance(workspace_ref.get("resources"), list)
    )


def _project_context_root(run_id: int, *, thread_id: str | None = None) -> str:
    base = brain_config.resolve_workspace_root(default=Path(tempfile.gettempdir()) / "illo-agent-runs").expanduser()
    if thread_id:
        safe_thread_id = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in str(thread_id))[:120]
        if safe_thread_id:
            return str(base / "ideas" / safe_thread_id)
    return str(base / f"run-{int(run_id)}")


def _record_project_activity(session, run_id: int, label: str, **payload: Any) -> None:
    run = session.get(AgentRunRow, int(run_id))
    if run is None:
        return
    event = activity_event(
        int(run_id),
        label,
        root_run_id=run.root_run_id or int(run_id),
        **payload,
    )
    row = AgentRunStore(session, auto_commit=True).append_event(event)
    stream_payload = dict(event.payload or {})
    stream_payload.update({
        "run_id": int(run_id),
        "root_run_id": int(run.root_run_id or run_id),
        "event_id": int(row.id),
        "run_event_id": int(row.id),
        "event_cursor": int(row.id),
        "sequence_no": int(row.sequence_no),
    })
    _project_live_event(session, event.event_type, stream_payload)


def _heartbeat_run_once(run_id: int, *, token: str, reason: str) -> bool:
    try:
        def _heartbeat():
            with UnitOfWork() as uow:
                return AgentRunStore(uow.session).heartbeat_run(
                    int(run_id),
                    token=token,
                    reason=reason,
                    min_interval_seconds=0,
                )

        return _run_db(_heartbeat)
    except Exception:
        logger.debug("agent_run_heartbeat_failed", extra={"run_id": run_id}, exc_info=True)
        return False


@contextmanager
def _run_heartbeat(run_id: int):
    token = f"{os.getpid()}:{uuid.uuid4().hex}"
    stop_event = threading.Event()
    _heartbeat_run_once(int(run_id), token=token, reason="runner_started")

    def _loop_heartbeat() -> None:
        interval = _runner_heartbeat_interval_seconds()
        while not stop_event.wait(interval):
            _heartbeat_run_once(int(run_id), token=token, reason="runner_running")

    thread = threading.Thread(
        target=_loop_heartbeat,
        name=f"agent-run-heartbeat-{int(run_id)}",
        daemon=True,
    )
    thread.start()
    try:
        yield token
    finally:
        stop_event.set()
        thread.join(timeout=min(2.0, _runner_heartbeat_interval_seconds()))


def _event_stream_payload(event, row, run: AgentRunRow) -> dict[str, Any]:
    payload = dict(event.payload or {})
    event_id = int(getattr(row, "id", 0) or 0)
    payload.update({
        "run_id": int(run.id),
        "root_run_id": int(run.root_run_id or run.id),
        "event_id": event_id,
        "run_event_id": event_id,
        "event_cursor": event_id,
        "sequence_no": int(getattr(row, "sequence_no", 0) or 0),
        "thread_id": run.thread_id,
        "idea_id": run.thread_id,
        "profile": run.profile,
        "execution_profile": run.profile,
    })
    return payload


def _normalize_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _latest_event_times_for_rows(session, rows: list[AgentRunRow]) -> tuple[dict[int, datetime], dict[int, datetime]]:
    run_ids = {int(row.id) for row in rows}
    root_ids = {int(row.root_run_id or row.id) for row in rows}
    latest_by_run: dict[int, datetime] = {}
    latest_by_root: dict[int, datetime] = {}
    if run_ids:
        for run_id, created_at in session.execute(
            select(AgentRunEventRow.run_id, func.max(AgentRunEventRow.created_at))
            .where(AgentRunEventRow.run_id.in_(run_ids))
            .group_by(AgentRunEventRow.run_id)
        ):
            parsed = _normalize_datetime(created_at)
            if parsed is not None:
                latest_by_run[int(run_id)] = parsed
    if root_ids:
        for root_id, created_at in session.execute(
            select(AgentRunEventRow.root_run_id, func.max(AgentRunEventRow.created_at))
            .where(AgentRunEventRow.root_run_id.in_(root_ids))
            .group_by(AgentRunEventRow.root_run_id)
        ):
            parsed = _normalize_datetime(created_at)
            if parsed is not None:
                latest_by_root[int(root_id)] = parsed
    return latest_by_run, latest_by_root


def _active_root_run_ids_for_children(session, rows: list[AgentRunRow]) -> set[int]:
    root_ids = {
        int(row.root_run_id)
        for row in rows
        if row.parent_run_id is not None and row.root_run_id is not None
    }
    if not root_ids:
        return set()
    return set(
        int(run_id)
        for run_id in session.scalars(
            select(AgentRunRow.id).where(
                AgentRunRow.id.in_(root_ids),
                AgentRunRow.status.in_(_PROCESS_ACTIVE_STATUS_VALUES),
            )
        )
    )


def _run_liveness_at(
    row: AgentRunRow,
    *,
    latest_event_by_run: dict[int, datetime],
    latest_event_by_root: dict[int, datetime],
) -> datetime | None:
    metadata = row.metadata_ if isinstance(row.metadata_, dict) else {}
    heartbeat = metadata.get("runner_heartbeat")
    heartbeat = dict(heartbeat) if isinstance(heartbeat, dict) else {}
    candidates = [
        _normalize_datetime(row.updated_at),
        _normalize_datetime(row.started_at),
        _normalize_datetime(row.created_at),
        _normalize_datetime(heartbeat.get("at")),
        latest_event_by_run.get(int(row.id)),
        latest_event_by_root.get(int(row.root_run_id or row.id)),
    ]
    live = [candidate for candidate in candidates if candidate is not None]
    return max(live) if live else None


def reap_stale_active_runs(
    *,
    now: datetime | None = None,
    stale_after_seconds: float | None = None,
    limit: int = 25,
) -> int:
    now = now or datetime.now(timezone.utc)
    stale_after_seconds = stale_after_seconds if stale_after_seconds is not None else _stale_run_seconds()
    cutoff = now - timedelta(seconds=float(stale_after_seconds))
    status_payloads: list[dict[str, Any]] = []
    reaped = 0

    def _reap():
        local_status_payloads: list[dict[str, Any]] = []
        local_reaped = 0
        with UnitOfWork() as uow:
            rows = list(
                uow.session.scalars(
                    select(AgentRunRow)
                    .where(
                        AgentRunRow.status.in_(_PROCESS_ACTIVE_STATUS_VALUES),
                        func.coalesce(AgentRunRow.updated_at, AgentRunRow.started_at, AgentRunRow.created_at) <= cutoff,
                    )
                    .order_by(func.coalesce(AgentRunRow.updated_at, AgentRunRow.started_at, AgentRunRow.created_at).asc())
                    .limit(max(1, int(limit)))
                ).all()
            )
            store = AgentRunStore(uow.session)
            latest_event_by_run, latest_event_by_root = _latest_event_times_for_rows(uow.session, rows)
            active_root_run_ids = _active_root_run_ids_for_children(uow.session, rows)
            for row in rows:
                if str(row.status or "") not in _PROCESS_ACTIVE_STATUS_VALUES:
                    continue
                root_run_id = int(row.root_run_id or row.id)
                if row.parent_run_id is not None and root_run_id in active_root_run_ids:
                    continue
                last_liveness_at = _run_liveness_at(
                    row,
                    latest_event_by_run=latest_event_by_run,
                    latest_event_by_root=latest_event_by_root,
                )
                if last_liveness_at is not None and last_liveness_at > cutoff:
                    continue
                metadata = row.metadata_ if isinstance(row.metadata_, dict) else {}
                heartbeat = metadata.get("runner_heartbeat")
                heartbeat = dict(heartbeat) if isinstance(heartbeat, dict) else {}
                payload = {
                    "error": "runner heartbeat stale",
                    "reason": "runner_heartbeat_stale",
                    "stale_after_seconds": int(stale_after_seconds),
                    "last_heartbeat_at": heartbeat.get("at"),
                    "last_heartbeat_reason": heartbeat.get("reason"),
                    "last_liveness_at": last_liveness_at.isoformat() if last_liveness_at else None,
                    "last_run_update_at": row.updated_at.isoformat() if row.updated_at else None,
                }
                event = run_event(int(row.id), "run.failed", payload, root_run_id=row.root_run_id)
                event_row = store.append_event(event)
                store.set_status(int(row.id), RunStatus.FAILED, reason="runner_heartbeat_stale")
                _project_live_event(uow.session, event.event_type, _event_stream_payload(event, event_row, row))
                status_payload = _settle_idea_for_terminal_root_run(uow.session, int(row.id))
                if status_payload:
                    local_status_payloads.append(status_payload)
                local_reaped += 1
        return local_status_payloads, local_reaped

    status_payloads, reaped = _run_db(_reap)

    for payload in status_payloads:
        publish_safe("status_change", payload)
    if reaped:
        logger.warning("agent_run_stale_reaped", extra={"count": reaped, "stale_after_seconds": stale_after_seconds})
    return reaped


def _reap_stale_runs_if_due(*, force: bool = False) -> int:
    global _last_stale_reconcile_monotonic
    now = time.monotonic()
    if not force and now - _last_stale_reconcile_monotonic < _stale_reconcile_interval_sec:
        return 0
    _last_stale_reconcile_monotonic = now
    try:
        return reap_stale_active_runs()
    except Exception:
        logger.exception("agent_run_stale_reconcile_failed")
        return 0


def _materialize_project_context(run_id: int) -> tuple[bool, dict[str, Any] | None]:
    with UnitOfWork() as uow:
        run = uow.session.get(AgentRunRow, int(run_id))
        if not _run_has_project_context(run):
            return True, None
        _record_project_activity(
            uow.session,
            int(run_id),
            "Preparing project context",
        )
        user_id = str(run.user_id) if run and run.user_id else None
        org_id = str(run.org_id) if run and run.org_id else None
        thread_id = str(run.thread_id) if run and run.thread_id else None

    result = materialize_project_context_workspaces(
        int(run_id),
        workspace_root=_project_context_root(int(run_id), thread_id=thread_id),
        user_id=user_id,
        org_id=org_id,
    )
    with UnitOfWork() as uow:
        _record_project_activity(
            uow.session,
            int(run_id),
            "Project context ready" if result.ok else "Project context unavailable",
            workspaces=result.workspaces,
            errors=result.errors[:3],
        )
    if not result.ok:
        details = "; ".join(result.errors[:3]) or "No project workspace was materialized."
        message = f"Project Context unavailable: {details}"
        return False, _mark_run_failed_after_runner_error(
            int(run_id),
            message,
            final_answer=(
                "I could not start this run because the selected Project Context did not "
                f"provide a usable workspace. {details}"
            ),
        )
    return True, None


def _mark_run_failed_after_runner_error(
    run_id: int,
    error: str,
    *,
    final_answer: str | None = None,
) -> dict[str, Any] | None:
    with UnitOfWork() as uow:
        store = AgentRunStore(uow.session)
        row = store.require_run(int(run_id))
        if coerce_run_status(row.status, default=RunStatus.FAILED) not in TERMINAL_RUN_STATUSES:
            if final_answer:
                store.append_final_answer_once(int(run_id), final_answer, root_run_id=row.root_run_id)
                store.append_event(
                    run_event(
                        int(run_id),
                        "run.text_completed",
                        {"text": final_answer},
                        root_run_id=row.root_run_id,
                    )
                )
            store.append_event(run_event(int(run_id), "run.failed", {"error": error}, root_run_id=row.root_run_id))
            store.set_status(int(run_id), RunStatus.FAILED, reason=error[:500])
        return _settle_idea_for_terminal_root_run(uow.session, int(run_id))


def run_queued_once(*, limit: int = 1) -> int:
    def _claim():
        with UnitOfWork() as uow:
            return AgentRunStore(uow.session).claim_next_run_ids(limit=limit)

    ids = _run_db(_claim)
    processed = 0
    for run_id in ids:
        try:
            with _run_heartbeat(int(run_id)):
                context_ready, status_payload = _materialize_project_context(int(run_id))
                if not context_ready:
                    if status_payload:
                        publish_safe("status_change", status_payload)
                    processed += 1
                    continue
                status_payload = None
                with UnitOfWork() as uow:
                    _engine_for_session(uow.session).run_existing(int(run_id))
                    status_payload = _settle_idea_for_terminal_root_run(uow.session, int(run_id))
            if status_payload:
                publish_safe("status_change", status_payload)
            processed += 1
        except Exception:
            logger.exception("agent_run_failed", extra={"run_id": run_id})
            try:
                status_payload = _mark_run_failed_after_runner_error(int(run_id), "runner_failed")
                if status_payload:
                    publish_safe("status_change", status_payload)
            except Exception:
                logger.exception("agent_run_failed_settlement_failed", extra={"run_id": run_id})
    return processed


def _loop(slot_stop_event: threading.Event | None = None) -> None:
    while not _stop_event.is_set() and not (slot_stop_event and slot_stop_event.is_set()):
        processed = run_queued_once()
        if not processed:
            if slot_stop_event:
                if slot_stop_event.wait(_poll_interval_sec):
                    break
            else:
                _stop_event.wait(_poll_interval_sec)


def _start_runner_slot_locked() -> None:
    global _runner_thread_index
    _runner_thread_index += 1
    slot_stop_event = threading.Event()
    thread = threading.Thread(
        target=_loop,
        args=(slot_stop_event,),
        name=f"agent-runner-{_runner_thread_index}",
        daemon=True,
    )
    _runner_slots.append((thread, slot_stop_event))
    thread.start()


def reconcile_runner_pool(*, allow_start: bool = False) -> int:
    desired = _runner_concurrency()
    if not allow_start and not (
        _runner_supervisor_thread and _runner_supervisor_thread.is_alive()
    ):
        return desired
    with _runner_lock:
        active_slots = [
            (thread, stop_event)
            for thread, stop_event in _runner_slots
            if thread.is_alive() and not stop_event.is_set()
        ]
        retiring_slots = [
            (thread, stop_event)
            for thread, stop_event in _runner_slots
            if thread.is_alive() and stop_event.is_set()
        ]
        _runner_slots[:] = active_slots + retiring_slots
        if len(active_slots) > desired:
            for _thread, stop_event in active_slots[desired:]:
                stop_event.set()
            active_slots = active_slots[:desired]
        while len(active_slots) < desired:
            _start_runner_slot_locked()
            active_slots = [
                (thread, stop_event)
                for thread, stop_event in _runner_slots
                if thread.is_alive() and not stop_event.is_set()
            ]
    return desired


def _supervisor_loop() -> None:
    while not _stop_event.is_set():
        try:
            _reap_stale_runs_if_due()
            reconcile_runner_pool(allow_start=True)
        except Exception:
            logger.exception("agent_run_runner_reconcile_failed")
        _stop_event.wait(_runner_reconcile_interval_sec)


def start_runner() -> None:
    global _runner_supervisor_thread
    if _runner_supervisor_thread and _runner_supervisor_thread.is_alive():
        return
    _stop_event.clear()
    _reap_stale_runs_if_due(force=True)
    concurrency = reconcile_runner_pool(allow_start=True)
    _runner_supervisor_thread = threading.Thread(
        target=_supervisor_loop,
        name="agent-runner-supervisor",
        daemon=True,
    )
    _runner_supervisor_thread.start()
    logger.info("agent_run_runner_started", extra={"concurrency": concurrency})


def stop_runner(*, drain_timeout_seconds: float | None = 2.0) -> None:
    global _runner_supervisor_thread
    _stop_event.set()
    with _runner_lock:
        for _thread, stop_event in _runner_slots:
            stop_event.set()

    deadline = None
    if drain_timeout_seconds is not None:
        deadline = time.monotonic() + max(0.0, float(drain_timeout_seconds))

    for thread in [thread for thread, _stop in list(_runner_slots)]:
        remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
        if thread.is_alive():
            thread.join(timeout=remaining)
            if deadline is not None and time.monotonic() >= deadline:
                break
    if _runner_supervisor_thread and _runner_supervisor_thread.is_alive():
        remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
        _runner_supervisor_thread.join(timeout=remaining)
    with _runner_lock:
        _runner_slots[:] = [
            (thread, stop_event)
            for thread, stop_event in _runner_slots
            if thread.is_alive()
        ]
    if not (_runner_supervisor_thread and _runner_supervisor_thread.is_alive()):
        _runner_supervisor_thread = None


def queue_status(*, consumer_running: bool | None = None, org_id: str | None = None) -> dict[str, Any]:
    def _counts():
        with UnitOfWork() as uow:
            stmt = select(AgentRunRow.status, func.count()).group_by(AgentRunRow.status)
            if org_id:
                stmt = stmt.where(AgentRunRow.org_id == org_id)
            return {str(status): int(count) for status, count in uow.session.execute(stmt).all()}

    counts = _run_db(_counts)
    active_threads = _active_runner_threads()
    return {
        "runner_running": bool(active_threads),
        "runner_concurrency": len(active_threads),
        "runner_configured_concurrency": _runner_concurrency(),
        "event_consumer_running": consumer_running,
        "counts": counts,
        "queued": counts.get("queued", 0),
        "running": sum(counts.get(status, 0) for status in ("starting", "running", "verifying")),
    }


__all__ = ["queue_status", "reap_stale_active_runs", "run_queued_once", "start_runner", "stop_runner"]
