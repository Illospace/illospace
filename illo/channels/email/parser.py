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


def auth_results(header: str) -> list[list[str]]:
    """Split results outside quoted strings and nested RFC 8601 comments.

    Keep quotes on tokens so quoted text cannot become a method or property.
    An incomplete comment, quote, or escape rejects the whole header.
    """
    clean = []
    depth, quoted, escaped = 0, False, False
    for char in header:
        if escaped:
            if not depth:
                clean.append(char)
            escaped = False
        elif char == "\\" and (depth or quoted):
            escaped = True
            if not depth:
                clean.append(char)
        elif depth:
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
        elif quoted:
            clean.append(char)
            if char == '"':
                quoted = False
        elif char == "(":
            depth = 1
            clean.append(" ")
        elif char == ")":
            raise ValueError("The authentication result has an invalid comment")
        else:
            clean.append(char)
            if char == '"':
                quoted = True
    if depth or quoted or escaped:
        raise ValueError("The authentication result is incomplete")
    tokens = re.findall(r'"(?:\\.|[^"\\])*"|[;=]|[^\s;="]+', "".join(clean))
    sections = [[]]
    for token in tokens:
        if token == ";":
            sections.append([])
        else:
            sections[-1].append(token.lower())
    return sections


def authenticated(message, sender: str, authserv_id: str | None) -> bool:
    """Trust one aligned DMARC pass from the selected receiving server.

    The receiver must strip incoming results that claim its own identity.
    Without a configured identity, use only the topmost header.
    """
    if not sender or "@" not in sender:
        return False
    for name, header in message.raw_items():
        if name.lower() != "authentication-results":
            continue
        # RFC 8601 results use raw ASCII syntax, never RFC 2047 decoding.
        header = re.sub(r"\r?\n[ \t]+", " ", header)
        if "=?" in header or "\r" in header or "\n" in header:
            return False
        try:
            sections = auth_results(header)
        except ValueError:
            return False
        if authserv_id and sections[0] != [authserv_id.lower()]:
            continue
        results = [part for part in sections[1:] if part[:2] == ["dmarc", "="]]
        count = sum(
            part[i : i + 2] == ["dmarc", "="] for part in sections[1:] for i in range(len(part) - 1)
        )
        if count != 1 or len(results) != 1 or results[0][:3] != ["dmarc", "=", "pass"]:
            return False
        result = results[0]
        identities = [
            result[i + 2].strip('"')
            for i in range(3, len(result) - 2)
            if result[i : i + 2] == ["header.from", "="]
        ]
        return len(identities) == 1 and aligned(identities[0], sender.rsplit("@", 1)[1])
    return False


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
