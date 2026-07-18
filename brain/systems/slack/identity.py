"""Slack-to-Illospace identity mapping helpers."""

from __future__ import annotations

from typing import Any, Mapping

from sqlalchemy import select

from brain.platform.db.models.external_agent import ExternalAgentConnectionRow
from brain.platform.db.models.org import User
from brain.systems.personality.person_context import normalize_communication_preferences


class SlackIdentityMappingError(ValueError):
    """Raised when a Slack identity mapping cannot be applied."""


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _slack_metadata(connection: ExternalAgentConnectionRow) -> dict[str, Any]:
    metadata = dict(connection.metadata_ or {})
    slack_metadata = metadata.get("slack")
    return dict(slack_metadata or {}) if isinstance(slack_metadata, Mapping) else {}


async def _connection_for_org(session, connection_id: str, org_id: str | None) -> ExternalAgentConnectionRow:
    connection = await session.get(ExternalAgentConnectionRow, str(connection_id))
    if connection is None:
        raise SlackIdentityMappingError("Slack connection not found")
    if connection.agent_kind != "slack" or connection.transport != "slack_socket_mode":
        raise SlackIdentityMappingError("Connection is not a Slack Socket Mode connection")
    if org_id and str(connection.org_id) != str(org_id):
        raise SlackIdentityMappingError("Slack connection is outside this org")
    return connection


async def link_slack_identity(
    session,
    *,
    connection_id: str,
    slack_user_id: str,
    user_id: str,
    org_id: str | None = None,
    display_name: str | None = None,
    communication_preferences: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    """Link a Slack user id to an Illospace user id for one Slack connection."""

    clean_slack_user_id = _clean(slack_user_id)
    clean_user_id = _clean(user_id)
    if not clean_slack_user_id or not clean_user_id:
        raise SlackIdentityMappingError("slack_user_id and user_id are required")
    connection = await _connection_for_org(session, connection_id, org_id)
    user = await session.get(User, clean_user_id)
    if user is None or str(user.org_id) != str(connection.org_id):
        raise SlackIdentityMappingError("Illospace user is outside this Slack connection org")

    root = dict(connection.metadata_ or {})
    slack_metadata = _slack_metadata(connection)
    identity_map = dict(slack_metadata.get("identity_map") or {})
    identity_map[clean_slack_user_id] = clean_user_id
    slack_metadata["identity_map"] = identity_map
    root["slack"] = slack_metadata

    identity_links = root.get("identity_links")
    identity_links = dict(identity_links) if isinstance(identity_links, Mapping) else {}
    slack_links = identity_links.get("slack")
    slack_links = dict(slack_links) if isinstance(slack_links, Mapping) else {}
    existing_link = slack_links.get(clean_slack_user_id)
    existing_link = dict(existing_link) if isinstance(existing_link, Mapping) else {}
    if _clean(existing_link.get("user_id")) != clean_user_id:
        existing_link = {}
    existing_metadata = existing_link.get("metadata")
    link_metadata = dict(existing_metadata) if isinstance(existing_metadata, Mapping) else {}
    if communication_preferences is not None:
        link_metadata["communication_preferences"] = normalize_communication_preferences(
            communication_preferences
        )
    slack_links[clean_slack_user_id] = {
        "user_id": clean_user_id,
        "display_name": _clean(display_name) or existing_link.get("display_name") or None,
        "metadata": link_metadata,
    }
    identity_links["slack"] = slack_links
    root["identity_links"] = identity_links
    connection.metadata_ = root
    await session.flush()
    return {"slack_user_id": clean_slack_user_id, "user_id": clean_user_id}


async def unlink_slack_identity(
    session,
    *,
    connection_id: str,
    slack_user_id: str,
    org_id: str | None = None,
) -> dict[str, str | bool]:
    connection = await _connection_for_org(session, connection_id, org_id)
    clean_slack_user_id = _clean(slack_user_id)
    root = dict(connection.metadata_ or {})
    slack_metadata = _slack_metadata(connection)
    identity_map = dict(slack_metadata.get("identity_map") or {})
    removed = clean_slack_user_id in identity_map
    identity_map.pop(clean_slack_user_id, None)
    slack_metadata["identity_map"] = identity_map
    root["slack"] = slack_metadata

    identity_links = root.get("identity_links")
    identity_links = dict(identity_links) if isinstance(identity_links, Mapping) else {}
    slack_links = identity_links.get("slack")
    slack_links = dict(slack_links) if isinstance(slack_links, Mapping) else {}
    slack_links.pop(clean_slack_user_id, None)
    if slack_links:
        identity_links["slack"] = slack_links
    else:
        identity_links.pop("slack", None)
    if identity_links:
        root["identity_links"] = identity_links
    else:
        root.pop("identity_links", None)
    connection.metadata_ = root
    await session.flush()
    return {"slack_user_id": clean_slack_user_id, "removed": removed}


async def list_slack_identity_mappings(
    session,
    *,
    connection_id: str,
    org_id: str | None = None,
) -> list[dict[str, str | None]]:
    connection = await _connection_for_org(session, connection_id, org_id)
    identity_map = dict(_slack_metadata(connection).get("identity_map") or {})
    if not identity_map:
        return []
    user_ids = [str(user_id) for user_id in identity_map.values()]
    users = {
        str(user.id): user
        for user in (
            await session.scalars(
                select(User).where(
                    User.org_id == str(connection.org_id),
                    User.id.in_(user_ids),
                )
            )
        ).all()
    }
    return [
        {
            "slack_user_id": str(slack_user_id),
            "user_id": str(user_id),
            "user_name": getattr(users.get(str(user_id)), "name", None),
        }
        for slack_user_id, user_id in sorted(identity_map.items())
    ]


__all__ = [
    "SlackIdentityMappingError",
    "link_slack_identity",
    "list_slack_identity_mappings",
    "unlink_slack_identity",
]
