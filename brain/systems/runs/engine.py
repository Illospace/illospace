"""Async engine for all agent-run recipes."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
import inspect
import logging
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from brain.systems.runs.context import RunContextLoader
from brain.systems.runs.domain import AgentRun, AgentRunRequest
from brain.systems.runs.events import activity_event, run_event, text_delta_event
from brain.systems.runs.execution_failure import RunExecutionFailure
from brain.systems.runs.failure_diagnostic import RunFailureStage
from brain.systems.runs.failures import (
    RunFailureCategory,
    coerce_failure_category,
    failure_category_for_error,
    failure_category_for_run_context,
    run_requires_durable_preservation,
    safe_terminal_run_message,
)
from brain.systems.runs.status import RunStatus, TERMINAL_RUN_STATUSES, coerce_run_status
from brain.systems.runs.store import (
    AsyncAgentRunStore,
    ExecutionClaim,
    ExecutionClaimLost,
    to_domain,
)
from brain.systems.runs.stream import RunStream
from brain.systems.runs.steering import SteeringInbox, SteeringMessage
from brain.systems.runs.tools import AsyncRunToolExecutor

logger = logging.getLogger(__name__)
_post_completion_tasks: set[asyncio.Task[None]] = set()


class AsyncRunRecipeHandler(Protocol):
    def execute(self, runtime: "RunRuntime") -> "RunRecipeResult | Awaitable[RunRecipeResult]":
        ...


@dataclass(frozen=True)
class RunRecipeResult:
    """Recipe outcome with successful output separated from failure presentation."""

    output: str = ""
    status: RunStatus = RunStatus.COMPLETED
    # Internal diagnostic text. It may contain provider or exception details.
    error: str | None = None
    # Optional user-safe text. Failed runs must never put raw errors here.
    final_output: str | None = None
    failure_category: RunFailureCategory | str | None = None
    failure_stage: RunFailureStage | None = None
    exception_type: type[BaseException] | None = None
    artifacts: tuple = ()
    post_completion_tasks: tuple[Callable[[], Awaitable[Any]], ...] = ()


async def _run_post_completion_task(run_id: int, task: Callable[[], Awaitable[Any]]) -> None:
    try:
        await task()
    except Exception:
        logger.exception("agent_run_post_completion_task_failed run_id=%s", run_id)


def _schedule_post_completion_task(run_id: int, task: Callable[[], Awaitable[Any]]) -> None:
    post_completion_task = asyncio.create_task(
        _run_post_completion_task(run_id, task),
        name=f"agent-run-{run_id}-post-completion",
    )
    _post_completion_tasks.add(post_completion_task)
    post_completion_task.add_done_callback(_post_completion_tasks.discard)


def _stream_payload(event, row, run: Any | None = None) -> dict[str, Any]:
    payload = dict(event.payload or {})
    event_id = int(getattr(row, "id", 0) or 0)
    payload.update({
        "run_id": int(event.run_id),
        "root_run_id": int(getattr(row, "root_run_id", None) or event.root_run_id or event.run_id),
        "event_id": event_id,
        "run_event_id": event_id,
        "event_cursor": event_id,
        "sequence_no": int(getattr(row, "sequence_no", 0) or event.sequence_no or 0),
    })
    if run is not None:
        payload.setdefault("thread_id", run.thread_id)
        payload.setdefault("idea_id", run.thread_id)
        profile = getattr(run.profile, "value", run.profile)
        if profile:
            payload.setdefault("profile", str(profile))
            payload.setdefault("execution_profile", str(profile))
    return payload


async def cancel_event_is_set(cancel_event: Any | None) -> bool:
    if cancel_event is None:
        return False
    checker = getattr(cancel_event, "a_is_set", None)
    if checker is None:
        checker = getattr(cancel_event, "is_set", None)
    if checker is None:
        return False
    result = checker()
    if inspect.isawaitable(result):
        result = await result
    return bool(result)


@dataclass
class RunRuntime:
    run: AgentRun
    request: AgentRunRequest
    store: AsyncAgentRunStore
    stream: RunStream
    steering: SteeringInbox
    context_loader: RunContextLoader
    tools: AsyncRunToolExecutor | None = None
    cancel_event: Any | None = None
    durable_steering_drain: Callable[[int], list[SteeringMessage] | Awaitable[list[SteeringMessage]]] | None = None

    async def activity(self, label: str, **payload) -> None:
        event = activity_event(self.run.id, label, root_run_id=self.run.root_run_id, **payload)
        row = await self.store.append_event(event)
        self.stream.publish(event.event_type, _stream_payload(event, row, self.run))

    async def text_delta(self, delta: str) -> None:
        event = text_delta_event(self.run.id, delta, root_run_id=self.run.root_run_id)
        row = await self.store.append_event(event)
        self.stream.publish(event.event_type, _stream_payload(event, row, self.run))

    async def drain_steering(self) -> list[str]:
        messages: list[SteeringMessage] = []
        if self.durable_steering_drain is not None:
            drained = self.durable_steering_drain(self.run.id)
            if inspect.isawaitable(drained):
                drained = await drained
            messages.extend(drained)
        else:
            messages.extend(await self.store.drain_steering(self.run.id))
        messages.extend(self.steering.drain(self.run.id))
        for message in messages:
            await self.store.append_event(
                run_event(
                    self.run.id,
                    "run.steering_received",
                    {"content": message.content, "user_id": message.user_id},
                    root_run_id=self.run.root_run_id,
                )
            )
        return [message.content for message in messages]

    def tool_executor(self) -> AsyncRunToolExecutor:
        if self.tools is None:
            self.tools = AsyncRunToolExecutor(self.store, stream=self.stream)
        return self.tools


class AsyncAgentRunEngine:
    def __init__(
        self,
        session: AsyncSession,
        *,
        recipes: dict[str, AsyncRunRecipeHandler],
        stream: RunStream | None = None,
        steering: SteeringInbox | None = None,
        context_loader: RunContextLoader | None = None,
        auto_commit_events: bool = False,
        cancel_event_factory: Callable[[int], Any] | None = None,
        durable_steering_drain: Callable[[int], list[SteeringMessage] | Awaitable[list[SteeringMessage]]] | None = None,
    ):
        self.store = AsyncAgentRunStore(session, auto_commit=auto_commit_events)
        self.recipes = recipes
        self.stream = stream or RunStream()
        self.steering = steering or SteeringInbox()
        self.context_loader = context_loader or RunContextLoader()
        self.cancel_event_factory = cancel_event_factory
        self.durable_steering_drain = durable_steering_drain

    async def run(self, request: AgentRunRequest) -> AgentRun:
        run = await self.store.create_run(request)
        return await self.run_existing(run.id)

    async def claim_next(self) -> AgentRun | None:
        return await self.store.claim_next()

    async def resume(self, run_id: int) -> AgentRun:
        return await self.run_existing(run_id)

    async def pause(self, run_id: int, *, reason: str | None = None) -> AgentRun:
        return await self.store.set_status(run_id, RunStatus.PAUSED, reason=reason)

    async def run_existing(self, run_id: int) -> AgentRun:
        execution_lease = getattr(self.store, "execution_lease", None)
        if callable(execution_lease):
            post_completion_tasks: list[Callable[[], Awaitable[Any]]] = []
            try:
                async with execution_lease(run_id) as execution_claim:
                    if execution_claim is None:
                        refresh_run = getattr(self.store, "refresh_run", None)
                        row = await refresh_run(run_id) if callable(refresh_run) else await self.store.require_run(run_id)
                        return to_domain(row)
                    completed = await self._run_existing_owned(
                        run_id,
                        execution_claim=execution_claim,
                        deferred_post_completion_tasks=post_completion_tasks,
                    )
            except ExecutionClaimLost:
                await self.store.session.rollback()
                refresh_run = getattr(self.store, "refresh_run", None)
                row = await refresh_run(run_id) if callable(refresh_run) else await self.store.require_run(run_id)
                return to_domain(row)
            for task in post_completion_tasks:
                _schedule_post_completion_task(run_id, task)
            return completed
        return await self._run_existing_owned(run_id)

    async def _run_existing_owned(
        self,
        run_id: int,
        *,
        execution_claim: ExecutionClaim | None = None,
        deferred_post_completion_tasks: list[Callable[[], Awaitable[Any]]] | None = None,
    ) -> AgentRun:
        row = (
            await self.store.assert_execution_claim(execution_claim)
            if execution_claim is not None
            else await self.store.require_run(run_id)
        )
        if coerce_run_status(row.status, default=RunStatus.FAILED) in TERMINAL_RUN_STATUSES:
            return to_domain(row)
        if not str(row.org_id or "").strip():
            return await self.fail(
                row.id,
                "AgentRun missing workspace org_id",
                failure_category=RunFailureCategory.INTERNAL,
                failure_stage=RunFailureStage.RUNNER_EXECUTION,
                execution_claim=execution_claim,
            )
        request = AgentRunRequest(
            org_id=row.org_id,
            user_id=row.user_id,
            thread_id=row.thread_id,
            message=row.input_message,
            profile=row.profile,
            recipe=row.recipe,
            parent_run_id=row.parent_run_id,
            root_run_id=row.root_run_id,
            target_ref=dict(row.target_ref or {}),
            workspace_ref=dict(row.workspace_ref or {}),
            model_policy=dict(row.model_policy or {}),
            metadata=dict(row.metadata_ or {}),
            deadline_at=row.deadline_at,
        )
        run = to_domain(row) if execution_claim is not None else await self._enter_running(row)
        cancel_event = self.cancel_event_factory(run.id) if self.cancel_event_factory else None
        runtime = RunRuntime(
            run=run,
            request=request,
            store=self.store,
            stream=self.stream,
            steering=self.steering,
            context_loader=self.context_loader,
            tools=AsyncRunToolExecutor(self.store, stream=self.stream),
            cancel_event=cancel_event,
            durable_steering_drain=self.durable_steering_drain,
        )
        recipe_name = str(row.recipe)
        recipe = self.recipes.get(recipe_name)
        if recipe is None:
            return await self.fail(
                run.id,
                f"No recipe registered for {recipe_name!r}",
                execution_claim=execution_claim,
            )
        try:
            result = recipe.execute(runtime)
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:
            raise RunExecutionFailure.capture(run.id, exc) from exc
        for artifact in result.artifacts:
            await self.store.append_artifact(artifact)
        if await cancel_event_is_set(cancel_event):
            return await self.cancel(
                run.id,
                reason="user_canceled",
                execution_claim=execution_claim,
            )
        result_status = RunStatus(result.status)
        if result_status == RunStatus.PAUSED:
            return await self.store.set_status(
                run.id,
                result_status,
                execution_claim=execution_claim,
            )
        if result_status == RunStatus.FAILED:
            error = str(result.error or result.output or "recipe_failed")
            return await self.fail(
                run.id,
                error,
                final_output=result.final_output,
                failure_category=result.failure_category or failure_category_for_error(error),
                failure_stage=result.failure_stage or RunFailureStage.RECIPE_EXECUTION,
                exception_type=result.exception_type,
                execution_claim=execution_claim,
            )
        if result_status == RunStatus.CANCELED:
            return await self.cancel(
                run.id,
                reason=result.error or result.output or None,
                execution_claim=execution_claim,
            )
        completed = await self.complete(
            run.id,
            output=result.final_output if result.final_output is not None else result.output,
            status=result_status,
            execution_claim=execution_claim,
        )
        if deferred_post_completion_tasks is None:
            for task in result.post_completion_tasks:
                _schedule_post_completion_task(run.id, task)
        else:
            deferred_post_completion_tasks.extend(result.post_completion_tasks)
        return completed

    async def complete(
        self,
        run_id: int,
        *,
        output: str = "",
        status: RunStatus = RunStatus.COMPLETED,
        execution_claim: ExecutionClaim | None = None,
    ) -> AgentRun:
        if status == RunStatus.FAILED:
            return await self.fail(
                run_id,
                output or "run_failed_during_completion",
                failure_stage=RunFailureStage.RUNNER_SETTLEMENT,
                execution_claim=execution_claim,
            )
        row = await self.store.require_run(run_id)
        if coerce_run_status(row.status, default=RunStatus.FAILED) in TERMINAL_RUN_STATUSES:
            return to_domain(row)
        try:
            from brain.systems.slack.chantier_reconciliation import (
                ChantierDeclareGuaranteeError,
                guarantee_chantier_record_for_run,
            )

            guarantee = await guarantee_chantier_record_for_run(
                self.store.session,
                run=row,
                output=output,
            )
        except ChantierDeclareGuaranteeError as exc:
            failure = str(exc).strip() or "tracker-record verification failed"
            await self.store.update_metadata(
                run_id,
                {
                    "chantier_declare_guarantee": {
                        "status": "failed",
                        "error": failure,
                    }
                },
            )
            await self.store.append_event(
                run_event(
                    run_id,
                    "run.chantier_declare_guarantee_failed",
                    {"error": failure},
                    root_run_id=row.root_run_id,
                )
            )
            return await self.fail(
                run_id,
                f"Chantier declare failed: {failure}",
                final_output=safe_terminal_run_message(RunStatus.FAILED, RunFailureCategory.INTERNAL),
                failure_category=RunFailureCategory.INTERNAL,
                failure_stage=RunFailureStage.COMPLETION_VERIFICATION,
                exception_type=type(exc),
                execution_claim=execution_claim,
            )
        if guarantee is not None:
            guarantee_metadata = guarantee.as_metadata()
            await self.store.update_metadata(
                run_id,
                {"chantier_declare_guarantee": guarantee_metadata},
            )
            await self.store.append_event(
                run_event(
                    run_id,
                    "run.chantier_declare_guaranteed",
                    guarantee_metadata,
                    root_run_id=row.root_run_id,
                )
            )
        row = await self._prepare_terminal_write(run_id)
        async with self._atomic_terminal_write():
            if coerce_run_status(row.status, default=RunStatus.FAILED) in TERMINAL_RUN_STATUSES:
                return to_domain(row)
            completed = await self.store.set_status(
                run_id,
                status,
                execution_claim=execution_claim,
            )
            if completed.status != status:
                return completed
            if output:
                artifact = await self.store.append_final_answer_once(
                    run_id,
                    output,
                    root_run_id=row.root_run_id,
                )
                safe_output = str(getattr(artifact, "text", None) or output)
                if not await self.store.has_event_type(run_id, "run.text_delta"):
                    await self.store.append_event(
                        run_event(
                            run_id,
                            "run.text_completed",
                            {"text": safe_output},
                            root_run_id=row.root_run_id,
                        )
                    )
            await self.store.append_event(
                run_event(run_id, "run.completed", {"status": status.value}, root_run_id=row.root_run_id)
            )
            await self._queue_chantier_continuation(run_id)
            return completed

    async def fail(
        self,
        run_id: int,
        error: str,
        *,
        final_output: str | None = None,
        failure_category: RunFailureCategory | str | None = None,
        failure_stage: RunFailureStage = RunFailureStage.RECIPE_EXECUTION,
        exception_type: type[BaseException] | None = None,
        execution_claim: ExecutionClaim | None = None,
    ) -> AgentRun:
        row = await self._prepare_terminal_write(run_id)
        async with self._atomic_terminal_write():
            if coerce_run_status(row.status, default=RunStatus.FAILED) in TERMINAL_RUN_STATUSES:
                return to_domain(row)
            category = coerce_failure_category(failure_category or failure_category_for_error(error))
            tool_execution_started = False
            if run_requires_durable_preservation(row.metadata_):
                for event_type in (
                    "run.tool_started",
                    "run.tool_completed",
                    "run.tool_failed",
                ):
                    if await self.store.has_event_type(run_id, event_type):
                        tool_execution_started = True
                        break
            category = failure_category_for_run_context(
                category,
                metadata=row.metadata_,
                tool_execution_started=tool_execution_started,
                failure_stage=failure_stage,
            )
            if category == RunFailureCategory.PRESERVATION_SETUP and final_output:
                final_output = safe_terminal_run_message(RunStatus.FAILED, category)
            safe_error = await self.store.safe_cycle_provider_error_text(run_id, str(error or ""))
            failed = await self.store.fail_run(
                run_id,
                category=category,
                stage=failure_stage,
                reason=safe_error,
                exception_type=exception_type,
                execution_claim=execution_claim,
            )
            if failed.status != RunStatus.FAILED:
                return failed
            if final_output:
                artifact = await self.store.append_final_answer_once(
                    run_id,
                    final_output,
                    root_run_id=row.root_run_id,
                )
                safe_output = str(getattr(artifact, "text", None) or final_output)
                if not await self.store.has_event_type(run_id, "run.text_delta"):
                    await self.store.append_event(
                        run_event(
                            run_id,
                            "run.text_completed",
                            {"text": safe_output},
                            root_run_id=row.root_run_id,
                        )
                    )
            await self.store.append_event(
                run_event(
                    run_id,
                    "run.failed",
                    {
                        "error": safe_error,
                        "failure_category": category.value,
                    },
                    root_run_id=row.root_run_id,
                )
            )
            await self._queue_chantier_continuation(run_id)
            return failed

    async def cancel(
        self,
        run_id: int,
        *,
        reason: str | None = None,
        execution_claim: ExecutionClaim | None = None,
    ) -> AgentRun:
        row = await self._prepare_terminal_write(run_id)
        async with self._atomic_terminal_write():
            if coerce_run_status(row.status, default=RunStatus.FAILED) in TERMINAL_RUN_STATUSES:
                return to_domain(row)
            canceled = await self.store.set_status(
                run_id,
                RunStatus.CANCELED,
                reason=reason,
                execution_claim=execution_claim,
            )
            if canceled.status != RunStatus.CANCELED:
                return canceled
            await self.store.append_event(
                run_event(run_id, "run.canceled", {"reason": reason}, root_run_id=row.root_run_id)
            )
            await self._queue_chantier_continuation(run_id)
            return canceled

    async def _queue_chantier_continuation(self, run_id: int) -> int | None:
        from brain.systems.runs.chantier_continuation import (
            queue_chantier_continuation_for_terminal_run,
        )

        return await queue_chantier_continuation_for_terminal_run(
            self.store.session,
            terminal_run_id=run_id,
        )

    async def _prepare_terminal_write(self, run_id: int):
        """Start terminal work from a clean, globally ordered lock boundary."""

        snapshot = await self.store.require_run(run_id)
        if coerce_run_status(snapshot.status, default=RunStatus.FAILED) in TERMINAL_RUN_STATUSES:
            return snapshot
        anchor_run_id = int(snapshot.parent_run_id or snapshot.id)
        await self.store.commit_event_boundary(run_id)
        return await self.store.lock_terminal_boundary(
            run_id,
            anchor_run_id=anchor_run_id,
        )

    @asynccontextmanager
    async def _atomic_terminal_write(self):
        """Keep terminal status, output, and semantic events in one transaction."""
        auto_commit = self.store.auto_commit
        self.store.auto_commit = False
        try:
            yield
        except BaseException:
            await self.store.session.rollback()
            raise
        else:
            if auto_commit:
                await self.store.session.commit()
        finally:
            self.store.auto_commit = auto_commit

    async def _enter_running(self, row) -> AgentRun:
        status = coerce_run_status(row.status, default=RunStatus.FAILED)
        if status in TERMINAL_RUN_STATUSES:
            return to_domain(row)
        if status == RunStatus.QUEUED:
            await self.store.claim_run(row.id)
            await self.store.set_status(row.id, RunStatus.RUNNING)
        elif status in {RunStatus.STARTING, RunStatus.PAUSED}:
            await self.store.set_status(row.id, RunStatus.RUNNING)
        elif status == RunStatus.RUNNING:
            await self.store.append_event(
                run_event(row.id, "run.resumed", {"from_status": status.value}, root_run_id=row.root_run_id)
            )
            return to_domain(await self.store.require_run(row.id))
        await self.store.append_event(
            run_event(row.id, "run.started", {"from_status": status.value}, root_run_id=row.root_run_id)
        )
        return to_domain(await self.store.require_run(row.id))


class StaticAnswerRecipe:
    def __init__(self, answer: str | Callable[[RunRuntime], str]):
        self.answer = answer

    async def execute(self, runtime: RunRuntime) -> RunRecipeResult:
        await runtime.activity("Thinking")
        output = self.answer(runtime) if callable(self.answer) else self.answer
        if inspect.isawaitable(output):
            output = await output
        for chunk in _chunks(str(output)):
            await runtime.text_delta(chunk)
        return RunRecipeResult(output=str(output))


def _chunks(text: str, *, size: int = 80) -> Iterable[str]:
    for index in range(0, len(text), size):
        yield text[index : index + size]


__all__ = [
    "AsyncAgentRunEngine",
    "AsyncRunRecipeHandler",
    "cancel_event_is_set",
    "RunRecipeResult",
    "RunRuntime",
    "StaticAnswerRecipe",
]
