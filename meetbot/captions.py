"""Pure rolling caption deduplication with no browser dependency."""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Callable

from meetbot.models import isoformat_utc

_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class CaptionLine:
    """One committed speaker-attributed caption line."""

    ts: str
    speaker: str
    text: str

    def as_dict(self) -> dict[str, str]:
        return {"ts": self.ts, "speaker": self.speaker, "text": self.text}


@dataclass(slots=True)
class _PendingCaption:
    line_id: str | None
    line: CaptionLine
    order: int


class RollingCaptionBuffer:
    """Replace in-flight Meet captions and commit only when a new line starts.

    Meet rewrites a caption DOM node while a person speaks. The observer supplies
    the node identity when possible. Updates for that identity replace the pending
    text. A new identity commits the prior line for that speaker. The text-shape
    fallback supports observers that cannot provide a stable node identity.
    """

    def __init__(self, *, clock: Callable[[], str] = isoformat_utc) -> None:
        self._clock = clock
        self._pending: dict[str, _PendingCaption] = {}
        self._closed_line_ids: set[tuple[str, str]] = set()
        self._next_order = 0

    def observe(
        self,
        speaker: str,
        text: str,
        *,
        line_id: str | None = None,
        ts: str | None = None,
    ) -> list[CaptionLine]:
        """Apply one DOM caption snapshot and return newly committed lines."""

        clean_speaker = _clean(speaker) or "Unknown speaker"
        clean_text = _clean(text)
        clean_line_id = _clean(line_id or "") or None
        line_key = (clean_speaker, clean_line_id) if clean_line_id else None
        if not clean_text or (line_key and line_key in self._closed_line_ids):
            return []

        current = self._pending.get(clean_speaker)
        if current is None:
            self._pending[clean_speaker] = self._new_pending(
                clean_speaker,
                clean_text,
                clean_line_id,
                ts,
            )
            return []

        if current.line.text == clean_text:
            return []

        if _is_same_inflight_line(current, clean_line_id, clean_text):
            current.line = CaptionLine(
                ts=current.line.ts,
                speaker=clean_speaker,
                text=clean_text,
            )
            return []

        committed = current.line
        if current.line_id:
            self._closed_line_ids.add((clean_speaker, current.line_id))
        self._pending[clean_speaker] = self._new_pending(
            clean_speaker,
            clean_text,
            clean_line_id,
            ts,
        )
        return [committed]

    def flush(self) -> list[CaptionLine]:
        """Commit all final in-flight captions in first-observed order."""

        pending = sorted(self._pending.values(), key=lambda item: item.order)
        self._pending.clear()
        for item in pending:
            if item.line_id:
                self._closed_line_ids.add((item.line.speaker, item.line_id))
        return [item.line for item in pending]

    def _new_pending(
        self,
        speaker: str,
        text: str,
        line_id: str | None,
        ts: str | None,
    ) -> _PendingCaption:
        pending = _PendingCaption(
            line_id=line_id,
            line=CaptionLine(ts=ts or self._clock(), speaker=speaker, text=text),
            order=self._next_order,
        )
        self._next_order += 1
        return pending


def _is_same_inflight_line(
    current: _PendingCaption,
    new_line_id: str | None,
    new_text: str,
) -> bool:
    if current.line_id or new_line_id:
        return bool(current.line_id and new_line_id and current.line_id == new_line_id)

    old_text = current.line.text
    if new_text.startswith(old_text) or old_text.startswith(new_text):
        return True
    return SequenceMatcher(a=old_text, b=new_text, autojunk=False).ratio() >= 0.6


def _clean(value: str) -> str:
    return _WHITESPACE.sub(" ", str(value or "")).strip()
