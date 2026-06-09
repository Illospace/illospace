"""Small AgentRun runner loop for Cortex."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager, suppress
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
from brain.contracts.statuses import ACTIVE_RUN_STATUS_VALUES, PROCESSING_RUN_STATUS_VALUES
from brain.systems.cortex.status import PROTECTED_IDEA_STATUSES
from brain.systems.runs.engine import AsyncAgentRunEngine
from brain.systems.runs.events import activity_event, run_event
from brain.systems.runs.status import RunStatus, TERMINAL_RUN_STATUSES, coerce_run_status
from brain.systems.runs.store import AsyncAgentRunStore
from brain.systems.runs.stream import RunStream
from brain.systems.runs.cortex.queue_health import (
    queued_backlog_snapshot_async as _shared_queued_backlog_snapshot_async,
    queued_watchdog_after_seconds as _shared_queued_watchdog_after_seconds,
    runner_concurrency as _shared_runner_concurrency,
)
from brain.systems.runs.ui_events import run_event_to_ui_message
from brain.systems.cortex.events import publish_live_safe, publish_safe
from brain.systems.cortex.project_context.materializer import (
    materialize_project_context_workspaces,
    project_context_has_materializable_resources,
)
from brain.systems.cortex.thought_lifecycle import TerminalRunSettlementCommand, settle_terminal_run
from brain.platform.db.models.agent_run import AgentRunArtifactRow, AgentRunEventRow, AgentRunRow
from brain.platform.db.models.idea import Idea, IdeaThread, ThreadDiscussionComment

logger = logging.getLogger(__name__)
UnitOfWork = None


def _unit_of_work_factory():
    global UnitOfWork
    if UnitOfWork is None:
        from brain.platform.db.repositories.unit_of_work import UnitOfWork as _UnitOfWork

        UnitOfWork = _UnitOfWork
    return UnitOfWork


def _run_cancel_token(run_id: int):
    from brain.systems.runs.cancel import RunCancelToken

    return RunCancelToken(run_id)


_stop_event = threading.Event()
_runner_lock = threading.Lock()
_runner_supervisor_thread: threading.Thread | None = None
_runner_slots: list[tuple[asyncio.Task[None], asyncio.Event]] = []
_runner_thread_index = 0
_poll_interval_sec = 0.5
_runner_reconcile_interval_sec = 2.0
_stale_reconcile_interval_sec = 30.0
_default_heartbeat_interval_sec = 10.0
_default_stale_run_sec = 300.0
_queued_watchdog_interval_sec = 5.0
_last_stale_reconcile_monotonic = 0.0
_last_queued_watchdog_monotonic = 0.0
_queued_watchdog_tasks: set[asyncio.Task[int]] = set()

_PROCESS_ACTIVE_STATUS_VALUES = PROCESSING_RUN_STATUS_VALUES


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


def _queued_watchdog_after_seconds() -> float:
    return _shared_queued_watchdog_after_seconds()


def _runner_concurrency() -> int:
    return _shared_runner_concurrency()


def _active_runner_count() -> int:
    with _runner_lock:
        return sum(1 for task, stop_event in _runner_slots if not task.done() and not stop_event.is_set())


def runner_health_snapshot() -> dict[str, int | bool]:
    supervisor_alive = bool(_runner_supervisor_thread and _runner_supervisor_thread.is_alive())
    active_runner_count = _active_runner_count()
    return {
        "runner_running": supervisor_alive and active_runner_count > 0,
        "supervisor_alive": supervisor_alive,
        "active_runner_count": active_runner_count,
        "configured_concurrency": _runner_concurrency(),
    }


def _int_value(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _project_live_event(event_type: str, payload: dict[str, Any]) -> None:
    run_id = _int_value(payload.get("run_id"))
    if run_id is None:
        return
    event_id = _int_value(payload.get("run_event_id") or payload.get("event_id") or payload.get("event_cursor")) or 0
    sequence_no = _int_value(payload.get("sequence_no")) or 0
    event = SimpleNamespace(
        id=event_id,
        event_type=event_type,
        payload=dict(payload or {}),
        run_id=run_id,
        root_run_id=_int_value(payload.get("root_run_id")) or run_id,
        sequence_no=sequence_no,
        created_at=None,
    )
    message = run_event_to_ui_message(event, org_id=str(payload.get("org_id") or "") or None)
    if not message:
        return
    message_type = message.pop("type")
    publish_live_safe(message_type, message)


async def _project_live_event_async(session, event_type: str, payload: dict[str, Any]) -> None:
    run_id = _int_value(payload.get("run_id"))
    if run_id is None:
        return
    run = await session.get(AgentRunRow, run_id)
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
            _project_live_event(event_type, payload)
        except Exception:
            logger.debug("agent_run_live_event_failed", exc_info=True)

    return _sink


async def _drain_steering_in_isolated_uow(run_id: int):
    async with _unit_of_work_factory()() as uow:
        return await AsyncAgentRunStore(uow.session).drain_steering(int(run_id))


def _engine_for_session(session) -> AsyncAgentRunEngine:
    from brain.systems.runs.recipes import default_recipes

    return AsyncAgentRunEngine(
        session,
        recipes=default_recipes(),
        stream=RunStream(_live_stream_sink(session)),
        auto_commit_events=True,
        cancel_event_factory=_run_cancel_token,
        durable_steering_drain=_drain_steering_in_isolated_uow,
    )


_TERMINAL_RUN_IDEA_STATUS = {
    "completed": "unread_reply",
    "failed": "failed",
    "canceled": "failed",
}
_AI_TIMELINE_SURFACES = {"ai_timeline", "thread_timeline", "cortex_thread", "main_thread"}
_THREAD_DISCUSSION_SURFACE = "thread_discussion"
_THREAD_DISCUSSION_THREAD_PREFIX = "thread-discussion:"
_SLACK_SURFACE = "slack"
_SLACK_REPLY_TOOL = "post_slack_reply"
_NON_TIMELINE_FINAL_ANSWER_TARGETS = {
    "discussion",
    "headless",
    "none",
    "originating_surface",
    _THREAD_DISCUSSION_SURFACE,
}


def _run_target_ref(run: AgentRunRow) -> dict[str, Any]:
    value = getattr(run, "target_ref", None)
    return value if isinstance(value, dict) else {}


def _run_metadata(run: AgentRunRow) -> dict[str, Any]:
    value = getattr(run, "metadata_", None)
    return value if isinstance(value, dict) else {}


def _run_context_maps(run: AgentRunRow):
    for container in (_run_target_ref(run), _run_metadata(run)):
        if container:
            yield container


def _run_is_headless(run: AgentRunRow) -> bool:
    for container in _run_context_maps(run):
        if bool(container.get("headless")):
            return True
    return False


def _run_is_thread_discussion_conversation(run: AgentRunRow) -> bool:
    for container in _run_context_maps(run):
        if container.get("kind") == _THREAD_DISCUSSION_SURFACE:
            return True
        if container.get("originating_surface") == _THREAD_DISCUSSION_SURFACE:
            return True
        if container.get("surface") == _THREAD_DISCUSSION_SURFACE:
            return True
    thread_id = str(getattr(run, "thread_id", "") or "")
    return thread_id.startswith(_THREAD_DISCUSSION_THREAD_PREFIX)


def _run_surface_value(run: AgentRunRow, key: str) -> str:
    for container in _run_context_maps(run):
        value = container.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _run_should_mirror_final_answer_to_timeline(run: AgentRunRow) -> bool:
    if _run_is_thread_discussion_conversation(run):
        return False
    target_surface = _run_surface_value(run, "final_answer_target_surface")
    if target_surface in _AI_TIMELINE_SURFACES:
        return True
    if target_surface in _NON_TIMELINE_FINAL_ANSWER_TARGETS:
        return False
    originating_surface = _run_surface_value(run, "originating_surface")
    return originating_surface != _THREAD_DISCUSSION_SURFACE


def _message_belongs_to_run(message: IdeaThread, run_id: int) -> bool:
    metadata = getattr(message, "metadata_", None)
    if not isinstance(metadata, dict):
        return False
    return str(metadata.get("run_id") or metadata.get("created_by_run_id") or "") == str(run_id)


def _message_surface(message: IdeaThread) -> str:
    metadata = getattr(message, "metadata_", None)
    if not isinstance(metadata, dict):
        return ""
    surface = metadata.get("surface") or metadata.get("target_surface")
    return str(surface or "").strip()


def _message_is_visible_run_timeline_output(message: IdeaThread, run_id: int) -> bool:
    if not _message_belongs_to_run(message, run_id):
        return False
    if str(getattr(message, "message_type", "") or "") == "agent_response":
        return True
    return _message_surface(message) in _AI_TIMELINE_SURFACES


def _attachment_key(attachment: dict[str, Any]) -> str:
    return str(
        attachment.get("url")
        or attachment.get("download_url")
        or attachment.get("public_url")
        or attachment.get("filename")
        or ""
    )


def _append_unique_attachments(target: list[dict[str, Any]], attachments: Any, seen: set[str]) -> None:
    if not isinstance(attachments, list):
        return
    for attachment in attachments:
        if not isinstance(attachment, dict):
            continue
        key = _attachment_key(attachment)
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        target.append(dict(attachment))


async def _same_run_visible_attachments(session, *, run: AgentRunRow, thread_id: str) -> list[dict[str, Any]]:
    """Carry attachments created by tools in this run into the mirrored final answer."""
    if not hasattr(session, "scalars"):
        return []

    run_id = int(run.id)
    attachments: list[dict[str, Any]] = []
    seen: set[str] = set()

    try:
        comments_result = await session.scalars(
            select(ThreadDiscussionComment)
            .where(
                ThreadDiscussionComment.thread_id == str(thread_id),
                ThreadDiscussionComment.author_kind == "illo",
            )
            .order_by(ThreadDiscussionComment.created_at.desc(), ThreadDiscussionComment.id.desc())
            .limit(100)
        )
        comments = list(comments_result.all())
    except Exception:
        comments = []
    for comment in comments:
        if _comment_belongs_to_run(comment, run_id):
            _append_unique_attachments(attachments, getattr(comment, "attachments", None), seen)

    try:
        messages_result = await session.scalars(
            select(IdeaThread)
            .where(
                IdeaThread.idea_id == str(thread_id),
                IdeaThread.role.in_(("illo", "assistant")),
            )
            .order_by(IdeaThread.created_at.desc(), IdeaThread.id.desc())
            .limit(100)
        )
        messages = list(messages_result.all())
    except Exception:
        messages = []
    for message in messages:
        if _message_belongs_to_run(message, run_id):
            _append_unique_attachments(attachments, getattr(message, "attachments", None), seen)

    return attachments


async def _latest_final_answer_artifact(session, *, run: AgentRunRow) -> tuple[str | None, int | None]:
    artifact = (
        await session.scalars(
            select(AgentRunArtifactRow)
            .where(
                AgentRunArtifactRow.run_id == int(run.id),
                AgentRunArtifactRow.artifact_type == "final_answer",
            )
            .order_by(AgentRunArtifactRow.created_at.desc(), AgentRunArtifactRow.id.desc())
            .limit(1)
        )
    ).first()
    text = str(getattr(artifact, "text", None) or "").strip()
    if not text:
        return None, None
    return text, getattr(artifact, "id", None)


async def _latest_unmirrored_final_answer(session, *, run: AgentRunRow, idea: Idea) -> tuple[str | None, int | None]:
    text, artifact_id = await _latest_final_answer_artifact(session, run=run)
    if not text:
        return None, None
    recent_responses = (
        await session.scalars(
            select(IdeaThread)
            .where(
                IdeaThread.idea_id == str(idea.id),
                IdeaThread.role.in_(("illo", "assistant")),
            )
            .order_by(IdeaThread.created_at.desc(), IdeaThread.id.desc())
            .limit(100)
        )
    ).all()
    if any(_message_is_visible_run_timeline_output(message, int(run.id)) for message in recent_responses):
        return None, None
    return text, artifact_id


def _run_discussion_trigger(run: AgentRunRow) -> dict[str, Any]:
    for container in _run_context_maps(run):
        trigger = container.get("discussion_trigger")
        if isinstance(trigger, dict):
            return dict(trigger)
    return {}


def _run_slack_trigger(run: AgentRunRow) -> dict[str, Any]:
    for container in _run_context_maps(run):
        trigger = container.get("slack_trigger")
        if isinstance(trigger, dict):
            return dict(trigger)
    return {}


def _run_is_slack_origin(run: AgentRunRow) -> bool:
    if _run_slack_trigger(run):
        return True
    for container in _run_context_maps(run):
        if container.get("originating_surface") == _SLACK_SURFACE:
            return True
        if container.get("source_surface") == _SLACK_SURFACE:
            return True
        if container.get("final_answer_target_surface") == _SLACK_SURFACE:
            return True
    return False


def _run_discussion_thread_id(run: AgentRunRow) -> str:
    trigger = _run_discussion_trigger(run)
    response_target = trigger.get("response_target") if isinstance(trigger.get("response_target"), dict) else {}
    target_ref = _run_target_ref(run)
    for candidate in (
        response_target.get("thread_id") if isinstance(response_target, dict) else None,
        trigger.get("thread_id"),
        target_ref.get("parent_thread_id"),
        target_ref.get("idea_id"),
    ):
        value = str(candidate or "").strip()
        if value:
            return value
    thread_id = str(getattr(run, "thread_id", "") or "")
    return (
        thread_id[len(_THREAD_DISCUSSION_THREAD_PREFIX):]
        if thread_id.startswith(_THREAD_DISCUSSION_THREAD_PREFIX)
        else ""
    )


def _comment_belongs_to_run(comment: ThreadDiscussionComment, run_id: int) -> bool:
    metadata = getattr(comment, "metadata_", None)
    if not isinstance(metadata, dict):
        return False
    return str(metadata.get("created_by_run_id") or "") == str(run_id)


async def _discussion_reply_already_recorded(session, *, run: AgentRunRow, thread_id: str) -> bool:
    if not hasattr(session, "scalars"):
        return False
    try:
        result = await session.scalars(
            select(ThreadDiscussionComment)
            .where(
                ThreadDiscussionComment.thread_id == str(thread_id),
                ThreadDiscussionComment.author_kind == "illo",
            )
            .order_by(ThreadDiscussionComment.created_at.desc(), ThreadDiscussionComment.id.desc())
            .limit(100)
        )
        comments = list(result.all())
    except Exception:
        return False
    return any(_comment_belongs_to_run(comment, int(run.id)) for comment in comments)


def _thread_discussion_comment_payload(comment: ThreadDiscussionComment) -> dict[str, Any]:
    return {
        "id": comment.id,
        "thread_id": str(comment.thread_id),
        "org_id": str(comment.org_id),
        "author_user_id": str(comment.author_user_id) if comment.author_user_id else None,
        "author_kind": comment.author_kind,
        "author_name": None,
        "author_color": None,
        "body": comment.body,
        "attachments": comment.attachments or [],
        "metadata": comment.metadata_ or {},
        "created_at": comment.created_at.isoformat() if comment.created_at else None,
    }


async def _settle_thread_discussion_conversation_run_async(
    session,
    run: AgentRunRow,
) -> dict[str, Any] | None:
    """Settle a Discussion-origin run back into Discussion, never the AI Timeline."""
    if not _run_is_thread_discussion_conversation(run):
        return None
    if run.parent_run_id is not None:
        return None
    thread_id = _run_discussion_thread_id(run)
    org_id = str(getattr(run, "org_id", "") or "").strip()
    if not thread_id or not org_id:
        return None
    if await _discussion_reply_already_recorded(session, run=run, thread_id=thread_id):
        return None
    final_answer, artifact_id = await _latest_final_answer_artifact(session, run=run)
    if not final_answer:
        return None

    trigger = _run_discussion_trigger(run)
    response_target = trigger.get("response_target") if isinstance(trigger.get("response_target"), dict) else {}
    reply_to_comment_id = response_target.get("reply_to_comment_id") or trigger.get("comment_id")
    metadata: dict[str, Any] = {
        "source": "agent_run_final_answer",
        "surface": _THREAD_DISCUSSION_SURFACE,
        "created_by_run_id": int(run.id),
        "artifact_id": artifact_id,
    }
    if reply_to_comment_id not in (None, ""):
        metadata["reply_to_comment_id"] = reply_to_comment_id

    comment = ThreadDiscussionComment(
        thread_id=thread_id,
        org_id=org_id,
        author_user_id=None,
        author_kind="illo",
        body=final_answer,
        attachments=[],
        metadata_=metadata,
    )
    session.add(comment)
    if hasattr(session, "flush"):
        await session.flush()
    payload = _thread_discussion_comment_payload(comment)
    publish_safe(
        "thread_discussion_comment",
        {"idea_id": thread_id, "org_id": org_id, "comment": payload},
    )
    return {
        "surface": _THREAD_DISCUSSION_SURFACE,
        "idea_id": thread_id,
        "run_id": int(run.id),
        "comment_id": getattr(comment, "id", None),
    }


async def _slack_reply_already_recorded(
    session,
    *,
    run: AgentRunRow,
) -> bool:
    if not hasattr(session, "scalars"):
        return False
    try:
        result = await session.scalars(
            select(AgentRunEventRow)
            .where(
                AgentRunEventRow.run_id == int(run.id),
                AgentRunEventRow.event_type == "run.tool_completed",
            )
            .order_by(AgentRunEventRow.sequence_no.desc(), AgentRunEventRow.id.desc())
            .limit(100)
        )
        events = list(result.all())
    except Exception:
        return False
    for event in events:
        payload = getattr(event, "payload", None)
        if not isinstance(payload, dict):
            continue
        if str(payload.get("tool_name") or payload.get("tool") or "") != _SLACK_REPLY_TOOL:
            continue
        result_preview = str(payload.get("result") or "").lower()
        if '"error"' not in result_preview and "'error'" not in result_preview:
            return True
    return False


def _slack_response_target(run: AgentRunRow) -> dict[str, Any]:
    trigger = _run_slack_trigger(run)
    response_target = trigger.get("response_target") if isinstance(trigger.get("response_target"), dict) else {}
    channel_id = str(response_target.get("channel_id") or trigger.get("channel_id") or "").strip()
    thread_ts = response_target.get("thread_ts")
    if thread_ts is not None:
        thread_ts = str(thread_ts or "").strip() or None
    trigger_channel_type = str(trigger.get("channel_type") or "").strip()
    trigger_message_ts = str(trigger.get("message_ts") or "").strip()
    if trigger_channel_type == "im":
        thread_ts = None
    elif not response_target.get("thread_ts") and thread_ts == trigger_message_ts:
        thread_ts = None
    return {
        "channel_id": channel_id,
        "thread_ts": thread_ts,
        "trigger": trigger,
    }


async def _slack_client_for_run(run: AgentRunRow):
    from brain.systems.slack.client import SlackWebClient
    from brain.systems.vault.runtime_secrets import RuntimeSecretContext, read_runtime_secret

    token = await read_runtime_secret(
        "SLACK_BOT_TOKEN",
        context=RuntimeSecretContext(
            actor_user_id=str(getattr(run, "user_id", "") or "").strip() or None,
            org_id=str(getattr(run, "org_id", "") or "").strip() or None,
            run_id=int(run.id),
            idea_id=str(getattr(run, "thread_id", "") or "").strip() or None,
        ),
        reason="Report a completed Slack-origin Illo run back to Slack.",
        requested_by="slack_run_settlement",
        access="service",
        allow_env_fallback=True,
    )
    return SlackWebClient(token)


async def _clear_slack_processing_status(client: Any, trigger: dict[str, Any]) -> None:
    set_status = getattr(client, "set_assistant_status", None)
    if not callable(set_status):
        return
    channel_id = str(trigger.get("channel_id") or "").strip()
    thread_ts = str(trigger.get("thread_ts") or trigger.get("message_ts") or "").strip()
    if not channel_id or not thread_ts:
        return
    with suppress(Exception):
        await set_status(channel_id=channel_id, thread_ts=thread_ts, status="")


async def _settle_slack_origin_run_async(
    session,
    run: AgentRunRow,
) -> dict[str, Any] | None:
    if not _run_is_slack_origin(run):
        return None
    if _run_is_headless(run):
        return None
    final_answer, artifact_id = await _latest_final_answer_artifact(session, run=run)
    if not final_answer:
        return None
    if await _slack_reply_already_recorded(session, run=run):
        return None

    target = _slack_response_target(run)
    channel_id = target["channel_id"]
    if not channel_id:
        return None
    try:
        client = await _slack_client_for_run(run)
        response = await client.post_message(
            channel=channel_id,
            text=final_answer,
            thread_ts=target["thread_ts"],
        )
        await _clear_slack_processing_status(client, target["trigger"])
    except Exception as exc:
        logger.info("slack_final_answer_settlement_failed: %s", exc, extra={"run_id": int(run.id)})
        return None
    return {
        "surface": _SLACK_SURFACE,
        "run_id": int(run.id),
        "artifact_id": artifact_id,
        "channel_id": channel_id,
        "thread_ts": target["thread_ts"],
        "slack": response,
    }


async def _settle_terminal_root_run_async(session, run_id: int) -> dict[str, Any] | None:
    run = await session.get(AgentRunRow, int(run_id))
    if run is None:
        return None
    if run.parent_run_id is None and _run_is_thread_discussion_conversation(run):
        return await _settle_thread_discussion_conversation_run_async(session, run)
    if _run_is_slack_origin(run):
        slack_payload = await _settle_slack_origin_run_async(session, run)
        if run.parent_run_id is not None:
            return slack_payload
    return await _settle_idea_for_terminal_root_run_async(session, run_id)


async def _settle_idea_for_terminal_root_run_async(session, run_id: int) -> dict[str, Any] | None:
    run = await session.get(AgentRunRow, int(run_id))
    if run is None or run.parent_run_id is not None:
        return None
    if _run_is_thread_discussion_conversation(run):
        return None
    if not _run_should_mirror_final_answer_to_timeline(run):
        return None
    target_status = _TERMINAL_RUN_IDEA_STATUS.get(str(run.status or ""))
    if not target_status or not run.thread_id:
        return None
    try:
        idea_id = str(uuid.UUID(str(run.thread_id)))
    except (TypeError, ValueError, AttributeError):
        return None
    idea = await session.get(Idea, idea_id)
    if idea is None:
        return None
    old_status = str(idea.status or "")
    if old_status in PROTECTED_IDEA_STATUSES:
        return None
    final_answer, artifact_id = await _latest_unmirrored_final_answer(session, run=run, idea=idea)
    attachments = await _same_run_visible_attachments(session, run=run, thread_id=str(idea.id))
    settlement = await settle_terminal_run(
        session,
        idea=idea,
        command=TerminalRunSettlementCommand(
            run_id=int(run.id),
            run_status=str(run.status or ""),
            final_answer=final_answer,
            artifact_id=artifact_id,
            attachments=attachments,
        ),
    )
    return settlement.status_change


async def _finalize_cycle_run_if_needed_async(run_id: int, *, status: str, error: str | None = None) -> None:
    if status not in {"completed", "failed"}:
        return
    try:
        from brain.systems.cycles.service import async_finalize_cycle_run_from_run

        await async_finalize_cycle_run_from_run(int(run_id), status=status, error=error)
    except Exception:
        logger.exception("cycle_run_settlement_failed", extra={"run_id": run_id, "status": status})


def _run_has_project_context(run: AgentRunRow | None) -> bool:
    if run is None:
        return False
    target_ref = run.target_ref if isinstance(run.target_ref, dict) else {}
    workspace_ref = run.workspace_ref if isinstance(run.workspace_ref, dict) else {}
    for context in (
        target_ref.get("project_context_snapshot"),
        workspace_ref.get("project_context_snapshot"),
        {"resources": workspace_ref.get("resources")},
    ):
        if isinstance(context, dict) and project_context_has_materializable_resources(context):
            return True
    return False


def _project_context_root(run_id: int, *, thread_id: str | None = None) -> str:
    base = brain_config.resolve_workspace_root(default=Path(tempfile.gettempdir()) / "illo-agent-runs").expanduser()
    if thread_id:
        safe_thread_id = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in str(thread_id))[:120]
        if safe_thread_id:
            return str(base / "ideas" / safe_thread_id)
    return str(base / f"run-{int(run_id)}")


async def _async_record_project_activity(session, run_id: int, label: str, **payload: Any) -> None:
    run = await session.get(AgentRunRow, int(run_id))
    if run is None:
        return
    event = activity_event(
        int(run_id),
        label,
        root_run_id=run.root_run_id or int(run_id),
        **payload,
    )
    row = await AsyncAgentRunStore(session, auto_commit=True).append_event(event)
    stream_payload = dict(event.payload or {})
    stream_payload.update({
        "run_id": int(run_id),
        "root_run_id": int(run.root_run_id or run_id),
        "event_id": int(row.id),
        "run_event_id": int(row.id),
        "event_cursor": int(row.id),
        "sequence_no": int(row.sequence_no),
    })
    await _project_live_event_async(session, event.event_type, stream_payload)


async def _heartbeat_run_once_async(run_id: int, *, token: str, reason: str) -> bool:
    try:
        async with _unit_of_work_factory()() as uow:
            return await AsyncAgentRunStore(uow.session).heartbeat_run(
                int(run_id),
                token=token,
                reason=reason,
                min_interval_seconds=0,
            )
    except Exception:
        logger.debug("agent_run_heartbeat_failed", extra={"run_id": run_id}, exc_info=True)
        return False


def _heartbeat_run_once(run_id: int, *, token: str, reason: str) -> bool:
    return asyncio.run(_heartbeat_run_once_async(int(run_id), token=token, reason=reason))


@asynccontextmanager
async def _run_heartbeat_async(run_id: int):
    token = f"{os.getpid()}:{uuid.uuid4().hex}"
    stop_event = asyncio.Event()
    await _heartbeat_run_once_async(int(run_id), token=token, reason="runner_started")

    async def _loop_heartbeat() -> None:
        interval = _runner_heartbeat_interval_seconds()
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                await _heartbeat_run_once_async(int(run_id), token=token, reason="runner_running")

    task = asyncio.create_task(_loop_heartbeat(), name=f"agent-run-heartbeat-{int(run_id)}")
    try:
        yield token
    finally:
        stop_event.set()
        try:
            await asyncio.wait_for(task, timeout=min(2.0, _runner_heartbeat_interval_seconds()))
        except asyncio.TimeoutError:
            task.cancel()
        except asyncio.CancelledError:
            pass


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


async def _latest_event_times_for_rows_async(session, rows: list[AgentRunRow]) -> tuple[dict[int, datetime], dict[int, datetime]]:
    run_ids = {int(row.id) for row in rows}
    root_ids = {int(row.root_run_id or row.id) for row in rows}
    latest_by_run: dict[int, datetime] = {}
    latest_by_root: dict[int, datetime] = {}
    if run_ids:
        result = await session.execute(
            select(AgentRunEventRow.run_id, func.max(AgentRunEventRow.created_at))
            .where(AgentRunEventRow.run_id.in_(run_ids))
            .group_by(AgentRunEventRow.run_id)
        )
        for run_id, created_at in result:
            parsed = _normalize_datetime(created_at)
            if parsed is not None:
                latest_by_run[int(run_id)] = parsed
    if root_ids:
        result = await session.execute(
            select(AgentRunEventRow.root_run_id, func.max(AgentRunEventRow.created_at))
            .where(AgentRunEventRow.root_run_id.in_(root_ids))
            .group_by(AgentRunEventRow.root_run_id)
        )
        for root_id, created_at in result:
            parsed = _normalize_datetime(created_at)
            if parsed is not None:
                latest_by_root[int(root_id)] = parsed
    return latest_by_run, latest_by_root


async def _active_root_run_ids_for_children_async(session, rows: list[AgentRunRow]) -> set[int]:
    root_ids = {
        int(row.root_run_id)
        for row in rows
        if row.parent_run_id is not None and row.root_run_id is not None
    }
    if not root_ids:
        return set()
    result = await session.scalars(
        select(AgentRunRow.id).where(
            AgentRunRow.id.in_(root_ids),
            AgentRunRow.status.in_(_PROCESS_ACTIVE_STATUS_VALUES),
        )
    )
    return {int(run_id) for run_id in result}


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


async def reap_stale_active_runs(
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

    async with _unit_of_work_factory()() as uow:
        result = await uow.session.scalars(
            select(AgentRunRow)
            .where(
                AgentRunRow.status.in_(_PROCESS_ACTIVE_STATUS_VALUES),
                func.coalesce(AgentRunRow.updated_at, AgentRunRow.started_at, AgentRunRow.created_at) <= cutoff,
            )
            .order_by(func.coalesce(AgentRunRow.updated_at, AgentRunRow.started_at, AgentRunRow.created_at).asc())
            .limit(max(1, int(limit)))
        )
        rows = list(result.all())
        store = AsyncAgentRunStore(uow.session)
        latest_event_by_run, latest_event_by_root = await _latest_event_times_for_rows_async(uow.session, rows)
        active_root_run_ids = await _active_root_run_ids_for_children_async(uow.session, rows)
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
            event_row = await store.append_event(event)
            await store.set_status(int(row.id), RunStatus.FAILED, reason="runner_heartbeat_stale")
            await _project_live_event_async(uow.session, event.event_type, _event_stream_payload(event, event_row, row))
            status_payload = await _settle_terminal_root_run_async(uow.session, int(row.id))
            if status_payload:
                status_payloads.append(status_payload)
            reaped += 1

    for payload in status_payloads:
        publish_safe("status_change", payload)
    if reaped:
        logger.warning("agent_run_stale_reaped", extra={"count": reaped, "stale_after_seconds": stale_after_seconds})
    return reaped


async def _reap_stale_runs_if_due_async(*, force: bool = False) -> int:
    global _last_stale_reconcile_monotonic
    now = time.monotonic()
    if not force and now - _last_stale_reconcile_monotonic < _stale_reconcile_interval_sec:
        return 0
    _last_stale_reconcile_monotonic = now
    try:
        return await reap_stale_active_runs()
    except Exception:
        logger.exception("agent_run_stale_reconcile_failed")
        return 0


async def _queued_backlog_snapshot_async() -> tuple[int, datetime | None, int]:
    return await _shared_queued_backlog_snapshot_async()


def _forget_queued_watchdog_task(task: asyncio.Task[int]) -> None:
    _queued_watchdog_tasks.discard(task)
    if task.cancelled():
        return
    try:
        processed = task.result()
    except Exception:
        logger.exception("agent_run_queued_watchdog_failed")
        return
    if processed:
        logger.info("agent_run_queued_watchdog_processed", extra={"processed": processed})


async def _nudge_stale_queued_runs_if_due_async(*, force: bool = False) -> bool:
    global _last_queued_watchdog_monotonic
    now_monotonic = time.monotonic()
    if not force and now_monotonic - _last_queued_watchdog_monotonic < _queued_watchdog_interval_sec:
        return False
    _last_queued_watchdog_monotonic = now_monotonic

    if any(not task.done() for task in _queued_watchdog_tasks):
        return False

    try:
        queued_count, oldest_queued_at, active_count = await _queued_backlog_snapshot_async()
    except Exception:
        logger.exception("agent_run_queued_watchdog_snapshot_failed")
        return False
    if queued_count <= 0 or oldest_queued_at is None:
        return False
    if active_count >= _runner_concurrency():
        return False

    age_seconds = (datetime.now(timezone.utc) - oldest_queued_at).total_seconds()
    if age_seconds < _queued_watchdog_after_seconds():
        return False

    logger.warning(
        "agent_run_queued_watchdog_nudge",
        extra={
            "queued": queued_count,
            "oldest_queued_age_seconds": int(age_seconds),
            "active_runs": active_count,
        },
    )
    task = asyncio.create_task(
        _run_queued_once_async(limit=1),
        name="agent-runner-queued-watchdog",
    )
    _queued_watchdog_tasks.add(task)
    task.add_done_callback(_forget_queued_watchdog_task)
    return True


async def _async_materialize_project_context(run_id: int) -> tuple[bool, dict[str, Any] | None]:
    async with _unit_of_work_factory()() as uow:
        run = await uow.session.get(AgentRunRow, int(run_id))
        if not _run_has_project_context(run):
            return True, None
        await _async_record_project_activity(
            uow.session,
            int(run_id),
            "Preparing project context",
        )
        user_id = str(run.user_id) if run and run.user_id else None
        org_id = str(run.org_id) if run and run.org_id else None
        thread_id = str(run.thread_id) if run and run.thread_id else None

    result = await materialize_project_context_workspaces(
        int(run_id),
        workspace_root=_project_context_root(int(run_id), thread_id=thread_id),
        user_id=user_id,
        org_id=org_id,
    )
    async with _unit_of_work_factory()() as uow:
        await _async_record_project_activity(
            uow.session,
            int(run_id),
            "Project context ready" if result.ok else "Project context unavailable",
            workspaces=result.workspaces,
            errors=result.errors[:3],
        )
    if not result.ok:
        details = "; ".join(result.errors[:3]) or "No project workspace was materialized."
        message = f"Project Context unavailable: {details}"
        return False, await _mark_run_failed_after_runner_error_async(
            int(run_id),
            message,
            final_answer=(
                "I could not start this run because the selected Project Context did not "
                f"provide a usable workspace. {details}"
            ),
        )
    return True, None


def _materialize_project_context(run_id: int) -> tuple[bool, dict[str, Any] | None]:
    """Synchronous runner-thread boundary for async Project Context DB work."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_async_materialize_project_context(int(run_id)))
    raise RuntimeError("Project Context materialization cannot run inside an active event loop")


