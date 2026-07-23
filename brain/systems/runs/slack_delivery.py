"""Shared Slack delivery boundary for AgentRun settlement messages."""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING, Any

from brain.systems.runs.events import run_event
from brain.systems.runs.status import RunStatus, coerce_run_status
from brain.systems.runs.store import AsyncAgentRunStore

if TYPE_CHECKING:
    from brain.systems.slack.thread_mute import ThreadPostMute


logger = logging.getLogger(__name__)

SLACK_SURFACE = "slack"


@dataclass(frozen=True)
class OpenAskArtifact:
    """A durable result that can answer a matching human ask."""

    kind: str
    reference: str
    title: str | None = None
    url: str | None = None


def _run_context_maps(run: Any):
    target_ref = getattr(run, "target_ref", None)
    metadata = getattr(run, "metadata_", None)
    if not isinstance(metadata, dict):
        metadata = getattr(run, "metadata", None)
    for container in (target_ref, metadata):
        if isinstance(container, dict) and container:
            yield container


def run_is_headless(run: Any) -> bool:
    return any(bool(container.get("headless")) for container in _run_context_maps(run))


def slack_trigger(run: Any) -> dict[str, Any]:
    for container in _run_context_maps(run):
        trigger = container.get("slack_trigger")
        if isinstance(trigger, dict):
            return dict(trigger)
    return {}


def is_slack_origin(run: Any) -> bool:
    if slack_trigger(run):
        return True
    for container in _run_context_maps(run):
        if container.get("originating_surface") == SLACK_SURFACE:
            return True
        if container.get("source_surface") == SLACK_SURFACE:
            return True
        if container.get("final_answer_target_surface") == SLACK_SURFACE:
            return True
    return False


def slack_response_target(run: Any) -> dict[str, Any]:
    trigger = slack_trigger(run)
    response_target = (
        trigger.get("response_target")
        if isinstance(trigger.get("response_target"), dict)
        else {}
    )
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


async def slack_client_for_run(run: Any):
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


async def slack_client_for_open_ask(open_ask: Any, *, answering_run_id: int | None):
    from brain.systems.slack.client import SlackWebClient
    from brain.systems.vault.runtime_secrets import RuntimeSecretContext, read_runtime_secret

    run_id = int(answering_run_id or getattr(open_ask, "origin_run_id", 0) or 0)
    token = await read_runtime_secret(
        "SLACK_BOT_TOKEN",
        context=RuntimeSecretContext(
            actor_user_id=(
                str(getattr(open_ask, "requester_user_id", "") or "").strip()
                or None
            ),
            org_id=str(getattr(open_ask, "org_id", "") or "").strip() or None,
            run_id=run_id or None,
            idea_id=None,
        ),
        reason="Deliver a durable artifact back to the human's originating Slack ask.",
        requested_by="open_ask_reply_back",
        access="service",
        allow_env_fallback=True,
    )
    return SlackWebClient(token)


async def clear_slack_processing_status(client: Any, trigger: dict[str, Any]) -> None:
    set_status = getattr(client, "set_assistant_status", None)
    if not callable(set_status):
        return
    channel_id = str(trigger.get("channel_id") or "").strip()
    thread_ts = str(trigger.get("thread_ts") or trigger.get("message_ts") or "").strip()
    if not channel_id or not thread_ts:
        return
    with suppress(Exception):
        await set_status(channel_id=channel_id, thread_ts=thread_ts, status="")


async def record_slack_thread_mute(
    session,
    *,
    run: Any,
    mute: ThreadPostMute,
    channel_id: str,
    thread_ts: str,
) -> None:
    await AsyncAgentRunStore(session).append_event(
        run_event(
            int(run.id),
            "run.slack_post_suppressed",
            {
                "reason": "thread_post_muted",
                "ledger_line": mute.ledger_line,
                "muted_by": mute.user,
                "muted_at": mute.ts,
                "channel_id": channel_id,
                "thread_ts": thread_ts,
            },
            root_run_id=run.root_run_id,
        )
    )


