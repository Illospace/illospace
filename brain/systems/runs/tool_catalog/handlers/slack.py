"""Slack tool handlers for AgentRun."""

from __future__ import annotations

import asyncio
from functools import partial
import json
import re
from typing import Any

from brain.platform.provider_alerts import (
    ProviderAlertPolicyError,
    classify_provider_alert_body,
)
from brain.systems.cycles.exception_ping import (
    cycle_exception_ping_context,
    exception_ping_payload_required,
    record_exception_ping_metadata_skip,
    slack_mentioned_teammates,
)
from brain.systems.runs.execution_context import get_or_create_agent_run_state
from brain.systems.runs.tool_catalog.handlers.common import _agent_context, _current_runtime_secret_context
from brain.systems.slack.client import SlackApiError, SlackDeliveryError
from brain.systems.slack.exception_ping_posting import post_exception_ping
from brain.systems.slack.provider_alert_posting import post_provider_alert
from brain.systems.slack.thread_mute import read_thread_post_mute
from brain.systems.slack.uploads import slack_image_upload_from_data_url

def _execution_metadata() -> dict[str, Any]:
    metadata = getattr(_agent_context, "execution_metadata", None)
    return metadata if isinstance(metadata, dict) else {}


def _metadata_trigger(name: str) -> dict[str, Any]:
    trigger = _execution_metadata().get(name)
    return dict(trigger) if isinstance(trigger, dict) else {}


def _current_target_ref() -> dict[str, Any]:
    target_ref = getattr(_agent_context, "target_ref", None)
    if isinstance(target_ref, dict):
        return dict(target_ref)
    target_ref = _execution_metadata().get("target_ref")
    return dict(target_ref) if isinstance(target_ref, dict) else {}


def _current_slack_trigger() -> dict[str, Any]:
    trigger = getattr(_agent_context, "slack_trigger", None)
    if isinstance(trigger, dict):
        return dict(trigger)
    trigger = _metadata_trigger("slack_trigger")
    if trigger:
        return trigger
    target_ref = _current_target_ref()
    if isinstance(target_ref.get("slack_trigger"), dict):
        return dict(target_ref["slack_trigger"])
    return {}


def _coerce_slack_limit(value: Any, default: int = 50) -> int:
    try:
        return max(1, min(int(value or default), 200))
    except (TypeError, ValueError):
        return default


def _coerce_slack_list_limit(value: Any, default: int = 200) -> int:
    try:
        return max(1, min(int(value or default), 1000))
    except (TypeError, ValueError):
        return default


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y", "on"}:
            return True
        if normalized in {"0", "false", "no", "n", "off"}:
            return False
    return bool(value)


_SLACK_EMOJI_NAME_PATTERN = re.compile(r"^[a-z0-9_+\-]{1,80}$")


def _normalize_slack_emoji_name(value: Any) -> str:
    emoji = str(value or "").strip().strip(":").lower()
    if not emoji:
        raise ValueError("react_to_slack_message requires an emoji name")
    if not _SLACK_EMOJI_NAME_PATTERN.fullmatch(emoji):
        raise ValueError(
            "react_to_slack_message emoji must be a Slack emoji name without colons"
        )
    return emoji


async def _durable_slack_reaction_state(
    *,
    channel_id: str,
    message_ts: str,
    emoji: str,
) -> dict[str, Any]:
    """Read persisted action manifests to survive run recovery and replay."""

    run_id = _execution_metadata().get("run_id")
    try:
        run_id = int(run_id or 0)
    except (TypeError, ValueError):
        run_id = 0
    if run_id <= 0:
        return {"enabled": False}

    from sqlalchemy import select

    from brain.platform.db.models.agent_run import ActionManifestRow
    from brain.platform.db.repositories.unit_of_work import UnitOfWork

    try:
        async with UnitOfWork() as uow:
            rows = list(
                (
                    await uow.session.scalars(
                        select(ActionManifestRow).where(
                            ActionManifestRow.run_id == run_id,
                            ActionManifestRow.tool_name == "react_to_slack_message",
                        )
                    )
                ).all()
            )
    except Exception as exc:
        return {"enabled": True, "error": str(exc)}

    conflicting_emoji = None
    already_succeeded = False
    for row in rows:
        target = getattr(row, "target", None)
        target = dict(target) if isinstance(target, dict) else {}
        target_channel = str(target.get("channel_id") or "").strip()
        target_ts = str(target.get("message_ts") or "").strip()
        if target_channel and target_channel != channel_id:
            continue
        if target_ts and target_ts != message_ts:
            continue
        attempted_emoji = str(target.get("emoji") or "").strip().strip(":").lower()
        if not attempted_emoji:
            continue
        if attempted_emoji != emoji:
            conflicting_emoji = attempted_emoji
            break
        if str(getattr(row, "outcome_status", "") or "") == "succeeded":
            already_succeeded = True
    return {
        "enabled": True,
        "conflicting_emoji": conflicting_emoji,
        "already_succeeded": already_succeeded,
    }