async def _mark_run_failed_after_runner_error_async(
    run_id: int,
    error: str,
    *,
    final_answer: str | None = None,
) -> dict[str, Any] | None:
    async with _unit_of_work_factory()() as uow:
        store = AsyncAgentRunStore(uow.session)
        row = await store.require_run(int(run_id))
        if coerce_run_status(row.status, default=RunStatus.FAILED) not in TERMINAL_RUN_STATUSES:
            if final_answer:
                await store.append_final_answer_once(int(run_id), final_answer, root_run_id=row.root_run_id)
                await store.append_event(
                    run_event(
                        int(run_id),
                        "run.text_completed",
                        {"text": final_answer},
                        root_run_id=row.root_run_id,
                    )
                )
            await store.append_event(run_event(int(run_id), "run.failed", {"error": error}, root_run_id=row.root_run_id))
            await store.set_status(int(run_id), RunStatus.FAILED, reason=error[:500])
        return await _settle_terminal_root_run_async(uow.session, int(run_id))


def _mark_run_failed_after_runner_error(
    run_id: int,
    error: str,
    *,
    final_answer: str | None = None,
) -> dict[str, Any] | None:
    return asyncio.run(
        _mark_run_failed_after_runner_error_async(
            int(run_id),
            error,
            final_answer=final_answer,
        )
    )


