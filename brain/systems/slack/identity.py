"""Slack-to-Illospace identity mapping helpers."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any, Mapping

from sqlalchemy import select

from brain.platform.db.models.external_agent import ExternalAgentConnectionRow
from brain.platform.db.models.org import User
from brain.systems.personality.person_context import normalize_communication_preferences


logger = logging.getLogger(__name__)


class SlackIdentityMappingError(ValueError):
    """A typed Slack identity mapping failure or non-raising diagnostic."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "mapping_error",
        slack_user_id: str | None = None,
        linked_user_id: str | None = None,
        mapped_user_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.slack_user_id = slack_user_id
        self.linked_user_id = linked_user_id
        self.mapped_user_id = mapped_user_id

    @classmethod
    def conflicting_user_ids(
        cls,
        *,
        slack_user_id: str,
        linked_user_id: str,
        mapped_user_id: str,
    ) -> SlackIdentityMappingError:
        return cls(
            (
                f"Conflicting Slack identity for {slack_user_id}: linked user_id "
                f"{linked_user_id} disagrees with mapped user_id {mapped_user_id}; "
                "omitting user_id"
            ),
            code="linked_mapped_user_id_conflict",
            slack_user_id=slack_user_id,
            linked_user_id=linked_user_id,
            mapped_user_id=mapped_user_id,
        )


@dataclass(frozen=True, slots=True)
class SlackIdentityRecord:
    """One Slack identity whose attributes cannot be combined across people."""

    slack_user_id: str
    display_name: str
    user_id: str | None

    def without_user_id(self) -> SlackIdentityRecord:
        return SlackIdentityRecord(
            slack_user_id=self.slack_user_id,
            display_name=self.display_name,
            user_id=None,
        )


@dataclass(frozen=True, slots=True)
class SlackIdentityNormalization:
    """Canonical Slack records plus diagnostics that were safe to recover from."""

    records: tuple[SlackIdentityRecord, ...]
    diagnostics: tuple[SlackIdentityMappingError, ...]

    def record_for_slack_user_id(
        self,
        slack_user_id: str,
    ) -> SlackIdentityRecord | None:
        clean_slack_user_id = _clean(slack_user_id)
        return next(
            (
                record
                for record in self.records
                if record.slack_user_id == clean_slack_user_id
            ),
            None,
        )


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def normalize_slack_identities(
    metadata: Mapping[str, Any],
) -> SlackIdentityNormalization:
    """Reconcile both Slack identity stores without ever splicing identities.

    Source precedence is explicit. ``identity_links.slack`` supplies display
    names. For ``user_id``, a populated value from whichever store has one is
    used; matching values collapse to one record. If both stores have populated
    values and they disagree, neither takes precedence: the record gets
    ``user_id=None`` and a :class:`SlackIdentityMappingError` diagnostic is
    returned and logged, never raised. Linked records precede map-only records.
    """

    metadata = _mapping(metadata)
    slack_metadata = _mapping(metadata.get("slack"))
    identity_map = {
        _clean(slack_user_id): user_id
        for slack_user_id, user_id in _mapping(
            slack_metadata.get("identity_map")
        ).items()
        if _clean(slack_user_id)
    }
    identity_links = {
        _clean(slack_user_id): link
        for slack_user_id, link in _mapping(
            _mapping(metadata.get("identity_links")).get("slack")
        ).items()
        if _clean(slack_user_id)
    }
    slack_user_ids = dict.fromkeys([*identity_links, *identity_map])
    records: list[SlackIdentityRecord] = []
    diagnostics: list[SlackIdentityMappingError] = []
    for slack_user_id in slack_user_ids:
        link = _mapping(identity_links.get(slack_user_id))
        linked_user_id = _clean(link.get("user_id")) or None
        mapped_user_id = _clean(identity_map.get(slack_user_id)) or None
        if linked_user_id and mapped_user_id and linked_user_id != mapped_user_id:
            diagnostic = SlackIdentityMappingError.conflicting_user_ids(
                slack_user_id=slack_user_id,
                linked_user_id=linked_user_id,
                mapped_user_id=mapped_user_id,
            )
            diagnostics.append(diagnostic)
            logger.warning("%s", diagnostic)
            user_id = None
        else:
            user_id = linked_user_id or mapped_user_id
        records.append(
            SlackIdentityRecord(
                slack_user_id=slack_user_id,
                display_name=_clean(link.get("display_name")) or slack_user_id,
                user_id=user_id,
            )
        )
    return SlackIdentityNormalization(
        records=tuple(records),
        diagnostics=tuple(diagnostics),
    )


def select_slack_identity_record(
    records: tuple[SlackIdentityRecord, ...],
    *,
    configured_slack_id: str | None,
    configured_user_id: str | None,
    configured_name: str | None,
    default_slack_user_id: str,
    default_name: str,
) -> SlackIdentityRecord:
    """Select one whole record from configured and default identity candidates."""

    by_slack_id = {record.slack_user_id: record for record in records}
    if configured_slack_id:
        return by_slack_id.get(configured_slack_id) or SlackIdentityRecord(
            slack_user_id=configured_slack_id,
            display_name=configured_name or configured_slack_id,
            user_id=None,
        )

    if configured_user_id:
        matched_user = next(
            (
                record
                for record in records
                if record.user_id == configured_user_id
            ),
            None,
        )
        if matched_user is not None:
            return matched_user

    default_name_match = next(
        (
            record
            for record in records
            if record.display_name.casefold() == default_name.casefold()
        ),
        None,
    )
    selected = default_name_match or by_slack_id.get(default_slack_user_id)
    if selected is None:
        selected = SlackIdentityRecord(
            slack_user_id=default_slack_user_id,
            display_name=default_name,
            user_id=None,
        )
    elif (
        selected.slack_user_id == default_slack_user_id
        and selected.display_name == default_slack_user_id
    ):
        selected = SlackIdentityRecord(
            slack_user_id=selected.slack_user_id,
            display_name=default_name,
            user_id=selected.user_id,
        )
    if configured_user_id:
        return selected.without_user_id()
    return selected


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
    normalization = normalize_slack_identities(connection.metadata_ or {})
    if not normalization.records:
        return []
    user_ids = [
        record.user_id
        for record in normalization.records
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
            normalization.records,
            key=lambda value: value.slack_user_id,
        )
    ]


__all__ = [
    "SlackIdentityNormalization",
    "SlackIdentityMappingError",
    "SlackIdentityRecord",
    "link_slack_identity",
    "list_slack_identity_mappings",
    "normalize_slack_identities",
    "select_slack_identity_record",
    "unlink_slack_identity",
]
