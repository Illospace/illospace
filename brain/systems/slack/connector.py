"""Self-hosted Slack Socket Mode connector helpers."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import logging
import os
from typing import Any, Mapping

from sqlalchemy import select

from brain.platform.async_io import async_http_client
from brain.platform.db.models.agent_run import AgentRunRow
from brain.platform.db.models.external_agent import ExternalAgentConnectionRow
from brain.platform.db.models.inbound import InboundEventRow
from brain.platform.db.models.org import User
from brain.systems.inbound.service import submit_inbound_envelope
from brain.systems.slack.client import (
    SlackApiError,
    SlackConfigurationError,
    SlackWebClient,
    slack_app_token_from_env,
    slack_bot_token_from_env,
)
from brain.systems.slack.ingress import normalize_slack_socket_event
from brain.systems.slack.monitored_intakes import (
    enrich_monitored_intake,
    is_monitored_intake,
)
from brain.systems.slack.monitors import monitored_channel_ids

logger = logging.getLogger(__name__)
SLACK_PROCESSING_STATUS = "is working on it..."


@dataclass(frozen=True)
class SlackConnectorConfig:
    bot_token: str
    app_token: str
    org_id: str | None = None
    owner_user_id: str | None = None
    team_id: str | None = None
    bot_user_id: str | None = None
    socket_mode_url: str | None = None

    @classmethod
    def from_env(cls) -> "SlackConnectorConfig":
        return cls(
            bot_token=slack_bot_token_from_env(),
            app_token=slack_app_token_from_env(),
            org_id=os.environ.get("ILLO_SLACK_ORG_ID") or os.environ.get("ILLO_ORG_ID"),
            owner_user_id=(
                os.environ.get("ILLO_SLACK_OWNER_USER_ID")
                or os.environ.get("ILLO_OWNER_USER_ID")
            ),
            team_id=os.environ.get("SLACK_TEAM_ID"),
            bot_user_id=os.environ.get("SLACK_BOT_USER_ID"),
        )

    @classmethod
    async def from_runtime(cls) -> "SlackConnectorConfig":
        """Load connector config through the central Vault-first runtime resolver."""

        org_id = os.environ.get("ILLO_SLACK_ORG_ID") or os.environ.get("ILLO_ORG_ID")
        owner_user_id = (
            os.environ.get("ILLO_SLACK_OWNER_USER_ID")
            or os.environ.get("ILLO_OWNER_USER_ID")
        )
        if not org_id or not owner_user_id:
            org_id, owner_user_id = await resolve_slack_connector_authority(
                org_id=org_id,
                owner_user_id=owner_user_id,
            )
        from brain.systems.vault.runtime_secrets import RuntimeSecretContext, read_runtime_secret

        secret_context = RuntimeSecretContext(
            actor_user_id=owner_user_id,
            org_id=org_id,
        )
        bot_token = await read_runtime_secret(
            "SLACK_BOT_TOKEN",
            context=secret_context,
            reason="Run the self-hosted Slack connector.",
            requested_by="slack_connector",
            access="service",
            allow_env_fallback=True,
        )
        app_token = await read_runtime_secret(
            "SLACK_APP_TOKEN",
            context=secret_context,
            reason="Run the self-hosted Slack connector.",
            requested_by="slack_connector",
            access="service",
            allow_env_fallback=True,
        )
        if not bot_token:
            raise SlackConfigurationError("SLACK_BOT_TOKEN is required")
        if not app_token:
            raise SlackConfigurationError("SLACK_APP_TOKEN is required")
        return cls(
            bot_token=bot_token,
            app_token=app_token,
            org_id=org_id,
            owner_user_id=owner_user_id,
            team_id=os.environ.get("SLACK_TEAM_ID"),
            bot_user_id=os.environ.get("SLACK_BOT_USER_ID"),
        )


def validate_slack_connector_tokens(*, bot_token: str, app_token: str) -> None:
    bot = str(bot_token or "").strip()
    app = str(app_token or "").strip()
    if bot.startswith("xapp-"):
        raise SlackConfigurationError(
            "Slack bot token must be a bot token, not an app-level Socket Mode token"
        )
    if app.startswith(("xoxb-", "xoxp-")):
        raise SlackConfigurationError(
            "Slack app-level Socket Mode token must be an app-level token, not a bot/user token"
        )


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def resolve_slack_connector_authority(
    *,
    org_id: str | None,
    owner_user_id: str | None,
) -> tuple[str, str]:
    if org_id and owner_user_id:
        return str(org_id), str(owner_user_id)

    from brain.platform.db.repositories.unit_of_work import UnitOfWork

    async with UnitOfWork() as uow:
        if owner_user_id:
            user = await uow.session.get(User, str(owner_user_id))
            if user is None:
                raise SlackConfigurationError("ILLO_SLACK_OWNER_USER_ID does not match an Illospace user")
            return str(org_id or user.org_id), str(owner_user_id)

        stmt = select(User).order_by(User.created_at.asc(), User.id.asc()).limit(1)
        if org_id:
            stmt = stmt.where(User.org_id == str(org_id))
        user = (await uow.session.scalars(stmt)).first()
    if user is None:
        raise SlackConfigurationError(
            "No Illospace user found; set ILLO_SLACK_ORG_ID and ILLO_SLACK_OWNER_USER_ID"
        )
    return str(org_id or user.org_id), str(owner_user_id or user.id)


async def ensure_slack_connection(
    session,
    *,
    org_id: str,
    owner_user_id: str,
    team_id: str | None = None,
    bot_user_id: str | None = None,
    team_name: str | None = None,
    status: str = "configured",
    last_error: str | None = None,
) -> ExternalAgentConnectionRow:
    """Create or refresh the durable Slack source/health record."""

    stmt = select(ExternalAgentConnectionRow).where(
        ExternalAgentConnectionRow.org_id == str(org_id),
        ExternalAgentConnectionRow.agent_kind == "slack",
        ExternalAgentConnectionRow.transport == "slack_socket_mode",
    )
    if team_id:
        stmt = stmt.where(ExternalAgentConnectionRow.remote_agent_id == str(team_id))
    stmt = stmt.order_by(ExternalAgentConnectionRow.created_at.asc()).limit(1)
    connection = (await session.scalars(stmt)).first()
    if connection is None:
        connection = ExternalAgentConnectionRow(
            org_id=str(org_id),
            owner_user_id=str(owner_user_id),
            display_name="Slack",
            agent_kind="slack",
            transport="slack_socket_mode",
            status=status,
            remote_agent_id=str(team_id) if team_id else None,
            remote_agent_card={},
            capabilities={},
            auth_metadata={},
            metadata_={},
        )
        session.add(connection)

    connection.owner_user_id = str(owner_user_id)
    connection.display_name = team_name or "Slack"
    connection.remote_agent_id = str(team_id) if team_id else connection.remote_agent_id
    connection.status = status
    connection.last_error = last_error
    connection.last_tested_at = utcnow()
    if status in {"connected", "online"}:
        connection.last_seen_at = utcnow()
    connection.auth_metadata = {
        "bot_token_ref": "env:SLACK_BOT_TOKEN",
        "app_token_ref": "env:SLACK_APP_TOKEN",
    }
    connection.capabilities = {
        **dict(connection.capabilities or {}),
        "slack": {
            "socket_mode": True,
            "app_mentions": True,
            "direct_messages": True,
            "read_context": True,
            "reply": True,
        },
    }
    slack_metadata = dict((connection.metadata_ or {}).get("slack") or {})
    if team_id:
        slack_metadata["team_id"] = str(team_id)
    if bot_user_id:
        slack_metadata["bot_user_id"] = str(bot_user_id)
    connection.metadata_ = {
        **dict(connection.metadata_ or {}),
        "slack": slack_metadata,
        "health": {
            "status": status,
            "last_error": last_error,
            "checked_at": utcnow().isoformat(),
        },
    }
    await session.flush()
    return connection


async def resolve_slack_team_metadata(config: SlackConnectorConfig) -> tuple[str | None, str | None, str | None]:
    """Resolve Slack team/bot metadata from env or Slack auth.test."""

    if config.team_id and config.bot_user_id:
        return config.team_id, config.bot_user_id, None

    auth = await SlackWebClient(config.bot_token).auth_test()
    team_id = config.team_id or auth.get("team_id")
    bot_user_id = config.bot_user_id or auth.get("user_id") or auth.get("bot_id")
    team_name = auth.get("team")
    return (
        str(team_id) if team_id else None,
        str(bot_user_id) if bot_user_id else None,
        str(team_name) if team_name else None,
    )


async def ensure_slack_connection_for_config(
    session,
    config: SlackConnectorConfig,
    *,
    status: str = "connected",
    last_error: str | None = None,
) -> tuple[ExternalAgentConnectionRow, SlackConnectorConfig]:
    """Create or refresh Slack health before waiting for the first event."""

    org_id, owner_user_id = await resolve_slack_connection_identity(session, config)
    team_name = None
    if last_error:
        team_id = config.team_id
        bot_user_id = config.bot_user_id
    else:
        team_id, bot_user_id, team_name = await resolve_slack_team_metadata(config)
    enriched_config = replace(
        config,
        org_id=config.org_id or org_id,
        owner_user_id=config.owner_user_id or owner_user_id,
        team_id=team_id,
        bot_user_id=bot_user_id,
    )
    connection = await ensure_slack_connection(
        session,
        org_id=org_id,
        owner_user_id=owner_user_id,
        team_id=team_id,
        bot_user_id=bot_user_id,
        team_name=team_name,
        status=status,
        last_error=last_error,
    )
    return connection, enriched_config


async def resolve_slack_connection_identity(session, config: SlackConnectorConfig) -> tuple[str, str]:
    """Resolve the org/user authority for a self-hosted Slack connector."""

    if config.org_id and config.owner_user_id:
        return str(config.org_id), str(config.owner_user_id)
    stmt = select(User).order_by(User.created_at.asc(), User.id.asc()).limit(1)
    user = (await session.scalars(stmt)).first()
    if user is None:
        raise SlackConfigurationError(
            "No Illospace user found; set ILLO_SLACK_ORG_ID and ILLO_SLACK_OWNER_USER_ID"
        )
    return str(user.org_id), str(user.id)


def socket_mode_ack(socket_payload: Mapping[str, Any]) -> dict[str, Any]:
    envelope_id = str(dict(socket_payload or {}).get("envelope_id") or "").strip()
    return {"envelope_id": envelope_id} if envelope_id else {}


async def process_socket_payload(
    session,
    *,
    connection: ExternalAgentConnectionRow | Mapping[str, Any],
    socket_payload: Mapping[str, Any],
    config: SlackConnectorConfig,
) -> dict[str, Any]:
    """Normalize and process a Socket Mode payload after its ack is sent."""

    ack = socket_mode_ack(socket_payload)
    monitored_channels = monitored_channel_ids(connection)
    envelope = normalize_slack_socket_event(
        socket_payload,
        bot_user_id=config.bot_user_id,
        monitored_channels=monitored_channels,
    )
    if envelope is None:
        return {"ack": ack, "ignored": True}
    monitored_intake = is_monitored_intake(envelope)
    existing_run_for_message = await _has_slack_run_for_envelope(session, connection, envelope)
    await _record_inbound_obligation_reply(
        session,
        connection=connection,
        envelope=envelope,
    )
    if monitored_intake:
        # Reflex acknowledgement: leave a 👀 on every observed message so the
        # channel knows Illo has seen it, independent of whether the ensuing
        # triage run decides to reply.
        await _acknowledge_monitored_message(config, envelope)
        await enrich_monitored_intake(
            envelope,
            connection=connection,
            bot_token=config.bot_token,
        )
    inbound = await submit_inbound_envelope(
        session,
        connection=connection,
        envelope=envelope,
        ingress_context={
            "transport": "slack_socket_mode",
            "envelope_id": ack.get("envelope_id"),
        },
    )
    if (
        not monitored_intake
        and not existing_run_for_message
        and _admitted_slack_run(inbound)
    ):
        await _set_processing_status(config, envelope)
    return {"ack": ack, "ignored": False, "inbound": inbound}


async def process_slack_history_message(
    session,
    *,
    connection: ExternalAgentConnectionRow,
    message: Mapping[str, Any],
    channel_id: str,
    config: SlackConnectorConfig,
    client: Any,
    gap_start: datetime,
) -> dict[str, Any]:
    """Replay one history message through the normal Slack inbound owners."""

    event = dict(message)
    event.setdefault("type", "message")
    event.setdefault("channel", str(channel_id))
    event.setdefault("channel_type", "channel")
    socket_payload = {
        "payload": {
            "team_id": config.team_id,
            "event_time": int(_slack_timestamp(event.get("ts"))),
            "event": event,
        }
    }
    envelope = normalize_slack_socket_event(
        socket_payload,
        bot_user_id=config.bot_user_id,
        monitored_channels=monitored_channel_ids(connection),
    )
    if envelope is None:
        return {"ignored": True, "acked": False}

    existing = await _has_inbound_event_for_envelope(
        session,
        connection,
        envelope,
    )
    # History has no Socket Mode transport ack. A visible reaction is the
    # catch-up acknowledgement for every actionable monitored message,
    # including an explicit human @Illo mention.
    acknowledged = await _acknowledge_monitored_message(
        config,
        envelope,
        client=client,
    )
    if existing is None and is_monitored_intake(envelope):
        await enrich_monitored_intake(
            envelope,
            connection=connection,
            bot_token=config.bot_token,
        )
    inbound = await submit_inbound_envelope(
        session,
        connection=connection,
        envelope=envelope,
        ingress_context={
            "transport": "slack_history_catch_up",
            "gap_start": gap_start.isoformat(),
        },
    )
    return {
        "ignored": False,
        "acked": acknowledged,
        "inbound": inbound,
    }


async def backfill_monitored_slack_history(
    session,
    *,
    gap_start: datetime,
    now: datetime,
    client_factory=None,
    max_pages_per_channel: int = 100,
) -> dict[str, Any]:
    """Backfill every configured monitored channel from one cold-start gap."""

    connections = (
        await session.scalars(
            select(ExternalAgentConnectionRow)
            .where(
                ExternalAgentConnectionRow.agent_kind == "slack",
                ExternalAgentConnectionRow.transport == "slack_socket_mode",
                ExternalAgentConnectionRow.disabled_at.is_(None),
            )
            .order_by(ExternalAgentConnectionRow.created_at.asc())
        )
    ).all()
    summary: dict[str, Any] = {
        "connections": 0,
        "channels": 0,
        "messages_read": 0,
        "ingested": 0,
        "deduplicated": 0,
        "acked": 0,
        "ignored": 0,
        "errors": [],
    }
    for connection in connections:
        channel_ids = sorted(monitored_channel_ids(connection))
        if not channel_ids:
            continue
        summary["connections"] += 1
        try:
            if client_factory is None:
                from brain.systems.slack.client import slack_web_client_from_runtime

                client = await slack_web_client_from_runtime(
                    requested_by="scheduler_cold_start_reconciliation",
                    reason="Backfill monitored Slack channels across a scheduler outage.",
                )
            else:
                client = await client_factory(connection)
        except Exception as exc:  # noqa: BLE001 - isolate one Slack connection
            summary["errors"].append(f"connection:{connection.id}:{exc}")
            continue

        slack_metadata = dict((connection.metadata_ or {}).get("slack") or {})
        config = SlackConnectorConfig(
            bot_token=str(getattr(client, "bot_token", "") or ""),
            app_token="",
            org_id=str(connection.org_id),
            owner_user_id=str(connection.owner_user_id),
            team_id=(
                str(slack_metadata.get("team_id") or connection.remote_agent_id or "")
                or None
            ),
            bot_user_id=str(slack_metadata.get("bot_user_id") or "") or None,
        )
        for channel_id in channel_ids:
            summary["channels"] += 1
            try:
                messages = await _read_history_window(
                    client,
                    channel_id=channel_id,
                    gap_start=gap_start,
                    now=now,
                    max_pages=max_pages_per_channel,
                )
            except Exception as exc:  # noqa: BLE001 - continue other channels
                summary["errors"].append(f"channel:{channel_id}:{exc}")
                continue
            summary["messages_read"] += len(messages)
            for message in messages:
                try:
                    result = await process_slack_history_message(
                        session,
                        connection=connection,
                        message=message,
                        channel_id=channel_id,
                        config=config,
                        client=client,
                        gap_start=gap_start,
                    )
                except Exception as exc:  # noqa: BLE001 - isolate one message
                    summary["errors"].append(
                        f"message:{channel_id}:{message.get('ts')}:{exc}"
                    )
                    continue
                if result["ignored"]:
                    summary["ignored"] += 1
                    continue
                summary["acked"] += int(bool(result["acked"]))
                if result["inbound"].get("idempotent_replay"):
                    summary["deduplicated"] += 1
                else:
                    summary["ingested"] += 1
    return summary


async def _read_history_window(
    client: Any,
    *,
    channel_id: str,
    gap_start: datetime,
    now: datetime,
    max_pages: int,
) -> list[dict[str, Any]]:
    oldest = f"{gap_start.timestamp():.6f}"
    latest = f"{now.timestamp():.6f}"
    cursor: str | None = None
    messages_by_ts: dict[str, dict[str, Any]] = {}
    for _page in range(max(1, int(max_pages))):
        payload = await client.conversation_history(
            channel=channel_id,
            limit=200,
            oldest=oldest,
            latest=latest,
            cursor=cursor,
        )
        for raw_message in payload.get("messages") or []:
            if not isinstance(raw_message, Mapping):
                continue
            message = dict(raw_message)
            message_ts = str(message.get("ts") or "").strip()
            if (
                message_ts
                and _slack_timestamp(oldest)
                <= _slack_timestamp(message_ts)
                <= _slack_timestamp(latest)
            ):
                messages_by_ts[message_ts] = message
        metadata = payload.get("response_metadata")
        metadata = metadata if isinstance(metadata, Mapping) else {}
        next_cursor = str(metadata.get("next_cursor") or "").strip()
        if not next_cursor:
            return [
                messages_by_ts[key]
                for key in sorted(messages_by_ts, key=_slack_timestamp)
            ]
        cursor = next_cursor
    raise RuntimeError(
        f"history exceeded {max_pages} pages; refusing a partial backfill"
    )


def _slack_timestamp(value: Any) -> Decimal:
    try:
        return Decimal(str(value or "0"))
    except (InvalidOperation, ValueError):
        return Decimal(0)


async def _record_inbound_obligation_reply(
    session: Any,
    *,
    connection: ExternalAgentConnectionRow | Mapping[str, Any],
    envelope: Mapping[str, Any],
) -> None:
    """Settle any typed obligation whose policy accepts this Slack reply."""

    if session is None:
        return
    payload = dict(envelope.get("payload") or {})
    message_ts = str(payload.get("message_ts") or "").strip()
    thread_ts = str(payload.get("thread_ts") or "").strip()
    slack_user_id = str(payload.get("slack_user_id") or "").strip()
    if (
        not message_ts
        or not thread_ts
        or message_ts == thread_ts
        or not slack_user_id
    ):
        return
    org_id = _connection_org_id(connection)
    channel_id = str(payload.get("channel_id") or "").strip()
    if not org_id or not channel_id:
        return
    try:
        from brain.systems.runs.open_asks import (
            record_inbound_slack_obligation_answer,
        )

        await record_inbound_slack_obligation_answer(
            session,
            org_id=org_id,
            channel_id=channel_id,
            thread_ts=thread_ts,
            slack_user_id=slack_user_id,
            message_ts=message_ts,
            answer_text=str(payload.get("text") or ""),
        )
    except Exception as exc:
        logger.exception("inbound_obligation_reply_recording_failed: %s", exc)


def _connection_org_id(connection: ExternalAgentConnectionRow | Mapping[str, Any]) -> str | None:
    if isinstance(connection, Mapping):
        return str(connection.get("org_id") or "").strip() or None
    return str(getattr(connection, "org_id", "") or "").strip() or None


def _envelope_idempotency_key(envelope: Mapping[str, Any]) -> str | None:
    key = str(envelope.get("idempotency_key") or "").strip()
    return key if key.startswith("slack:") else None


async def _has_slack_run_for_envelope(
    session,
    connection: ExternalAgentConnectionRow | Mapping[str, Any],
    envelope: Mapping[str, Any],
) -> bool:
    if not callable(getattr(session, "scalar", None)):
        return False
    org_id = _connection_org_id(connection)
    key = _envelope_idempotency_key(envelope)
    if not org_id or not key:
        return False
    stmt = (
        select(AgentRunRow.id)
        .where(
            AgentRunRow.org_id == org_id,
            AgentRunRow.source_idempotency_scope == "slack",
            AgentRunRow.source_idempotency_key == key,
        )
        .limit(1)
    )
    return (await session.scalar(stmt)) is not None


async def _has_inbound_event_for_envelope(
    session,
    connection: ExternalAgentConnectionRow | Mapping[str, Any],
    envelope: Mapping[str, Any],
) -> InboundEventRow | None:
    if not callable(getattr(session, "scalar", None)):
        return None
    connection_id = (
        str(connection.get("id") or "").strip()
        if isinstance(connection, Mapping)
        else str(getattr(connection, "id", "") or "").strip()
    )
    key = _envelope_idempotency_key(envelope)
    if not connection_id or not key:
        return None
    return await session.scalar(
        select(InboundEventRow)
        .where(
            InboundEventRow.connection_id == connection_id,
            InboundEventRow.idempotency_key == key,
        )
        .limit(1)
    )


def _admitted_slack_run(inbound: Mapping[str, Any]) -> bool:
    if inbound.get("idempotent_replay"):
        return False
    outcome = inbound.get("ilo_outcome")
    outcome = outcome if isinstance(outcome, Mapping) else {}
    return outcome.get("operation") == "slack_run_admitted" and outcome.get("run_id") is not None


async def _set_processing_status(config: SlackConnectorConfig, envelope: Mapping[str, Any]) -> None:
    payload = envelope.get("payload") if isinstance(envelope.get("payload"), Mapping) else {}
    channel_id = str(payload.get("channel_id") or "").strip()
    thread_ts = str(payload.get("thread_ts") or payload.get("message_ts") or "").strip()
    if not channel_id or not thread_ts:
        return
    try:
        client = SlackWebClient(config.bot_token, timeout=2.0)
        await client.set_assistant_status(
            channel_id=channel_id,
            thread_ts=thread_ts,
            status=SLACK_PROCESSING_STATUS,
        )
    except Exception as exc:
        logger.info("slack_processing_status_failed: %s", exc)


async def _acknowledge_monitored_message(
    config: SlackConnectorConfig,
    envelope: Mapping[str, Any],
    *,
    client: Any | None = None,
) -> bool:
    """Add the 👀 reaction to an observed monitored-channel message.

    Best-effort: a missing ``reactions:write`` scope or an already-present
    reaction must not break inbound processing.
    """

    payload = dict(envelope.get("payload") or {})
    channel_id = str(payload.get("channel_id") or "").strip()
    message_ts = str(payload.get("message_ts") or "").strip()
    if not channel_id or not message_ts:
        return False
    try:
        active_client = client or SlackWebClient(config.bot_token)
        await active_client.add_reaction(
            channel=channel_id,
            timestamp=message_ts,
            name="eyes",
        )
        return True
    except SlackApiError as exc:
        if exc.error == "already_reacted":
            return True
        logger.info("slack_monitor_reaction_failed: %s", exc)
    except Exception as exc:
        logger.info("slack_monitor_reaction_failed: %s", exc)
    return False


async def open_socket_mode_url(config: SlackConnectorConfig) -> str:
    """Open a Slack Socket Mode URL using the app-level token."""

    async with async_http_client(timeout=10.0) as client:
        response = await client.post(
            "https://slack.com/api/apps.connections.open",
            headers={"Authorization": f"Bearer {config.app_token}"},
        )
        response.raise_for_status()
        data = response.json()
    if not data.get("ok") or not data.get("url"):
        raise SlackApiError(str(data.get("error") or "socket_mode_open_failed"))
    return str(data["url"])


async def run_socket_mode_loop(
    *,
    config: SlackConnectorConfig | None = None,
    session_factory=None,
    connection_factory=None,
) -> None:
    """Run the long-lived Socket Mode loop.

    The connector sends Slack acknowledgements before it performs durable
    inbound processing. This keeps transport semantics out of Illo runtime
    behavior while preserving a simple self-hosted process.
    """

    import websockets

    config = config or await SlackConnectorConfig.from_runtime()
    _, config = await _ensure_runtime_connection(
        config,
        status="configured",
        session_factory=session_factory,
        connection_factory=connection_factory,
    )
    try:
        validate_slack_connector_tokens(
            bot_token=config.bot_token,
            app_token=config.app_token,
        )
        socket_url = config.socket_mode_url or await open_socket_mode_url(config)
    except Exception as exc:
        await _ensure_runtime_connection(
            config,
            status="error",
            last_error=str(exc),
            session_factory=session_factory,
            connection_factory=connection_factory,
        )
        raise
    logger.info("slack_socket_mode_connecting")
    async with websockets.connect(socket_url) as websocket:
        logger.info("slack_socket_mode_connected")
        _, config = await _ensure_runtime_connection(
            config,
            status="connected",
            session_factory=session_factory,
            connection_factory=connection_factory,
        )
        async for raw_message in websocket:
            socket_payload = json.loads(raw_message)
            ack = socket_mode_ack(socket_payload)
            if ack:
                await websocket.send(json.dumps(ack))
            try:
                await _process_runtime_socket_payload(
                    config,
                    socket_payload=socket_payload,
                    session_factory=session_factory,
                    connection_factory=connection_factory,
                )
            except Exception as exc:
                logger.exception("slack_socket_mode_payload_failed: %s", exc)


async def _ensure_runtime_connection(
    config: SlackConnectorConfig,
    *,
    status: str,
    last_error: str | None = None,
    session_factory=None,
    connection_factory=None,
) -> tuple[ExternalAgentConnectionRow, SlackConnectorConfig]:
    if session_factory is not None:
        async with session_factory() as session:
            if connection_factory is not None:
                connection = await connection_factory(session)
                if connection is None:
                    raise SlackConfigurationError("Slack connection factory returned no connection")
                return connection, config
            return await ensure_slack_connection_for_config(
                session,
                config,
                status=status,
                last_error=last_error,
            )

    from brain.platform.db.repositories.unit_of_work import UnitOfWork

    async with UnitOfWork() as uow:
        return await ensure_slack_connection_for_config(
            uow.session,
            config,
            status=status,
            last_error=last_error,
        )


async def _process_runtime_socket_payload(
    config: SlackConnectorConfig,
    *,
    socket_payload: Mapping[str, Any],
    session_factory=None,
    connection_factory=None,
) -> None:
    if session_factory is not None:
        async with session_factory() as session:
            if connection_factory is not None:
                connection = await connection_factory(session)
                if connection is None:
                    raise SlackConfigurationError("Slack connection factory returned no connection")
            else:
                connection, config = await ensure_slack_connection_for_config(
                    session,
                    config,
                    status="connected",
                )
            await process_socket_payload(
                session,
                connection=connection,
                socket_payload=socket_payload,
                config=config,
            )
        return

    from brain.platform.db.repositories.unit_of_work import UnitOfWork

    async with UnitOfWork() as uow:
        connection, config = await ensure_slack_connection_for_config(
            uow.session,
            config,
            status="connected",
        )
        await process_socket_payload(
            uow.session,
            connection=connection,
            socket_payload=socket_payload,
            config=config,
        )


async def run_forever() -> None:
    while True:
        config = None
        try:
            config = await SlackConnectorConfig.from_runtime()
            await run_socket_mode_loop(config=config)
        except Exception as exc:
            if config is not None:
                try:
                    from brain.platform.db.repositories.unit_of_work import UnitOfWork

                    async with UnitOfWork() as uow:
                        await ensure_slack_connection_for_config(
                            uow.session,
                            config,
                            status="error",
                            last_error=str(exc),
                        )
                except Exception:
                    logger.exception("slack_socket_mode_error_record_failed")
            logger.exception("slack_socket_mode_loop_failed: %s", exc)
            await asyncio.sleep(5)


__all__ = [
    "SlackConnectorConfig",
    "ensure_slack_connection",
    "ensure_slack_connection_for_config",
    "socket_mode_ack",
    "process_socket_payload",
    "resolve_slack_connection_identity",
    "resolve_slack_team_metadata",
    "open_socket_mode_url",
    "run_socket_mode_loop",
    "run_forever",
    "validate_slack_connector_tokens",
]