def _normalize_slack_channel_types(value: Any) -> str:
    allowed = {"public_channel", "private_channel", "mpim", "im"}
    if value is None:
        requested = ["public_channel", "private_channel", "mpim", "im"]
    elif isinstance(value, str):
        requested = [part.strip() for part in value.split(",")]
    else:
        requested = [str(part).strip() for part in value]
    normalized = [part for part in requested if part in allowed]
    return ",".join(dict.fromkeys(normalized)) or "public_channel,private_channel,mpim,im"


def _slack_channel_payload(channel: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": channel.get("id"),
        "name": channel.get("name") or channel.get("user"),
        "is_channel": channel.get("is_channel"),
        "is_group": channel.get("is_group"),
        "is_im": channel.get("is_im"),
        "is_mpim": channel.get("is_mpim"),
        "is_private": channel.get("is_private"),
        "is_member": channel.get("is_member"),
        "is_archived": channel.get("is_archived"),
        "num_members": channel.get("num_members"),
        "topic": channel.get("topic") if isinstance(channel.get("topic"), dict) else None,
        "purpose": channel.get("purpose") if isinstance(channel.get("purpose"), dict) else None,
    }


def _slack_connection_online(connection: Any) -> bool:
    return str(getattr(connection, "status", "") or "").strip().lower() in {"connected", "online"}


def _slack_event_type_to_channel_type(channel_type: str, channel_id: str = "") -> str:
    normalized = str(channel_type or "").strip().lower()
    if normalized in {"public_channel", "private_channel", "mpim", "im"}:
        return normalized
    if normalized == "channel":
        return "public_channel"
    if normalized == "group":
        return "private_channel"
    if normalized:
        return normalized
    if channel_id.startswith("D"):
        return "im"
    if channel_id.startswith("G"):
        return "private_channel"
    return "public_channel"


def _observed_slack_channel_payload(event: Any) -> dict[str, Any] | None:
    payload = getattr(event, "raw_payload", None)
    if not isinstance(payload, dict):
        return None
    channel_id = str(payload.get("channel_id") or "").strip()
    if not channel_id:
        return None
    channel_type = str(payload.get("channel_type") or "").strip()
    slack_channel_type = _slack_event_type_to_channel_type(channel_type, channel_id)
    is_private_channel = slack_channel_type == "private_channel"
    observed_at = getattr(event, "created_at", None)
    return {
        "id": channel_id,
        "name": str(payload.get("channel_name") or "").strip() or None,
        "is_channel": slack_channel_type in {"public_channel", "private_channel"},
        "is_group": is_private_channel,
        "is_im": slack_channel_type == "im",
        "is_mpim": slack_channel_type == "mpim",
        "is_private": is_private_channel,
        "is_member": True,
        "is_archived": None,
        "num_members": None,
        "topic": None,
        "purpose": None,
        "source": "observed_slack_event",
        "channel_type": channel_type or None,
        "slack_channel_type": slack_channel_type,
        "permalink": payload.get("permalink") or None,
        "observed_at": observed_at.isoformat() if observed_at else None,
    }


async def _observed_slack_channels_for_connection(
    session,
    *,
    org_id: str,
    connection_id: str,
    requested_types: set[str],
    limit: int = 100,
) -> list[dict[str, Any]]:
    from sqlalchemy import select

    from brain.platform.db.models.inbound import InboundEventRow

    stmt = (
        select(InboundEventRow)
        .where(
            InboundEventRow.org_id == str(org_id),
            InboundEventRow.connection_id == str(connection_id),
            InboundEventRow.kind == "slack_message",
        )
        .order_by(InboundEventRow.created_at.desc(), InboundEventRow.id.desc())
        .limit(max(1, min(int(limit or 100), 500)))
    )
    observed: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for event in list((await session.scalars(stmt)).all()):
        channel = _observed_slack_channel_payload(event)
        if not channel:
            continue
        channel_id = str(channel.get("id") or "")
        if channel_id in seen_ids:
            continue
        if str(channel.get("slack_channel_type") or "") not in requested_types:
            continue
        seen_ids.add(channel_id)
        observed.append(channel)
    return observed


