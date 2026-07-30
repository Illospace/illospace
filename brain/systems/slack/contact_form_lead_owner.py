"""Single-source owner policy for contact-form lead intake."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from brain.systems.runs.obligation_specs import ObligationAnswerer
from brain.systems.slack.identity import (
    normalize_slack_identities,
    select_slack_identity_record,
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
        normalization = normalize_slack_identities(metadata)

        configured_user_id = _clean(configured.get("user_id")) or None
        configured_slack_id = _clean(configured.get("slack_user_id")) or None
        configured_name = _clean(configured.get("name")) or None
        selected = select_slack_identity_record(
            normalization.records,
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
]
