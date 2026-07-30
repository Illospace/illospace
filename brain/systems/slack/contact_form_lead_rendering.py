"""Skill handoff and deterministic reminders for contact-form leads."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Mapping

from brain.systems.runs.obligation_specs import ObligationAnswerer
from brain.systems.slack.contact_form_leads import ContactFormLead


CONTACT_FORM_LEAD_SKILL = "contact-form-lead-intake"
CONTACT_FORM_LEAD_INTAKE_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class ContactFormLeadSlackResponseTarget:
    """Canonical Slack destination supplied to the contact-form lead skill."""

    channel_id: str
    thread_ts: str

    @classmethod
    def from_slack_trigger(
        cls,
        slack_trigger_payload: Mapping[str, Any],
    ) -> ContactFormLeadSlackResponseTarget:
        response_target = slack_trigger_payload.get("response_target")
        target = response_target if isinstance(response_target, Mapping) else {}
        return cls(
            channel_id=_clean(target.get("channel_id")),
            thread_ts=_clean(target.get("thread_ts")),
        )

    def to_payload(self) -> dict[str, str]:
        if not _clean(self.channel_id):
            raise ValueError(
                "contact-form lead intake requires a Slack response target channel_id"
            )
        if not _clean(self.thread_ts):
            raise ValueError(
                "contact-form lead intake requires a Slack response target thread_ts"
            )
        return {
            "channel_id": _clean(self.channel_id),
            "thread_ts": _clean(self.thread_ts),
        }


@dataclass(frozen=True, slots=True)
class ContactFormLeadIntakeContext:
    """Versioned model input for one decoded contact-form lead."""

    lead: ContactFormLead
    owner: ObligationAnswerer
    slack_response_target: ContactFormLeadSlackResponseTarget
    source_permalink: str | None = None
    schema_version: int = CONTACT_FORM_LEAD_INTAKE_SCHEMA_VERSION

    @classmethod
    def from_slack_trigger(
        cls,
        lead: ContactFormLead,
        owner: ObligationAnswerer,
        slack_trigger_payload: Mapping[str, Any],
    ) -> ContactFormLeadIntakeContext:
        return cls(
            lead=lead,
            owner=owner,
            slack_response_target=(
                ContactFormLeadSlackResponseTarget.from_slack_trigger(
                    slack_trigger_payload
                )
            ),
            source_permalink=_clean(slack_trigger_payload.get("permalink")) or None,
        )

    def to_payload(self) -> dict[str, Any]:
        if self.schema_version != CONTACT_FORM_LEAD_INTAKE_SCHEMA_VERSION:
            raise ValueError(
                "unsupported contact-form lead intake schema_version "
                f"{self.schema_version}"
            )
        return {
            "schema_version": self.schema_version,
            "lead": self.lead.to_payload(),
            "owner": self.owner.to_metadata(),
            "slack_response_target": self.slack_response_target.to_payload(),
            "source_permalink": self.source_permalink,
        }

    def serialize(self) -> str:
        """Validate and serialize the internal prompt contract."""

        return json.dumps(
            self.to_payload(),
            ensure_ascii=False,
            sort_keys=True,
        )


def contact_form_lead_run_message(
    lead: ContactFormLead,
    owner: ObligationAnswerer,
    slack_trigger_payload: Mapping[str, Any],
    *,
    mandate: str | None = None,
) -> str:
    """Invoke the installed skill with an optional connection overlay."""

    intake_context = ContactFormLeadIntakeContext.from_slack_trigger(
        lead,
        owner,
        slack_trigger_payload,
    )
    invocation = [
        f"/{CONTACT_FORM_LEAD_SKILL}",
        "",
        (
            "Load the current installed procedure for this skill with "
            "skill_view, then execute it for this monitored contact-form event."
        ),
    ]
    if mandate:
        invocation.extend(
            [
                "",
                "Connection overlay:",
                (
                    "Apply this extra instruction within the installed skill's "
                    "contracts:"
                ),
                "",
                mandate,
            ]
        )
    return "\n".join(
        [
            *invocation,
            "",
            "Intake context:",
            intake_context.serialize(),
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
    "CONTACT_FORM_LEAD_INTAKE_SCHEMA_VERSION",
    "CONTACT_FORM_LEAD_SKILL",
    "ContactFormLeadIntakeContext",
    "ContactFormLeadReminderRenderer",
    "ContactFormLeadSlackResponseTarget",
    "contact_form_lead_run_message",
]