async def _slack_client_from_runtime():
    from brain.systems.slack.client import SlackWebClient
    from brain.systems.vault.runtime_secrets import read_runtime_secret

    token = await read_runtime_secret(
        "SLACK_BOT_TOKEN",
        context=_current_runtime_secret_context(),
        reason="Use the configured Slack app to read and reply from Illo's Slack teammate surface.",
        requested_by="slack_runtime_tool",
        access="service",
        allow_env_fallback=True,
    )
    return SlackWebClient(token)


def _looks_like_slack_user_id(value: str) -> bool:
    return value.startswith(("U", "W"))


async def _resolve_post_channel(client, channel_id: str, visibility: str) -> str:
    if visibility == "ephemeral" or not _looks_like_slack_user_id(channel_id):
        return channel_id
    response = await client.open_conversation(users=channel_id)
    channel = response.get("channel") if isinstance(response.get("channel"), dict) else {}
    resolved = str(channel.get("id") or "").strip()
    return resolved or channel_id


async def _clear_processing_status(client: Any, trigger: dict[str, Any]) -> None:
    set_status = getattr(client, "set_assistant_status", None)
    if not callable(set_status):
        return
    channel_id = str(trigger.get("channel_id") or "").strip()
    thread_ts = str(trigger.get("thread_ts") or trigger.get("message_ts") or "").strip()
    if not channel_id or not thread_ts:
        return
    try:
        await set_status(channel_id=channel_id, thread_ts=thread_ts, status="")
    except Exception:
        return


async def _post_slack_content(
    client: Any,
    channel_id: str,
    *,
    text: str,
    thread_ts: str | None,
    visibility: str,
    user_id: str | None,
    trigger: dict[str, Any],
    image_upload: Any,
) -> tuple[Any, str | None]:
    if image_upload is not None:
        response = await client.upload_file(
            channel=channel_id,
            file_bytes=image_upload.file_bytes,
            filename=image_upload.filename,
            title=image_upload.title,
            initial_comment=text if text.strip() else None,
            thread_ts=thread_ts,
            alt_txt=image_upload.alt_txt,
            content_type=image_upload.content_type,
        )
    elif visibility == "ephemeral":
        target_user = str(user_id or trigger.get("slack_user_id") or "").strip()
        if not target_user:
            return None, json.dumps({"error": "ephemeral Slack replies require a Slack user id"})
        response = await client.post_ephemeral(
            channel=channel_id,
            user=target_user,
            text=text,
            thread_ts=thread_ts,
        )
    else:
        response = await client.post_message(
            channel=channel_id,
            text=text,
            thread_ts=thread_ts,
        )
    return response, None