async def post_slack_run_message(
    session,
    *,
    run: Any,
    text: str,
    artifact_id: int | None = None,
) -> dict[str, Any] | None:
    target = slack_response_target(run)
    channel_id = target["channel_id"]
    if not channel_id:
        return None
    try:
        client = await slack_client_for_run(run)
        if target["thread_ts"]:
            from brain.systems.slack.thread_mute import read_thread_post_mute

            mute = await read_thread_post_mute(
                client,
                channel_id=channel_id,
                thread_ts=target["thread_ts"],
                illo_user_id=str(target["trigger"].get("bot_user_id") or "").strip() or None,
            )
            if mute is not None:
                await clear_slack_processing_status(client, target["trigger"])
                await record_slack_thread_mute(
                    session,
                    run=run,
                    mute=mute,
                    channel_id=channel_id,
                    thread_ts=target["thread_ts"],
                )
                return {
                    "surface": SLACK_SURFACE,
                    "run_id": int(run.id),
                    "artifact_id": artifact_id,
                    "channel_id": channel_id,
                    "thread_ts": target["thread_ts"],
                    "suppressed": True,
                    "ledger_line": mute.ledger_line,
                }
        response = await client.post_message(
            channel=channel_id,
            text=text,
            thread_ts=target["thread_ts"],
        )
        await clear_slack_processing_status(client, target["trigger"])
        if (
            coerce_run_status(
                getattr(run, "status", None),
                default=RunStatus.FAILED,
            )
            == RunStatus.COMPLETED
        ):
            from brain.systems.runs.open_asks import mark_origin_run_answer_delivered

            try:
                await mark_origin_run_answer_delivered(
                    session,
                    origin_run_id=int(run.id),
                    answer_text=text,
                    slack_response=response,
                )
            except Exception as exc:
                logger.info(
                    "open_ask_run_answer_recording_failed: %s",
                    exc,
                    extra={"run_id": int(run.id)},
                )
    except Exception as exc:
        logger.info("slack_run_message_failed: %s", exc, extra={"run_id": int(run.id)})
        return None
    return {
        "surface": SLACK_SURFACE,
        "run_id": int(run.id),
        "artifact_id": artifact_id,
        "channel_id": channel_id,
        "thread_ts": target["thread_ts"],
        "slack": response,
    }


def _artifact_value(artifact: OpenAskArtifact | dict[str, Any], key: str) -> str:
    if isinstance(artifact, OpenAskArtifact):
        value = getattr(artifact, key)
    else:
        value = artifact.get(key)
    return str(value or "").strip()


def _quoted_ask(ask_text: str) -> str:
    lines = str(ask_text or "").splitlines() or [""]
    return "\n".join(f"> {line}" for line in lines)


def open_ask_artifact_message(
    open_ask: Any,
    artifact: OpenAskArtifact | dict[str, Any],
) -> str:
    requester_name = str(
        getattr(open_ask, "requester_name", None)
        or f"<@{getattr(open_ask, 'requester_slack_id', '')}>"
    ).strip()
    requester_slack_id = str(
        getattr(open_ask, "requester_slack_id", "") or ""
    ).strip()
    requester = requester_name
    if requester_slack_id and f"<@{requester_slack_id}>" not in requester:
        requester = f"{requester_name} (<@{requester_slack_id}>)"

    kind = _artifact_value(artifact, "kind") or "artifact"
    reference = _artifact_value(artifact, "reference")
    title = _artifact_value(artifact, "title")
    url = _artifact_value(artifact, "url")
    mechanism = f"{kind} {reference}".strip()
    if title:
        mechanism = f"{mechanism} — {title}"
    if url:
        mechanism = f"{mechanism}\n{url}"
    return (
        f"*Open ask answered for {requester}*\n"
        f"*Originating request:*\n{_quoted_ask(getattr(open_ask, 'ask_text', ''))}\n\n"
        f"*Mechanism:* {mechanism}"
    )


def _open_ask_thread_ts(open_ask: Any) -> str | None:
    channel_type = str(getattr(open_ask, "channel_type", "") or "").strip()
    if channel_type == "im":
        return None
    return str(getattr(open_ask, "thread_ts", "") or "").strip() or None


