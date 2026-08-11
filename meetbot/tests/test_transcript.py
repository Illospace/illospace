from __future__ import annotations

import json
from pathlib import Path

from meetbot.captions import CaptionLine
from meetbot.models import MeetbotSessionOutcome, Origin, SessionRecord
from meetbot.transcript import TranscriptWriter


def _record(session_id: str) -> SessionRecord:
    transcript_path, transcript_md_path = TranscriptWriter.public_paths(session_id)
    return SessionRecord(
        session_id=session_id,
        meeting_url="https://meet.google.com/abc-defg-hij",
        display_name="Illo (notetaker)",
        origin=Origin(channel="C123", thread_ts="1234.500"),
        requested_by="U123",
        transcript_path=transcript_path,
        transcript_md_path=transcript_md_path,
        started_at="2026-08-03T14:00:00Z",
        joined_at="2026-08-03T14:00:05Z",
        ended_at="2026-08-03T14:07:00Z",
        status="ended",
        outcome=MeetbotSessionOutcome.LEFT,
        participants=["Alice", "Bob"],
        status_history=[
            {"status": "starting", "ts": "2026-08-03T14:00:00Z"},
            {"status": "ended", "ts": "2026-08-03T14:07:00Z"},
        ],
    )


def test_transcript_writer_creates_all_specified_formats(tmp_path: Path) -> None:
    record = _record("session-1")
    writer = TranscriptWriter(tmp_path, record.session_id)
    writer.start(record)
    lines = [
        CaptionLine(ts="2026-08-03T14:06:00Z", speaker="Bob", text="Second line"),
        CaptionLine(ts="2026-08-03T14:01:00Z", speaker="Alice", text="First line"),
    ]
    for line in lines:
        writer.append(line)
    record.caption_lines = 2

    writer.finalize(record, lines)

    transcript_rows = [
        json.loads(line) for line in writer.jsonl_path.read_text(encoding="utf-8").splitlines()
    ]
    assert transcript_rows == [
        {"ts": "2026-08-03T14:01:00Z", "speaker": "Alice", "text": "First line"},
        {"ts": "2026-08-03T14:06:00Z", "speaker": "Bob", "text": "Second line"},
    ]

    markdown = writer.markdown_path.read_text(encoding="utf-8")
    assert "# Meeting transcript" in markdown
    assert "https://meet.google.com/abc-defg-hij" in markdown
    assert "Participants seen: Alice, Bob" in markdown
    assert "_[00:00]_" in markdown
    assert "_[00:05]_" in markdown
    assert "**Alice**: First line" in markdown

    session = json.loads(writer.session_path.read_text(encoding="utf-8"))
    assert session["status"] == "ended"
    assert session["caption_lines"] == 2
    assert session["origin"] == {"channel": "C123", "thread_ts": "1234.500"}
    assert [entry["status"] for entry in session["status_history"]] == ["starting", "ended"]