async def _handle_post_slack_reply(
    body: str = "",
    channel_id: str | None = None,
    thread_ts: str | None = None,
    visibility: str | None = None,
    user_id: str | None = None,
    image_data: str | None = None,
    image_filename: str | None = None,
    image_title: str | None = None,
    image_alt: str | None = None,
    answers_open_ask: bool = False,
    exception_ping: dict[str, Any] | None = None,
) -> str:
    """Post an Illo-authored reply to the originating Slack surface."""

    answers_open_ask = _coerce_bool(answers_open_ask, default=False)
    submitted_text = str(body or "")
    text = submitted_text
    try:
        image_upload = slack_image_upload_from_data_url(
            image_data,
            filename=image_filename,
            title=image_title,
            alt_txt=image_alt,
        )
    except ValueError as exc:
        return json.dumps({"error": str(exc)})
    if not text.strip() and image_upload is None:
        return json.dumps({"error": "post_slack_reply requires body or image_data"})

    # Coordinator-side generators should apply the same checked-in map while
    # composing summaries.  This posting-path gate is still the authority, so a
    # fresh run cannot ship a generated false-HIGH provider alert.
    try:
        alert_decision = classify_provider_alert_body(text)
    except ProviderAlertPolicyError as exc:
        return json.dumps(
            {
                "ok": False,
                "posted": False,
                "error": "provider_alert_policy_unavailable",
                "detail": str(exc),
                "submitted_chars": len(submitted_text),
                "posted_chars": 0,
            }
        )
    if alert_decision is not None:
        text = alert_decision.body

    trigger = _current_slack_trigger()
    response_target = trigger.get("response_target") if isinstance(trigger.get("response_target"), dict) else {}
    target_channel = str(channel_id or response_target.get("channel_id") or trigger.get("channel_id") or "").strip()
    target_thread_ts = thread_ts if thread_ts is not None else response_target.get("thread_ts")
    if target_thread_ts is not None:
        target_thread_ts = str(target_thread_ts or "").strip() or None
    trigger_channel_id = str(trigger.get("channel_id") or "").strip()
    trigger_message_ts = str(trigger.get("message_ts") or "").strip()
    trigger_channel_type = str(trigger.get("channel_type") or "").strip()
    response_target_thread_ts = response_target.get("thread_ts")
    if target_channel == trigger_channel_id:
        if trigger_channel_type == "im":
            target_thread_ts = None
        elif not response_target_thread_ts and target_thread_ts == trigger_message_ts:
            target_thread_ts = None
    target_visibility = str(visibility or response_target.get("visibility") or "public").strip().lower()
    if target_visibility not in {"public", "ephemeral"}:
        return json.dumps({"error": "post_slack_reply visibility must be public or ephemeral"})
    if image_upload is not None and target_visibility == "ephemeral":
        return json.dumps({"error": "post_slack_reply image uploads must be public"})
    if not target_channel:
        return json.dumps({"error": "post_slack_reply requires channel_id outside a Slack-triggered run"})

    cycle_ping_context = cycle_exception_ping_context(_execution_metadata())
    ping_payload = dict(exception_ping) if isinstance(exception_ping, dict) else None
    if exception_ping is not None and ping_payload is None:
        return json.dumps(
            {
                "ok": False,
                "posted": False,
                "error": "invalid_exception_ping",
                "detail": "exception_ping must be an object",
            }
        )
    if ping_payload is not None and cycle_ping_context is None:
        return json.dumps(
            {
                "ok": False,
                "posted": False,
                "error": "invalid_exception_ping",
                "detail": "exception_ping is only valid in a persisted Cycle run",
            }
        )
    if (
        ping_payload is None
        and exception_ping_payload_required(
            text,
            cycle_context=cycle_ping_context,
        )
    ):
        try:
            await record_exception_ping_metadata_skip(
                cycle_run_id=int(cycle_ping_context["cycle_run_id"]),
                run_kind=str(cycle_ping_context["run_kind"]),
            )
        except Exception as exc:
            return json.dumps(
                {
                    "ok": False,
                    "posted": False,
                    "error": "exception_ping_ledger_unavailable",
                    "detail": str(exc),
                }
            )
        return json.dumps(
            {
                "ok": False,
                "posted": False,
                "suppressed": True,
                "error": "exception_ping_metadata_required",
                "ledger_line": "Slack skipped: exception_ping metadata required",
            }
        )
    if ping_payload is not None:
        mentioned = slack_mentioned_teammates(text)
        target_teammate_id = str(
            ping_payload.get("target_teammate_id") or ""
        ).strip()
        if mentioned and target_teammate_id not in mentioned:
            return json.dumps(
                {
                    "ok": False,
                    "posted": False,
                    "error": "exception_ping_target_mismatch",
                    "mentioned_teammate_ids": list(mentioned),
                    "target_teammate_id": target_teammate_id,
                }
            )

    try:
        client = await _slack_client_from_runtime()
        if target_thread_ts:
            mute = await read_thread_post_mute(
                client,
                channel_id=target_channel,
                thread_ts=target_thread_ts,
                illo_user_id=str(trigger.get("bot_user_id") or "").strip() or None,
            )
            if mute is not None:
                await _clear_processing_status(client, trigger)
                return json.dumps(
                    {
                        "ok": True,
                        "posted": False,
                        "suppressed": True,
                        "reason": "thread_post_muted",
                        "ledger_line": mute.ledger_line,
                        "muted_by": mute.user,
                        "muted_at": mute.ts,
                        "channel_id": target_channel,
                        "thread_ts": target_thread_ts,
                        "visibility": target_visibility,
                        "counts_as_visible_response": True,
                        "submitted_chars": len(text),
                        "posted_chars": 0,
                        "submitted_bytes": len(text.encode("utf-8")),
                        "posted_bytes": 0,
                        "chunk_count": 0,
                        "truncated": False,
                    }
                )
        if ping_payload is not None and cycle_ping_context is not None:
            return await post_exception_ping(
                cycle_run_id=int(cycle_ping_context["cycle_run_id"]),
                run_kind=str(cycle_ping_context["run_kind"]),
                payload=ping_payload,
                channel_id=target_channel,
                thread_ts=target_thread_ts,
                visibility=target_visibility,
                submitted_body=submitted_text,
                body=text,
                uploaded_image=image_upload is not None,
                resolve_channel=partial(
                    _resolve_post_channel,
                    client,
                    visibility=target_visibility,
                ),
                post=partial(
                    _post_slack_content,
                    client,
                    text=text,
                    thread_ts=target_thread_ts,
                    visibility=target_visibility,
                    user_id=user_id,
                    trigger=trigger,
                    image_upload=image_upload,
                ),
                clear_processing_status=partial(
                    _clear_processing_status,
                    client,
                    trigger,
                ),
            )
        if alert_decision is not None:
            return await post_provider_alert(
                client,
                org_id=str(
                    getattr(_agent_context, "org_id", None)
                    or _execution_metadata().get("org_id")
                    or ""
                ).strip(),
                channel_id=target_channel,
                thread_ts=target_thread_ts,
                visibility=target_visibility,
                illo_user_id=str(trigger.get("bot_user_id") or "").strip() or None,
                submitted_body=submitted_text,
                decision=alert_decision,
                uploaded_image=image_upload is not None,
                resolve_channel=partial(
                    _resolve_post_channel,
                    client,
                    visibility=target_visibility,
                ),
                post=partial(
                    _post_slack_content,
                    client,
                    text=text,
                    thread_ts=target_thread_ts,
                    visibility=target_visibility,
                    user_id=user_id,
                    trigger=trigger,
                    image_upload=image_upload,
                ),
                clear_processing_status=partial(_clear_processing_status, client, trigger),
            )
        target_channel = await _resolve_post_channel(client, target_channel, target_visibility)
        response, early_result = await _post_slack_content(
            client,
            target_channel,
            text=text,
            thread_ts=target_thread_ts,
            visibility=target_visibility,
            user_id=user_id,
            trigger=trigger,
            image_upload=image_upload,
        )
        if early_result is not None:
            return early_result
        await _clear_processing_status(client, trigger)
    except SlackDeliveryError as exc:
        return json.dumps(exc.to_result())
    except Exception as exc:
        return json.dumps(
            {
                "ok": False,
                "error": str(exc),
                "submitted_chars": len(text),
                "posted_chars": None,
                "submitted_bytes": len(text.encode("utf-8")),
                "posted_bytes": None,
                "chunk_count": 0,
                "truncated": None,
            }
        )

    submitted_chars = int(response.get("submitted_chars", len(text)))
    posted_chars = int(response.get("posted_chars", submitted_chars))
    answered_open_asks = 0
    execution_metadata = _execution_metadata()
    open_ask_context = execution_metadata.get("open_ask")
    if answers_open_ask and isinstance(open_ask_context, dict):
        from brain.systems.runs.slack_delivery import (
            record_origin_run_answer_delivery,
        )

        run_id = execution_metadata.get("run_id") or getattr(
            _agent_context,
            "run_id",
            None,
        )
        try:
            run_id = int(run_id) if run_id not in (None, "") else None
        except (TypeError, ValueError):
            run_id = None
        answered_open_asks = await record_origin_run_answer_delivery(
            origin_run_id=run_id,
            answer_text=text,
            slack_response=response,
        )
    return json.dumps(
        {
            "ok": True,
            "channel_id": target_channel,
            "thread_ts": target_thread_ts,
            "visibility": target_visibility,
            "uploaded_image": image_upload is not None,
            "submitted_chars": submitted_chars,
            "posted_chars": posted_chars,
            "submitted_bytes": int(response.get("submitted_bytes", len(text.encode("utf-8")))),
            "posted_bytes": int(response.get("posted_bytes", len(text.encode("utf-8")))),
            "chunk_count": int(response.get("chunk_count", 1)),
            "truncated": bool(response.get("truncated", False)),
            "answers_open_ask": bool(answers_open_ask),
            "answered_open_asks": answered_open_asks,
            "slack": response,
        },
        default=str,
    )


