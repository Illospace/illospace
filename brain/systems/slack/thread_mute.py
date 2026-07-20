"""Thread-scoped Slack post-mute policy for explicit human stand-downs."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import re
from typing import Any, Mapping, Sequence
import unicodedata


_ILLO_NAME_PATTERN = re.compile(r"(?<![\w@])@?illo\b", re.IGNORECASE)
_DISMISSAL_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bnot\s+for\s+you\b",
        r"\bthis\s+is\s+for\s+(?:the\s+)?team\b",
        r"\bleave\s+(?:this|it)\s+to\s+us\b",
        r"\b(?:we(?:'ve|\s+have)\s+got|we\s+got)\s+(?:this|it)\b",
        r"\bon\s+g[èe]re\b",
        r"\bc['’]?est\s+pour\s+l['’]?[ée]quipe\b",
        r"\blaisse(?:z)?[- ]nous\s+g[ée]rer\b",
    )
)
_REINVITE_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\b(?:can|could|would|will)\s+you\b",
        r"\bplease\s+(?:help|check|look|investigate|take|handle|review|join)\b",
        r"\b(?:peux|pourrais|pouvez|pourriez)[- ](?:tu|vous)\b",
        r"\best-ce\s+que\s+(?:tu\s+peux|vous\s+pouvez)\b",
    )
)


@dataclass(frozen=True)
class ThreadPostMute:
    """The latest active human stand-down in a Slack thread."""

    user: str
    ts: str

    @property
    def ledger_line(self) -> str:
        return f"thread muted by {self.user} at {self.ts}"


def _normalized_text(value: Any) -> str:
    return (
        unicodedata.normalize("NFKC", str(value or ""))
        .casefold()
        .replace("’", "'")
    )


def _addresses_illo(text: str, *, illo_user_id: str | None) -> bool:
    if illo_user_id:
        mention = re.compile(
            rf"<@{re.escape(str(illo_user_id).casefold())}(?:\|[^>]+)?>",
            re.IGNORECASE,
        )
        if mention.search(text):
            return True
    return bool(_ILLO_NAME_PATTERN.search(text))


def _is_human_message(message: Mapping[str, Any], *, illo_user_id: str | None) -> bool:
    user = str(message.get("user") or "").strip()
    if not user:
        return False
    if illo_user_id and user.casefold() == str(illo_user_id).strip().casefold():
        return False
    if message.get("bot_id") or message.get("bot_profile") or message.get("app_id"):
        return False
    return str(message.get("subtype") or "").strip() != "bot_message"


def _author_label(message: Mapping[str, Any]) -> str:
    profile = message.get("user_profile")
    if isinstance(profile, Mapping):
        for key in ("display_name", "real_name", "name"):
            label = " ".join(str(profile.get(key) or "").split())
            if label:
                return label[:80]
    return " ".join(str(message.get("user") or "human").split())[:80] or "human"


def _message_ts_key(message: Mapping[str, Any]) -> tuple[int, Decimal, str]:
    ts = str(message.get("ts") or "").strip()
    try:
        return (0, Decimal(ts), "")
    except InvalidOperation:
        return (1, Decimal(0), ts)


def _matches_any(patterns: Sequence[re.Pattern[str]], text: str) -> bool:
    return any(pattern.search(text) for pattern in patterns)


def find_thread_post_mute(
    messages: Sequence[Mapping[str, Any]],
    *,
    illo_user_id: str | None = None,
) -> ThreadPostMute | None:
    """Return the active mute after applying human directives chronologically.

    A directive must both address Illo and use a conservative stand-down or
    re-invite phrase. A dismissal wins over a re-invite in the same message, so
    requests such as "can you leave this to us" cannot accidentally lift a mute.
    """

    active_mute: ThreadPostMute | None = None
    for message in sorted(messages, key=_message_ts_key):
        if not isinstance(message, Mapping) or not _is_human_message(
            message,
            illo_user_id=illo_user_id,
        ):
            continue
        text = _normalized_text(message.get("text"))
        if not _addresses_illo(text, illo_user_id=illo_user_id):
            continue
        if _matches_any(_DISMISSAL_PATTERNS, text):
            active_mute = ThreadPostMute(
                user=_author_label(message),
                ts=str(message.get("ts") or "").strip(),
            )
        elif _matches_any(_REINVITE_PATTERNS, text):
            active_mute = None
    return active_mute


async def read_thread_post_mute(
    client: Any,
    *,
    channel_id: str,
    thread_ts: str,
    illo_user_id: str | None = None,
) -> ThreadPostMute | None:
    """Read the complete Slack thread and resolve its current post-mute state."""

    read_replies = getattr(client, "conversation_replies", None)
    if not callable(read_replies):
        return None

    messages: list[Mapping[str, Any]] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()
    while True:
        response = await read_replies(
            channel=channel_id,
            thread_ts=thread_ts,
            limit=200,
            cursor=cursor,
        )
        if not isinstance(response, Mapping):
            break
        messages.extend(
            message
            for message in response.get("messages") or []
            if isinstance(message, Mapping)
        )
        metadata = response.get("response_metadata")
        metadata = metadata if isinstance(metadata, Mapping) else {}
        next_cursor = str(metadata.get("next_cursor") or "").strip()
        if not next_cursor or next_cursor in seen_cursors:
            break
        seen_cursors.add(next_cursor)
        cursor = next_cursor

    return find_thread_post_mute(messages, illo_user_id=illo_user_id)


__all__ = ["ThreadPostMute", "find_thread_post_mute", "read_thread_post_mute"]
