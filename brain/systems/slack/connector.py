"""Self-hosted Slack Socket Mode connector helpers."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import os
from typing import Any, Mapping

import httpx
from sqlalchemy import select

from brain.platform.db.models.external_agent import ExternalAgentConnectionRow
from brain.platform.db.models.org import User
from brain.systems.inbound.service import submit_inbound_envelope
from brain.systems.slack.client import (
    SlackApiError,
    SlackConfigurationError,
    slack_app_token_from_env,
    slack_bot_token_from_env,
)
from brain.systems.slack.ingress import normalize_slack_socket_event

logger = logging.getLogger(__name__)


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


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


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
    envelope = normalize_slack_socket_event(
        socket_payload,
        bot_user_id=config.bot_user_id,
    )
    if envelope is None:
        return {"ack": ack, "ignored": True}
    inbound = await submit_inbound_envelope(
        session,
        connection=connection,
        envelope=envelope,
        ingress_context={
            "transport": "slack_socket_mode",
            "envelope_id": ack.get("envelope_id"),
        },
    )
    return {"ack": ack, "ignored": False, "inbound": inbound}


async def open_socket_mode_url(config: SlackConnectorConfig) -> str:
    """Open a Slack Socket Mode URL using the app-level token."""

    async with httpx.AsyncClient(timeout=10.0) as client:
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

    from brain.platform.db.repositories.unit_of_work import UnitOfWork

    config = config or SlackConnectorConfig.from_env()
    socket_url = config.socket_mode_url or await open_socket_mode_url(config)
    logger.info("slack_socket_mode_connecting")
    async with websockets.connect(socket_url) as websocket:
        logger.info("slack_socket_mode_connected")
        async for raw_message in websocket:
            socket_payload = json.loads(raw_message)
            ack = socket_mode_ack(socket_payload)
            if ack:
                await websocket.send(json.dumps(ack))
            try:
                if session_factory is not None:
                    async with session_factory() as session:
                        connection = await connection_factory(session) if connection_factory else None
                        if connection is None:
                            raise SlackConfigurationError("Slack connection factory returned no connection")
                        await process_socket_payload(
                            session,
                            connection=connection,
                            socket_payload=socket_payload,
                            config=config,
                        )
                else:
                    async with UnitOfWork() as uow:
                        org_id, owner_user_id = await resolve_slack_connection_identity(uow.session, config)
                        connection = await ensure_slack_connection(
                            uow.session,
                            org_id=org_id,
                            owner_user_id=owner_user_id,
                            team_id=config.team_id,
                            bot_user_id=config.bot_user_id,
                            status="connected",
                        )
                        await process_socket_payload(
                            uow.session,
                            connection=connection,
                            socket_payload=socket_payload,
                            config=config,
                        )
            except Exception as exc:
                logger.exception("slack_socket_mode_payload_failed: %s", exc)


async def run_forever() -> None:
    while True:
        try:
            await run_socket_mode_loop()
        except Exception as exc:
            logger.exception("slack_socket_mode_loop_failed: %s", exc)
            await asyncio.sleep(5)


__all__ = [
    "SlackConnectorConfig",
    "ensure_slack_connection",
    "socket_mode_ack",
    "process_socket_payload",
    "resolve_slack_connection_identity",
    "open_socket_mode_url",
    "run_socket_mode_loop",
    "run_forever",
]