async def _handle_react_to_slack_message(
    emoji: str,
) -> str:
    """React once to the triggering Slack message."""

    try:
        normalized_emoji = _normalize_slack_emoji_name(emoji)
    except ValueError as exc:
        return json.dumps({"error": str(exc)})

    trigger = _current_slack_trigger()
    target_channel = str(trigger.get("channel_id") or "").strip()
    target_message_ts = str(trigger.get("message_ts") or "").strip()
    if not target_channel or not target_message_ts:
        return json.dumps(
            {
                "error": "react_to_slack_message requires a Slack-triggered run"
            }
        )

    durable_state = await _durable_slack_reaction_state(
        channel_id=target_channel,
        message_ts=target_message_ts,
        emoji=normalized_emoji,
    )
    if durable_state.get("error"):
        return json.dumps(
            {
                "ok": False,
                "error": "react_to_slack_message could not verify prior reaction state",
            }
        )
    if durable_state.get("conflicting_emoji"):
        return json.dumps(
            {
                "ok": False,
                "error": (
                    "react_to_slack_message cannot stack a different emoji on the "
                    "triggering Slack message in one run"
                ),
                "channel_id": target_channel,
                "message_ts": target_message_ts,
                "emoji": normalized_emoji,
                "attempted_emoji": durable_state["conflicting_emoji"],
                "reaction_already_attempted": True,
            }
        )
    if durable_state.get("already_succeeded"):
        return json.dumps(
            {
                "ok": True,
                "channel_id": target_channel,
                "message_ts": target_message_ts,
                "emoji": normalized_emoji,
                "deduplicated": True,
                "counts_as_visible_response": True,
                "slack": {"ok": True, "durable_replay": True},
            }
        )

    reaction_attempts: dict[tuple[str, str], dict[str, str]] = get_or_create_agent_run_state(
        "slack_reaction_attempts",
        dict,
    )
    target = (target_channel, target_message_ts)
    attempt = reaction_attempts.get(target)
    if attempt is not None and attempt.get("emoji") != normalized_emoji:
        return json.dumps(
            {
                "ok": False,
                "error": (
                    "react_to_slack_message cannot stack a different emoji on the "
                    "triggering Slack message in one run"
                ),
                "channel_id": target_channel,
                "message_ts": target_message_ts,
                "emoji": normalized_emoji,
                "attempted_emoji": attempt.get("emoji"),
                "reaction_already_attempted": True,
            }
        )
    if attempt is not None and attempt.get("status") != "failed":
        return json.dumps(
            {
                "ok": False,
                "error": (
                    "react_to_slack_message already attempted this emoji for the "
                    "triggering Slack message in this run"
                ),
                "channel_id": target_channel,
                "message_ts": target_message_ts,
                "emoji": normalized_emoji,
                "reaction_already_attempted": True,
            }
        )
    if attempt is None:
        attempt = {"emoji": normalized_emoji, "status": "in_flight"}
        reaction_attempts[target] = attempt
    else:
        attempt["status"] = "in_flight"

    try:
        client = await _slack_client_from_runtime()
        response = await client.add_reaction(
            channel=target_channel,
            timestamp=target_message_ts,
            name=normalized_emoji,
        )
        deduplicated = False
    except asyncio.CancelledError:
        attempt["status"] = "failed"
        raise
    except SlackApiError as exc:
        if exc.error != "already_reacted":
            attempt["status"] = "failed"
            return json.dumps({"ok": False, "error": str(exc)})
        response = {"ok": True, "already_reacted": True}
        deduplicated = True
    except Exception as exc:
        attempt["status"] = "failed"
        return json.dumps({"ok": False, "error": str(exc)})

    attempt["status"] = "succeeded"
    await _clear_processing_status(client, trigger)
    return json.dumps(
        {
            "ok": True,
            "channel_id": target_channel,
            "message_ts": target_message_ts,
            "emoji": normalized_emoji,
            "deduplicated": deduplicated,
            "counts_as_visible_response": True,
            "slack": response,
        },
        default=str,
    )


