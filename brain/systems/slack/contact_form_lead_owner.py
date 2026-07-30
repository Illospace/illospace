"""Single-source owner policy for contact-form lead intake."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping

from brain.kernel.common.coercion import as_mapping, optional_text
from brain.systems.runs.obligation_specs import ObligationAnswerer
from brain.systems.slack.identity import (
    SlackIdentityRecord,
    normalize_slack_identities,
)


@dataclass(frozen=True, slots=True)
class ContactFormOwnerPolicy:
    default_name: str
    default_slack_user_id: str

    def resolve(self, connection: Any) -> ObligationAnswerer:
        """Resolve the owner once; downstream consumers receive a complete value."""

        metadata = _connection_metadata(connection)
        slack_metadata = as_mapping(metadata.get("slack"))
        configured = as_mapping(slack_metadata.get("contact_form_lead_owner"))
        records, _conflicts = normalize_slack_identities(metadata)

        selected = self._select_identity_record(
            records,
            configured_slack_id=_configured_text(
                configured.get("slack_user_id")
            ),
            configured_user_id=_configured_text(configured.get("user_id")),
            configured_name=_configured_text(configured.get("name")),
        )
        return ObligationAnswerer(
            name=selected.display_name,
            slack_user_id=selected.slack_user_id,
            user_id=selected.user_id,
        )

    def _select_identity_record(
        self,
        records: Mapping[str, SlackIdentityRecord],
        *,
        configured_slack_id: str | None,
        configured_user_id: str | None,
        configured_name: str | None,
    ) -> SlackIdentityRecord:
        """Select one whole record from configured and default owner candidates."""

        if configured_slack_id:
            return records.get(configured_slack_id) or SlackIdentityRecord(
                slack_user_id=configured_slack_id,
                display_name=configured_name or configured_slack_id,
                user_id=None,
            )

        if configured_user_id:
            matched_user = next(
                (
                    record
                    for record in records.values()
                    if record.user_id == configured_user_id
                ),
                None,
            )
            if matched_user is not None:
                return matched_user

        default_name_match = next(
            (
                record
                for record in records.values()
                if record.display_name.casefold() == self.default_name.casefold()
            ),
            None,
        )
        selected = default_name_match or records.get(self.default_slack_user_id)
        if selected is None:
            selected = SlackIdentityRecord(
                slack_user_id=self.default_slack_user_id,
                display_name=self.default_name,
                user_id=None,
            )
        elif (
            selected.slack_user_id == self.default_slack_user_id
            and selected.display_name == self.default_slack_user_id
        ):
            selected = replace(selected, display_name=self.default_name)
        if configured_user_id:
            return replace(selected, user_id=None)
        return selected


CONTACT_FORM_OWNER_POLICY = ContactFormOwnerPolicy(
    default_name="Reda",
    default_slack_user_id="U04R1A6MZST",
)


def _configured_text(value: Any) -> str | None:
    return optional_text(value) if value else None


def _connection_value(connection: Any, key: str) -> Any:
    if isinstance(connection, Mapping):
        return connection.get(key)
    return getattr(connection, key, None)


def _connection_metadata(connection: Any) -> dict[str, Any]:
    return as_mapping(
        _connection_value(connection, "metadata_")
        or _connection_value(connection, "metadata")
    )


__all__ = [
    "CONTACT_FORM_OWNER_POLICY",
    "ContactFormOwnerPolicy",
]
