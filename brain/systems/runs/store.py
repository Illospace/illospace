"""Single persistence boundary for agent runs."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from contextlib import asynccontextmanager, nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import logging
import os
import threading
from typing import Any
import uuid
import weakref

from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.integrations.provider_error_sentinel import (
    PROVIDER_ERROR_SENTINEL_PREFIX,
    provider_error_kind,
    safe_provider_error_sentinel,
)
from brain.systems.runs.domain import (
    AgentRun,
    AgentRunArtifact,
    AgentRunEvent,
    AgentRunRequest,
    ArtifactType,
    EventVisibility,
    RunProfile,
    RunRecipe,
)
from brain.systems.runs.events import run_event, status_changed_event
from brain.systems.runs.failures import safe_terminal_run_message
from brain.systems.runs.ids import trace_id_for_run_id
from brain.systems.runs.status import (
    RunStatus,
    RunTransitionError,
    TERMINAL_RUN_STATUSES,
    coerce_run_status,
    ensure_run_transition,
)
from brain.systems.runs.steering import SteeringMessage
from brain.platform.db.models.agent_run import (
    AgentRunArtifactRow,
    AgentRunEventRow,
    AgentRunRow,
)

logger = logging.getLogger(__name__)

_STEERING_SUBMITTED_EVENT = "run.steering_submitted"
_STEERING_CURSOR_METADATA_KEY = "steering_cursor_sequence_no"
_RUNNER_HEARTBEAT_METADATA_KEY = "runner_heartbeat"
_RUNNER_HEARTBEAT_STATUSES = frozenset({RunStatus.STARTING, RunStatus.RUNNING, RunStatus.VERIFYING})
_DEFERRED_RUN_TARGET_METADATA_KEYS = ("queued_after_run_id", "queue_after_run_id")
_DEFERRED_RUN_ACTIVE_STATUS_VALUES = frozenset({
    RunStatus.QUEUED.value,
    RunStatus.STARTING.value,
    RunStatus.RUNNING.value,
    RunStatus.PAUSED.value,
    RunStatus.VERIFYING.value,
})
_SOURCE_IDEMPOTENCY_METADATA_KEYS = ("idempotency_key", "idempotencyKey")
_CREATABLE_RUN_STATUSES = frozenset({RunStatus.QUEUED, RunStatus.STARTING})
_CHILD_CREATION_TOKEN_METADATA_KEY = "parent_step_creation_token"
_DEADLOCK_RETRY_ATTEMPTS = 3
_DEADLOCK_RETRY_BASE_SECONDS = 0.05
_UNSET = object()


def _agent_run_deadline_seconds() -> int:
    try:
        return max(1, int(os.getenv("AGENT_RUN_DEADLINE_SECONDS", "900")))
    except (TypeError, ValueError):
        return 900


def _agent_run_max_interruption_requeues() -> int:
    try:
        return max(0, int(os.getenv("AGENT_RUN_MAX_INTERRUPTION_REQUEUES", "3")))
    except (TypeError, ValueError):
        return 3


@dataclass(frozen=True, slots=True)
class ExecutionClaim:
    """Durable ownership fence for one recipe execution attempt."""

    run_id: int
    token: str
    attempt: int
    lost: asyncio.Event = field(default_factory=asyncio.Event, compare=False, repr=False)


class ExecutionClaimLost(RuntimeError):
    """Raised when an execution owner loses its durable fencing claim."""


_EVENT_LOCKS_GUARD = threading.Lock()
_EVENT_LOCKS: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop,
    weakref.WeakValueDictionary[int, asyncio.Lock],
] = weakref.WeakKeyDictionary()


def _event_lock(run_id: int) -> asyncio.Lock:
    """Return a coroutine-aware lock scoped to the active event loop and run."""

    loop = asyncio.get_running_loop()
    with _EVENT_LOCKS_GUARD:
        loop_locks = _EVENT_LOCKS.setdefault(loop, weakref.WeakValueDictionary())
        lock = loop_locks.get(int(run_id))
        if lock is None:
            lock = asyncio.Lock()
            loop_locks[int(run_id)] = lock
        return lock


def _creatable_run_status(value: RunStatus | str) -> RunStatus:
    try:
        status = value if isinstance(value, RunStatus) else RunStatus(str(value).strip().lower())
    except ValueError as exc:
        raise ValueError(f"Unsupported initial run status: {value!r}") from exc
    if status not in _CREATABLE_RUN_STATUSES:
        raise ValueError(
            f"Initial run status must be queued or starting, got {status.value!r}"
        )
    return status


def _parent_step_key_hash(step_key: str | None) -> str | None:
    normalized = str(step_key or "").strip()
    return sha256(normalized.encode()).hexdigest() if normalized else None


async def _await_uninterruptibly(awaitable):
    task = asyncio.create_task(awaitable)
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                return task.result()


def _is_postgres_deadlock(exc: BaseException) -> bool:
    if not isinstance(exc, DBAPIError):
        return False

    pending: list[BaseException] = [exc.orig]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        sqlstate = (
            getattr(current, "sqlstate", None)
            or getattr(current, "pgcode", None)
            or getattr(getattr(current, "diag", None), "sqlstate", None)
        )
        if sqlstate == "40P01":
            return True
        if isinstance(current.__cause__, BaseException):
            pending.append(current.__cause__)
    return False


def to_domain(row: AgentRunRow) -> AgentRun:
    return AgentRun(
        id=_row_value(row, "id"),
        trace_id=_row_value(row, "trace_id") or trace_id_for_run_id(_row_value(row, "id")) or "",
        org_id=_row_value(row, "org_id"),
        user_id=_row_value(row, "user_id"),
        thread_id=_row_value(row, "thread_id"),
        parent_run_id=_row_value(row, "parent_run_id"),
        root_run_id=_row_value(row, "root_run_id"),
        profile=RunProfile(_row_value(row, "profile")),
        recipe=RunRecipe(_row_value(row, "recipe")),
        status=coerce_run_status(_row_value(row, "status"), default=RunStatus.FAILED),
        input_message=_row_value(row, "input_message"),
        target_ref=dict(_row_value(row, "target_ref") or {}),
        workspace_ref=dict(_row_value(row, "workspace_ref") or {}),
        model_policy=dict(_row_value(row, "model_policy") or {}),
        context_summary=_row_value(row, "context_summary"),
        metadata=dict(_row_value(row, "metadata_") or {}),
        created_at=_row_value(row, "created_at"),
        started_at=_row_value(row, "started_at"),
        deadline_at=_row_value(row, "deadline_at"),
        closeout_expires_at=_row_value(row, "closeout_expires_at"),
        expired_at=_row_value(row, "expired_at"),
        paused_at=_row_value(row, "paused_at"),
        completed_at=_row_value(row, "completed_at"),
        failed_at=_row_value(row, "failed_at"),
        canceled_at=_row_value(row, "canceled_at"),
        updated_at=_row_value(row, "updated_at"),
    )


def _row_value(row: AgentRunRow, key: str) -> Any:
    return row.__dict__.get(key)


def _deferred_run_target_id(row: AgentRunRow) -> int | None:
    metadata = row.metadata_ if isinstance(row.metadata_, dict) else {}
    for key in _DEFERRED_RUN_TARGET_METADATA_KEYS:
        raw = metadata.get(key)
        if raw in (None, ""):
            continue
        try:
            target_id = int(str(raw))
        except (TypeError, ValueError):
            continue
        return target_id if target_id != int(row.id) else None
    return None


def _heartbeat_write_needed(
    row: AgentRunRow,
    *,
    now: datetime,
    min_interval_seconds: float,
) -> bool:
    status = coerce_run_status(row.status, default=RunStatus.FAILED)
    if status not in _RUNNER_HEARTBEAT_STATUSES:
        return False
    if min_interval_seconds <= 0:
        return True

    metadata = dict(row.metadata_ or {})
    previous = metadata.get(_RUNNER_HEARTBEAT_METADATA_KEY)
    previous = dict(previous) if isinstance(previous, dict) else {}
    previous_at = _parse_datetime(previous.get("at"))
    return (
        previous_at is None
        or (now - previous_at).total_seconds() >= min_interval_seconds
    )


def _source_idempotency_parts(request: AgentRunRequest) -> tuple[str | None, str | None]:
    metadata = dict(request.metadata or {})
    key = ""
    for metadata_key in _SOURCE_IDEMPOTENCY_METADATA_KEYS:
        key = str(metadata.get(metadata_key) or "").strip()
        if key:
            break
    if not key:
        return None, None
    # Slack can deliver one human mention through multiple event shapes, while
    # chantier and opted-in worker continuation hooks can race across terminal
    # workers. Lock those canonical event identities at the run boundary
    # without changing idempotency semantics for unrelated sources.
    if not key.startswith(
        ("slack:", "chantier:", "worker:continuation:", "knowledge:")
    ):
        return None, None

    work_intake = metadata.get("work_intake")
    source = ""
    if isinstance(work_intake, dict):
        source = str(work_intake.get("source") or "").strip()
    source = source or str(metadata.get("source") or "").strip()
    if source.startswith("trigger:"):
        source = source.split(":", 1)[1].strip()
    return (source or "unknown")[:80], key


async def _reconcile_inbound_triage_run_if_needed(session: AsyncSession, row: AgentRunRow) -> None:
    metadata = row.metadata_ if isinstance(row.metadata_, dict) else {}
    inbound_event = metadata.get("inbound_event")
    if not isinstance(inbound_event, dict) or not inbound_event.get("event_id"):
        return
    from brain.systems.inbound.reconciliation import reconcile_inbound_triage_run

    await reconcile_inbound_triage_run(session, row)


class AsyncAgentRunStore:
    """Async persistence boundary for agent runs."""

    def __init__(self, session: AsyncSession, *, auto_commit: bool = False):
        self.session = session
        self.auto_commit = bool(auto_commit)

    async def _acquire_agent_run_locks(
        self,
        run_ids: Iterable[int | None],
        *,
        key_share: bool,
        no_key_update: bool = False,
        skip_locked: bool = False,
    ) -> set[int]:
        """Acquire one lock mode across run rows in ascending global order."""
        lock_ids = sorted({int(run_id) for run_id in run_ids if run_id is not None})
        if not lock_ids or self._dialect_name() != "postgresql":
            return set()
        statement = (
            select(AgentRunRow.id)
            .where(AgentRunRow.id.in_(lock_ids))
            .order_by(AgentRunRow.id.asc())
            .with_for_update(
                read=key_share,
                key_share=key_share or no_key_update,
                skip_locked=skip_locked,
            )
        )
        rows = (await self.session.scalars(statement)).all()
        return {int(run_id) for run_id in rows}

    async def _try_locked_run(
        self,
        run_id: int,
        *,
        root_run_id: int | None = None,
        skip_locked: bool = False,
    ) -> AgentRunRow | None:
        """Lock a root/current pair in order, with the current row mutable.

        ``FOR NO KEY UPDATE`` still serializes every run mutation while staying
        compatible with child-event ``FOR KEY SHARE`` locks on the root. Run
        primary keys are immutable, so the stronger ``FOR UPDATE`` lock only
        creates unnecessary root/child lock convoys.
        """
        run_id = int(run_id)
        if self._dialect_name() != "postgresql":
            return await self.refresh_run(run_id)

        if root_run_id is None:
            snapshot = await self.refresh_run(run_id)
            root_run_id = int(snapshot.root_run_id or run_id)
        else:
            root_run_id = int(root_run_id or run_id)

        for lock_id in sorted({root_run_id, run_id}):
            locked_ids = await self._acquire_agent_run_locks(
                [lock_id],
                key_share=lock_id != run_id,
                no_key_update=lock_id == run_id,
                skip_locked=skip_locked,
            )
            if lock_id not in locked_ids:
                return None
        return await self.refresh_run(run_id)

    async def lock_terminal_boundary(
        self,
        run_id: int,
        *,
        anchor_run_id: int | None = None,
    ) -> AgentRunRow:
        """Lock a terminal run and its fan-out anchor in global order.

        Terminal hooks inspect and may mutate the fan-out anchor after changing
        the terminal child.  Both rows therefore need the same mutable lock,
        acquired together before either is changed, so concurrent root/child
        finalizers cannot acquire the pair in opposite orders.
        """

        run_id = int(run_id)
        anchor_run_id = int(anchor_run_id or run_id)
        locked_ids = await self._acquire_agent_run_locks(
            [anchor_run_id, run_id],
            key_share=False,
            no_key_update=True,
        )
        if self._dialect_name() == "postgresql" and run_id not in locked_ids:
            raise LookupError(f"Run {run_id} not found")
        return await self.refresh_run(run_id)

    async def _run_for_source_idempotency(
        self,
        *,
        org_id: str | None,
        scope: str | None,
        key: str | None,
    ) -> AgentRunRow | None:
        if not org_id or not scope or not key:
            return None
        stmt = (
            select(AgentRunRow)
            .where(
                AgentRunRow.org_id == str(org_id),
                AgentRunRow.source_idempotency_scope == str(scope),
                AgentRunRow.source_idempotency_key == str(key),
            )
            .order_by(AgentRunRow.id.asc())
            .limit(1)
        )
        return (await self.session.scalars(stmt)).first()

    async def _run_for_parent_step(
        self,
        *,
        parent_run_id: int | None,
        parent_step_key_hash: str | None,
    ) -> AgentRunRow | None:
        if parent_run_id is None or not parent_step_key_hash:
            return None
        stmt = (
            select(AgentRunRow)
            .where(
                AgentRunRow.parent_run_id == int(parent_run_id),
                AgentRunRow.parent_step_key_hash == str(parent_step_key_hash),
            )
            .order_by(AgentRunRow.id.asc())
            .limit(1)
        )
        return (await self.session.scalars(stmt)).first()

    async def create_run(
        self,
        request: AgentRunRequest,
        *,
        initial_status: RunStatus | str = RunStatus.QUEUED,
        parent_step_key_hash: str | None = None,
    ) -> AgentRun:
        profile = request.normalized_profile
        recipe = request.normalized_recipe
        status = _creatable_run_status(initial_status)
        parent_step_key_hash = str(parent_step_key_hash or "").strip() or None
        existing = await self._run_for_parent_step(
            parent_run_id=request.parent_run_id,
            parent_step_key_hash=parent_step_key_hash,
        )
        if existing is not None:
            return to_domain(existing)
        source_idempotency_scope, source_idempotency_key = (
            _source_idempotency_parts(request)
            if request.parent_run_id is None
            else (None, None)
        )
        existing = await self._run_for_source_idempotency(
            org_id=request.org_id,
            scope=source_idempotency_scope,
            key=source_idempotency_key,
        )
        if existing is not None:
            return to_domain(existing)

        if request.parent_run_id is not None:
            await self._acquire_agent_run_locks(
                [request.root_run_id or request.parent_run_id, request.parent_run_id],
                key_share=True,
            )
        admitted_at = datetime.now(timezone.utc)
        row = AgentRunRow(
            org_id=request.org_id,
            user_id=request.user_id,
            thread_id=request.thread_id,
            parent_run_id=request.parent_run_id,
            root_run_id=request.root_run_id,
            profile=profile.value,
            recipe=recipe.value,
            status=status.value,
            input_message=request.message,
            target_ref=dict(request.target_ref or {}),
            workspace_ref=dict(request.workspace_ref or {}),
            model_policy=dict(request.model_policy or {}),
            metadata_=dict(request.metadata or {}),
            source_idempotency_scope=source_idempotency_scope,
            source_idempotency_key=source_idempotency_key,
            parent_step_key_hash=parent_step_key_hash,
            started_at=admitted_at if status == RunStatus.STARTING else None,
            deadline_at=request.deadline_at
            or admitted_at + timedelta(seconds=_agent_run_deadline_seconds()),
        )
        try:
            async with self.session.begin_nested():
                self.session.add(row)
                await self.session.flush()
                row.trace_id = trace_id_for_run_id(row.id)
                if row.root_run_id is None:
                    row.root_run_id = row.id
                await self.session.flush()
        except IntegrityError:
            existing = await self._run_for_parent_step(
                parent_run_id=request.parent_run_id,
                parent_step_key_hash=parent_step_key_hash,
            )
            if existing is not None:
                return to_domain(existing)
            existing = await self._run_for_source_idempotency(
                org_id=request.org_id,
                scope=source_idempotency_scope,
                key=source_idempotency_key,
            )
            if existing is not None:
                return to_domain(existing)
            raise
        await self.append_event(
            run_event(
                row.id,
                "run.created",
                {"profile": profile.value, "recipe": recipe.value, "status": status.value},
                root_run_id=row.root_run_id,
            )
        )
        return to_domain(row)

    async def create_child_run(
        self,
        parent: AgentRun | AgentRunRow,
        *,
        initial_status: RunStatus | str = RunStatus.QUEUED,
        **kwargs: Any,
    ) -> AgentRun:
        child, _created = await self.create_child_run_with_result(
            parent,
            initial_status=initial_status,
            **kwargs,
        )
        return child

    async def create_child_run_with_result(
        self,
        parent: AgentRun | AgentRunRow,
        *,
        initial_status: RunStatus | str = RunStatus.QUEUED,
        **kwargs: Any,
    ) -> tuple[AgentRun, bool]:
        status = _creatable_run_status(initial_status)
        parent_run = parent if isinstance(parent, AgentRunRow) else await self.require_run(parent.id)
        recipe = kwargs["recipe"]
        recipe_value = recipe.value if isinstance(recipe, RunRecipe) else str(recipe)
        step_key = kwargs.get("step_key")
        thread_id = kwargs.get("thread_id") or parent_run.thread_id
        metadata_payload = dict(kwargs.get("metadata") or {})
        creation_token = uuid.uuid4().hex if step_key else None
        auto_commit = self.auto_commit
        commit_at_end = auto_commit or status == RunStatus.STARTING
        self.auto_commit = False
        try:
            # Python's sqlite3 legacy transaction mode does not begin a real
            # outer transaction for SELECT before SAVEPOINT. Force one so the
            # nested idempotent insert cannot commit ahead of its parent event.
            if self._dialect_name() == "sqlite":
                await self.session.execute(
                    update(AgentRunRow)
                    .where(AgentRunRow.id == int(parent_run.id))
                    .values(updated_at=AgentRunRow.updated_at)
                )
            else:
                parent_run = await self._locked_run(
                    int(parent_run.id),
                    root_run_id=parent_run.root_run_id or parent_run.id,
                )
            if step_key:
                metadata_payload["parent_step_key"] = step_key
                metadata_payload[_CHILD_CREATION_TOKEN_METADATA_KEY] = creation_token
                existing = await self.child_run_for_step(parent_run.id, step_key)
                if existing is not None:
                    child = to_domain(existing)
                    await self._append_child_created_once(parent_run, child, step_key)
                    if commit_at_end:
                        await self.session.commit()
                    return child, False
            child = await self.create_run(
                AgentRunRequest(
                    org_id=parent_run.org_id,
                    user_id=parent_run.user_id,
                    thread_id=str(thread_id),
                    parent_run_id=parent_run.id,
                    root_run_id=parent_run.root_run_id or parent_run.id,
                    profile=kwargs.get("profile") or parent_run.profile,
                    recipe=recipe_value,
                    message=kwargs["message"],
                    target_ref=dict(
                        kwargs["target_ref"]
                        if kwargs.get("target_ref") is not None
                        else parent_run.target_ref or {}
                    ),
                    workspace_ref=dict(
                        kwargs["workspace_ref"]
                        if kwargs.get("workspace_ref") is not None
                        else parent_run.workspace_ref or {}
                    ),
                    model_policy=dict(
                        kwargs["model_policy"]
                        if kwargs.get("model_policy") is not None
                        else parent_run.model_policy or {}
                    ),
                    metadata=metadata_payload,
                    deadline_at=parent_run.deadline_at,
                ),
                initial_status=status,
                parent_step_key_hash=_parent_step_key_hash(step_key),
            )
            created_here = not step_key or child.metadata.get(_CHILD_CREATION_TOKEN_METADATA_KEY) == creation_token
            await self._append_child_created_once(parent_run, child, step_key)
            if commit_at_end:
                await self.session.commit()
            return child, created_here
        except BaseException:
            await self.session.rollback()
            raise
        finally:
            self.auto_commit = auto_commit

    async def _append_child_created_once(
        self,
        parent_run: AgentRunRow,
        child: AgentRun,
        step_key: str | None,
    ) -> bool:
        existing_events = (
            await self.session.scalars(
                select(AgentRunEventRow).where(
                    AgentRunEventRow.run_id == int(parent_run.id),
                    AgentRunEventRow.event_type == "run.child_created",
                )
            )
        ).all()
        if any(
            int((event.payload or {}).get("child_run_id") or 0) == int(child.id)
            for event in existing_events
        ):
            return False
        await self.append_event(
            run_event(
                parent_run.id,
                "run.child_created",
                {"child_run_id": child.id, "recipe": child.recipe.value, "step_key": step_key},
                root_run_id=parent_run.root_run_id or parent_run.id,
            )
        )
        return True

    async def child_run_for_step(self, parent_run_id: int, step_key: str) -> AgentRunRow | None:
        hashed = await self._run_for_parent_step(
            parent_run_id=parent_run_id,
            parent_step_key_hash=_parent_step_key_hash(step_key),
        )
        if hashed is not None:
            return hashed
        rows = (
            await self.session.scalars(
                select(AgentRunRow)
                .where(AgentRunRow.parent_run_id == int(parent_run_id))
                .order_by(AgentRunRow.id.asc())
            )
        ).all()
        for row in rows:
            metadata = row.metadata_ if isinstance(row.metadata_, dict) else {}
            if str(metadata.get("parent_step_key") or "") == str(step_key):
                return row
        return None

    @asynccontextmanager
    async def execution_lease(self, run_id: int):
        """Claim one fenced recipe attempt without pinning a DB connection."""
        run_id = int(run_id)
        token = uuid.uuid4().hex
        await self.session.commit()
        observed_owner = False
        claim: ExecutionClaim | None = None
        try:
            while claim is None:
                row = await self.refresh_run(run_id)
                status = coerce_run_status(row.status, default=RunStatus.FAILED)
                if status in TERMINAL_RUN_STATUSES:
                    await self.session.rollback()
                    yield None
                    return
                if observed_owner and status == RunStatus.PAUSED and not row.execution_token:
                    await self.session.rollback()
                    yield None
                    return
                claim = await self._try_acquire_execution_claim(
                    row,
                    token=token,
                )
                if claim is not None:
                    break
                observed_owner = True
                if not await self._wait_for_execution_retry(run_id):
                    yield None
                    return
        except BaseException:
            await _await_uninterruptibly(self.session.rollback())
            if claim is not None:
                await _await_uninterruptibly(self._release_execution_claim(claim))
            raise

        try:
            yield claim
        except BaseException:
            await _await_uninterruptibly(self.session.rollback())
            raise
        else:
            await _await_uninterruptibly(self.session.commit())
        finally:
            await _await_uninterruptibly(self._release_execution_claim(claim))

    async def _try_acquire_execution_claim(
        self,
        row: AgentRunRow,
        *,
        token: str,
    ) -> ExecutionClaim | None:
        auto_commit = self.auto_commit
        self.auto_commit = False
        run_id = int(row.id)
        attempt = 1
        try:
            while True:
                try:
                    return await self._try_acquire_execution_claim_transaction(
                        row,
                        token=token,
                    )
                except Exception as exc:
                    await _await_uninterruptibly(self.session.rollback())
                    if (
                        not _is_postgres_deadlock(exc)
                        or attempt >= _DEADLOCK_RETRY_ATTEMPTS
                    ):
                        raise
                    delay_seconds = _DEADLOCK_RETRY_BASE_SECONDS * (2 ** (attempt - 1))
                    logger.warning(
                        "agent_run_deadlock_retry",
                        extra={
                            "run_id": run_id,
                            "operation": "acquire_execution_claim",
                            "attempt": attempt,
                            "max_attempts": _DEADLOCK_RETRY_ATTEMPTS,
                            "delay_seconds": delay_seconds,
                        },
                    )
                    await asyncio.sleep(delay_seconds)
                    row = await self.refresh_run(run_id)
                    attempt += 1
                except BaseException:
                    await _await_uninterruptibly(self.session.rollback())
                    raise
        finally:
            self.auto_commit = auto_commit

    async def _try_acquire_execution_claim_transaction(
        self,
        row: AgentRunRow,
        *,
        token: str,
    ) -> ExecutionClaim | None:
        row = await self._locked_run(
            int(row.id),
            root_run_id=row.root_run_id or row.id,
        )
        status = coerce_run_status(row.status, default=RunStatus.FAILED)
        if status in TERMINAL_RUN_STATUSES:
            return None
        now = datetime.now(timezone.utc)
        initial_attempt = int(row.execution_attempt or 0)
        attempt = initial_attempt + 1
        result = await self.session.execute(
            update(AgentRunRow)
            .where(
                AgentRunRow.id == int(row.id),
                AgentRunRow.status == status.value,
                AgentRunRow.execution_attempt == initial_attempt,
                AgentRunRow.status.in_(_DEFERRED_RUN_ACTIVE_STATUS_VALUES),
                AgentRunRow.execution_token.is_(None),
            )
            .values(
                status=RunStatus.RUNNING.value,
                execution_token=str(token),
                execution_attempt=attempt,
                started_at=row.started_at or now,
                updated_at=now,
            )
            .returning(AgentRunRow.id)
            .execution_options(synchronize_session=False)
        )
        if result.scalar_one_or_none() is None:
            await self.session.rollback()
            return None

        root_run_id = row.root_run_id or row.id
        if status == RunStatus.QUEUED:
            await self.append_event(
                status_changed_event(
                    row.id,
                    from_status=RunStatus.QUEUED.value,
                    to_status=RunStatus.STARTING.value,
                    root_run_id=root_run_id,
                    reason="claimed",
                )
            )
            await self.append_event(
                status_changed_event(
                    row.id,
                    from_status=RunStatus.STARTING.value,
                    to_status=RunStatus.RUNNING.value,
                    root_run_id=root_run_id,
                )
            )
        elif status != RunStatus.RUNNING:
            await self.append_event(
                status_changed_event(
                    row.id,
                    from_status=status.value,
                    to_status=RunStatus.RUNNING.value,
                    root_run_id=root_run_id,
                )
            )
        lifecycle_event = (
            "run.resumed"
            if status in {RunStatus.RUNNING, RunStatus.PAUSED, RunStatus.VERIFYING}
            else "run.started"
        )
        await self.append_event(
            run_event(
                row.id,
                lifecycle_event,
                {"from_status": status.value, "execution_attempt": attempt},
                root_run_id=root_run_id,
            )
        )
        await self.session.commit()
        return ExecutionClaim(run_id=int(row.id), token=str(token), attempt=attempt)

    async def refresh_run(self, run_id: int) -> AgentRunRow:
        row = (
            await self.session.scalars(
                select(AgentRunRow)
                .where(AgentRunRow.id == int(run_id))
                .execution_options(populate_existing=True)
            )
        ).one_or_none()
        if row is None:
            raise LookupError(f"Run {run_id} not found")
        return row

    async def _wait_for_execution_retry(self, run_id: int) -> bool:
        try:
            timeout_seconds = max(1.0, float(os.getenv("AGENT_RUN_LEASE_WAIT_SECONDS", "900")))
        except ValueError:
            timeout_seconds = 900.0
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        delay = 0.05
        while True:
            row = await self.refresh_run(run_id)
            status = coerce_run_status(row.status, default=RunStatus.FAILED)
            active_token = str(row.execution_token or "")
            await self.session.rollback()
            if status in TERMINAL_RUN_STATUSES or (
                status == RunStatus.PAUSED and not active_token
            ):
                return False
            if not active_token:
                return True
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError(f"Timed out waiting for AgentRun {run_id} execution owner")
            await asyncio.sleep(min(delay, remaining))
            delay = min(1.0, delay * 1.7)

    async def assert_execution_claim(self, claim: ExecutionClaim) -> AgentRunRow:
        row = await self.refresh_run(claim.run_id)
        if (
            claim.lost.is_set()
            or str(row.execution_token or "") != claim.token
            or int(row.execution_attempt or 0) != claim.attempt
            or coerce_run_status(row.status, default=RunStatus.FAILED) in TERMINAL_RUN_STATUSES
        ):
            raise ExecutionClaimLost(
                f"AgentRun {claim.run_id} execution claim {claim.attempt} is no longer current"
            )
        return row

    async def _release_execution_claim(self, claim: ExecutionClaim) -> None:
        await self.session.execute(
            update(AgentRunRow)
            .where(
                AgentRunRow.id == claim.run_id,
                AgentRunRow.execution_token == claim.token,
                AgentRunRow.execution_attempt == claim.attempt,
            )
            .values(execution_token=None)
        )
        await self.session.commit()

    async def claim_next_run_ids(self, *, limit: int = 1) -> list[int]:
        ids: list[int] = []
        for _ in range(max(0, int(limit))):
            claimed = await self.claim_next()
            if claimed is None:
                break
            ids.append(int(claimed.id))
        return ids

    async def claim_next(self) -> AgentRun | None:
        batch_size = 25
        seen_ids: list[int] = []
        while True:
            stmt = (
                select(AgentRunRow)
                .where(AgentRunRow.status == RunStatus.QUEUED.value)
                .order_by(AgentRunRow.created_at.asc(), AgentRunRow.id.asc())
                .limit(batch_size)
            )
            if seen_ids:
                stmt = stmt.where(~AgentRunRow.id.in_(seen_ids))
            rows = (await self.session.scalars(stmt)).all()
            if not rows:
                return None
            for row in rows:
                locked_row = await self._try_locked_run(
                    int(row.id),
                    root_run_id=row.root_run_id or row.id,
                    skip_locked=self._dialect_name() == "postgresql",
                )
                if locked_row is None:
                    seen_ids.append(int(row.id))
                    continue
                if locked_row.status != RunStatus.QUEUED.value:
                    seen_ids.append(int(row.id))
                    continue
                if await self._deferred_run_dependency_active(locked_row):
                    seen_ids.append(int(row.id))
                    continue
                claimed, _changed = await self._set_status_on_locked_run(
                    locked_row,
                    RunStatus.STARTING,
                    reason="claimed",
                )
                return claimed

    async def claim_run(self, run_id: int) -> AgentRun | None:
        row = await self._locked_run(run_id)
        if row.status != RunStatus.QUEUED.value:
            return None
        claimed, _changed = await self._set_status_on_locked_run(
            row,
            RunStatus.STARTING,
            reason="claimed",
        )
        return claimed

    async def get_run(self, run_id: int) -> AgentRunRow | None:
        return await self.session.get(AgentRunRow, int(run_id))

    async def require_run(self, run_id: int) -> AgentRunRow:
        row = await self.get_run(run_id)
        if row is None:
            raise LookupError(f"Run {run_id} not found")
        return row

    async def metadata_for_run(self, run_id: int) -> dict[str, Any]:
        return dict((await self.require_run(run_id)).metadata_ or {})

    async def update_metadata(self, run_id: int, patch: dict[str, Any]) -> dict[str, Any]:
        row = await self._locked_run(run_id)
        metadata = dict(row.metadata_ or {})
        metadata.update(dict(patch or {}))
        row.metadata_ = metadata
        await self.session.flush()
        return metadata

    async def heartbeat_run(
        self,
        run_id: int,
        *,
        token: str | None = None,
        reason: str | None = None,
        min_interval_seconds: float = 0.0,
        now: datetime | None = None,
    ) -> bool:
        now = now or datetime.now(timezone.utc)
        snapshot = await self.refresh_run(run_id)
        if not _heartbeat_write_needed(
            snapshot,
            now=now,
            min_interval_seconds=min_interval_seconds,
        ):
            return False

        row = await self._locked_run(
            run_id,
            root_run_id=snapshot.root_run_id or snapshot.id,
        )
        if not _heartbeat_write_needed(
            row,
            now=now,
            min_interval_seconds=min_interval_seconds,
        ):
            return False

        metadata = dict(row.metadata_ or {})
        previous = metadata.get(_RUNNER_HEARTBEAT_METADATA_KEY)
        previous = dict(previous) if isinstance(previous, dict) else {}
        metadata[_RUNNER_HEARTBEAT_METADATA_KEY] = {
            "at": now.isoformat(),
            "token": token or previous.get("token"),
            "reason": reason or previous.get("reason") or "running",
        }
        row.metadata_ = metadata
        await self.session.flush()
        return True

    async def set_status(
        self,
        run_id: int,
        status: RunStatus | str,
        *,
        reason: str | None = None,
        execution_claim: ExecutionClaim | None = None,
    ) -> AgentRun:
        run, _changed = await self.set_status_with_result(
            run_id,
            status,
            reason=reason,
            execution_claim=execution_claim,
        )
        return run

    async def interrupt_and_requeue(
        self,
        run_id: int,
        *,
        reason: str = "worker_shutdown",
        interrupted_at: datetime | None = None,
        details: dict[str, Any] | None = None,
    ) -> tuple[AgentRun, bool]:
        """Fence an abandoned execution attempt and requeue it within a fixed cap.

        ``interrupted`` is intentionally an audited event/metadata state rather
        than a durable row status: claimers already consume ``queued`` rows, so
        recovery remains atomic and requires no intermediate-state sweeper. A
        run that exhausts the cap is settled as ``expired`` with partial state
        preserved instead of looping forever.
        """

        snapshot = await self.require_run(run_id)
        snapshot_metadata = snapshot.metadata_ if isinstance(snapshot.metadata_, dict) else {}
        terminal_interruption = _coerce_int(
            snapshot_metadata.get("interruption_count"),
            default=0,
        ) >= _agent_run_max_interruption_requeues()
        if terminal_interruption:
            anchor_run_id = int(snapshot.parent_run_id or snapshot.id)
            await self.commit_event_boundary(run_id)
            row = await self.lock_terminal_boundary(
                run_id,
                anchor_run_id=anchor_run_id,
            )
        else:
            row = await self._locked_run(run_id)
        current = coerce_run_status(row.status, default=RunStatus.FAILED)
        metadata = dict(row.metadata_ or {})
        previous_count = _coerce_int(metadata.get("interruption_count"), default=0)
        requeue_allowed = previous_count < _agent_run_max_interruption_requeues()
        if not requeue_allowed and not terminal_interruption:
            anchor_run_id = int(row.parent_run_id or row.id)
            await self.commit_event_boundary(run_id)
            row = await self.lock_terminal_boundary(
                run_id,
                anchor_run_id=anchor_run_id,
            )
            current = coerce_run_status(row.status, default=RunStatus.FAILED)
            metadata = dict(row.metadata_ or {})
            previous_count = _coerce_int(metadata.get("interruption_count"), default=0)
            requeue_allowed = previous_count < _agent_run_max_interruption_requeues()
        target_status = RunStatus.QUEUED if requeue_allowed else RunStatus.EXPIRED
        try:
            current, target = ensure_run_transition(
                current,
                target_status,
                allow_interrupted_requeue=requeue_allowed,
            )
        except RunTransitionError:
            return to_domain(row), False
        if current == target:
            return to_domain(row), False

        interrupted_at = interrupted_at or datetime.now(timezone.utc)
        if interrupted_at.tzinfo is None:
            interrupted_at = interrupted_at.replace(tzinfo=timezone.utc)
        interruption = {
            **dict(details or {}),
            "reason": str(reason or "worker_shutdown"),
            "interrupted_at": interrupted_at.isoformat(),
            "from_status": current.value,
            "execution_attempt": int(row.execution_attempt or 0),
            "requeued": requeue_allowed,
        }
        metadata["interruption"] = interruption
        metadata["interruption_count"] = previous_count + 1
        metadata.pop("failure", None)

        auto_commit = self.auto_commit
        self.auto_commit = False
        try:
            transitioned_run, changed = await self._set_status_on_locked_run(
                row,
                target,
                reason=interruption["reason"],
                transitioned_at=interrupted_at,
                metadata_update=metadata,
                allow_interrupted_requeue=requeue_allowed,
                expected_execution_token=row.execution_token,
                expected_execution_attempt=int(row.execution_attempt or 0),
                before_status_events=(
                    run_event(
                        int(row.id),
                        "run.interrupted",
                        interruption,
                        root_run_id=row.root_run_id,
                        producer="worker_shutdown",
                    ),
                ),
                after_status_events=(
                    run_event(
                        int(row.id),
                        "run.requeued" if requeue_allowed else "run.interruption_limit_exhausted",
                        interruption,
                        root_run_id=row.root_run_id,
                        producer="worker_shutdown",
                    ),
                ),
            )
            if changed and not requeue_allowed:
                final_output = str(safe_terminal_run_message(RunStatus.EXPIRED) or "")
                if final_output:
                    artifact = await self.append_final_answer_once(
                        int(row.id),
                        final_output,
                        root_run_id=row.root_run_id,
                    )
                    safe_output = str(getattr(artifact, "text", None) or final_output)
                    if not await self.has_event_type(int(row.id), "run.text_delta"):
                        await self.append_event(
                            run_event(
                                int(row.id),
                                "run.text_completed",
                                {"text": safe_output},
                                root_run_id=row.root_run_id,
                                producer="worker_shutdown",
                            )
                        )
                await self.append_event(
                    run_event(
                        int(row.id),
                        "run.expired",
                        {
                            "reason": "interruption_limit_exhausted",
                            "interruption_count": previous_count + 1,
                        },
                        root_run_id=row.root_run_id,
                        producer="worker_shutdown",
                    )
                )
                from brain.systems.runs.chantier_continuation import (
                    queue_chantier_continuation_for_terminal_run,
                )

                await queue_chantier_continuation_for_terminal_run(
                    self.session,
                    terminal_run_id=int(row.id),
                )
            if auto_commit:
                await self.session.commit()
            return transitioned_run, changed
        except BaseException:
            if auto_commit:
                await self.session.rollback()
            raise
        finally:
            self.auto_commit = auto_commit

    async def set_status_with_result(
        self,
        run_id: int,
        status: RunStatus | str,
        *,
        reason: str | None = None,
        execution_claim: ExecutionClaim | None = None,
        expected_updated_at: datetime | None | object = _UNSET,
        expected_execution_token: str | None | object = _UNSET,
        expected_execution_attempt: int | object = _UNSET,
        rollback_on_conflict: bool = True,
        transitioned_at: datetime | None = None,
        metadata_update: dict[str, Any] | None = None,
        allow_interrupted_requeue: bool = False,
        before_status_events: tuple[AgentRunEvent, ...] = (),
        after_status_events: tuple[AgentRunEvent, ...] = (),
    ) -> tuple[AgentRun, bool]:
        if execution_claim is not None and execution_claim.lost.is_set():
            raise ExecutionClaimLost(
                f"AgentRun {run_id} execution claim {execution_claim.attempt} is no longer current"
            )
        row = await self._locked_run(run_id)
        return await self._set_status_on_locked_run(
            row,
            status,
            reason=reason,
            execution_claim=execution_claim,
            expected_updated_at=expected_updated_at,
            expected_execution_token=expected_execution_token,
            expected_execution_attempt=expected_execution_attempt,
            rollback_on_conflict=rollback_on_conflict,
            transitioned_at=transitioned_at,
            metadata_update=metadata_update,
            allow_interrupted_requeue=allow_interrupted_requeue,
            before_status_events=before_status_events,
            after_status_events=after_status_events,
        )

    async def _set_status_on_locked_run(
        self,
        row: AgentRunRow,
        status: RunStatus | str,
        *,
        reason: str | None = None,
        execution_claim: ExecutionClaim | None = None,
        expected_updated_at: datetime | None | object = _UNSET,
        expected_execution_token: str | None | object = _UNSET,
        expected_execution_attempt: int | object = _UNSET,
        rollback_on_conflict: bool = True,
        transitioned_at: datetime | None = None,
        metadata_update: dict[str, Any] | None = None,
        allow_interrupted_requeue: bool = False,
        before_status_events: tuple[AgentRunEvent, ...] = (),
        after_status_events: tuple[AgentRunEvent, ...] = (),
    ) -> tuple[AgentRun, bool]:
        run_id = int(row.id)
        if execution_claim is not None and execution_claim.lost.is_set():
            raise ExecutionClaimLost(
                f"AgentRun {run_id} execution claim {execution_claim.attempt} is no longer current"
            )
        persisted_status = coerce_run_status(row.status, default=RunStatus.FAILED)
        if execution_claim is not None and (
            str(row.execution_token or "") != execution_claim.token
            or int(row.execution_attempt or 0) != execution_claim.attempt
            or persisted_status in TERMINAL_RUN_STATUSES
        ):
            execution_claim.lost.set()
            raise ExecutionClaimLost(
                f"AgentRun {run_id} execution claim {execution_claim.attempt} is no longer current"
            )
        if persisted_status in TERMINAL_RUN_STATUSES:
            return to_domain(row), False
        current, target = ensure_run_transition(
            row.status,
            status,
            allow_interrupted_requeue=allow_interrupted_requeue,
        )
        if current == target:
            if execution_claim is not None:
                await self.assert_execution_claim(execution_claim)
            return to_domain(row), False
        now = transitioned_at or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        values: dict[str, Any] = {
            "status": target.value,
            "updated_at": now,
        }
        if target == RunStatus.STARTING:
            values["started_at"] = row.started_at or now
        elif target == RunStatus.PAUSED:
            values["paused_at"] = now
        elif target == RunStatus.COMPLETED:
            values["completed_at"] = now
        elif target == RunStatus.FAILED:
            values["failed_at"] = now
        elif target == RunStatus.CANCELED:
            values["canceled_at"] = now
        elif target == RunStatus.EXPIRED:
            values["expired_at"] = now
        if target == RunStatus.QUEUED:
            values["paused_at"] = None
        if target in TERMINAL_RUN_STATUSES or target in {RunStatus.PAUSED, RunStatus.QUEUED}:
            values["execution_token"] = None
        if metadata_update is not None:
            values["metadata_"] = dict(metadata_update)

        predicates = [
            AgentRunRow.id == int(run_id),
            AgentRunRow.status == current.value,
        ]
        if execution_claim is not None:
            predicates.extend([
                AgentRunRow.execution_token == execution_claim.token,
                AgentRunRow.execution_attempt == execution_claim.attempt,
            ])
        if expected_updated_at is not _UNSET:
            predicates.append(AgentRunRow.updated_at == expected_updated_at)
        if expected_execution_token is not _UNSET:
            predicates.append(AgentRunRow.execution_token == expected_execution_token)
        if expected_execution_attempt is not _UNSET:
            predicates.append(AgentRunRow.execution_attempt == int(expected_execution_attempt))
        result = await self.session.execute(
            update(AgentRunRow)
            .where(*predicates)
            .values(**values)
            .returning(AgentRunRow.id)
            .execution_options(synchronize_session=False)
        )
        if result.scalar_one_or_none() is None:
            if rollback_on_conflict:
                await self.session.rollback()
            if execution_claim is not None:
                execution_claim.lost.set()
                raise ExecutionClaimLost(
                    f"AgentRun {run_id} execution claim {execution_claim.attempt} lost a status race"
                )
            return to_domain(await self.refresh_run(run_id)), False

        for event in before_status_events:
            await self.append_event(event)
        await self.append_event(
            status_changed_event(
                int(run_id),
                from_status=current.value,
                to_status=target.value,
                root_run_id=row.root_run_id,
                reason=reason,
            )
        )
        for event in after_status_events:
            await self.append_event(event)
        row = await self.refresh_run(run_id)
        if target in TERMINAL_RUN_STATUSES:
            await _reconcile_inbound_triage_run_if_needed(self.session, row)
        if self.auto_commit:
            await self.session.commit()
        return to_domain(row), True

    async def append_steering(
        self,
        run_id: int,
        content: str,
        *,
        user_id: str | None = None,
        thread_message_id: int | None = None,
    ) -> AgentRunEventRow:
        run = await self.require_run(run_id)
        message = SteeringMessage(run_id=run.id, content=content, user_id=user_id).normalized()
        if not message.content:
            raise ValueError("Steering content is required")
        payload: dict[str, Any] = {"content": message.content, "user_id": message.user_id}
        if thread_message_id is not None:
            payload["thread_message_id"] = int(thread_message_id)
        return await self.append_event(
            run_event(
                run.id,
                _STEERING_SUBMITTED_EVENT,
                payload,
                root_run_id=run.root_run_id,
                producer="user",
            )
        )

    async def drain_steering(self, run_id: int) -> list[SteeringMessage]:
        run = await self.require_run(run_id)
        metadata = dict(run.metadata_ or {})
        cursor = _coerce_int(metadata.get(_STEERING_CURSOR_METADATA_KEY), default=0)
        rows = (
            await self.session.scalars(
                select(AgentRunEventRow)
                .where(
                    AgentRunEventRow.run_id == int(run_id),
                    AgentRunEventRow.event_type == _STEERING_SUBMITTED_EVENT,
                    AgentRunEventRow.sequence_no > cursor,
                )
                .order_by(AgentRunEventRow.sequence_no.asc(), AgentRunEventRow.id.asc())
            )
        ).all()
        if not rows:
            if self.auto_commit:
                await self.session.rollback()
            return []

        run = await self._locked_run(run_id)
        metadata = dict(run.metadata_ or {})
        cursor = _coerce_int(metadata.get(_STEERING_CURSOR_METADATA_KEY), default=0)
        rows = [row for row in rows if int(row.sequence_no) > cursor]
        if not rows:
            if self.auto_commit:
                await self.session.rollback()
            return []

        metadata[_STEERING_CURSOR_METADATA_KEY] = int(rows[-1].sequence_no)
        run.metadata_ = metadata
        await self.session.flush()

        messages: list[SteeringMessage] = []
        for row in rows:
            payload = row.payload or {}
            message = SteeringMessage(
                run_id=int(row.run_id),
                content=str(payload.get("content") or ""),
                user_id=str(payload.get("user_id")) if payload.get("user_id") else None,
                created_at=row.created_at,
            ).normalized()
            if message.content:
                messages.append(message)
        if self.auto_commit:
            await self.session.commit()
        return messages

    async def append_event(self, event: AgentRunEvent) -> AgentRunEventRow:
        async with _event_lock(int(event.run_id)):
            return await self._append_event_locked(event)

    async def commit_event_boundary(self, run_id: int) -> None:
        """Publish prior run work before crossing into a new lock boundary.

        Tool handlers such as ``spawn_worker`` deliberately persist through a
        separate session.  Keeping the runner transaction open after
        ``run.tool_started`` leaves both the AgentRun row lock and its advisory
        event-stream lock held while that handler tries to lock the same run,
        producing a cross-session lock cycle. Terminal writes likewise release
        prior child locks here before acquiring their root/child pair in global
        order. Serialize the commit with event appends so parallel tool calls
        cannot operate on the shared ``AsyncSession`` while its transaction is
        closing.
        """

        async with _event_lock(int(run_id)):
            await self.session.commit()

    async def _append_event_locked(self, event: AgentRunEvent) -> AgentRunEventRow:
        await self._acquire_agent_run_locks(
            [event.root_run_id or event.run_id, event.run_id],
            key_share=True,
        )
        await self.lock_event_stream(
            int(event.run_id),
        )
        sequence_no = event.sequence_no
        if sequence_no is None:
            with getattr(self.session, "no_autoflush", nullcontext()):
                sequence_no = int(
                    await self.session.scalar(
                        select(func.coalesce(func.max(AgentRunEventRow.sequence_no), 0)).where(
                            AgentRunEventRow.run_id == event.run_id
                        )
                    )
                    or 0
                ) + 1
        row = AgentRunEventRow(
            run_id=event.run_id,
            root_run_id=event.root_run_id or event.run_id,
            sequence_no=sequence_no,
            event_type=event.event_type,
            payload=dict(event.payload or {}),
            producer=event.producer,
            visibility=(
                event.visibility.value if isinstance(event.visibility, EventVisibility) else str(event.visibility)
            ),
        )
        self.session.add(row)
        await self.session.flush()
        if self.auto_commit:
            await self.session.commit()
        return row

    async def lock_event_stream(self, run_id: int) -> None:
        """Serialize event writers for one run."""
        if self._dialect_name() != "postgresql":
            return
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(:run_id)"),
            {"run_id": int(run_id)},
        )

    async def try_lock_event_stream(self, run_id: int) -> bool:
        """Try to fence one event stream without waiting into a lock cycle."""
        if self._dialect_name() != "postgresql":
            return True
        return bool(
            await self.session.scalar(
                text("SELECT pg_try_advisory_xact_lock(:run_id)"),
                {"run_id": int(run_id)},
            )
        )

    async def lock_run_liveness(
        self,
        run_id: int,
        root_run_id: int | None = None,
    ) -> tuple[AgentRunRow, AgentRunRow | None] | None:
        """Fence heartbeat and event writes while stale evidence is revalidated."""
        run_id = int(run_id)
        root_run_id = int(root_run_id or run_id)
        lock_ids = sorted({run_id, root_run_id})
        if self._dialect_name() == "sqlite":
            await self.session.execute(
                update(AgentRunRow)
                .where(AgentRunRow.id.in_(lock_ids))
                .values(updated_at=AgentRunRow.updated_at)
            )
        else:
            await self._acquire_agent_run_locks(
                lock_ids,
                key_share=False,
            )
        if not await self.try_lock_event_stream(run_id):
            return None
        row = await self.refresh_run(run_id)
        root_row = row if root_run_id == run_id else await self.get_run(root_run_id)
        return row, root_row

    async def safe_cycle_provider_error_text(self, run_id: int, text_value: str) -> str:
        if text_value.startswith(PROVIDER_ERROR_SENTINEL_PREFIX):
            return text_value
        detected_provider_error = provider_error_kind(text_value)
        if not detected_provider_error:
            return text_value
        run = await self.session.get(AgentRunRow, int(run_id))
        metadata = dict(getattr(run, "metadata_", None) or {}) if run is not None else {}
        if metadata.get("source") != "cycle" and not metadata.get("cycle_run_id"):
            return text_value
        logger.error(
            "cycle_provider_error_text_blocked run_id=%s raw_error=%s",
            run_id,
            text_value,
        )
        return safe_provider_error_sentinel(detected_provider_error)

    async def append_artifact(self, artifact: AgentRunArtifact) -> AgentRunArtifactRow:
        async with _event_lock(int(artifact.run_id)):
            artifact_type = (
                artifact.artifact_type.value
                if isinstance(artifact.artifact_type, ArtifactType)
                else str(artifact.artifact_type)
            )
            artifact_text = artifact.text
            if artifact_type == ArtifactType.FINAL_ANSWER.value and artifact_text is not None:
                artifact_text = await self.safe_cycle_provider_error_text(
                    int(artifact.run_id),
                    str(artifact_text),
                )
            await self._acquire_agent_run_locks(
                [artifact.root_run_id or artifact.run_id, artifact.run_id],
                key_share=True,
            )
            row = AgentRunArtifactRow(
                run_id=artifact.run_id,
                root_run_id=artifact.root_run_id or artifact.run_id,
                artifact_type=artifact_type,
                title=artifact.title,
                payload=dict(artifact.payload or {}),
                text=artifact_text,
                uri=artifact.uri,
                visibility=(
                    artifact.visibility.value
                    if isinstance(artifact.visibility, EventVisibility)
                    else str(artifact.visibility)
                ),
            )
            self.session.add(row)
            await self.session.flush()
            await self._append_event_locked(
                run_event(
                    artifact.run_id,
                    "run.artifact_created",
                    {"artifact_id": row.id, "artifact_type": row.artifact_type, "title": row.title},
                    root_run_id=row.root_run_id,
                )
            )
            return row

    async def append_final_answer_once(
        self,
        run_id: int,
        text_value: str,
        *,
        root_run_id: int | None = None,
    ) -> AgentRunArtifactRow | None:
        text_value = str(text_value or "")
        if not text_value:
            return None
        text_value = await self.safe_cycle_provider_error_text(int(run_id), text_value)
        existing = (
            await self.session.scalars(
                select(AgentRunArtifactRow)
                .where(
                    AgentRunArtifactRow.run_id == int(run_id),
                    AgentRunArtifactRow.artifact_type == ArtifactType.FINAL_ANSWER.value,
                    AgentRunArtifactRow.text == text_value,
                )
                .order_by(AgentRunArtifactRow.id.asc())
                .limit(1)
            )
        ).first()
        if existing is not None:
            return existing
        from brain.systems.runs.artifacts import final_answer_artifact

        return await self.append_artifact(final_answer_artifact(run_id, text_value, root_run_id=root_run_id))

    async def latest_artifact_text(self, run_id: int, artifact_type: ArtifactType | str = ArtifactType.FINAL_ANSWER) -> str:
        artifact_type_value = artifact_type.value if isinstance(artifact_type, ArtifactType) else str(artifact_type)
        row = (
            await self.session.scalars(
                select(AgentRunArtifactRow)
                .where(
                    AgentRunArtifactRow.run_id == int(run_id),
                    AgentRunArtifactRow.artifact_type == artifact_type_value,
                )
                .order_by(AgentRunArtifactRow.created_at.desc(), AgentRunArtifactRow.id.desc())
                .limit(1)
            )
        ).first()
        return str(getattr(row, "text", None) or "") if row is not None else ""

    async def list_artifacts(self, run_id: int) -> list[AgentRunArtifactRow]:
        result = await self.session.scalars(
            select(AgentRunArtifactRow)
            .where(AgentRunArtifactRow.run_id == int(run_id))
            .order_by(AgentRunArtifactRow.created_at.asc(), AgentRunArtifactRow.id.asc())
        )
        return list(result)

    async def has_event_type(self, run_id: int, event_type: str) -> bool:
        return (
            await self.session.scalar(
                select(AgentRunEventRow.id)
                .where(
                    AgentRunEventRow.run_id == int(run_id),
                    AgentRunEventRow.event_type == str(event_type),
                )
                .limit(1)
            )
        ) is not None

    @staticmethod
    def to_domain(row: AgentRunRow) -> AgentRun:
        return to_domain(row)

    async def _deferred_run_dependency_active(self, row: AgentRunRow) -> bool:
        target_id = _deferred_run_target_id(row)
        if target_id is None:
            return False

        target_status = await self.session.scalar(
            select(AgentRunRow.status).where(AgentRunRow.id == int(target_id)).limit(1)
        )
        if str(target_status or "").lower() in _DEFERRED_RUN_ACTIVE_STATUS_VALUES:
            return True

        if not row.thread_id:
            return False
        older_active = await self.session.scalar(
            select(AgentRunRow.id)
            .where(
                AgentRunRow.thread_id == row.thread_id,
                AgentRunRow.parent_run_id.is_(None),
                AgentRunRow.id < int(row.id),
                AgentRunRow.status.in_(sorted(_DEFERRED_RUN_ACTIVE_STATUS_VALUES)),
            )
            .order_by(AgentRunRow.created_at.asc(), AgentRunRow.id.asc())
            .limit(1)
        )
        return older_active is not None

    async def _locked_run(
        self,
        run_id: int,
        *,
        root_run_id: int | None = None,
    ) -> AgentRunRow:
        row = await self._try_locked_run(
            int(run_id),
            root_run_id=root_run_id,
        )
        if row is None:
            raise LookupError(f"Run {run_id} not found")
        return row

    def _dialect_name(self) -> str:
        bind = self.session.get_bind()
        return str(getattr(getattr(bind, "dialect", None), "name", "") or "")


__all__ = [
    "AsyncAgentRunStore",
    "ExecutionClaim",
    "ExecutionClaimLost",
    "to_domain",
]


def _coerce_int(value: Any, *, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value.strip():
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