async def _handle_read_slack_conversation(
    scope: str = "thread",
    channel_id: str | None = None,
    thread_ts: str | None = None,
    limit: int = 50,
) -> str:
    """Read bounded Slack context for the current Slack-triggered run."""

    trigger = _current_slack_trigger()
    response_target = trigger.get("response_target") if isinstance(trigger.get("response_target"), dict) else {}
    target_channel = str(channel_id or response_target.get("channel_id") or trigger.get("channel_id") or "").strip()
    target_thread_ts = str(thread_ts or response_target.get("thread_ts") or trigger.get("thread_ts") or "").strip()
    normalized_scope = str(scope or "thread").strip().lower()
    capped_limit = _coerce_slack_limit(limit)

    if normalized_scope == "triggering_message":
        message = {
            "user": trigger.get("slack_user_id"),
            "text": trigger.get("text"),
            "ts": trigger.get("message_ts"),
            "permalink": trigger.get("permalink"),
        }
        return json.dumps({"ok": True, "scope": normalized_scope, "messages": [message]}, default=str)

    if not target_channel:
        return json.dumps({"error": "read_slack_conversation requires channel_id outside a Slack-triggered run"})
    try:
        client = await _slack_client_from_runtime()
        if normalized_scope == "thread":
            if not target_thread_ts:
                return json.dumps({"error": "read_slack_conversation scope=thread requires thread_ts"})
            response = await client.conversation_replies(
                channel=target_channel,
                thread_ts=target_thread_ts,
                limit=capped_limit,
            )
        elif normalized_scope == "recent_channel":
            response = await client.conversation_history(
                channel=target_channel,
                limit=capped_limit,
                latest=trigger.get("message_ts"),
            )
        else:
            return json.dumps({"error": "read_slack_conversation scope must be triggering_message, thread, or recent_channel"})
    except Exception as exc:
        return json.dumps({"error": str(exc)})

    messages = list(response.get("messages") or [])
    return json.dumps(
        {
            "ok": True,
            "scope": normalized_scope,
            "channel_id": target_channel,
            "thread_ts": target_thread_ts or None,
            "count": len(messages),
            "messages": messages[:capped_limit],
        },
        default=str,
    )


