"""Read Slack text without decoding escapes or changing message authority."""

import re

MAX_MESSAGE_CHARS = 8000
MAX_ELEMENTS = 1000
MAX_DEPTH = 12
CUT_MARKER = "\n[Slack message cut]"


class _Reader:
    def __init__(self, limit: int):
        self.remaining = limit + 1
        self.elements = 0
        self.cut = False

    def text(self, value) -> str:
        if not isinstance(value, str):
            return ""
        result = value[:self.remaining]
        self.remaining -= len(result)
        self.cut |= len(result) < len(value)
        return result

    def nodes(self, values, depth: int, separator="\n") -> str:
        if not isinstance(values, list):
            return ""
        parts = []
        for value in values:
            if self.elements >= MAX_ELEMENTS or self.remaining <= 0:
                self.cut = True
                break
            rendered = self.node(value, depth)
            if rendered:
                parts.append(rendered)
        return separator.join(parts)

    def node(self, value, depth: int = 0) -> str:
        self.elements += 1
        if depth > MAX_DEPTH or self.elements > MAX_ELEMENTS:
            self.cut = True
            return ""
        if not isinstance(value, dict):
            return ""
        kind = value.get("type")
        if not isinstance(kind, str) or len(kind) > 32:
            return ""
        child = depth + 1
        if kind in {"plain_text", "mrkdwn", "text"}:
            content = self.text(value.get("text"))
            style = value.get("style")
            if kind == "text" and isinstance(style, dict) and content:
                for name, marker in (("code", "`"), ("bold", "*"),
                                     ("italic", "_"), ("strike", "~")):
                    if style.get(name):
                        content = marker + content + marker
            return content
        if kind in {"header", "section"}:
            parts = [self.node(value.get("text"), child)]
            parts.append(self.nodes(value.get("fields"), child))
            # A section can also have a link button beside its text.
            if kind == "section":
                parts.append(self.node(value.get("accessory"), child))
            return "\n".join(filter(None, parts))
        if kind in {"context", "actions", "rich_text"}:
            return self.nodes(value.get("elements"), child)
        if kind in {"button", "link"}:
            label = (
                self.node(value.get("text"), child) if kind == "button"
                else self.text(value.get("text"))
            )
            url = self.text(value.get("url"))
            if kind == "button":
                label = " ".join(label.split())
            return f"<{url}|{label}>" if url and label and url != label else url or label
        if kind in {"user", "channel", "usergroup"}:
            identity = self.text(value.get({
                "user": "user_id", "channel": "channel_id", "usergroup": "usergroup_id",
            }[kind]))
            if not identity:
                return ""
            return {"user": f"<@{identity}>", "channel": f"<#{identity}>",
                    "usergroup": f"<!subteam^{identity}>"}[kind]
        if kind == "emoji":
            name = self.text(value.get("name"))
            return f":{name}:" if name else ""
        if kind == "broadcast":
            name = value.get("range")
            return f"<!{name}>" if isinstance(name, str) and name in {
                "here", "channel", "everyone",
            } else ""
        if kind in {"rich_text_section", "rich_text_quote", "rich_text_preformatted"}:
            content = self.nodes(value.get("elements"), child, "")
            if kind == "rich_text_quote" and content:
                return "> " + content.replace("\n", "\n> ")
            if kind == "rich_text_preformatted" and content:
                return "```\n" + content + "\n```"
            return content
        if kind == "rich_text_list":
            content = self.nodes(value.get("elements"), child)
            indent = value.get("indent", 0)
            indent = min(max(indent, 0), MAX_DEPTH) if isinstance(indent, int) else 0
            offset = value.get("offset", 0)
            offset = min(max(offset, 0), MAX_ELEMENTS) if isinstance(offset, int) else 0
            return "\n".join(
                "  " * indent + (f"{i + offset}. " if value.get("style") == "ordered" else "- ")
                + part for i, part in enumerate(content.splitlines(), 1)
            )
        return ""

    def attachments(self, values) -> str:
        if not isinstance(values, list):
            return ""
        parts = []
        for attachment in values:
            if self.elements >= MAX_ELEMENTS or self.remaining <= 0:
                self.cut = True
                break
            self.elements += 1
            if not isinstance(attachment, dict):
                continue
            parts.append(self.text(attachment.get("pretext")))
            title = self.text(attachment.get("title"))
            url = self.text(attachment.get("title_link"))
            parts.append(f"<{url}|{title}>" if title and url else title or url)
            parts.append(self.text(attachment.get("text")))
            fields = attachment.get("fields")
            if isinstance(fields, list):
                for field in fields:
                    if self.elements >= MAX_ELEMENTS or self.remaining <= 0:
                        self.cut = True
                        break
                    self.elements += 1
                    if isinstance(field, dict):
                        parts.append(": ".join(filter(None, [
                            self.text(field.get("title")), self.text(field.get("value")),
                        ])))
            parts.append(self.nodes(attachment.get("blocks"), 0))
            parts.append(self.text(attachment.get("footer")))
        return "\n".join(filter(None, parts))


def _comparison(value: str) -> str:
    """Compare Slack link spellings without decoding or changing the output."""
    def link(match):
        target, label = match.groups()
        if target.startswith(("@", "#", "!subteam^")):
            return f"<{target}>"
        if target.startswith(("https://", "http://", "mailto:")):
            return f"{label} {target}" if label and label != target else target
        return match[0]

    value = re.sub(r"<([^<>\s|]+)(?:\|([^<>]*))?>", link, value)
    value = re.sub(r"(?m)^\s*[•-] ", "- ", value)
    return " ".join(value.split())


def message_text(message: dict, *, max_chars: int = MAX_MESSAGE_CHARS) -> str:
    """Keep typed text intact when blocks repeat it. Bound all added text and work."""
    if max_chars < len(CUT_MARKER):
        raise ValueError("The text limit must fit the cut marker")
    raw = message.get("text")
    raw = raw if isinstance(raw, str) else ""
    reader = _Reader(max_chars)
    blocks = reader.nodes(message.get("blocks"), 0)
    attachments = reader.attachments(message.get("attachments"))
    rich = "\n".join(filter(None, [blocks, attachments]))
    # Compare only bounded strings. Never decode Slack escapes or control tokens.
    plain = raw[:max_chars + 1]
    normalized_plain = _comparison(plain)
    normalized_rich = _comparison(rich)
    prefix = normalized_plain.removesuffix("...").removesuffix("…").rstrip()
    if not rich or (normalized_rich in normalized_plain and not reader.cut):
        result = plain
    elif normalized_plain in normalized_rich or (prefix and normalized_rich.startswith(prefix)):
        result = rich
    else:
        result = plain + "\n" + rich if plain else rich
    if len(result) > max_chars or len(raw) > max_chars or reader.cut:
        return result[:max_chars - len(CUT_MARKER)] + CUT_MARKER
    return result
