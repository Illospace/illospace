"""Slack channel monitoring configuration helpers.

A "monitored" Slack channel is one where Illo passively observes *every* message
(not just @-mentions/DMs), acknowledges each with a lightweight reaction, and
decides per-message whether the content is ticket-worthy. The set of monitored
channels lives on the Slack connection's ``metadata_["slack"]["monitored_channels"]``
so it can be configured at runtime by Illo (via ``manage_slack``) and picked up by
the connector on the next event with no restart.
"""

from __future__ import annotations

from typing import Any, Mapping

from brain.platform.db.models.external_agent import ExternalAgentConnectionRow


CONTACT_FORM_LEAD_MANDATE_KEY = "contact_form_lead_mandate"
CONTACT_FORM_LEAD_MANDATE_MAX_CHARS = 20_000
DISABLED_INTAKES_KEY = "disabled_intakes"


class SlackMonitorConfigError(ValueError):
    """Raised when a Slack channel monitor cannot be configured."""


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _connection_metadata(connection: Any) -> dict[str, Any]:
    metadata = getattr(connection, "metadata_", None)
    if metadata is None and isinstance(connection, Mapping):
        metadata = connection.get("metadata_") or connection.get("metadata")
    return dict(metadata or {})


def _slack_metadata(connection: Any) -> dict[str, Any]:
    slack_metadata = _connection_metadata(connection).get("slack")
    return dict(slack_metadata or {}) if isinstance(slack_metadata, Mapping) else {}


def _normalize_entries(raw: Any) -> list[dict[str, Any]]:
    """Normalize stored monitored-channel entries into a list of dicts.

    Accepts either a list of channel-id strings or a list of dicts so older or
    hand-edited configurations keep working.
    """

    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw or []:
        if isinstance(item, Mapping):
            channel_id = _clean(item.get("channel_id") or item.get("id"))
            if not channel_id or channel_id in seen:
                continue
            entry = {"channel_id": channel_id}
            channel_name = _clean(item.get("channel_name") or item.get("name"))
            if channel_name:
                entry["channel_name"] = channel_name
            entry["enabled"] = item.get("enabled") is not False
            entries.append(entry)
            seen.add(channel_id)
        else:
            channel_id = _clean(item)
            if not channel_id or channel_id in seen:
                continue
            entries.append({"channel_id": channel_id, "enabled": True})
            seen.add(channel_id)
    return entries


def monitored_channels(connection: ExternalAgentConnectionRow) -> list[dict[str, Any]]:
    """Return normalized monitored-channel configuration without a DB read."""

    return _normalize_entries(_slack_metadata(connection).get("monitored_channels"))


def monitored_channel_ids(connection: ExternalAgentConnectionRow) -> set[str]:
    """Return the set of enabled monitored channel ids for a connection.

    Pure read helper (no session) used by the connector on the hot path.
    """

    entries = monitored_channels(connection)
    return {entry["channel_id"] for entry in entries if entry.get("enabled", True)}


def contact_form_lead_mandate(
    connection: ExternalAgentConnectionRow,
) -> str | None:
    """Return the optional connection overlay for contact-form lead behavior."""

    value = _clean(
        _slack_metadata(connection).get(CONTACT_FORM_LEAD_MANDATE_KEY)
    )
    return value or None


def _configurable_intake_origins() -> frozenset[str]:
    from brain.systems.slack.monitored_intakes import (
        configurable_monitored_intake_origins,
    )

    return configurable_monitored_intake_origins()


def disabled_intake_origins(
    connection: ExternalAgentConnectionRow,
) -> set[str]:
    """Return disabled typed-intake origins; all typed intakes default enabled."""

    raw = _slack_metadata(connection).get(DISABLED_INTAKES_KEY)
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return set()
    valid_origins = _configurable_intake_origins()
    return {
        origin
        for item in raw
        if (origin := _clean(item)) in valid_origins
    }