async def _slack_connection_for_tool(session, *, org_id: str, connection_id: str | None = None):
    from sqlalchemy import select

    from brain.platform.db.models.external_agent import ExternalAgentConnectionRow

    stmt = select(ExternalAgentConnectionRow).where(
        ExternalAgentConnectionRow.org_id == str(org_id),
        ExternalAgentConnectionRow.agent_kind == "slack",
        ExternalAgentConnectionRow.transport == "slack_socket_mode",
    )
    if connection_id:
        stmt = stmt.where(ExternalAgentConnectionRow.id == str(connection_id))
    stmt = stmt.order_by(ExternalAgentConnectionRow.created_at.asc(), ExternalAgentConnectionRow.id.asc())
    rows = list((await session.scalars(stmt)).all())
    if not rows:
        return None, "Slack connection not found"
    if not connection_id and len(rows) > 1:
        online_rows = [row for row in rows if _slack_connection_online(row)]
        if len(online_rows) == 1:
            return online_rows[0], None
        return None, "connection_id is required when multiple Slack connections exist"
    return rows[0], None


def _slack_connection_payload(connection) -> dict[str, Any]:
    metadata = dict(connection.metadata_ or {})
    metadata.pop("identity_links", None)
    return {
        "id": str(connection.id),
        "display_name": connection.display_name,
        "team_id": connection.remote_agent_id,
        "status": connection.status,
        "last_seen_at": connection.last_seen_at.isoformat() if connection.last_seen_at else None,
        "last_error": connection.last_error,
        "metadata": metadata,
    }


