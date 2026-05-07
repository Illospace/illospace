"""Shared @mention parsing and Illo invocation intent."""
from __future__ import annotations

import re
from dataclasses import dataclass

MENTION_TOKEN_RE = re.compile(r"(?<!\w)@([A-Za-z0-9._-]+)")
ILLO_MENTION_TOKEN = "illo"
TEAM_MENTION_WITHOUT_ILLO_SKIP_REASON = "team_mention_without_illo"


def extract_mention_token_list(body: str) -> list[str]:
    """Return normalized mention tokens in text order."""
    tokens: list[str] = []
    for match in MENTION_TOKEN_RE.finditer(body or ""):
        token = match.group(1).strip().lower().rstrip(".,:;!?")
        if token:
            tokens.append(token)
    return tokens


def extract_mention_tokens(body: str) -> set[str]:
    """Return normalized mention tokens as a set."""
    return set(extract_mention_token_list(body))


@dataclass(frozen=True)
class MentionIntent:
    mentions: frozenset[str]
    has_illo: bool
    has_people: bool
    should_invoke_illo: bool
    skip_reason: str | None


def classify_mention_intent(
    body: str,
    *,
    invoke_without_mentions: bool = True,
) -> MentionIntent:
    """Classify whether a message should invoke Illo based on @mentions."""
    mentions = frozenset(extract_mention_tokens(body))
    has_illo = ILLO_MENTION_TOKEN in mentions
    has_people = bool(mentions - {ILLO_MENTION_TOKEN})
    should_invoke_illo = has_illo or (invoke_without_mentions and not mentions)
    skip_reason = None if should_invoke_illo else TEAM_MENTION_WITHOUT_ILLO_SKIP_REASON
    return MentionIntent(
        mentions=mentions,
        has_illo=has_illo,
        has_people=has_people,
        should_invoke_illo=should_invoke_illo,
        skip_reason=skip_reason,
    )


__all__ = [
    "ILLO_MENTION_TOKEN",
    "MENTION_TOKEN_RE",
    "MentionIntent",
    "TEAM_MENTION_WITHOUT_ILLO_SKIP_REASON",
    "classify_mention_intent",
    "extract_mention_token_list",
    "extract_mention_tokens",
]
