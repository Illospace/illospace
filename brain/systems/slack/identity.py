"""Slack-to-Illospace identity mapping helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import logging
from typing import Any, Mapping

from sqlalchemy import select

from brain.kernel.common.coercion import as_mapping
from brain.platform.db.models.external_agent import ExternalAgentConnectionRow
from brain.platform.db.models.org import User
from brain.systems.personality.person_context import normalize_communication_preferences


logger = logging.getLogger(__name__)


class SlackIdentityMappingError(ValueError):
    """Raised when a Slack identity mapping cannot be applied."""


class SlackIdentitySource(StrEnum):
    """Persisted source that contributed to a canonical Slack identity."""

    LINK = "external_connection.identity_links"
    MAP = "external_connection.slack.identity_map"


@dataclass(frozen=True, slots=True)
class SlackIdentityConflict:
    """A recoverable disagreement between the two Slack identity stores."""

    slack_user_id: str
    linked_user_id: str
    mapped_user_id: str
    code: str = field(
        default="linked_mapped_user_id_conflict",
        init=False,
    )

    def __str__(self) -> str:
        return (
            f"Conflicting Slack identity for {self.slack_user_id}: linked user_id "
            f"{self.linked_user_id} disagrees with mapped user_id "
            f"{self.mapped_user_id}; omitting user_id"
        )


@dataclass(frozen=True, slots=True)
class SlackIdentityRecord:
    """One reconciled Slack identity plus normalized source provenance."""

    slack_user_id: str
    display_name: str
    user_id: str | None
    sources: frozenset[SlackIdentitySource] = frozenset()
    linked_user_id: str | None = None
    mapped_user_id: str | None = None
    link_display_name: str | None = None
    link_metadata: Mapping[str, Any] = field(default_factory=dict)
    map_metadata: Mapping[str, Any] = field(default_factory=dict)


def _clean(value: Any) -> str:
    return str(value or "").strip()


def normalize_slack_identities(
    metadata: Mapping[str, Any],
) -> tuple[
    dict[str, SlackIdentityRecord],
    tuple[SlackIdentityConflict, ...],
]:
    """Reconcile both Slack identity stores without ever splicing identities.

    Source precedence is explicit. ``identity_links.slack`` supplies display
    names. For ``user_id``, a populated value from whichever store has one is
    used; matching values collapse to one record. If both stores have populated
    values and they disagree, neither takes precedence: the record is marked
    conflicted with ``user_id=None`` and a :class:`SlackIdentityConflict`
    value is returned and logged, never raised. Linked records precede
    map-only records.
    """

    metadata = as_mapping(metadata)
    slack_metadata = as_mapping(metadata.get("slack"))
    identity_map = {
        clean_slack_user_id: user_id
        for slack_user_id, user_id in as_mapping(
            slack_metadata.get("identity_map")
        ).items()
        if (clean_slack_user_id := _clean(slack_user_id))
    }
    identity_links = {
        clean_slack_user_id: link
        for slack_user_id, link in as_mapping(
            as_mapping(metadata.get("identity_links")).get("slack")
        ).items()
        if (clean_slack_user_id := _clean(slack_user_id))
    }
    slack_user_ids = dict.fromkeys([*identity_links, *identity_map])
    records: dict[str, SlackIdentityRecord] = {}
    conflicts: list[SlackIdentityConflict] = []
    for slack_user_id in slack_user_ids:
        link = as_mapping(identity_links.get(slack_user_id))
        linked_user_id = _clean(link.get("user_id")) or None
        mapped_user_id = _clean(identity_map.get(slack_user_id)) or None
        link_display_name = _clean(link.get("display_name")) or None
        if linked_user_id and mapped_user_id and linked_user_id != mapped_user_id:
            conflict = SlackIdentityConflict(
                slack_user_id=slack_user_id,
                linked_user_id=linked_user_id,
                mapped_user_id=mapped_user_id,
            )
            conflicts.append(conflict)
            logger.warning("%s", conflict)
            user_id = None
        else:
            user_id = linked_user_id or mapped_user_id
        sources = frozenset(
            source
            for source, source_records in (
                (SlackIdentitySource.LINK, identity_links),
                (SlackIdentitySource.MAP, identity_map),
            )
            if slack_user_id in source_records
        )
        records[slack_user_id] = SlackIdentityRecord(
            slack_user_id=slack_user_id,
            display_name=link_display_name or slack_user_id,
            user_id=user_id,
            sources=sources,
            linked_user_id=linked_user_id,
            mapped_user_id=mapped_user_id,
            link_display_name=link_display_name,
            link_metadata=as_mapping(link.get("metadata")),
            map_metadata={
                "team_id": slack_metadata.get("team_id"),
                "bot_user_id": slack_metadata.get("bot_user_id"),
            },
        )
    return records, tuple(conflicts)


def _slack_metadata(connection: ExternalAgentConnectionRow) -> dict[str, Any]:
    return as_mapping(as_mapping(connection.metadata_).get("slack"))


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
    link_metadata: Mapping[str, Any] | None = None,
    replace_profile: bool = False,
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
    if (
        replace_profile
        or _clean(existing_link.get("user_id")) != clean_user_id
    ):
        existing_link = {}
    existing_metadata = existing_link.get("metadata")
    normalized_link_metadata = (
        as_mapping(link_metadata)
        if link_metadata is not None
        else as_mapping(existing_metadata)
    )
    if communication_preferences is not None:
        normalized_link_metadata["communication_preferences"] = (
            normalize_communication_preferences(communication_preferences)
        )
    slack_links[clean_slack_user_id] = {
        "user_id": clean_user_id,
        "display_name": (
            _clean(display_name)
            or existing_link.get("display_name")
            or None
        ),
        "metadata": normalized_link_metadata,
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
    records, _conflicts = normalize_slack_identities(connection.metadata_ or {})
    if not records:
        return []
    user_ids = [
        record.user_id
        for record in records.values()
        if record.user_id is not None
    ]
    users = {}
    if user_ids:
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
            "slack_user_id": record.slack_user_id,
            "user_id": record.user_id,
            "user_name": getattr(users.get(str(record.user_id)), "name", None),
        }
        for record in sorted(
            # Operators must see unresolved and conflicted pairs to repair them.
            records.values(),
            key=lambda value: value.slack_user_id,
        )
    ]


__all__ = [
    "SlackIdentityConflict",
    "SlackIdentityMappingError",
    "SlackIdentityRecord",
    "SlackIdentitySource",
    "link_slack_identity",
    "list_slack_identity_mappings",
    "normalize_slack_identities",
    "unlink_slack_identity",
]
