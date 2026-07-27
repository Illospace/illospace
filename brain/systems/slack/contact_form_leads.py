"""Deterministic parsing and routing copy for website contact-form leads."""

from __future__ import annotations

import re
from typing import Any, Mapping


CONTACT_FORM_LEAD_ORIGIN = "contact_form_lead"
CONTACT_FORM_TITLE = "New Contact Form Submission"
DEFAULT_CONTACT_FORM_OWNER_NAME = "Reda"
DEFAULT_CONTACT_FORM_OWNER_SLACK_ID = "U04R1A6MZST"

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
_NUMBERED_ASK_PATTERN = re.compile(
    r"(?m)^[ \t]*\d+[.)][ \t]*(?P<ask>[^\n]+)"
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


def _plain_slack_text(text: str) -> str:
    """Normalize line endings without changing submitted field values."""

    return str(text or "").replace("\r\n", "\n")


def parse_contact_form_lead(text: str) -> dict[str, str | None] | None:
    """Parse the contact-form bot shape, independent of field order.

    Classification requires the explicit submission title and all non-optional
    fields. Phone is intentionally optional. A monitored channel alone is never
    enough to produce a lead.
    """

    normalized = _plain_slack_text(text)
    if CONTACT_FORM_TITLE.casefold() not in normalized.casefold():
        return None

    matches = list(_FIELD_PATTERN.finditer(normalized))
    fields: dict[str, str] = {}
    for index, match in enumerate(matches):
        label = " ".join(match.group("label").casefold().split())
        key = _FIELD_KEY_BY_LABEL[label]
        end = matches[index + 1].start() if index + 1 < len(matches) else len(normalized)
        value = _clean(normalized[match.end() : end])
        if value:
            fields[key] = value

    if not _REQUIRED_FIELDS.issubset(fields):
        return None
    if "@" not in fields["email"]:
        return None
    return {
        "name": fields["name"],
        "email": fields["email"],
        "company_website": fields["company_website"],
        "phone": fields.get("phone"),
        "message": fields["message"],
    }


def extract_contact_form_asks(message: str) -> list[str]:
    """Break a lead's message into separately actionable questions."""

    source = _clean(message)
    if not source:
        return []

    asks: list[str] = []
    numbered_spans: list[tuple[int, int]] = []
    for match in _NUMBERED_ASK_PATTERN.finditer(source):
        ask = " ".join(match.group("ask").split())
        if ask:
            asks.append(ask)
            numbered_spans.append(match.span())

    remainder_parts: list[str] = []
    cursor = 0
    for start, end in numbered_spans:
        remainder_parts.append(source[cursor:start])
        cursor = end
    remainder_parts.append(source[cursor:])
    remainder = "\n".join(remainder_parts)
    pending_context = ""
    for sentence in re.split(r"(?<=[.!?])[ \t]+|\n+", remainder):
        ask = " ".join(sentence.split()).strip(" -•")
        if "?" in ask:
            if pending_context:
                ask = f"{pending_context} {ask}"
            asks.append(ask)
            pending_context = ""
        elif any(
            marker in ask.casefold()
            for marker in ("verification", "enablement", "may require", "feature")
        ):
            pending_context = ask

    if not asks:
        asks.append(" ".join(source.split()))

    deduplicated: list[str] = []
    seen: set[str] = set()
    for ask in asks:
        key = ask.casefold()
        if key in seen:
            continue
        deduplicated.append(ask)
        seen.add(key)
    return deduplicated


def infer_contact_form_vertical(lead: Mapping[str, Any]) -> str:
    website = _clean(lead.get("company_website")).casefold()
    message = _clean(lead.get("message")).casefold()
    evidence = f"{website} {message}"
    if any(
        term in evidence
        for term in ("lingerie", "bikini", "intimate apparel", "corset", "thong")
    ):
        return "Lingerie / intimate-apparel e-commerce"
    if "bergzeit" in evidence or any(
        term in evidence for term in ("outdoor apparel", "outdoor retail")
    ):
        return "Outdoor retail e-commerce"
    return "E-commerce / retail"


def contact_form_lead_dossier(lead: Mapping[str, Any]) -> str:
    """Render the exact safe dossier a contact-form intake run should post."""

    owner = _mapping(lead.get("owner"))
    owner_name = _clean(owner.get("name")) or DEFAULT_CONTACT_FORM_OWNER_NAME
    owner_slack_id = (
        _clean(owner.get("slack_user_id")) or DEFAULT_CONTACT_FORM_OWNER_SLACK_ID
    )
    owner_mention = f"<@{owner_slack_id}>"
    name = _clean(lead.get("name")) or "Unknown sender"
    email = _clean(lead.get("email")) or "not provided"
    website = _clean(lead.get("company_website")) or "not provided"
    phone = _clean(lead.get("phone"))
    asks = extract_contact_form_asks(_clean(lead.get("message")))

    lines = [
        "*Contact-form lead*",
        f"*Who:* {name} — {email}",
        f"*Vertical:* {infer_contact_form_vertical(lead)}",
        f"*Site:* {website}",
    ]
    if phone:
        lines.append(f"*Phone:* {phone}")
    lines.extend(["", "*Asks:*"])
    for index, ask in enumerate(asks, start=1):
        lines.extend(
            [
                f"{index}. {ask}",
                (
                    "   *Answer:* needs a human answer — no verified product "
                    "capability source is attached to this intake."
                ),
            ]
        )
    lines.extend(
        [
            "",
            f"*Owner:* {owner_mention} ({owner_name})",
            (
                f"*Next action:* {owner_mention}, reply in this thread with verified "
                f"answers for {name}, then send them to {email}."
            ),
        ]
    )
    return "\n".join(lines)


def contact_form_lead_owner(connection: Any) -> dict[str, str | None]:
    """Resolve the named sales owner, defaulting to Reda.

    An explicit per-connection owner wins. Otherwise, prefer the verified Slack
    identity link named Reda, then the connection owner's linked Slack identity.
    The Uwear workspace's Reda id is the final default so the dossier always has
    a real mention rather than a fictional on-call role.
    """

    metadata = _connection_metadata(connection)
    slack_metadata = _mapping(metadata.get("slack"))
    configured = _mapping(slack_metadata.get("contact_form_lead_owner"))
    identity_map = _mapping(slack_metadata.get("identity_map"))
    identity_links = _mapping(_mapping(metadata.get("identity_links")).get("slack"))

    configured_user_id = _clean(configured.get("user_id")) or None
    configured_slack_id = _clean(configured.get("slack_user_id")) or None
    configured_name = _clean(configured.get("name")) or None
    if configured_slack_id:
        return {
            "name": configured_name or DEFAULT_CONTACT_FORM_OWNER_NAME,
            "slack_user_id": configured_slack_id,
            "user_id": configured_user_id,
        }

    for slack_user_id, raw_link in identity_links.items():
        link = _mapping(raw_link)
        if _clean(link.get("display_name")).casefold() != "reda":
            continue
        return {
            "name": _clean(link.get("display_name")) or DEFAULT_CONTACT_FORM_OWNER_NAME,
            "slack_user_id": _clean(slack_user_id),
            "user_id": _clean(link.get("user_id")) or None,
        }

    owner_user_id = (
        configured_user_id
        or _clean(_connection_value(connection, "owner_user_id"))
        or None
    )
    if owner_user_id:
        for slack_user_id, mapped_user_id in identity_map.items():
            if _clean(mapped_user_id) != owner_user_id:
                continue
            link = _mapping(identity_links.get(slack_user_id))
            return {
                "name": _clean(link.get("display_name"))
                or DEFAULT_CONTACT_FORM_OWNER_NAME,
                "slack_user_id": _clean(slack_user_id),
                "user_id": owner_user_id,
            }

    return {
        "name": DEFAULT_CONTACT_FORM_OWNER_NAME,
        "slack_user_id": DEFAULT_CONTACT_FORM_OWNER_SLACK_ID,
        "user_id": owner_user_id,
    }


def attach_contact_form_lead_owner(
    envelope: dict[str, Any],
    connection: Any,
) -> None:
    payload = _mapping(envelope.get("payload"))
    lead = _mapping(payload.get("contact_form_lead"))
    if not lead:
        return
    lead["owner"] = contact_form_lead_owner(connection)
    payload["contact_form_lead"] = lead
    envelope["payload"] = payload


__all__ = [
    "CONTACT_FORM_LEAD_ORIGIN",
    "CONTACT_FORM_TITLE",
    "DEFAULT_CONTACT_FORM_OWNER_NAME",
    "DEFAULT_CONTACT_FORM_OWNER_SLACK_ID",
    "attach_contact_form_lead_owner",
    "contact_form_lead_dossier",
    "contact_form_lead_owner",
    "extract_contact_form_asks",
    "infer_contact_form_vertical",
    "parse_contact_form_lead",
]
