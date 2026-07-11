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
from brain.systems.runs.domain import AgentRun, AgentRunRequest, ArtifactType, RunProfile, RunRecipe
from brain.systems.runs.events import activity_event, run_event, text_delta_event
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
    output: str
    status: RunStatus = RunStatus.COMPLETED
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
    engine: "AsyncAgentRunEngine | None" = None
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

    async def run_step(self, step_key: str, fn: Callable[[], Any]) -> Any:
        if await self.store.step_completed(self.run.id, step_key):
            await self.store.skip_step(self.run.id, step_key)
            return await self.store.step_result(self.run.id, step_key)
        await self.store.start_step(self.run.id, step_key)
        try:
            result = fn()
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:
            await self.store.fail_step(self.run.id, step_key, str(exc))
            raise
        return await self.store.complete_step(self.run.id, step_key, result)

    async def step(self, step_key: str, fn: Callable[[], Any]) -> Any:
        return await self.run_step(step_key, fn)

    async def create_child_run(
        self,
        *,
        recipe: RunRecipe | str,
        message: str,
        step_key: str,
        profile: RunProfile | str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AgentRun:
        return await self.store.create_child_run(
            self.run,
            initial_status=RunStatus.STARTING,
            recipe=recipe,
            message=message,
            profile=profile or self.request.profile,
            step_key=step_key,
            target_ref=self.request.target_ref,
            workspace_ref=self.request.workspace_ref,
            model_policy=self.request.model_policy,
            metadata=metadata,
        )

    async def run_child(
        self,
        *,
        recipe: RunRecipe | str,
        message: str,
        step_key: str,
        profile: RunProfile | str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AgentRun:
        if self.engine is None:
            raise RuntimeError("RunRuntime cannot execute child runs without an engine")
        child = await self.create_child_run(
            recipe=recipe,
            message=message,
            step_key=step_key,
            profile=profile,
            metadata=metadata,
        )
        return await self.engine.run_existing(child.id)

    async def child_output(self, child: AgentRun) -> str:
        return await self.store.latest_artifact_text(child.id, ArtifactType.FINAL_ANSWER)

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
            return await self.store.set_status(
                row.id,
                RunStatus.FAILED,
                reason="AgentRun missing workspace org_id",
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
            engine=self,
            tools=AsyncRunToolExecutor(self.store, stream=self.stream),
            cancel_event=cancel_event,
            durable_steering_drain=self.durable_steering_drain,
        )
        recipe = self.recipes.get(request.normalized_recipe.value)
        if recipe is None:
            return await self.fail(
                run.id,
                f"No recipe registered for {request.normalized_recipe.value!r}",
                execution_claim=execution_claim,
            )
        try:
            result = recipe.execute(runtime)
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:
            return await self.fail(run.id, str(exc), execution_claim=execution_claim)
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
            return await self.fail(
                run.id,
                result.output or "recipe_failed",
                final_output=result.output or None,
                execution_claim=execution_claim,
            )
        if result_status == RunStatus.CANCELED:
            return await self.cancel(
                run.id,
                reason=result.output or None,
                execution_claim=execution_claim,
            )
        completed = await self.complete(
            run.id,
            output=result.output,
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
        async with self._atomic_terminal_write():
            row = await self.store.require_run(run_id)
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
            return completed

    async def fail(
        self,
        run_id: int,
        error: str,
        *,
        final_output: str | None = None,
        execution_claim: ExecutionClaim | None = None,
    ) -> AgentRun:
        async with self._atomic_terminal_write():
            row = await self.store.require_run(run_id)
            if coerce_run_status(row.status, default=RunStatus.FAILED) in TERMINAL_RUN_STATUSES:
                return to_domain(row)
            safe_error = await self.store.safe_cycle_provider_error_text(run_id, str(error or ""))
            failed = await self.store.set_status(
                run_id,
                RunStatus.FAILED,
                reason=safe_error,
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
                run_event(run_id, "run.failed", {"error": safe_error}, root_run_id=row.root_run_id)
            )
            return failed

    async def cancel(
        self,
        run_id: int,
        *,
        reason: str | None = None,
        execution_claim: ExecutionClaim | None = None,
    ) -> AgentRun:
        async with self._atomic_terminal_write():
            row = await self.store.require_run(run_id)
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
            return canceled

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
