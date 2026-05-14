"""Journal router — nightly consolidation reflections."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query

from brain.app.api.auth import get_current_user
from brain.app.api.deps import rate_limit
from brain.kernel import config

router = APIRouter(
    prefix="/api",
    tags=["journal"],
    dependencies=[Depends(rate_limit)],
)


def _journal_dir() -> Path:
    return config.JOURNAL_DIR


@router.get("/journal")
async def list_journal_entries(
    limit: int | None = Query(None, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: dict[str, Any] = Depends(get_current_user),
):
    journal_dir = _journal_dir()
    entries = []
    if journal_dir.exists():
        files = sorted(journal_dir.glob("*.md"), reverse=True)
        if offset:
            files = files[offset:]
        if limit is not None:
            files = files[:limit]
        for f in files:
            entries.append({
                "filename": f.name,
                "content": f.read_text(encoding="utf-8"),
            })
    return entries
