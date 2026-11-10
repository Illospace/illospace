"""Archive imports keep old refs without widening private entries."""

import json

from illo.db import all_rows, one
from illo.ledger import items
from illo.sources.archive import import_archive, resolve_ref


def archive(tmp_path, entries):
    path = tmp_path / "archive.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in entries))
    return path


async def test_idempotent_alias_owner_and_dates(conn, tmp_path):
    path = archive(
        tmp_path,
        [
            {
                "source_ref": "old-one",
                "title": "Old decision",
                "text": "Keep it",
                "owner": "ada@acme.example",
                "visibility": "private",
                "aliases": ["memory:45"],
                "provenance": {"source": "old"},
                "links": ["https://example.com/decision"],
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-02-01T00:00:00Z",
            }
        ],
    )
    # Use the fixture's exact invented address.
    person = await one(conn, "SELECT * FROM people WHERE key='ada'")
    data = json.loads(path.read_text())
    data["owner"] = person["emails"][0]
    path.write_text(json.dumps(data))
    first = await import_archive(conn, path)
    second = await import_archive(conn, path)
    assert (first.loaded, second.updated) == (1, 1)
    assert len(await all_rows(conn, "SELECT * FROM items")) == 1
    canonical = await items.get(conn, "archive:old-one", principal="person:ada")
    assert canonical["body"] == "Keep it"
    assert canonical["meta"]["provenance"] == {"source": "old"}
    assert canonical["created_at"].year == 2024
    assert (await resolve_ref(conn, "memory:45", principal="person:ada"))["id"] == canonical["id"]
    assert (await items.get(conn, "old-one", principal="person:ada"))["id"] == canonical["id"]
    assert await resolve_ref(conn, "memory:45", principal="person:ben") is None
    assert await resolve_ref(conn, "memory:45") is None


async def test_skip_private_unknown_and_invalid_lines(conn, tmp_path):
    path = archive(
        tmp_path,
        [
            {"source_ref": "a", "title": "Unknown", "owner": None, "visibility": "private"},
            {"source_ref": "b", "title": "Unknown", "owner": "nobody", "visibility": "private"},
            {"source_ref": "c", "title": "Team", "owner": "nobody"},
        ],
    )
    with path.open("a") as stream:
        stream.write("\nnot json\n{}\n")
    counts = await import_archive(conn, path)
    assert (counts.loaded, counts.skipped) == (1, 4)
    assert counts.reasons["Private entry has no active owner"] == 2
    assert counts.reasons["Invalid entry"] == 2
    assert (await items.get(conn, "archive:c"))["visibility"] == "team"


async def test_dry_run_and_updates(conn, tmp_path):
    path = archive(tmp_path, [{"source_ref": "legacy:a", "title": "Before", "aliases": ["old:a"]}])
    assert (await import_archive(conn, path, dry_run=True)).loaded == 1
    assert not await all_rows(conn, "SELECT * FROM items")
    assert not await all_rows(conn, "SELECT * FROM item_aliases")
    await import_archive(conn, path)
    path.write_text(json.dumps({"source_ref": "legacy:a", "title": "After", "aliases": ["old:a"]}))
    assert (await import_archive(conn, path)).updated == 1
    assert (await items.get(conn, "old:a"))["title"] == "After"


async def test_alias_collision_never_steals_an_item(conn, tmp_path):
    path = archive(
        tmp_path,
        [
            {"source_ref": "one", "title": "One", "aliases": ["shared"]},
            {"source_ref": "two", "title": "Two", "aliases": ["shared"]},
        ],
    )
    counts = await import_archive(conn, path)
    assert (counts.loaded, counts.skipped) == (1, 1)
    assert (await items.get(conn, "shared"))["title"] == "One"
    assert await items.get(conn, "archive:two") is None