async def _run_queued_once_async(*, limit: int = 1) -> int:
    async with _unit_of_work_factory()() as uow:
        ids = await AsyncAgentRunStore(uow.session).claim_next_run_ids(limit=limit)
    processed = 0
    for run_id in ids:
        try:
            async with _run_heartbeat_async(int(run_id)):
                context_ready, status_payload = await _async_materialize_project_context(int(run_id))
                if not context_ready:
                    await _finalize_cycle_run_if_needed_async(
                        int(run_id),
                        status="failed",
                        error="Project Context unavailable",
                    )
                    if status_payload:
                        publish_safe("status_change", status_payload)
                    processed += 1
                    continue
                status_payload = None
                async with _unit_of_work_factory()() as uow:
                    completed_run = await _engine_for_session(uow.session).run_existing(int(run_id))
                    completed_status = str(getattr(completed_run.status, "value", completed_run.status) or "")
                    status_payload = await _settle_terminal_root_run_async(uow.session, int(run_id))
                await _finalize_cycle_run_if_needed_async(int(run_id), status=completed_status)
            if status_payload:
                publish_safe("status_change", status_payload)
            processed += 1
        except Exception:
            logger.exception("agent_run_failed", extra={"run_id": run_id})
            try:
                status_payload = await _mark_run_failed_after_runner_error_async(int(run_id), "runner_failed")
                await _finalize_cycle_run_if_needed_async(int(run_id), status="failed", error="runner_failed")
                if status_payload:
                    publish_safe("status_change", status_payload)
            except Exception:
                logger.exception("agent_run_failed_settlement_failed", extra={"run_id": run_id})
    return processed