async def _handle_manage_slack(
    action: str,
    connection_id: str | None = None,
    slack_user_id: str | None = None,
    user_id: str | None = None,
    display_name: str | None = None,
    communication_preferences: dict[str, Any] | None = None,
    channel_types: str | list[str] | None = None,
    channel_id: str | None = None,
    channel_name: str | None = None,
    limit: int = 200,
    cursor: str | None = None,
    include_archived: bool = False,
) -> str:
    """Inspect Slack connection health, identity mappings, and channel monitors."""

    normalized_action = str(action or "").strip().lower()
    org_id = str(getattr(_agent_context, "org_id", "") or "").strip()
    if not org_id:
        return json.dumps({"error": "manage_slack could not access this workspace context"})

    from brain.platform.db.models.external_agent import ExternalAgentConnectionRow
    from brain.platform.db.repositories.unit_of_work import UnitOfWork
    from brain.systems.slack.identity import (
        link_slack_identity,
        list_slack_identity_mappings,
        unlink_slack_identity,
    )
    from brain.systems.slack.monitors import (
        SlackMonitorConfigError,
        add_monitored_channel,
        list_monitored_channels,
        remove_monitored_channel,
    )

    async with UnitOfWork() as uow:
        if normalized_action == "status":
            from sqlalchemy import select

            stmt = (
                select(ExternalAgentConnectionRow)
                .where(
                    ExternalAgentConnectionRow.org_id == org_id,
                    ExternalAgentConnectionRow.agent_kind == "slack",
                    ExternalAgentConnectionRow.transport == "slack_socket_mode",
                )
                .order_by(ExternalAgentConnectionRow.created_at.asc(), ExternalAgentConnectionRow.id.asc())
            )
            rows = list((await uow.session.scalars(stmt)).all())
            statuses = {str(row.status or "").strip().lower() for row in rows}
            if not rows:
                setup_state = "not_connected"
            elif statuses & {"connected", "online"}:
                setup_state = "connected"
            elif "error" in statuses:
                setup_state = "error"
            else:
                setup_state = "configured"
            return json.dumps(
                {
                    "ok": True,
                    "setup_state": setup_state,
                    "needs_connection": not bool(rows),
                    "connection_count": len(rows),
                    "connections": [_slack_connection_payload(row) for row in rows],
                },
                default=str,
            )

        if normalized_action == "open_alert_surges":
            from brain.systems.slack.provider_alert_surge import (
                list_open_provider_alert_surges,
            )

            surges = await list_open_provider_alert_surges(
                uow.session,
                org_id=org_id,
            )
            return json.dumps(
                {
                    "ok": True,
                    "open_alert_surges": surges,
                    "count": len(surges),
                },
                default=str,
            )

        connection, error = await _slack_connection_for_tool(
            uow.session,
            org_id=org_id,
            connection_id=connection_id,
        )
        if error:
            return json.dumps({"error": error})

        if normalized_action == "list_channels":
            try:
                client = await _slack_client_from_runtime()
                normalized_channel_types = _normalize_slack_channel_types(channel_types)
                response = await client.conversations_list(
                    types=normalized_channel_types,
                    limit=_coerce_slack_list_limit(limit),
                    cursor=str(cursor or "").strip() or None,
                    exclude_archived=not _coerce_bool(include_archived, default=False),
                )
            except Exception as exc:
                return json.dumps({"error": str(exc)})
            channels = [
                _slack_channel_payload(channel)
                for channel in list(response.get("channels") or [])
                if isinstance(channel, dict)
            ]
            observed_channels = await _observed_slack_channels_for_connection(
                uow.session,
                org_id=org_id,
                connection_id=str(connection.id),
                requested_types={part for part in normalized_channel_types.split(",") if part},
            )
            existing_channel_ids = {str(channel.get("id") or "") for channel in channels}
            observed_channels = [
                channel
                for channel in observed_channels
                if str(channel.get("id") or "") and str(channel.get("id") or "") not in existing_channel_ids
            ]
            channels.extend(observed_channels)
            metadata = response.get("response_metadata") if isinstance(response.get("response_metadata"), dict) else {}
            return json.dumps(
                {
                    "ok": True,
                    "connection": _slack_connection_payload(connection),
                    "count": len(channels),
                    "channels": channels,
                    "requested_channel_types": normalized_channel_types,
                    "observed_channel_count": len(observed_channels),
                    "next_cursor": str(metadata.get("next_cursor") or "") or None,
                    "visibility_note": (
                        "Slack only returns conversations visible to the configured bot token; "
                        "private channels may require the bot to be invited and the app to have the right scopes. "
                        "Slack-origin mentions are also surfaced as observed_slack_event channels when Slack listing omits them."
                    ),
                },
                default=str,
            )

        if normalized_action == "list_mappings":
            mappings = await list_slack_identity_mappings(
                uow.session,
                connection_id=str(connection.id),
                org_id=org_id,
            )
            return json.dumps({"ok": True, "connection": _slack_connection_payload(connection), "mappings": mappings}, default=str)
        if normalized_action == "link_identity":
            mapping = await link_slack_identity(
                uow.session,
                connection_id=str(connection.id),
                slack_user_id=str(slack_user_id or ""),
                user_id=str(user_id or ""),
                org_id=org_id,
                display_name=display_name,
                communication_preferences=communication_preferences,
            )
            return json.dumps({"ok": True, "mapping": mapping}, default=str)
        if normalized_action == "unlink_identity":
            mapping = await unlink_slack_identity(
                uow.session,
                connection_id=str(connection.id),
                slack_user_id=str(slack_user_id or ""),
                org_id=org_id,
            )
            return json.dumps({"ok": True, "mapping": mapping}, default=str)

        if normalized_action == "list_monitored":
            channels = await list_monitored_channels(
                uow.session,
                connection_id=str(connection.id),
                org_id=org_id,
            )
            return json.dumps(
                {
                    "ok": True,
                    "connection": _slack_connection_payload(connection),
                    "monitored_channels": channels,
                },
                default=str,
            )
        if normalized_action == "monitor_channel":
            if not str(channel_id or "").strip():
                return json.dumps({"error": "monitor_channel requires: channel_id"})
            try:
                result = await add_monitored_channel(
                    uow.session,
                    connection_id=str(connection.id),
                    channel_id=str(channel_id),
                    org_id=org_id,
                    channel_name=channel_name,
                )
            except SlackMonitorConfigError as exc:
                return json.dumps({"error": str(exc)})
            return json.dumps({"ok": True, **result}, default=str)
        if normalized_action == "unmonitor_channel":
            if not str(channel_id or "").strip():
                return json.dumps({"error": "unmonitor_channel requires: channel_id"})
            try:
                result = await remove_monitored_channel(
                    uow.session,
                    connection_id=str(connection.id),
                    channel_id=str(channel_id),
                    org_id=org_id,
                )
            except SlackMonitorConfigError as exc:
                return json.dumps({"error": str(exc)})
            return json.dumps({"ok": True, **result}, default=str)

    return json.dumps(
        {
            "error": (
                "manage_slack action must be status, list_channels, list_mappings, "
                "link_identity, unlink_identity, list_monitored, monitor_channel, "
                "unmonitor_channel, or open_alert_surges"
            )
        }
    )


__all__ = [
    "_handle_manage_slack",
    "_handle_post_slack_reply",
    "_handle_read_slack_conversation",
]
