"""TLS mailbox and SMTP clients. Blocking protocol calls run outside the event loop."""

import asyncio
import imaplib
import re
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from illo.channels.email.parser import clean_header, mailbox
from illo.vault import Vault


async def password(ctx, *, smtp: bool = False) -> str:
    config = ctx.config
    value = config.smtp_password if smtp else config.email_password
    name = config.smtp_password_secret if smtp else config.email_password_secret
    if name:
        if not config.vault_key:
            raise ValueError("The email vault key is missing")
        async with ctx.pool.connection() as conn:
            secret = await Vault(config.vault_key.get_secret_value()).get(conn, name)
        if not secret:
            raise ValueError("The email secret is missing")
        return secret
    if not value:
        raise ValueError("The email password is missing")
    return value.get_secret_value()


class MailboxClient:
    def __init__(self, config, secret):
        self.config = config
        self.secret = secret

    def _connect(self):
        client = imaplib.IMAP4_SSL(
            self.config.email_host,
            self.config.email_port,
            ssl_context=ssl.create_default_context(),
            timeout=10,
        )
        try:
            client.login(self.config.email_user, self.secret)
            if client.select(self.config.email_folder)[0] != "OK":
                raise RuntimeError("The email folder is unavailable")
            return client
        except BaseException:
            client.logout()
            raise

    def _unseen(self):
        client = self._connect()
        try:
            status, result = client.uid("search", None, "UNSEEN")
            if status != "OK":
                raise RuntimeError("The mailbox search failed")
            messages = []
            for uid in result[0].split()[:50]:
                status, sizes = client.uid("fetch", uid, "(RFC822.SIZE)")
                size = re.search(
                    rb"RFC822.SIZE\s+(\d+)", b" ".join(s for s in sizes if isinstance(s, bytes))
                )
                if status != "OK" or not size or int(size[1]) > self.config.email_max_bytes:
                    continue
                status, rows = client.uid("fetch", uid, "(BODY.PEEK[])")
                if status == "OK":
                    for row in rows:
                        if isinstance(row, tuple) and len(row[1]) <= self.config.email_max_bytes:
                            messages.append((uid.decode(), row[1]))
            return messages
        finally:
            client.logout()

    async def unseen(self):
        return await asyncio.to_thread(self._unseen)

    def _seen(self, uid):
        client = self._connect()
        try:
            if client.uid("store", uid, "+FLAGS.SILENT", "(\\Seen)")[0] != "OK":
                raise RuntimeError("The email could not be marked as seen")
        finally:
            client.logout()

    async def seen(self, uid):
        await asyncio.to_thread(self._seen, uid)


class SMTPClient:
    def __init__(self, config, secret):
        self.config = config
        self.secret = secret

    def _send(self, message):
        config = self.config
        context = ssl.create_default_context()
        if config.smtp_starttls:
            client = smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=10)
        else:
            client = smtplib.SMTP_SSL(
                config.smtp_host, config.smtp_port, timeout=10, context=context
            )
        with client:
            if config.smtp_starttls:
                client.ehlo()
                client.starttls(context=context)
                client.ehlo()
            client.login(config.smtp_user or config.email_user, self.secret)
            client.send_message(message)

    async def send(self, message):
        await asyncio.to_thread(self._send, message)


async def send_email(ctx, *, to, subject, body, in_reply_to=None, references=None):
    config = ctx.config
    if not config.smtp_host or not config.smtp_from:
        raise ValueError("SMTP is not configured")
    message = EmailMessage()
    message["From"] = mailbox(config.smtp_from)
    message["To"] = mailbox(to)
    message["Subject"] = clean_header(subject, 500)
    message["Message-ID"] = make_msgid()
    message["Date"] = formatdate(localtime=False)
    if in_reply_to:
        message["In-Reply-To"] = clean_header(in_reply_to)
        message["References"] = clean_header((references or "") + " " + in_reply_to, 20000).strip()
    message.set_content(body)
    await SMTPClient(config, await password(ctx, smtp=True)).send(message)
    return {"message_id": str(message["Message-ID"])}
