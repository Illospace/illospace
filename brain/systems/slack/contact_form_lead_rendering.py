"""Vertical policy and deterministic rendering for contact-form leads."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Mapping

from brain.systems.runs.obligation_specs import ObligationAnswerer
from brain.systems.slack.contact_form_leads import ContactFormLead


_NUMBERED_ASK_PATTERN = re.compile(
    r"(?m)^[ \t]*\d+[.)][ \t]*(?P<ask>[^\n]+)"
)


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


def infer_contact_form_vertical(lead: ContactFormLead) -> str:
    evidence = f"{lead.company_website} {lead.message}".casefold()
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


def contact_form_lead_dossier(
    lead: ContactFormLead,
    owner: ObligationAnswerer,
) -> str:
    """Render safe intake copy without asserting unverified capabilities."""

    owner_mention = f"<@{owner.slack_user_id}>"
    asks = extract_contact_form_asks(lead.message)
    lines = [
        "*Contact-form lead*",
        f"*Who:* {lead.name} — {lead.email}",
        f"*Vertical:* {infer_contact_form_vertical(lead)}",
        f"*Site:* {lead.company_website}",
    ]
    if lead.phone:
        lines.append(f"*Phone:* {lead.phone}")
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
            f"*Owner:* {owner_mention} ({owner.name})",
            (
                f"*Next action:* {owner_mention}, reply in this thread with verified "
                f"answers for {lead.name}, then send them to {lead.email}."
            ),
        ]
    )
    return "\n".join(lines)


def contact_form_lead_run_message(
    dossier: str,
    slack_trigger_payload: Mapping[str, Any],
) -> str:
    response_target = slack_trigger_payload.get("response_target")
    response_target = response_target if isinstance(response_target, Mapping) else {}
    return "\n".join(
        [
            "A qualified website contact-form lead arrived in a monitored Slack channel.",
            "The connector already acknowledged the source message with 👀.",
            (
                "Post the exact dossier below once with post_slack_reply in the source "
                f"thread (channel_id={slack_trigger_payload.get('channel_id')}, "
                f"thread_ts={response_target.get('thread_ts')}) and set "
                "answers_open_ask=false."
            ),
            (
                "Do not research, infer, rephrase, or add product claims in this intake "
                "run. Unknown capability claims must remain marked needs a human answer."
            ),
            "",
            dossier,
        ]
    )


@dataclass(frozen=True, slots=True)
class ContactFormLeadReminderRenderer:
    lead: ContactFormLead
    owner: ObligationAnswerer

    def render(self) -> str:
        return (
            f"<@{self.owner.slack_user_id}> ({self.owner.name}), this contact-form "
            f"lead from {self.lead.name} is still unanswered after 24h. Next action: "
            "reply in this thread with verified answers, then send them to "
            f"{self.lead.email}."
        )


def _clean(value: Any) -> str:
    return str(value or "").strip()


__all__ = [
    "ContactFormLeadReminderRenderer",
    "contact_form_lead_dossier",
    "contact_form_lead_run_message",
    "extract_contact_form_asks",
    "infer_contact_form_vertical",
]