def run_queued_once(*, limit: int = 1) -> int:
    return asyncio.run(_run_queued_once_async(limit=limit))


async def _sleep_or_stop(delay: float, slot_stop_event: asyncio.Event | None = None) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, delay)
    while not _stop_event.is_set() and not (slot_stop_event and slot_stop_event.is_set()):
        remaining = deadline - loop.time()
        if remaining <= 0:
            return
        await asyncio.sleep(min(0.1, remaining))


async def _loop(slot_stop_event: asyncio.Event | None = None) -> None:
    while not _stop_event.is_set() and not (slot_stop_event and slot_stop_event.is_set()):
        try:
            processed = await _run_queued_once_async()
        except Exception:
            logger.exception("agent_run_runner_slot_failed")
            processed = 0
        if not processed:
            await _sleep_or_stop(_poll_interval_sec, slot_stop_event)


def _start_runner_slot_locked() -> None:
    global _runner_thread_index
    _runner_thread_index += 1
    slot_stop_event = asyncio.Event()
    task = asyncio.create_task(
        _loop(slot_stop_event),
        name=f"agent-runner-{_runner_thread_index}",
    )
    _runner_slots.append((task, slot_stop_event))


def reconcile_runner_pool(*, allow_start: bool = False) -> int:
    desired = _runner_concurrency()
    if not allow_start and not (
        _runner_supervisor_thread and _runner_supervisor_thread.is_alive()
    ):
        return desired
    with _runner_lock:
        for task, stop_event in _runner_slots:
            if task.done() and not task.cancelled() and not stop_event.is_set():
                exc = task.exception()
                if exc is not None:
                    logger.error(
                        "agent_run_runner_slot_crashed",
                        extra={"task": task.get_name()},
                        exc_info=(type(exc), exc, exc.__traceback__),
                    )
        active_slots = [
            (task, stop_event)
            for task, stop_event in _runner_slots
            if not task.done() and not stop_event.is_set()
        ]
        retiring_slots = [
            (task, stop_event)
            for task, stop_event in _runner_slots
            if not task.done() and stop_event.is_set()
        ]
        _runner_slots[:] = active_slots + retiring_slots
        if len(active_slots) > desired:
            for _task, stop_event in active_slots[desired:]:
                stop_event.set()
            active_slots = active_slots[:desired]
        while len(active_slots) < desired:
            _start_runner_slot_locked()
            active_slots = [
                (task, stop_event)
                for task, stop_event in _runner_slots
                if not task.done() and not stop_event.is_set()
            ]
    return desired


