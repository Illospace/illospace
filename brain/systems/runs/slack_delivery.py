"""Shared Slack delivery boundary for AgentRun settlement messages."""

from __future__ import annotations

from contextlib import suppress
import logging
from typing import TYPE_CHECKING, Any

from brain.systems.runs.events import run_event
from brain.systems.runs.store import AsyncAgentRunStore

if TYPE_CHECKING:
    from brain.systems.slack.thread_mute import ThreadPostMute


logger = logging.getLogger(__name__)

SLACK_SURFACE = "slack"


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


__all__ = [
    "SLACK_SURFACE",
    "clear_slack_processing_status",
    "is_slack_origin",
    "post_slack_run_message",
    "record_slack_thread_mute",
    "run_is_headless",
    "slack_client_for_run",
    "slack_response_target",
    "slack_trigger",
]
