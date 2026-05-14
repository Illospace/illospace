#!/usr/bin/env python3
"""Sync brain (PostgreSQL) → markdown files.

Run after nightly consolidation to keep markdown files current.
The brain is the source of truth; files are a cache for quick access.
"""

import os
import sys
import asyncio
from datetime import datetime

from sqlalchemy import text

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 3))))  # repo root
import brain.kernel.config as config
from brain.platform.db.repositories.unit_of_work import UnitOfWork
WORKSPACE = str(config.WORKSPACE_ROOT)
MEMORY_DIR = str(config.JOURNAL_DIR)  # illo-brain/journal/ — standalone


async def sync_lessons():
    """Export high-salience lessons from brain → memory/lessons.md"""
    async with UnitOfWork() as uow:
        result = await uow.session.execute(text("""
            SELECT id, content, salience, created_at
            FROM memories
            WHERE memory_type = 'lesson' AND NOT archived
            ORDER BY salience DESC, created_at DESC
        """))
        rows = result.mappings().all()

    lines = [
        "# Lessons Learned",
        f"_Auto-synced from brain: {datetime.now().strftime('%Y-%m-%d %H:%M')}_",
        f"_{len(rows)} lessons in brain_\n",
    ]

    for row in rows:
        mid = row["id"]
        content = row["content"]
        salience = row["salience"]
        created = row["created_at"]
        date_str = created.strftime('%Y-%m-%d') if created else '?'
        # Truncate long lessons for the file cache
        short = content[:300].replace('\n', ' ')
        if len(content) > 300:
            short += '...'
        lines.append(f"- **[#{mid} s:{salience} {date_str}]** {short}\n")

    path = os.path.join(MEMORY_DIR, "lessons.md")
    with open(path, 'w') as f:
        f.write('\n'.join(lines))
    print(f"Synced {len(rows)} lessons → {path}")



if __name__ == '__main__':
    asyncio.run(sync_lessons())
    print("Brain → files sync complete.")
