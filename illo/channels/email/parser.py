"""Parse email without granting authority to a forged sender or a forward."""

import hashlib
import re
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses

from illo.gateway import InboundEvent
from illo.tools.web import PageText, outside_data

QUOTE_START = re.compile(
    r"(?im)^(?:\s*>|\s*On .{1,500}wrote:\s*$|\s*-{2,}\s*(?:Original|Forwarded) message\s*-*|\s*Begin forwarded message:|From:\s+.+)"
)


class EmailText(PageText):
    """Keep the start of HTML quotes visible after HTML becomes plain text."""

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        classes = (attributes.get("class") or "").lower().split()
        if (
            tag == "blockquote"
            or {"gmail_quote", "yahoo_quoted"} & set(classes)
            or attributes.get("id") == "divRplyFwdMsg"
        ):
            self.parts.append("\n> ")
        super().handle_starttag(tag, attrs)


def aligned(domain: str, from_domain: str) -> bool:
    """Use exact alignment. Do not guess a registrable domain without a suffix list."""
    return domain.lower().rstrip(".") == from_domain.lower().rstrip(".")


def authenticated(message, sender: str, authserv_id: str | None) -> bool:
    """Trust only the first result, from the configured receiving server.

    That server must strip incoming results that claim its own identity.
    Exact alignment is stricter than relaxed DMARC alignment.
    """
    if not authserv_id or not sender or "@" not in sender:
        return False
    headers = message.get_all("Authentication-Results", [])
    if not headers:
        return False
    sections = str(headers[0]).split(";")
    if sections[0].strip().lower() != authserv_id.lower():
        return False
    domain = sender.rsplit("@", 1)[1]
    passed = set()
    for section in sections[1:]:
        match = re.match(r"\s*(dmarc|spf|dkim)\s*=\s*pass\b", section, re.I)
        if not match:
            continue
        method = match[1].lower()
        field = {"dmarc": "header.from", "spf": "smtp.mailfrom", "dkim": "header.d"}[method]
        identity = re.search(r"\b" + re.escape(field) + r'\s*=\s*"?([^\s;"()]+)', section, re.I)
        if identity and aligned(identity[1].rsplit("@", 1)[-1], domain):
            passed.add(method)
    return "dmarc" in passed or {"spf", "dkim"} <= passed


def clean_header(value: str, limit: int = 998) -> str:
    if any(c in value for c in "\r\n\x00") or len(value) > limit:
        raise ValueError("The email header is invalid")
    return value


def mailbox(value: str) -> str:
    clean_header(value, 320)
    addresses = getaddresses([value])
    if len(addresses) != 1 or not addresses[0][1] or "@" not in addresses[0][1]:
        raise ValueError("Use one email address")
    address = addresses[0][1].lower()
    if value.strip().lower() != address or any(c.isspace() for c in address):
        raise ValueError("Use one plain email address")
    return address


def parse_message(
    raw: bytes, *, authserv_id: str | None, max_bytes: int = 2 * 1024 * 1024
) -> InboundEvent:
    if len(raw) > max_bytes:
        raise ValueError("The email is too large")
    message = BytesParser(policy=policy.default).parsebytes(raw)
    addresses = getaddresses(message.get_all("From", []))
    sender = addresses[0][1].lower() if len(addresses) == 1 and not message.defects else ""
    try:
        sender = mailbox(sender)
    except ValueError:
        sender = ""
    trusted = authenticated(message, sender, authserv_id)
    part = message.get_body(preferencelist=("plain", "html"))
    text = ""
    if part:
        try:
            text = part.get_content()
        except (LookupError, UnicodeError):
            text = (part.get_payload(decode=True) or b"").decode("utf-8", errors="replace")
        if part.get_content_type() == "text/html":
            parser = EmailText()
            parser.feed(text)
            text = parser.text()
    quote = QUOTE_START.search(text)
    forwarded = bool(quote)
    if quote:
        text = (
            text[: quote.start()].strip()
            + "\n\n"
            + outside_data(text[quote.start() :], "forwarded-message")
        )
    attachments = []
    for attachment in message.iter_attachments():
        attachments.append(
            {
                "name": str(attachment.get_filename() or "attachment")[:255],
                "size": len(attachment.get_payload(decode=True) or b""),
            }
        )
    if attachments:
        text += "\n\n" + outside_data(
            "\n".join(f"{a['name']} ({a['size']} bytes)" for a in attachments), "attachments"
        )
    message_id = str(message.get("Message-ID", "")).strip()
    if not re.fullmatch(r"<[^<>\s]{1,900}>", message_id):
        message_id = "<sha256-" + hashlib.sha256(raw).hexdigest() + "@invalid>"
    references = " ".join(re.findall(r"<[^<>\s]{1,900}>", str(message.get("References", "")))[-20:])
    subject = " ".join(str(message.get("Subject", "")).split())[:500]
    return InboundEvent(
        channel="email",
        external_id=message_id,
        kind="message",
        sender=sender if trusted else "",
        text=text,
        reply_to=message_id,
        payload={
            "subject": subject,
            "references": references,
            "forwarded": forwarded,
            "contains_outside_text": forwarded,
            "authenticated": trusted,
            "attachments": attachments,
        },
    )
