"""Durable JSONL, Markdown, and session-record transcript output."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Iterable

from meetbot.captions import CaptionLine
from meetbot.models import SessionRecord


class TranscriptWriter:
    """Write one meeting transcript below the shared uploads volume."""

    def __init__(self, uploads_root: Path, session_id: str) -> None:
        self.session_id = session_id
        self.directory = uploads_root / "meetings" / session_id
        self.jsonl_path = self.directory / "transcript.jsonl"
        self.markdown_path = self.directory / "transcript.md"
        self.session_path = self.directory / "session.json"

    @staticmethod
    def public_paths(session_id: str) -> tuple[str, str]:
        base = f"brain/uploads/meetings/{session_id}"
        return f"{base}/transcript.jsonl", f"{base}/transcript.md"

    def start(self, record: SessionRecord) -> None:
        """Create an empty transcript and the initial session record."""

        self.directory.mkdir(parents=True, exist_ok=True)
        self.jsonl_path.touch(exist_ok=True)
        self.write_session(record)

    def append(self, line: CaptionLine) -> None:
        """Append one newly committed caption line."""

        with self.jsonl_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line.as_dict(), ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")

    def finalize(self, record: SessionRecord, lines: Iterable[CaptionLine]) -> None:
        """Rewrite chronologically, render Markdown, and save final metadata."""

        ordered = sorted(lines, key=lambda line: line.ts)
        jsonl = "".join(
            f"{json.dumps(line.as_dict(), ensure_ascii=False, separators=(',', ':'))}\n"
            for line in ordered
        )
        _atomic_write(self.jsonl_path, jsonl)
        _atomic_write(self.markdown_path, _render_markdown(record, ordered))
        self.write_session(record)

    def write_session(self, record: SessionRecord) -> None:
        """Atomically persist the current lifecycle record."""

        content = json.dumps(record.session_document(), ensure_ascii=False, indent=2) + "\n"
        _atomic_write(self.session_path, content)


def _render_markdown(record: SessionRecord, lines: list[CaptionLine]) -> str:
    participants = ", ".join(record.participants) if record.participants else "None observed"
    ended_at = record.ended_at or "In progress"
    output = [
        "# Meeting transcript",
        "",
        f"- Meeting URL: {record.meeting_url}",
        f"- Started: {record.started_at}",
        f"- Ended: {ended_at}",
        f"- Participants seen: {participants}",
        "",
    ]
    last_marker: int | None = None
    started = _parse_timestamp(record.started_at)
    for line in lines:
        elapsed_minutes = max(0, int((_parse_timestamp(line.ts) - started).total_seconds() // 60))
        marker = (elapsed_minutes // 5) * 5
        if marker != last_marker:
            output.extend((f"_[{marker // 60:02d}:{marker % 60:02d}]_", ""))
            last_marker = marker
        output.extend((f"**{line.speaker}**: {line.text}", ""))
    return "\n".join(output).rstrip() + "\n"


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)