async def _supervisor_async_loop() -> None:
    first_reconcile = True
    try:
        while not _stop_event.is_set():
            try:
                reconcile_runner_pool(allow_start=True)
                await _reap_stale_runs_if_due_async(force=first_reconcile)
                await _nudge_stale_queued_runs_if_due_async(force=first_reconcile)
                first_reconcile = False
            except Exception:
                logger.exception("agent_run_runner_reconcile_failed")
            await _sleep_or_stop(_runner_reconcile_interval_sec)
    finally:
        with _runner_lock:
            slots = list(_runner_slots)
        for _task, stop_event in slots:
            stop_event.set()
        live_tasks = [task for task, _stop_event in slots if not task.done()]
        if live_tasks:
            done, pending = await asyncio.wait(
                live_tasks,
                timeout=max(1.0, _runner_reconcile_interval_sec),
            )
            for task in pending:
                task.cancel()
            if pending:
                cancelled, still_pending = await asyncio.wait(pending, timeout=1.0)
                for task in still_pending:
                    logger.warning(
                        "agent_run_runner_slot_shutdown_timeout",
                        extra={"task": task.get_name()},
                    )
                done.update(cancelled)
            for task in done:
                with suppress(asyncio.CancelledError, Exception):
                    await task
        queued_watchdogs = [task for task in _queued_watchdog_tasks if not task.done()]
        for task in queued_watchdogs:
            task.cancel()
        if queued_watchdogs:
            done, pending = await asyncio.wait(queued_watchdogs, timeout=1.0)
            for task in pending:
                logger.warning(
                    "agent_run_queued_watchdog_shutdown_timeout",
                    extra={"task": task.get_name()},
                )
            for task in done:
                with suppress(asyncio.CancelledError, Exception):
                    await task
        _queued_watchdog_tasks.clear()
        with _runner_lock:
            _runner_slots.clear()