async def post_open_ask_artifact_reply(
    session,
    *,
    origin_ref: str,
    artifact: OpenAskArtifact | dict[str, Any],
    answering_run_id: int | None,
    client: Any | None = None,
) -> dict[str, Any]:
    """Reply to every matching open ask and close only confirmed deliveries."""

    from brain.systems.runs.open_asks import (
        mark_open_ask_answered,
        open_asks_for_origin_ref,
    )

    rows = await open_asks_for_origin_ref(
        session,
        origin_ref,
        for_update=True,
    )
    result: dict[str, Any] = {
        "origin_ref": str(origin_ref),
        "matched": len(rows),
        "delivered": 0,
        "origin_asks": [],
    }
    for row in rows:
        channel_id = str(row.channel_id)
        thread_ts = _open_ask_thread_ts(row)
        text = open_ask_artifact_message(row, artifact)
        active_client = client
        try:
            if active_client is None:
                active_client = await slack_client_for_open_ask(
                    row,
                    answering_run_id=answering_run_id,
                )
            if thread_ts:
                from brain.systems.slack.thread_mute import read_thread_post_mute

                mute = await read_thread_post_mute(
                    active_client,
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                    illo_user_id=str(row.bot_user_id or "").strip() or None,
                )
                if mute is not None:
                    result["origin_asks"].append(
                        {
                            "open_ask_id": row.id,
                            "requester": row.requester_name,
                            "ask": row.ask_text,
                            "channel_id": channel_id,
                            "thread_ts": thread_ts,
                            "delivered": False,
                            "suppressed": True,
                            "ledger_line": mute.ledger_line,
                        }
                    )
                    continue
            response = await active_client.post_message(
                channel=channel_id,
                text=text,
                thread_ts=thread_ts,
            )
            await clear_slack_processing_status(
                active_client,
                {
                    "channel_id": channel_id,
                    "thread_ts": row.thread_ts,
                    "message_ts": row.thread_ts,
                },
            )
        except Exception as exc:
            logger.info(
                "open_ask_reply_back_failed: %s",
                exc,
                extra={
                    "open_ask_id": row.id,
                    "answering_run_id": answering_run_id,
                },
            )
            result["origin_asks"].append(
                {
                    "open_ask_id": row.id,
                    "requester": row.requester_name,
                    "ask": row.ask_text,
                    "channel_id": channel_id,
                    "thread_ts": thread_ts,
                    "delivered": False,
                    "error": str(exc),
                }
            )
            continue

        artifact_kind = _artifact_value(artifact, "kind")
        artifact_ref = (
            _artifact_value(artifact, "url")
            or _artifact_value(artifact, "reference")
        )
        mark_open_ask_answered(
            row,
            answer_text=text,
            answered_by_run_id=answering_run_id,
            artifact_kind=artifact_kind,
            artifact_ref=artifact_ref,
            slack_response=response,
        )
        result["delivered"] += 1
        result["origin_asks"].append(
            {
                "open_ask_id": row.id,
                "requester": row.requester_name,
                "requester_slack_id": row.requester_slack_id,
                "ask": row.ask_text,
                "channel_id": channel_id,
                "thread_ts": thread_ts,
                "thread_permalink": row.thread_permalink,
                "mechanism": {
                    "kind": artifact_kind,
                    "reference": _artifact_value(artifact, "reference"),
                    "title": _artifact_value(artifact, "title") or None,
                    "url": _artifact_value(artifact, "url") or None,
                },
                "announcement": text,
                "delivered": True,
                "slack": response,
            }
        )
    return result


async def deliver_open_ask_artifact_reply(
    *,
    origin_ref: str | None,
    artifact: OpenAskArtifact | dict[str, Any],
    answering_run_id: int | None,
) -> dict[str, Any] | None:
    """Unit-of-work wrapper for artifact handlers that have already committed."""

    normalized_ref = str(origin_ref or "").strip()
    if not normalized_ref:
        return None
    from brain.platform.db.repositories.unit_of_work import UnitOfWork

    try:
        async with UnitOfWork() as uow:
            return await post_open_ask_artifact_reply(
                uow.session,
                origin_ref=normalized_ref,
                artifact=artifact,
                answering_run_id=answering_run_id,
            )
    except Exception as exc:
        logger.info(
            "open_ask_artifact_delivery_failed: %s",
            exc,
            extra={"answering_run_id": answering_run_id},
        )
        return {
            "origin_ref": normalized_ref,
            "matched": None,
            "delivered": 0,
            "error": str(exc),
        }


async def record_origin_run_answer_delivery(
    *,
    origin_run_id: int | None,
    answer_text: str,
    slack_response: Any,
) -> int:
    """Close a run's ask after an explicit Slack answer tool confirms delivery."""

    if not origin_run_id:
        return 0
    from brain.platform.db.repositories.unit_of_work import UnitOfWork
    from brain.systems.runs.open_asks import mark_origin_run_answer_delivered

    try:
        async with UnitOfWork() as uow:
            rows = await mark_origin_run_answer_delivered(
                uow.session,
                origin_run_id=int(origin_run_id),
                answer_text=answer_text,
                slack_response=slack_response,
            )
            return len(rows)
    except Exception as exc:
        logger.info(
            "open_ask_explicit_answer_recording_failed: %s",
            exc,
            extra={"origin_run_id": origin_run_id},
        )
        return 0


__all__ = [
    "OpenAskArtifact",
    "SLACK_SURFACE",
    "clear_slack_processing_status",
    "deliver_open_ask_artifact_reply",
    "is_slack_origin",
    "open_ask_artifact_message",
    "post_open_ask_artifact_reply",
    "post_slack_run_message",
    "record_origin_run_answer_delivery",
    "record_slack_thread_mute",
    "run_is_headless",
    "slack_client_for_open_ask",
    "slack_client_for_run",
    "slack_response_target",
    "slack_trigger",
]
