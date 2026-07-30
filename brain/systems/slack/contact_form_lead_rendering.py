"""Runtime mandate handoff and deterministic reminders for contact-form leads."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Mapping

from brain.systems.runs.obligation_specs import ObligationAnswerer
from brain.systems.slack.contact_form_leads import ContactFormLead


CONTACT_FORM_LEAD_SKILL = "contact-form-lead-intake"


def contact_form_lead_run_message(
    lead: ContactFormLead,
    owner: ObligationAnswerer,
    slack_trigger_payload: Mapping[str, Any],
    *,
    mandate: str | None = None,
) -> str:
    """Hand raw intake context to the installed, runtime-editable lead skill."""

    response_target = slack_trigger_payload.get("response_target")
    response_target = (
        response_target if isinstance(response_target, Mapping) else {}
    )
    intake_context = {
        "lead": lead.to_payload(),
        "owner": owner.to_metadata(),
        "source": {
            "channel_id": slack_trigger_payload.get("channel_id"),
            "thread_ts": response_target.get("thread_ts"),
            "permalink": slack_trigger_payload.get("permalink"),
        },
    }
    if mandate:
        mandate_header = [
            "Execute this Slack connection's runtime-configured contact-form mandate:",
            "",
            mandate,
        ]
    else:
        mandate_header = [
            f"/{CONTACT_FORM_LEAD_SKILL}",
            "",
            (
                "Load the current installed procedure for this skill with "
                "skill_view, then execute it for this monitored contact-form event."
            ),
        ]
    return "\n".join(
        [
            *mandate_header,
            "",
            "Intake context:",
            json.dumps(
                intake_context,
                ensure_ascii=False,
                sort_keys=True,
            ),
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


__all__ = [
    "CONTACT_FORM_LEAD_SKILL",
    "ContactFormLeadReminderRenderer",
    "contact_form_lead_run_message",
]
