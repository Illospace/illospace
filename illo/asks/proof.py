"""Bound request material and resolve saved references with the knowledge visibility rule."""

import json
import re
from urllib.parse import unquote, urlsplit

from illo.ledger import items
from illo.tools.registry import Refusal

PROOF_LIMIT = 'The proof is too large. Use 40 values and 20,000 bytes at most, 3 levels deep.'
CARD_LIMIT = 'This is too long for a Slack card. Use 30,000 characters at most.'
# The workflow uses area to select the owner. No other proof key controls routing.
ROUTING_KEYS = frozenset({'area'})


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def check(value):
    count = 0

    def walk(node, depth):
        nonlocal count
        if isinstance(node, (dict, list)):
            if depth > 3:
                raise Refusal(PROOF_LIMIT)
            for child in node.values() if isinstance(node, dict) else node:
                count += 1
                if count > 40:
                    raise Refusal(PROOF_LIMIT)
                walk(child, depth + 1)

    walk(value, 1)
    try:
        if len(compact(value).encode('utf-8')) > 20000:
            raise Refusal(PROOF_LIMIT)
    except (TypeError, ValueError, UnicodeError):
        raise Refusal(PROOF_LIMIT) from None
    return value


def saved_ref(value):
    return isinstance(value, str) and re.fullmatch(r'saved:[1-9][0-9]*', value) is not None


def references(value):
    if saved_ref(value):
        yield value
    elif isinstance(value, (list, dict)):
        for child in value.values() if isinstance(value, dict) else value:
            yield from references(child)


async def saved_items(conn, value, principal):
    return {ref: await items.get(conn, ref, principal=principal)
            for ref in dict.fromkeys(references(value))}


def label(key):
    text = str(key).replace('_', ' ')
    return text[:1].upper() + text[1:]


def entries(value, order=None):
    """Give the entries in the order of the requester. The stored value does not keep that order."""
    keys = [key for key in order or [] if key in value] if isinstance(order, list) else []
    keys += [key for key in value if key not in keys]
    return ((key, value[key]) for key in keys if key not in ROUTING_KEYS and key not in {'text', 'body'})


def body(value):
    return value.get('text') or value.get('body') or ''


def address(value):
    from illo.tasks.pages.markdown import safe_url

    if not isinstance(value, str) or not safe_url(value):
        return None
    parts = urlsplit(value)
    name = unquote(parts.path.rsplit('/', 1)[-1])
    title = 'Picture: ' + name if parts.path.lower().endswith(('.png', '.jpg', '.jpeg', '.webp', '.gif')) else value
    return value, title


def links(value):
    if not isinstance(value, list):
        value = [value]
    for entry in value:
        url = entry.get('url', '') if isinstance(entry, dict) else entry
        if found := address(url):
            yield url, scalar((entry.get('title') if isinstance(entry, dict) else None) or urlsplit(found[0]).hostname)


def scalar(value):
    return value if isinstance(value, str) else compact(value)
