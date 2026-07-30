"""Skill handoff and deterministic reminders for contact-form leads."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Mapping

from brain.systems.runs.obligation_specs import ObligationAnswerer
from brain.systems.slack.contact_form_leads import ContactFormLead


CONTACT_FORM_LEAD_SKILL = "contact-form-lead-intake"


@dataclass(frozen=True, slots=True)
class ContactFormLeadSlackResponseTarget:
    """Canonical Slack destination supplied to the contact-form lead skill."""

    channel_id: str
    thread_ts: str

    def __post_init__(self) -> None:
        channel_id = _clean(self.channel_id)
        thread_ts = _clean(self.thread_ts)
        if not channel_id or not thread_ts:
            missing = "channel_id" if not channel_id else "thread_ts"
            raise ValueError(
                f"contact-form lead intake requires a Slack response target {missing}"
            )
        object.__setattr__(self, "channel_id", channel_id)
        object.__setattr__(self, "thread_ts", thread_ts)

    @classmethod
    def from_slack_trigger(
        cls,
        slack_trigger_payload: Mapping[str, Any],
    ) -> ContactFormLeadSlackResponseTarget:
        response_target = slack_trigger_payload.get("response_target")
        target = response_target if isinstance(response_target, Mapping) else {}
        return cls(
            channel_id=target.get("channel_id"),
            thread_ts=target.get("thread_ts"),
        )

    def to_payload(self) -> dict[str, str]:
        return {
            "channel_id": self.channel_id,
            "thread_ts": self.thread_ts,
        }


@dataclass(frozen=True, slots=True)
class ContactFormLeadIntakeContext:
    """Canonical model input for one decoded contact-form lead."""

    lead: ContactFormLead
    owner: ObligationAnswerer
    slack_response_target: ContactFormLeadSlackResponseTarget
    source_permalink: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_permalink", _clean(self.source_permalink) or None)

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
            source_permalink=slack_trigger_payload.get("permalink"),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "lead": self.lead.to_payload(),
            "owner": self.owner.to_metadata(),
            "slack_response_target": self.slack_response_target.to_payload(),
            "source_permalink": self.source_permalink,
        }

    def serialize(self) -> str:
        """Serialize the internal prompt contract."""

        return json.dumps(self.to_payload(), ensure_ascii=False, sort_keys=True)


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
    "CONTACT_FORM_LEAD_SKILL",
    "ContactFormLeadReminderRenderer",
    "contact_form_lead_run_message",
]
