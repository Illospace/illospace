"""Single-source owner policy for contact-form lead intake."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from brain.systems.runs.obligation_specs import ObligationAnswerer


@dataclass(frozen=True, slots=True)
class ContactFormOwnerPolicy:
    default_name: str
    default_slack_user_id: str

    def resolve(self, connection: Any) -> ObligationAnswerer:
        """Resolve the owner once; downstream consumers receive a complete value."""

        metadata = _connection_metadata(connection)
        slack_metadata = _mapping(metadata.get("slack"))
        configured = _mapping(slack_metadata.get("contact_form_lead_owner"))
        identity_map = _mapping(slack_metadata.get("identity_map"))
        identity_links = _mapping(_mapping(metadata.get("identity_links")).get("slack"))

        configured_user_id = _clean(configured.get("user_id")) or None
        configured_slack_id = _clean(configured.get("slack_user_id")) or None
        configured_name = _clean(configured.get("name")) or None
        if configured_slack_id:
            link = _mapping(identity_links.get(configured_slack_id))
            return ObligationAnswerer(
                name=(
                    configured_name
                    or _clean(link.get("display_name"))
                    or self.default_name
                ),
                slack_user_id=configured_slack_id,
                user_id=(
                    configured_user_id
                    or _clean(link.get("user_id"))
                    or _clean(identity_map.get(configured_slack_id))
                    or None
                ),
            )

        if configured_user_id:
            for slack_user_id, mapped_user_id in identity_map.items():
                if _clean(mapped_user_id) != configured_user_id:
                    continue
                link = _mapping(identity_links.get(slack_user_id))
                return ObligationAnswerer(
                    name=(
                        configured_name
                        or _clean(link.get("display_name"))
                        or self.default_name
                    ),
                    slack_user_id=_clean(slack_user_id),
                    user_id=configured_user_id,
                )

        for slack_user_id, raw_link in identity_links.items():
            link = _mapping(raw_link)
            display_name = _clean(link.get("display_name"))
            if display_name.casefold() != self.default_name.casefold():
                continue
            return ObligationAnswerer(
                name=display_name or self.default_name,
                slack_user_id=_clean(slack_user_id),
                user_id=_clean(link.get("user_id")) or None,
            )

        default_link = _mapping(identity_links.get(self.default_slack_user_id))
        return ObligationAnswerer(
            name=_clean(default_link.get("display_name")) or self.default_name,
            slack_user_id=self.default_slack_user_id,
            user_id=(
                configured_user_id
                or _clean(default_link.get("user_id"))
                or _clean(identity_map.get(self.default_slack_user_id))
                or None
            ),
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


__all__ = ["CONTACT_FORM_OWNER_POLICY", "ContactFormOwnerPolicy"]
