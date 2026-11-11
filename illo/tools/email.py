"""Outbound email waits for its requester's approval."""

from pydantic import Field, field_validator

from illo.channels.email.parser import clean_header, mailbox
from illo.tools.registry import Arguments, Tool


class EmailArguments(Arguments):
    to: str = Field(max_length=320)
    subject: str = Field(max_length=500)
    body: str = Field(min_length=1, max_length=50000)
    in_reply_to: str | None = Field(default=None, max_length=998)

    @field_validator("to")
    @classmethod
    def valid_to(cls, value):
        return mailbox(value)

    @field_validator("subject", "in_reply_to")
    @classmethod
    def valid_header(cls, value):
        return clean_header(value) if value is not None else None


async def draft_only(ctx, args):
    raise RuntimeError("Email must pass through a draft")


def register(registry) -> None:
    # The architecture requires a draft for all outbound email, including team mail.
    registry.add(
        Tool(
            name="email_draft",
            family="email",
            risk="send_outside",
            destination=lambda args: "outside:" + args["to"],
            personal=False,
            handler=draft_only,
            arguments=EmailArguments,
            description='Draft an email to `to`, with `subject` and `body`. The result is always a draft card in the DM of the person you act for; the mail goes out only when they tap Send.',
            profiles=frozenset({"person_dm", "agent_request"}),
        )
    )