def _supervisor_loop() -> None:
    asyncio.run(_supervisor_async_loop())


def start_runner() -> None:
    global _runner_supervisor_thread
    if _runner_supervisor_thread and _runner_supervisor_thread.is_alive():
        return
    _stop_event.clear()
    concurrency = _runner_concurrency()
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

    deadline = None
    if drain_timeout_seconds is not None:
        deadline = time.monotonic() + max(0.0, float(drain_timeout_seconds))

    if _runner_supervisor_thread and _runner_supervisor_thread.is_alive():
        remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
        _runner_supervisor_thread.join(timeout=remaining)
    if not (_runner_supervisor_thread and _runner_supervisor_thread.is_alive()):
        _runner_supervisor_thread = None


async def queue_status_async(*, consumer_running: bool | None = None, org_id: str | None = None) -> dict[str, Any]:
    async with _unit_of_work_factory()() as uow:
        stmt = select(AgentRunRow.status, func.count()).group_by(AgentRunRow.status)
        if org_id:
            stmt = stmt.where(AgentRunRow.org_id == org_id)
        result = await uow.session.execute(stmt)
        counts = {str(status): int(count) for status, count in result.all()}
    active_runner_count = _active_runner_count()
    return {
        "runner_running": bool(active_runner_count),
        "runner_concurrency": active_runner_count,
        "runner_configured_concurrency": _runner_concurrency(),
        "event_consumer_running": consumer_running,
        "counts": counts,
        "queued": counts.get("queued", 0),
        "running": sum(counts.get(status, 0) for status in ACTIVE_RUN_STATUS_VALUES),
    }


def queue_status(*, consumer_running: bool | None = None, org_id: str | None = None) -> dict[str, Any]:
    return asyncio.run(queue_status_async(consumer_running=consumer_running, org_id=org_id))


__all__ = [
    "queue_status",
    "queue_status_async",
    "reap_stale_active_runs",
    "runner_health_snapshot",
    "run_queued_once",
    "start_runner",
    "stop_runner",
]
