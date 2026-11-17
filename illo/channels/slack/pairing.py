"""Recognize the whole agent sign-in message before any text enters a model."""

import re

PAIR_MESSAGE = re.compile(r"connect\s+([A-Z0-9]{4})-([A-Z0-9]{4})", re.IGNORECASE)


def pairing_code(text):
    match = PAIR_MESSAGE.fullmatch(text.strip().strip("`").strip())
    return "".join(match.groups()).upper() if match else None