async def _connection_for_org(
    session,
    connection_id: str,
    org_id: str | None,
) -> ExternalAgentConnectionRow:
    connection = await session.get(ExternalAgentConnectionRow, str(connection_id))
    if connection is None:
        raise SlackMonitorConfigError("Slack connection not found")
    if connection.agent_kind != "slack" or connection.transport != "slack_socket_mode":
        raise SlackMonitorConfigError("Connection is not a Slack Socket Mode connection")
    if org_id and str(connection.org_id) != str(org_id):
        raise SlackMonitorConfigError("Slack connection is outside this org")
    return connection


def _write_entries(connection: ExternalAgentConnectionRow, entries: list[dict[str, Any]]) -> None:
    root = dict(connection.metadata_ or {})
    slack_metadata = _slack_metadata(connection)
    slack_metadata["monitored_channels"] = entries
    root["slack"] = slack_metadata
    connection.metadata_ = root


async def _write_contact_form_lead_mandate(
    session,
    *,
    connection_id: str,
    mandate: str | None,
    org_id: str | None = None,
) -> dict[str, Any]:
    connection = await _connection_for_org(session, connection_id, org_id)
    root = dict(connection.metadata_ or {})
    slack_metadata = _slack_metadata(connection)
    if mandate is not None:
        slack_metadata[CONTACT_FORM_LEAD_MANDATE_KEY] = mandate
    else:
        slack_metadata.pop(CONTACT_FORM_LEAD_MANDATE_KEY, None)
    root["slack"] = slack_metadata
    connection.metadata_ = root
    await session.flush()
    return {
        "connection_id": str(connection.id),
        "metadata_path": f"slack.{CONTACT_FORM_LEAD_MANDATE_KEY}",
        "mandate": mandate,
        "cleared": mandate is None,
    }


async def set_contact_form_lead_mandate(
    session,
    *,
    connection_id: str,
    mandate: str,
    org_id: str | None = None,
) -> dict[str, Any]:
    """Persist a non-empty operator-editable skill overlay."""

    clean_mandate = _clean(mandate)
    if not clean_mandate:
        raise SlackMonitorConfigError(
            "set_contact_form_lead_mandate requires a non-empty mandate"
        )
    if len(clean_mandate) > CONTACT_FORM_LEAD_MANDATE_MAX_CHARS:
        raise SlackMonitorConfigError(
            "mandate exceeds "
            f"{CONTACT_FORM_LEAD_MANDATE_MAX_CHARS} characters"
        )
    return await _write_contact_form_lead_mandate(
        session,
        connection_id=connection_id,
        mandate=clean_mandate,
        org_id=org_id,
    )


async def clear_contact_form_lead_mandate(
    session,
    *,
    connection_id: str,
    org_id: str | None = None,
) -> dict[str, Any]:
    """Clear the operator-editable skill overlay."""

    return await _write_contact_form_lead_mandate(
        session,
        connection_id=connection_id,
        mandate=None,
        org_id=org_id,
    )


def _validated_configurable_intake_origin(intake: str) -> str:
    from brain.systems.slack.monitored_intakes import (
        SLACK_CHANNEL_MESSAGE_ORIGIN,
    )

    clean_intake = _clean(intake)
    if not clean_intake:
        raise SlackMonitorConfigError("intake is required")
    if clean_intake == SLACK_CHANNEL_MESSAGE_ORIGIN:
        raise SlackMonitorConfigError(
            f"{SLACK_CHANNEL_MESSAGE_ORIGIN} is the fallback intake; "
            "use unmonitor_channel for channel-level monitoring"
        )
    valid_origins = _configurable_intake_origins()
    if clean_intake not in valid_origins:
        valid = ", ".join(sorted(valid_origins)) or "(none)"
        raise SlackMonitorConfigError(
            f"Unknown monitored intake {clean_intake!r}; "
            f"valid typed intakes: {valid}"
        )
    return clean_intake


