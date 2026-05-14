#!/usr/bin/env python3
"""Raw episode encoder helper.

Semantic memory extraction now lives in :mod:`brain.systems.memory.harvest` and must be
LLM-decided plus schema-validated. This module intentionally does not classify
lessons, preferences, decisions, procedures, or other semantic types.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 3))))

from brain.app.cli.memory import add_memory


@dataclass
class ExtractedLesson:
    """Raw episode container.

    ``lesson_type`` remains for old callers, but the only value emitted here is
    ``raw_episode``. Do not use it as a semantic type.
    """

    content: str
    lesson_type: str
    salience: float
    source: str


def extract_lessons(text: str, source: str = "session") -> list[ExtractedLesson]:
    """Return one low-salience raw episode without semantic classification."""
    content = " ".join(str(text or "").split())
    if len(content) < 20:
        return []
    if len(content) > 1200:
        content = content[:1190].rstrip() + "..."
    return [
        ExtractedLesson(
            content=content,
            lesson_type="raw_episode",
            salience=3.0,
            source=source,
        )
    ]


async def encode_lessons(lessons: list[ExtractedLesson], dry_run: bool = False) -> list[dict]:
    """Encode raw episodes via ``add_memory``."""
    results = []
    for lesson in lessons:
        if dry_run:
            results.append({
                "content": lesson.content,
                "type": "raw_episode",
                "salience": lesson.salience,
                "dry_run": True,
            })
            continue
        result = await add_memory(
            content=lesson.content,
            memory_type="episode",
            salience=lesson.salience,
            source=f"raw_encoder:{lesson.source}",
            tags=["raw_episode"],
        )
        results.append({
            "content": lesson.content,
            "type": "raw_episode",
            "salience": lesson.salience,
            "result": result,
        })
    return results


async def auto_encode_session(text: str, dry_run: bool = False) -> list[dict]:
    """Capture a raw session episode."""
    lessons = extract_lessons(text, source="session")
    return await encode_lessons(lessons, dry_run=dry_run)


async def auto_encode_agent_run(text: str, dry_run: bool = False) -> list[dict]:
    """Capture a raw AgentRun episode."""
    lessons = extract_lessons(text, source="agent_run")
    return await encode_lessons(lessons, dry_run=dry_run)


def main():
    parser = argparse.ArgumentParser(description="Raw episode capture")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--session-summary", help="Session summary text to capture as a raw episode")
    group.add_argument("--from-agent-run", help="AgentRun output text to capture as a raw episode")
    parser.add_argument("--dry-run", action="store_true", help="Capture but don't encode")
    args = parser.parse_args()

    if args.session_summary:
        results = asyncio.run(auto_encode_session(args.session_summary, dry_run=args.dry_run))
        source = "session"
    else:
        results = asyncio.run(auto_encode_agent_run(args.from_agent_run, dry_run=args.dry_run))
        source = "agent_run"

    import json

    print(json.dumps({"source": source, "episodes_found": len(results), "episodes": results}, indent=2, default=str))


if __name__ == "__main__":
    main()
