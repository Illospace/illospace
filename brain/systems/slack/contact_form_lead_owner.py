"""Single-source owner policy for contact-form lead intake."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from brain.systems.runs.obligation_specs import ObligationAnswerer


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
class ContactFormOwnerPolicy:
    default_name: str
    default_slack_user_id: str

    def resolve(self, connection: Any) -> ObligationAnswerer:
        """Resolve the owner once; downstream consumers receive a complete value."""

        metadata = _connection_metadata(connection)
        slack_metadata = _mapping(metadata.get("slack"))
        configured = _mapping(slack_metadata.get("contact_form_lead_owner"))
        records = _slack_identity_records(metadata)

        configured_user_id = _clean(configured.get("user_id")) or None
        configured_slack_id = _clean(configured.get("slack_user_id")) or None
        configured_name = _clean(configured.get("name")) or None
        selected = _select_identity_record(
            records,
            configured_slack_id=configured_slack_id,
            configured_user_id=configured_user_id,
            configured_name=configured_name,
            default_slack_user_id=self.default_slack_user_id,
            default_name=self.default_name,
        )
        return ObligationAnswerer(
            name=selected.display_name,
            slack_user_id=selected.slack_user_id,
            user_id=selected.user_id,
        )


CONTACT_FORM_OWNER_POLICY = ContactFormOwnerPolicy(
    default_name="Reda",
    default_slack_user_id="U04R1A6MZST",
)


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _slack_identity_records(
    metadata: Mapping[str, Any],
) -> tuple[SlackIdentityRecord, ...]:
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
    slack_user_ids = dict.fromkeys(
        [
            *(_clean(value) for value in identity_links),
            *(_clean(value) for value in identity_map),
        ]
    )
    records: list[SlackIdentityRecord] = []
    for slack_user_id in slack_user_ids:
        if not slack_user_id:
            continue
        link = _mapping(identity_links.get(slack_user_id))
        linked_user_id = _clean(link.get("user_id")) or None
        mapped_user_id = _clean(identity_map.get(slack_user_id)) or None
        if linked_user_id and mapped_user_id and linked_user_id != mapped_user_id:
            user_id = None
        else:
            user_id = linked_user_id or mapped_user_id
        records.append(
            SlackIdentityRecord(
                slack_user_id=slack_user_id,
                display_name=(
                    _clean(link.get("display_name"))
                    or slack_user_id
                ),
                user_id=user_id,
            )
        )
    return tuple(records)


def _select_identity_record(
    records: tuple[SlackIdentityRecord, ...],
    *,
    configured_slack_id: str | None,
    configured_user_id: str | None,
    configured_name: str | None,
    default_slack_user_id: str,
    default_name: str,
) -> SlackIdentityRecord:
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


def _connection_value(connection: Any, key: str) -> Any:
    if isinstance(connection, Mapping):
        return connection.get(key)
    return getattr(connection, key, None)


def _connection_metadata(connection: Any) -> dict[str, Any]:
    return _mapping(
        _connection_value(connection, "metadata_")
        or _connection_value(connection, "metadata")
    )


__all__ = [
    "CONTACT_FORM_OWNER_POLICY",
    "ContactFormOwnerPolicy",
    "SlackIdentityRecord",
]
