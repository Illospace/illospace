"""Journal router — nightly consolidation reflections."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query

from brain.app.api.auth import get_current_user
from brain.app.api.deps import rate_limit
from brain.kernel import config
from brain.platform.async_io import run_blocking

router = APIRouter(
    prefix="/api",
    tags=["journal"],
    dependencies=[Depends(rate_limit)],
)


def _journal_dir() -> Path:
    return config.JOURNAL_DIR


def _list_journal_entries_sync(journal_dir: Path, *, limit: int | None, offset: int) -> list[dict[str, str]]:
    if not journal_dir.exists():
        return []
    files = sorted(journal_dir.glob("*.md"), reverse=True)
    if offset:
        files = files[offset:]
    if limit is not None:
        files = files[:limit]
    return [
        {
            "filename": item.name,
            "content": item.read_text(encoding="utf-8"),
        }
        for item in files
    ]


@router.get("/journal")
async def list_journal_entries(
    limit: int | None = Query(None, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: dict[str, Any] = Depends(get_current_user),
):
    journal_dir = _journal_dir()
    return await run_blocking(_list_journal_entries_sync, journal_dir, limit=limit, offset=offset)
