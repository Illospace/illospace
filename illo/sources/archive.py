"""Import old memory without changing its ownership or visibility."""

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from illo.db import one
from illo.ledger import items, people


class Entry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_ref: str = Field(min_length=1)
    title: str
    text: str = ""
    summary: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None
    owner: str | None = None
    visibility: Literal["team", "private"] = "team"
    provenance: dict = Field(default_factory=dict)
    links: list = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)


@dataclass
class Counts:
    loaded: int = 0
    updated: int = 0
    skipped: int = 0
    reasons: Counter = field(default_factory=Counter)

    def skip(self, reason):
        self.skipped += 1
        self.reasons[reason] += 1

    def text(self):
        reasons = "; ".join(f"{reason}: {count}" for reason, count in sorted(self.reasons.items()))
        return f"Loaded: {self.loaded}. Updated: {self.updated}. Skipped: {self.skipped}." + (
            f" Reasons: {reasons}." if reasons else ""
        )


class AliasConflict(ValueError):
    pass


async def resolve_ref(conn, source_ref, *, principal="team"):
    """Alias resolution keeps the same visibility check as a canonical ref."""
    return await items.get(conn, source_ref, principal=principal)


async def _store(conn, entry):
    ref = entry.source_ref if ":" in entry.source_ref else "archive:" + entry.source_ref
    owner = None
    if entry.owner:
        owner = await people.get(conn, entry.owner) or await people.by_email(conn, entry.owner)
    if entry.visibility == "private" and not owner:
        return "skip", "Private entry has no active owner"
    existing = await one(conn, "SELECT id,kind FROM items WHERE source_ref=%s FOR UPDATE", (ref,))
    if existing and existing["kind"] != "archive":
        return "skip", "Source ref belongs to another kind"
    aliases = set(entry.aliases)
    if ref != entry.source_ref:
        aliases.add(entry.source_ref)
    for alias in {*aliases, ref}:
        row = await one(
            conn,
            """
            SELECT id FROM items WHERE source_ref=%s
            UNION SELECT item_id AS id FROM item_aliases WHERE alias=%s
        """,
            (alias, alias),
        )
        if row and (not existing or row["id"] != existing["id"]):
            raise AliasConflict
    row = await items.upsert(
        conn,
        source_ref=ref,
        kind="archive",
        title=entry.title,
        body=entry.text,
        summary=entry.summary,
        owner_person_id=owner["id"] if owner else None,
        visibility=entry.visibility,
        links=entry.links,
        meta={
            "provenance": entry.provenance,
            "created_at": entry.created_at.isoformat() if entry.created_at else None,
            "updated_at": entry.updated_at.isoformat() if entry.updated_at else None,
        },
        source_at=entry.updated_at or entry.created_at,
    )
    if entry.created_at:
        await conn.execute(
            "UPDATE items SET created_at=%s WHERE id=%s", (entry.created_at, row["id"])
        )
    for alias in sorted(aliases - {ref}):
        result = await one(
            conn,
            """
            INSERT INTO item_aliases(alias,item_id) VALUES (%s,%s)
            ON CONFLICT(alias) DO UPDATE SET item_id=EXCLUDED.item_id
            WHERE item_aliases.item_id=EXCLUDED.item_id RETURNING item_id
        """,
            (alias, row["id"]),
        )
        if not result:
            raise AliasConflict
    return ("updated" if existing else "loaded"), None


async def import_archive(conn, path: Path | str, *, dry_run=False) -> Counts:
    """Count bad lines and continue. Dry runs roll back every database change."""
    counts = Counts()
    with Path(path).open(encoding="utf-8") as stream:
        async with conn.transaction(force_rollback=dry_run):
            for line in stream:
                if not line.strip():
                    continue
                try:
                    entry = Entry.model_validate(json.loads(line))
                except (ValueError, ValidationError):
                    counts.skip("Invalid entry")
                    continue
                try:
                    async with conn.transaction():
                        state, reason = await _store(conn, entry)
                except AliasConflict:
                    counts.skip("Alias belongs to another item")
                    continue
                if state == "skip":
                    counts.skip(reason)
                else:
                    setattr(counts, state, getattr(counts, state) + 1)
    return counts


async def setup(ctx) -> None:
    """Archive imports run through the CLI, without a model."""
