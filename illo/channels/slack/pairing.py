"""Recognize the whole agent sign-in message before any text enters a model."""

import re

PAIR_MESSAGE = re.compile(r"connect\s+([A-Z0-9]{4})-([A-Z0-9]{4})", re.IGNORECASE)
# Some apps post for the person and add a signature such as "*Sent using* <@U123>".
# The words change with the language of the person, so only the shape counts:
# one short line in one emphasis style, then one user mention, at the end.
LABEL = r"[^*_<>\n\r\v\f\x1c-\x1e\x85  ]{1,40}"
SIGNATURE = re.compile(rf"(?:\*{LABEL}\*|_{LABEL}_)[ \t]?<@[A-Z0-9]{{1,20}}>\Z")
# A real message is short. The limit also keeps the work small for a hostile text.
MAX_CHARS = 200


def _parts(text):
    text = text.strip()
    if len(text) > MAX_CHARS:
        return None, False
    signed = SIGNATURE.search(text)
    if signed:
        head = text[:signed.start()]
        # White space must separate the message from the signature.
        if head == head.rstrip():
            return None, False
        text = head
    match = PAIR_MESSAGE.fullmatch(text.strip().strip("`").strip())
    return ("".join(match.groups()).upper(), bool(signed)) if match else (None, False)


def pairing_code(text):
    return _parts(text)[0]


def sent_by_app(text):
    """True when an app signed the pairing message. The card then says so."""
    return _parts(text)[1]