async def _write_intake_enabled(
    session,
    *,
    connection_id: str,
    intake: str,
    enabled: bool,
    org_id: str | None = None,
) -> dict[str, Any]:
    clean_intake = _validated_configurable_intake_origin(intake)
    connection = await _connection_for_org(session, connection_id, org_id)
    disabled_intakes = disabled_intake_origins(connection)
    if enabled:
        disabled_intakes.discard(clean_intake)
    else:
        disabled_intakes.add(clean_intake)

    root = dict(connection.metadata_ or {})
    slack_metadata = _slack_metadata(connection)
    if disabled_intakes:
        slack_metadata[DISABLED_INTAKES_KEY] = sorted(disabled_intakes)
    else:
        slack_metadata.pop(DISABLED_INTAKES_KEY, None)
    root["slack"] = slack_metadata
    connection.metadata_ = root
    await session.flush()
    return {
        "connection_id": str(connection.id),
        "metadata_path": f"slack.{DISABLED_INTAKES_KEY}",
        "intake": clean_intake,
        "enabled": enabled,
        "disabled_intakes": sorted(disabled_intakes),
    }


async def disable_intake(
    session,
    *,
    connection_id: str,
    intake: str,
    org_id: str | None = None,
) -> dict[str, Any]:
    """Disable one typed monitored intake without unmonitoring its channel."""

    return await _write_intake_enabled(
        session,
        connection_id=connection_id,
        intake=intake,
        enabled=False,
        org_id=org_id,
    )


async def enable_intake(
    session,
    *,
    connection_id: str,
    intake: str,
    org_id: str | None = None,
) -> dict[str, Any]:
    """Restore the default-enabled routing for one typed monitored intake."""

    return await _write_intake_enabled(
        session,
        connection_id=connection_id,
        intake=intake,
        enabled=True,
        org_id=org_id,
    )


async def list_monitored_channels(
    session,
    *,
    connection_id: str,
    org_id: str | None = None,
) -> list[dict[str, Any]]:
    connection = await _connection_for_org(session, connection_id, org_id)
    return monitored_channels(connection)


async def add_monitored_channel(
    session,
    *,
    connection_id: str,
    channel_id: str,
    org_id: str | None = None,
    channel_name: str | None = None,
) -> dict[str, Any]:
    clean_channel_id = _clean(channel_id)
    if not clean_channel_id:
        raise SlackMonitorConfigError("channel_id is required")
    connection = await _connection_for_org(session, connection_id, org_id)
    entries = _normalize_entries(_slack_metadata(connection).get("monitored_channels"))
    clean_channel_name = _clean(channel_name) or None
    updated = False
    for entry in entries:
        if entry["channel_id"] == clean_channel_id:
            entry["enabled"] = True
            if clean_channel_name:
                entry["channel_name"] = clean_channel_name
            updated = True
            break
    if not updated:
        entry = {"channel_id": clean_channel_id, "enabled": True}
        if clean_channel_name:
            entry["channel_name"] = clean_channel_name
        entries.append(entry)
    _write_entries(connection, entries)
    await session.flush()
    return {
        "channel_id": clean_channel_id,
        "channel_name": clean_channel_name,
        "monitored": True,
        "monitored_channels": entries,
    }


async def remove_monitored_channel(
    session,
    *,
    connection_id: str,
    channel_id: str,
    org_id: str | None = None,
) -> dict[str, Any]:
    clean_channel_id = _clean(channel_id)
    if not clean_channel_id:
        raise SlackMonitorConfigError("channel_id is required")
    connection = await _connection_for_org(session, connection_id, org_id)
    entries = _normalize_entries(_slack_metadata(connection).get("monitored_channels"))
    remaining = [entry for entry in entries if entry["channel_id"] != clean_channel_id]
    removed = len(remaining) != len(entries)
    _write_entries(connection, remaining)
    await session.flush()
    return {
        "channel_id": clean_channel_id,
        "removed": removed,
        "monitored_channels": remaining,
    }


__all__ = [
    "CONTACT_FORM_LEAD_MANDATE_KEY",
    "DISABLED_INTAKES_KEY",
    "SlackMonitorConfigError",
    "add_monitored_channel",
    "clear_contact_form_lead_mandate",
    "contact_form_lead_mandate",
    "disable_intake",
    "disabled_intake_origins",
    "enable_intake",
    "list_monitored_channels",
    "monitored_channel_ids",
    "monitored_channels",
    "remove_monitored_channel",
    "set_contact_form_lead_mandate",
]
