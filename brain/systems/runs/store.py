"""Single persistence boundary for agent runs."""

from __future__ import annotations

from datetime import datetime, timezone
import threading
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

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
from brain.systems.runs.ids import trace_id_for_run_id
from brain.systems.runs.status import RunStatus, coerce_run_status, ensure_run_transition
from brain.systems.runs.steering import SteeringMessage
from brain.platform.db.models.agent_run import (
    AgentRunArtifactRow,
    AgentRunEventRow,
    AgentRunRow,
)

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


_EVENT_LOCKS_GUARD = threading.Lock()
_EVENT_LOCKS: dict[int, threading.RLock] = {}


def _event_lock(run_id: int) -> threading.RLock:
    with _EVENT_LOCKS_GUARD:
        lock = _EVENT_LOCKS.get(int(run_id))
        if lock is None:
            lock = threading.RLock()
            _EVENT_LOCKS[int(run_id)] = lock
        return lock


class AgentRunStore:
    def __init__(self, session: Session, *, auto_commit: bool = False):
        self.session = session
        self.auto_commit = bool(auto_commit)

    def create_run(self, request: AgentRunRequest) -> AgentRun:
        profile = request.normalized_profile
        recipe = request.normalized_recipe
        row = AgentRunRow(
            org_id=request.org_id,
            user_id=request.user_id,
            thread_id=request.thread_id,
            parent_run_id=request.parent_run_id,
            root_run_id=request.root_run_id,
            profile=profile.value,
            recipe=recipe.value,
            status=RunStatus.QUEUED.value,
            input_message=request.message,
            target_ref=dict(request.target_ref or {}),
            workspace_ref=dict(request.workspace_ref or {}),
            model_policy=dict(request.model_policy or {}),
            metadata_=dict(request.metadata or {}),
        )
        self.session.add(row)
        self.session.flush()
        row.trace_id = trace_id_for_run_id(row.id)
        if row.root_run_id is None:
            row.root_run_id = row.id
        self.session.flush()
        self.append_event(
            run_event(
                row.id,
                "run.created",
                {"profile": profile.value, "recipe": recipe.value},
                root_run_id=row.root_run_id,
            )
        )
        return self.to_domain(row)

    def create_child_run(
        self,
        parent: AgentRun | AgentRunRow,
        *,
        recipe: RunRecipe | str,
        message: str,
        profile: RunProfile | str | None = None,
        step_key: str | None = None,
        target_ref: dict[str, Any] | None = None,
        workspace_ref: dict[str, Any] | None = None,
        model_policy: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AgentRun:
        parent_run = parent if isinstance(parent, AgentRunRow) else self.require_run(parent.id)
        recipe_value = recipe.value if isinstance(recipe, RunRecipe) else str(recipe)
        metadata_payload = dict(metadata or {})
        if step_key:
            metadata_payload["parent_step_key"] = step_key
            existing = self.child_run_for_step(parent_run.id, step_key)
            if existing is not None:
                return self.to_domain(existing)
        child = self.create_run(
            AgentRunRequest(
                org_id=parent_run.org_id,
                user_id=parent_run.user_id,
                thread_id=parent_run.thread_id,
                parent_run_id=parent_run.id,
                root_run_id=parent_run.root_run_id or parent_run.id,
                profile=profile or parent_run.profile,
                recipe=recipe_value,
                message=message,
                target_ref=dict(target_ref if target_ref is not None else parent_run.target_ref or {}),
                workspace_ref=dict(workspace_ref if workspace_ref is not None else parent_run.workspace_ref or {}),
                model_policy=dict(model_policy if model_policy is not None else parent_run.model_policy or {}),
                metadata=metadata_payload,
            )
        )
        self.append_event(
            run_event(
                parent_run.id,
                "run.child_created",
                {"child_run_id": child.id, "recipe": child.recipe.value, "step_key": step_key},
                root_run_id=parent_run.root_run_id or parent_run.id,
            )
        )
        return child

    def child_run_for_step(self, parent_run_id: int, step_key: str) -> AgentRunRow | None:
        rows = self.session.scalars(
            select(AgentRunRow)
            .where(AgentRunRow.parent_run_id == int(parent_run_id))
            .order_by(AgentRunRow.id.asc())
        ).all()
        for row in rows:
            metadata = row.metadata_ if isinstance(row.metadata_, dict) else {}
            if str(metadata.get("parent_step_key") or "") == str(step_key):
                return row
        return None

    def child_runs(self, parent_run_id: int) -> list[AgentRunRow]:
        return list(
            self.session.scalars(
                select(AgentRunRow)
                .where(AgentRunRow.parent_run_id == int(parent_run_id))
                .order_by(AgentRunRow.id.asc())
            )
        )

    def get_child_run(self, parent_run_id: int, child_run_id: int) -> AgentRunRow | None:
        row = self.get_run(child_run_id)
        if row is None or row.parent_run_id != int(parent_run_id):
            return None
        return row

    def get_run(self, run_id: int) -> AgentRunRow | None:
        return self.session.get(AgentRunRow, run_id)

    def require_run(self, run_id: int) -> AgentRunRow:
        row = self.get_run(run_id)
        if row is None:
            raise LookupError(f"Run {run_id} not found")
        return row

    def claim_next_run_ids(self, *, limit: int = 1) -> list[int]:
        ids: list[int] = []
        for _ in range(max(0, int(limit))):
            claimed = self.claim_next()
            if claimed is None:
                break
            ids.append(int(claimed.id))
        return ids

    def claim_next(self) -> AgentRun | None:
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
            if self._dialect_name() == "postgresql":
                stmt = stmt.with_for_update(skip_locked=True)
            rows = self.session.scalars(stmt).all()
            if not rows:
                return None
            for row in rows:
                if self._deferred_run_dependency_active(row):
                    seen_ids.append(int(row.id))
                    continue
                return self.set_status(row.id, RunStatus.STARTING, reason="claimed")

    def _deferred_run_target_id(self, row: AgentRunRow) -> int | None:
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

    def _deferred_run_dependency_active(self, row: AgentRunRow) -> bool:
        target_id = self._deferred_run_target_id(row)
        if target_id is None:
            return False

        target_status = self.session.scalar(
            select(AgentRunRow.status).where(AgentRunRow.id == int(target_id)).limit(1)
        )
        if str(target_status or "").lower() in _DEFERRED_RUN_ACTIVE_STATUS_VALUES:
            return True

        if not row.thread_id:
            return False
        older_active = self.session.scalar(
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

    def claim_run(self, run_id: int) -> AgentRun | None:
        row = self._locked_run(run_id)
        if row.status != RunStatus.QUEUED.value:
            return None
        return self.set_status(row.id, RunStatus.STARTING, reason="claimed")

    def metadata_for_run(self, run_id: int) -> dict[str, Any]:
        return dict(self.require_run(run_id).metadata_ or {})

    def update_metadata(self, run_id: int, patch: dict[str, Any]) -> dict[str, Any]:
        row = self.require_run(run_id)
        metadata = dict(row.metadata_ or {})
        metadata.update(dict(patch or {}))
        row.metadata_ = metadata
        self.session.flush()
        return metadata

    def heartbeat_run(
        self,
        run_id: int,
        *,
        token: str | None = None,
        reason: str | None = None,
        min_interval_seconds: float = 0.0,
        now: datetime | None = None,
    ) -> bool:
        """Refresh the runner-owned liveness marker without creating UI noise."""
        row = self.require_run(run_id)
        status = coerce_run_status(row.status, default=RunStatus.FAILED)
        if status not in _RUNNER_HEARTBEAT_STATUSES:
            return False

        now = now or datetime.now(timezone.utc)
        metadata = dict(row.metadata_ or {})
        previous = metadata.get(_RUNNER_HEARTBEAT_METADATA_KEY)
        previous = dict(previous) if isinstance(previous, dict) else {}
        if min_interval_seconds > 0:
            previous_at = _parse_datetime(previous.get("at"))
            if previous_at is not None and (now - previous_at).total_seconds() < min_interval_seconds:
                return False

        metadata[_RUNNER_HEARTBEAT_METADATA_KEY] = {
            "at": now.isoformat(),
            "token": token or previous.get("token"),
            "reason": reason or previous.get("reason") or "running",
        }
        row.metadata_ = metadata
        self.session.flush()
        return True

    def cursor_for_run(self, run_id: int) -> dict[str, Any]:
        metadata = self.metadata_for_run(run_id)
        cursor = metadata.get("cursor")
        return dict(cursor) if isinstance(cursor, dict) else {"completed_steps": {}}

    def set_cursor(self, run_id: int, cursor: dict[str, Any]) -> dict[str, Any]:
        metadata = self.metadata_for_run(run_id)
        metadata["cursor"] = dict(cursor or {})
        self.require_run(run_id).metadata_ = metadata
        self.session.flush()
        return metadata["cursor"]

    def start_step(self, run_id: int, step_key: str) -> None:
        cursor = self.cursor_for_run(run_id)
        cursor["current_step"] = step_key
        self.set_cursor(run_id, cursor)
        run = self.require_run(run_id)
        self.append_event(
            run_event(run_id, "run.step_started", {"step": step_key, "step_key": step_key}, root_run_id=run.root_run_id)
        )

    def complete_step(self, run_id: int, step_key: str, result: Any = None) -> Any:
        cursor = self.cursor_for_run(run_id)
        completed = dict(cursor.get("completed_steps") or {})
        completed[step_key] = {"result": _jsonable(result), "completed_at": datetime.now(timezone.utc).isoformat()}
        cursor["completed_steps"] = completed
        if cursor.get("current_step") == step_key:
            cursor.pop("current_step", None)
        self.set_cursor(run_id, cursor)
        run = self.require_run(run_id)
        self.append_event(
            run_event(
                run_id,
                "run.step_completed",
                {"step": step_key, "step_key": step_key, "result": _jsonable(result)},
                root_run_id=run.root_run_id,
            )
        )
        return result

    def fail_step(self, run_id: int, step_key: str, error: str) -> None:
        self.update_metadata(run_id, {"last_failed_step": step_key})
        run = self.require_run(run_id)
        self.append_event(
            run_event(
                run_id,
                "run.step_failed",
                {"step": step_key, "step_key": step_key, "error": error},
                root_run_id=run.root_run_id,
            )
        )

    def skip_step(self, run_id: int, step_key: str) -> None:
        run = self.require_run(run_id)
        self.append_event(
            run_event(run_id, "run.step_skipped", {"step": step_key, "step_key": step_key}, root_run_id=run.root_run_id)
        )

    def step_result(self, run_id: int, step_key: str) -> Any | None:
        completed = self.cursor_for_run(run_id).get("completed_steps") or {}
        if step_key not in completed:
            return None
        entry = completed.get(step_key)
        if isinstance(entry, dict):
            return entry.get("result")
        return entry

    def step_completed(self, run_id: int, step_key: str) -> bool:
        return step_key in (self.cursor_for_run(run_id).get("completed_steps") or {})

    def set_status(self, run_id: int, status: RunStatus | str, *, reason: str | None = None) -> AgentRun:
        row = self.require_run(run_id)
        current, target = ensure_run_transition(row.status, status)
        if current == target:
            return self.to_domain(row)
        now = datetime.now(timezone.utc)
        row.status = target.value
        if target == RunStatus.STARTING:
            row.started_at = row.started_at or now
        elif target == RunStatus.PAUSED:
            row.paused_at = now
        elif target == RunStatus.COMPLETED:
            row.completed_at = now
        elif target == RunStatus.FAILED:
            row.failed_at = now
        elif target == RunStatus.CANCELED:
            row.canceled_at = now
        self.append_event(
            status_changed_event(
                row.id,
                from_status=current.value,
                to_status=target.value,
                root_run_id=row.root_run_id,
                reason=reason,
            )
        )
        return self.to_domain(row)

    def append_steering(
        self,
        run_id: int,
        content: str,
        *,
        user_id: str | None = None,
        thread_message_id: int | None = None,
    ) -> AgentRunEventRow:
        run = self.require_run(run_id)
        message = SteeringMessage(run_id=run.id, content=content, user_id=user_id).normalized()
        if not message.content:
            raise ValueError("Steering content is required")
        payload: dict[str, Any] = {"content": message.content, "user_id": message.user_id}
        if thread_message_id is not None:
            payload["thread_message_id"] = int(thread_message_id)
        return self.append_event(
            run_event(
                run.id,
                _STEERING_SUBMITTED_EVENT,
                payload,
                root_run_id=run.root_run_id,
                producer="user",
            )
        )

    def drain_steering(self, run_id: int) -> list[SteeringMessage]:
        run = self.require_run(run_id)
        metadata = dict(run.metadata_ or {})
        cursor = _coerce_int(metadata.get(_STEERING_CURSOR_METADATA_KEY), default=0)
        rows = self.session.scalars(
            select(AgentRunEventRow)
            .where(
                AgentRunEventRow.run_id == int(run_id),
                AgentRunEventRow.event_type == _STEERING_SUBMITTED_EVENT,
                AgentRunEventRow.sequence_no > cursor,
            )
            .order_by(AgentRunEventRow.sequence_no.asc(), AgentRunEventRow.id.asc())
        ).all()
        if not rows:
            if self.auto_commit:
                self.session.rollback()
            return []

        run = self._locked_run(run_id)
        metadata = dict(run.metadata_ or {})
        cursor = _coerce_int(metadata.get(_STEERING_CURSOR_METADATA_KEY), default=0)
        rows = [row for row in rows if int(row.sequence_no) > cursor]
        if not rows:
            if self.auto_commit:
                self.session.rollback()
            return []

        metadata[_STEERING_CURSOR_METADATA_KEY] = int(rows[-1].sequence_no)
        run.metadata_ = metadata
        self.session.flush()

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
            self.session.commit()
        return messages

    def append_event(self, event: AgentRunEvent) -> AgentRunEventRow:
        with _event_lock(int(event.run_id)):
            if self._dialect_name() == "postgresql":
                self.session.execute(text("SELECT pg_advisory_xact_lock(:run_id)"), {"run_id": int(event.run_id)})
            sequence_no = event.sequence_no
            if sequence_no is None:
                sequence_no = int(
                    self.session.scalar(
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
            self.session.flush()
            if self.auto_commit:
                self.session.commit()
            return row

    def has_event_type(self, run_id: int, event_type: str) -> bool:
        return self.session.scalar(
            select(AgentRunEventRow.id)
            .where(
                AgentRunEventRow.run_id == int(run_id),
                AgentRunEventRow.event_type == str(event_type),
            )
            .limit(1)
        ) is not None

    def append_artifact(self, artifact: AgentRunArtifact) -> AgentRunArtifactRow:
        artifact_type = (
            artifact.artifact_type.value
            if isinstance(artifact.artifact_type, ArtifactType)
            else str(artifact.artifact_type)
        )
        row = AgentRunArtifactRow(
            run_id=artifact.run_id,
            root_run_id=artifact.root_run_id or artifact.run_id,
            artifact_type=artifact_type,
            title=artifact.title,
            payload=dict(artifact.payload or {}),
            text=artifact.text,
            uri=artifact.uri,
            visibility=(
                artifact.visibility.value
                if isinstance(artifact.visibility, EventVisibility)
                else str(artifact.visibility)
            ),
        )
        self.session.add(row)
        self.session.flush()
        self.append_event(
            run_event(
                artifact.run_id,
                "run.artifact_created",
                {"artifact_id": row.id, "artifact_type": row.artifact_type, "title": row.title},
                root_run_id=row.root_run_id,
            )
        )
        return row

    def append_final_answer_once(
        self,
        run_id: int,
        text_value: str,
        *,
        root_run_id: int | None = None,
    ) -> AgentRunArtifactRow | None:
        text_value = str(text_value or "")
        if not text_value:
            return None
        existing = self.session.scalars(
            select(AgentRunArtifactRow)
            .where(
                AgentRunArtifactRow.run_id == int(run_id),
                AgentRunArtifactRow.artifact_type == ArtifactType.FINAL_ANSWER.value,
                AgentRunArtifactRow.text == text_value,
            )
            .order_by(AgentRunArtifactRow.id.asc())
            .limit(1)
        ).first()
        if existing is not None:
            return existing
        from brain.systems.runs.artifacts import final_answer_artifact

        return self.append_artifact(final_answer_artifact(run_id, text_value, root_run_id=root_run_id))

    def latest_artifact_text(self, run_id: int, artifact_type: ArtifactType | str = ArtifactType.FINAL_ANSWER) -> str:
        artifact_type_value = artifact_type.value if isinstance(artifact_type, ArtifactType) else str(artifact_type)
        row = self.session.scalars(
            select(AgentRunArtifactRow)
            .where(
                AgentRunArtifactRow.run_id == int(run_id),
                AgentRunArtifactRow.artifact_type == artifact_type_value,
            )
            .order_by(AgentRunArtifactRow.created_at.desc(), AgentRunArtifactRow.id.desc())
            .limit(1)
        ).first()
        return str(getattr(row, "text", None) or "") if row is not None else ""

    def list_artifacts(self, run_id: int) -> list[AgentRunArtifactRow]:
        return list(
            self.session.scalars(
                select(AgentRunArtifactRow)
                .where(AgentRunArtifactRow.run_id == int(run_id))
                .order_by(AgentRunArtifactRow.created_at.asc(), AgentRunArtifactRow.id.asc())
            )
        )

    def to_domain(self, row: AgentRunRow) -> AgentRun:
        return AgentRun(
            id=row.id,
            trace_id=row.trace_id or trace_id_for_run_id(row.id) or "",
            org_id=row.org_id,
            user_id=row.user_id,
            thread_id=row.thread_id,
            parent_run_id=row.parent_run_id,
            root_run_id=row.root_run_id,
            profile=RunProfile(row.profile),
            recipe=RunRecipe(row.recipe),
            status=coerce_run_status(row.status, default=RunStatus.FAILED),
            input_message=row.input_message,
            target_ref=dict(row.target_ref or {}),
            workspace_ref=dict(row.workspace_ref or {}),
            model_policy=dict(row.model_policy or {}),
            context_summary=row.context_summary,
            metadata=dict(row.metadata_ or {}),
            created_at=row.created_at,
            started_at=row.started_at,
            paused_at=row.paused_at,
            completed_at=row.completed_at,
            failed_at=row.failed_at,
            canceled_at=row.canceled_at,
            updated_at=row.updated_at,
        )

    def _locked_run(self, run_id: int) -> AgentRunRow:
        stmt = select(AgentRunRow).where(AgentRunRow.id == int(run_id)).limit(1)
        if self._dialect_name() == "postgresql":
            stmt = stmt.with_for_update()
        row = self.session.scalars(stmt).first()
        if row is None:
            raise LookupError(f"Run {run_id} not found")
        return row

    def _dialect_name(self) -> str:
        bind = self.session.get_bind()
        return str(getattr(getattr(bind, "dialect", None), "name", "") or "")


class AsyncAgentRunStore:
    """Async facade for the agent-run store.

    Each method runs the existing synchronous domain operation inside
    ``AsyncSession.run_sync``. Callers should open a short-lived async session,
    perform the persistence operation, then commit before any long-running
    model, tool, browser, or connector I/O.
    """

    def __init__(self, session: AsyncSession, *, auto_commit: bool = False):
        self.session = session
        self.auto_commit = bool(auto_commit)

    async def _run(self, fn):
        def _invoke(sync_session: Session):
            return fn(AgentRunStore(sync_session, auto_commit=self.auto_commit))

        return await self.session.run_sync(_invoke)

    async def create_run(self, request: AgentRunRequest) -> AgentRun:
        return await self._run(lambda store: store.create_run(request))

    async def create_child_run(self, parent: AgentRun | AgentRunRow, **kwargs: Any) -> AgentRun:
        return await self._run(lambda store: store.create_child_run(parent, **kwargs))

    async def claim_next(self) -> AgentRun | None:
        return await self._run(lambda store: store.claim_next())

    async def claim_run(self, run_id: int) -> AgentRun | None:
        return await self._run(lambda store: store.claim_run(run_id))

    async def set_status(self, run_id: int, status: RunStatus | str, *, reason: str | None = None) -> AgentRun:
        return await self._run(lambda store: store.set_status(run_id, status, reason=reason))

    async def append_event(self, event: AgentRunEvent) -> AgentRunEventRow:
        return await self._run(lambda store: store.append_event(event))

    async def append_artifact(self, artifact: AgentRunArtifact) -> AgentRunArtifactRow:
        return await self._run(lambda store: store.append_artifact(artifact))


__all__ = ["AgentRunStore", "AsyncAgentRunStore"]


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    return str(value)


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
