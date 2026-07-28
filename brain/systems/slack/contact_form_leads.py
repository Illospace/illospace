"""Slack wire-format decoder for website contact-form submissions."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Mapping


CONTACT_FORM_LEAD_ORIGIN = "contact_form_lead"
CONTACT_FORM_TITLE = "New Contact Form Submission"

_FIELD_KEY_BY_LABEL = {
    "name": "name",
    "email": "email",
    "company website": "company_website",
    "phone": "phone",
    "message": "message",
}
_REQUIRED_FIELDS = frozenset({"name", "email", "company_website", "message"})
_FIELD_PATTERN = re.compile(
    r"(?im)^[ \t]*(?:[-•][ \t]*)?"
    r"[*_`]*(?P<label>Name|Email|Company[ \t]+Website|Phone|Message)"
    r"[*_`]*[ \t]*:[*_`]*[ \t]*"
)


@dataclass(frozen=True, slots=True)
class ContactFormLead:
    """Decoded fields at the Slack wire-format boundary."""

    name: str
    email: str
    company_website: str
    phone: str | None
    message: str

    @classmethod
    def decode(cls, visible_text: str) -> ContactFormLead | None:
        """Decode one submission independently of field order."""

        normalized = str(visible_text or "").replace("\r\n", "\n")
        if CONTACT_FORM_TITLE.casefold() not in normalized.casefold():
            return None

        matches = list(_FIELD_PATTERN.finditer(normalized))
        fields: dict[str, str] = {}
        for index, match in enumerate(matches):
            label = " ".join(match.group("label").casefold().split())
            key = _FIELD_KEY_BY_LABEL[label]
            end = (
                matches[index + 1].start()
                if index + 1 < len(matches)
                else len(normalized)
            )
            value = _clean(normalized[match.end() : end])
            if value:
                fields[key] = value

        if not _REQUIRED_FIELDS.issubset(fields):
            return None
        if "@" not in fields["email"]:
            return None
        return cls(
            name=fields["name"],
            email=fields["email"],
            company_website=fields["company_website"],
            phone=fields.get("phone"),
            message=fields["message"],
        )

    @classmethod
    def from_payload(cls, value: Mapping[str, Any]) -> ContactFormLead:
        return cls(
            name=_clean(value.get("name")),
            email=_clean(value.get("email")),
            company_website=_clean(value.get("company_website")),
            phone=_clean(value.get("phone")) or None,
            message=_clean(value.get("message")),
        )

    def to_payload(self) -> dict[str, str | None]:
        return {
            "name": self.name,
            "email": self.email,
            "company_website": self.company_website,
            "phone": self.phone,
            "message": self.message,
        }


def _clean(value: Any) -> str:
    return str(value or "").strip()


__all__ = [
    "CONTACT_FORM_LEAD_ORIGIN",
    "CONTACT_FORM_TITLE",
    "ContactFormLead",
]
