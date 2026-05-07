"""One engine for all agent-run recipes."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy.orm import Session

from brain.systems.runs.context import RunContextLoader
from brain.systems.runs.domain import AgentRun, AgentRunRequest, ArtifactType, RunProfile, RunRecipe
from brain.systems.runs.events import activity_event, run_event, text_delta_event
from brain.systems.runs.status import RunStatus, TERMINAL_RUN_STATUSES, coerce_run_status
from brain.systems.runs.store import AgentRunStore
from brain.systems.runs.stream import RunStream
from brain.systems.runs.steering import SteeringInbox, SteeringMessage
from brain.systems.runs.tools import RunToolExecutor


class RunRecipeHandler(Protocol):
    def execute(self, runtime: "RunRuntime") -> "RunRecipeResult":
        ...


@dataclass(frozen=True)
class RunRecipeResult:
    output: str
    status: RunStatus = RunStatus.COMPLETED
    artifacts: tuple = ()


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


@dataclass
class RunRuntime:
    run: AgentRun
    request: AgentRunRequest
    store: AgentRunStore
    stream: RunStream
    steering: SteeringInbox
    context_loader: RunContextLoader
    engine: "AgentRunEngine | None" = None
    tools: RunToolExecutor | None = None
    cancel_event: Any | None = None
    durable_steering_drain: Callable[[int], list[SteeringMessage]] | None = None

    def activity(self, label: str, **payload) -> None:
        event = activity_event(self.run.id, label, root_run_id=self.run.root_run_id, **payload)
        row = self.store.append_event(event)
        self.stream.publish(event.event_type, _stream_payload(event, row, self.run))

    def text_delta(self, delta: str) -> None:
        event = text_delta_event(self.run.id, delta, root_run_id=self.run.root_run_id)
        row = self.store.append_event(event)
        self.stream.publish(event.event_type, _stream_payload(event, row, self.run))

    def drain_steering(self) -> list[str]:
        messages: list[SteeringMessage] = []
        if self.durable_steering_drain is not None:
            messages.extend(self.durable_steering_drain(self.run.id))
        else:
            durable_drain = getattr(self.store, "drain_steering", None)
            if callable(durable_drain):
                messages.extend(durable_drain(self.run.id))
        messages.extend(self.steering.drain(self.run.id))
        for message in messages:
            self.store.append_event(
                run_event(
                    self.run.id,
                    "run.steering_received",
                    {"content": message.content, "user_id": message.user_id},
                    root_run_id=self.run.root_run_id,
                )
            )
        return [message.content for message in messages]

    def run_step(self, step_key: str, fn: Callable[[], Any]) -> Any:
        step_methods = ("complete_step", "fail_step", "start_step", "step_completed")
        if not all(hasattr(self.store, name) for name in step_methods):
            self.store.append_event(
                run_event(
                    self.run.id,
                    "run.step_started",
                    {"step": step_key, "step_key": step_key},
                    root_run_id=self.run.root_run_id,
                )
            )
            try:
                result = fn()
            except Exception as exc:
                self.store.append_event(
                    run_event(
                        self.run.id,
                        "run.step_failed",
                        {"step": step_key, "step_key": step_key, "error": str(exc)},
                        root_run_id=self.run.root_run_id,
                    )
                )
                raise
            self.store.append_event(
                run_event(
                    self.run.id,
                    "run.step_completed",
                    {"step": step_key, "step_key": step_key, "result": result},
                    root_run_id=self.run.root_run_id,
                )
            )
            return result
        if hasattr(self.store, "step_completed") and self.store.step_completed(self.run.id, step_key):
            self.store.skip_step(self.run.id, step_key)
            return self.store.step_result(self.run.id, step_key)
        self.store.start_step(self.run.id, step_key)
        try:
            result = fn()
        except Exception as exc:
            self.store.fail_step(self.run.id, step_key, str(exc))
            raise
        return self.store.complete_step(self.run.id, step_key, result)

    def step(self, step_key: str, fn: Callable[[], Any]) -> Any:
        return self.run_step(step_key, fn)

    def create_child_run(
        self,
        *,
        recipe: RunRecipe | str,
        message: str,
        step_key: str,
        profile: RunProfile | str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AgentRun:
        if not hasattr(self.store, "create_child_run"):
            return self.store.create_run(
                AgentRunRequest(
                    org_id=self.request.org_id,
                    user_id=self.request.user_id,
                    thread_id=self.request.thread_id,
                    message=message,
                    profile=profile or self.request.profile,
                    recipe=recipe,
                    parent_run_id=self.run.id,
                    root_run_id=self.run.root_run_id or self.run.id,
                    target_ref=dict(self.request.target_ref or {}),
                    workspace_ref=dict(self.request.workspace_ref or {}),
                    model_policy=dict(self.request.model_policy or {}),
                    metadata={"parent_step_key": step_key, **dict(metadata or {})},
                )
            )
        return self.store.create_child_run(
            self.run,
            recipe=recipe,
            message=message,
            profile=profile or self.request.profile,
            step_key=step_key,
            target_ref=self.request.target_ref,
            workspace_ref=self.request.workspace_ref,
            model_policy=self.request.model_policy,
            metadata=metadata,
        )

    def run_child(
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
        child = self.create_child_run(
            recipe=recipe,
            message=message,
            step_key=step_key,
            profile=profile,
            metadata=metadata,
        )
        return self.engine.run_existing(child.id)

    def child_output(self, child: AgentRun) -> str:
        return self.store.latest_artifact_text(child.id, ArtifactType.FINAL_ANSWER)

    def tool_executor(self) -> RunToolExecutor:
        if self.tools is None:
            self.tools = RunToolExecutor(self.store, stream=self.stream)
        return self.tools


class AgentRunEngine:
    def __init__(
        self,
        session: Session,
        *,
        recipes: dict[str, RunRecipeHandler],
        stream: RunStream | None = None,
        steering: SteeringInbox | None = None,
        context_loader: RunContextLoader | None = None,
        auto_commit_events: bool = False,
        cancel_event_factory: Callable[[int], Any] | None = None,
        durable_steering_drain: Callable[[int], list[SteeringMessage]] | None = None,
    ):
        self.store = AgentRunStore(session, auto_commit=auto_commit_events)
        self.recipes = recipes
        self.stream = stream or RunStream()
        self.steering = steering or SteeringInbox()
        self.context_loader = context_loader or RunContextLoader()
        self.cancel_event_factory = cancel_event_factory
        self.durable_steering_drain = durable_steering_drain

    def run(self, request: AgentRunRequest) -> AgentRun:
        run = self.store.create_run(request)
        return self.run_existing(run.id)

    def claim_next(self) -> AgentRun | None:
        return self.store.claim_next()

    def resume(self, run_id: int) -> AgentRun:
        return self.run_existing(run_id)

    def pause(self, run_id: int, *, reason: str | None = None) -> AgentRun:
        return self.store.set_status(run_id, RunStatus.PAUSED, reason=reason)

    def run_existing(self, run_id: int) -> AgentRun:
        row = self.store.require_run(run_id)
        if coerce_run_status(row.status, default=RunStatus.FAILED) in TERMINAL_RUN_STATUSES:
            return self.store.to_domain(row)
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
        run = self._enter_running(row)
        cancel_event = self.cancel_event_factory(run.id) if self.cancel_event_factory else None
        runtime = RunRuntime(
            run=run,
            request=request,
            store=self.store,
            stream=self.stream,
            steering=self.steering,
            context_loader=self.context_loader,
            engine=self,
            tools=RunToolExecutor(self.store, stream=self.stream),
            cancel_event=cancel_event,
            durable_steering_drain=self.durable_steering_drain,
        )
        recipe = self.recipes.get(request.normalized_recipe.value)
        if recipe is None:
            return self.fail(run.id, f"No recipe registered for {request.normalized_recipe.value!r}")
        try:
            result = recipe.execute(runtime)
        except Exception as exc:
            return self.fail(run.id, str(exc))
        for artifact in result.artifacts:
            self.store.append_artifact(artifact)
        if cancel_event is not None and cancel_event.is_set():
            return self.cancel(run.id, reason="user_canceled")
        result_status = RunStatus(result.status)
        if result_status == RunStatus.PAUSED:
            return self.store.set_status(run.id, result_status)
        if result_status == RunStatus.FAILED:
            if result.output:
                self.store.append_final_answer_once(run.id, result.output, root_run_id=run.root_run_id)
                if not self.store.has_event_type(run.id, "run.text_delta"):
                    self.store.append_event(
                        run_event(run.id, "run.text_completed", {"text": result.output}, root_run_id=run.root_run_id)
                    )
            return self.fail(run.id, result.output or "recipe_failed")
        if result_status == RunStatus.CANCELED:
            return self.cancel(run.id, reason=result.output or None)
        return self.complete(run.id, output=result.output, status=result_status)

    def complete(
        self,
        run_id: int,
        *,
        output: str = "",
        status: RunStatus = RunStatus.COMPLETED,
    ) -> AgentRun:
        row = self.store.require_run(run_id)
        if coerce_run_status(row.status, default=RunStatus.FAILED) in TERMINAL_RUN_STATUSES:
            return self.store.to_domain(row)
        if output:
            self.store.append_final_answer_once(run_id, output, root_run_id=row.root_run_id)
            if not self.store.has_event_type(run_id, "run.text_delta"):
                self.store.append_event(
                    run_event(run_id, "run.text_completed", {"text": output}, root_run_id=row.root_run_id)
                )
        self.store.append_event(
            run_event(run_id, "run.completed", {"status": status.value}, root_run_id=row.root_run_id)
        )
        return self.store.set_status(run_id, status)

    def fail(self, run_id: int, error: str) -> AgentRun:
        row = self.store.require_run(run_id)
        if coerce_run_status(row.status, default=RunStatus.FAILED) in TERMINAL_RUN_STATUSES:
            return self.store.to_domain(row)
        self.store.append_event(run_event(run_id, "run.failed", {"error": error}, root_run_id=row.root_run_id))
        return self.store.set_status(run_id, RunStatus.FAILED, reason=error)

    def cancel(self, run_id: int, *, reason: str | None = None) -> AgentRun:
        row = self.store.require_run(run_id)
        if coerce_run_status(row.status, default=RunStatus.FAILED) in TERMINAL_RUN_STATUSES:
            return self.store.to_domain(row)
        self.store.append_event(run_event(run_id, "run.canceled", {"reason": reason}, root_run_id=row.root_run_id))
        return self.store.set_status(run_id, RunStatus.CANCELED, reason=reason)

    def _enter_running(self, row) -> AgentRun:
        status = coerce_run_status(row.status, default=RunStatus.FAILED)
        if status in TERMINAL_RUN_STATUSES:
            return self.store.to_domain(row)
        if status == RunStatus.QUEUED:
            self.store.claim_run(row.id)
            self.store.set_status(row.id, RunStatus.RUNNING)
        elif status in {RunStatus.STARTING, RunStatus.PAUSED}:
            self.store.set_status(row.id, RunStatus.RUNNING)
        elif status == RunStatus.RUNNING:
            self.store.append_event(
                run_event(row.id, "run.resumed", {"from_status": status.value}, root_run_id=row.root_run_id)
            )
            return self.store.to_domain(self.store.require_run(row.id))
        self.store.append_event(
            run_event(row.id, "run.started", {"from_status": status.value}, root_run_id=row.root_run_id)
        )
        return self.store.to_domain(self.store.require_run(row.id))


class StaticAnswerRecipe:
    def __init__(self, answer: str | Callable[[RunRuntime], str]):
        self.answer = answer

    def execute(self, runtime: RunRuntime) -> RunRecipeResult:
        runtime.activity("Thinking")
        output = self.answer(runtime) if callable(self.answer) else self.answer
        for chunk in _chunks(output):
            runtime.text_delta(chunk)
        return RunRecipeResult(output=output)


def _chunks(text: str, *, size: int = 80) -> Iterable[str]:
    for index in range(0, len(text), size):
        yield text[index : index + size]


__all__ = [
    "AgentRunEngine",
    "RunRecipeHandler",
    "RunRecipeResult",
    "RunRuntime",
    "StaticAnswerRecipe",
]
