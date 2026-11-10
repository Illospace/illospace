"""Email intake and replies share the gateway's authority checks."""

import logging

from illo.channels.email.parser import parse_message
from illo.channels.email.transport import MailboxClient, password, send_email
from illo.gateway.replies import error_text
from illo.ledger import people, sensors
from illo.slackapi import dm_person

log = logging.getLogger(__name__)


class EmailAdapter:
    def __init__(self, ctx):
        self.ctx = ctx

    async def show_working(self, event):
        pass

    async def deliver_final(self, event, text):
        # Never use Reply-To. Only the authenticated, mapped sender gets a reply.
        async with self.ctx.pool.connection() as conn:
            person = await people.by_email(conn, event.sender)
        if not person:
            return
        mode = self.ctx.workspace.config.email.reply
        failures = []
        if mode in {"slack", "both"} and person["slack_id"]:
            try:
                if not await dm_person(self.ctx, person["key"], text):
                    raise RuntimeError("Slack is unavailable")
            except Exception:
                failures.append("Slack")
        if mode in {"email", "both"}:
            try:
                subject = event.payload.get("subject", "")
                await send_email(
                    self.ctx,
                    to=event.sender,
                    subject=subject if subject.lower().startswith("re:") else "Re: " + subject,
                    body=text,
                    in_reply_to=event.reply_to,
                    references=event.payload.get("references"),
                )
            except Exception:
                failures.append("email")
        if failures:
            raise RuntimeError("The reply could not reach all destinations")

    async def deliver_error(self, event, *, category="internal"):
        await self.deliver_final(event, error_text(category))


async def unknown_sender(ctx, event, authority):
    if event.channel != "email" or authority.principal != "outsider":
        return None
    if ctx.workspace.config.email.unknown_senders == "notify":
        # A fixed notice avoids turning an outside message into a team request.
        await dm_person(
            ctx,
            ctx.workspace.config.default_owner,
            "An email from an unknown or unverified sender waits in the mailbox. "
            "Illo did not reply. Subject: " + event.payload.get("subject", ""),
        )
    return ""


async def poll_mailbox(ctx, client=None):
    client = client or MailboxClient(ctx.config, await password(ctx))
    for uid, raw in await client.unseen():
        try:
            event = parse_message(
                raw, authserv_id=ctx.config.email_authserv_id, max_bytes=ctx.config.email_max_bytes
            )
        except ValueError:
            log.warning("Skipped an invalid or oversized email")
            continue
        await ctx.gateway.receive(event)
        # receive commits the inbox before it returns, even if the brain fails.
        await client.seen(uid)


async def perform_email(ctx, draft):
    if draft["state"] != "approved":
        raise ValueError("The email draft needs approval")
    arguments = draft["content"].get("arguments", draft["content"])
    return await send_email(ctx, **arguments)


async def setup(ctx) -> None:
    ctx.gateway.register("email", EmailAdapter(ctx))
    ctx.gateway.register_claim(unknown_sender)
    ctx.draft_performers["email"] = perform_email
    ctx.draft_performers["email_draft"] = perform_email
    if ctx.config.email_host:
        if not (ctx.config.email_authserv_id or "").strip():
            detail = (
                "Email is disabled. Set ILLO_EMAIL_AUTHSERV_ID to a trusted receiver or topmost."
            )
            log.error(detail)
            async with ctx.pool.connection() as conn:
                await sensors.error(conn, "email:auth", detail)
            return
        if not ctx.config.email_user:
            raise ValueError("Set the email user")
        # Check the credential before the first timer. Do not print its value.
        await password(ctx)

        async def poll():
            await poll_mailbox(ctx)

        ctx.every(ctx.config.email_poll_seconds, poll)
