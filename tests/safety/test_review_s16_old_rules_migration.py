"""Rule 8: the migration removes old non-file rules without a fixed scope."""

from illo.db import migrate, one
from illo.ledger import rules


async def test_rule_08_old_rules_without_scope_are_removed(conn, ada):
    rows = []
    for origin, match, removed in [
        ("card", {}, True),
        ("proposal", {"lane_source": "t3"}, True),
        ("card", {"repository": "acme-example/engine"}, True),
        ("card", {"lane_source": None, "repository": None}, False),
        ("proposal", {"lane_source": "t3", "repository": "acme-example/engine"}, False),
        ("file", {}, False),
    ]:
        row = await rules.create(
            conn,
            owner_person_id=ada["id"],
            text="Use Preview.",
            origin=origin,
            match=match,
            answer_option_label="Preview",
        )
        rows.append((row["id"], removed))
    already_removed = await rules.create(conn, owner_person_id=ada["id"], text="Old rule.")
    await conn.execute(
        "UPDATE rules SET removed_at=now()-interval '1 day' WHERE id=%s", (already_removed["id"],)
    )
    old_time = (
        await one(conn, "SELECT removed_at FROM rules WHERE id=%s", (already_removed["id"],))
    )["removed_at"]
    # Recreate the state before this migration while retaining the existing schema.
    await conn.execute("DELETE FROM schema_migrations WHERE name='0503_rules_remove_unscoped.sql'")
    await conn.commit()
    await migrate(conn)
    for rule_id, removed in rows:
        row = await one(conn, "SELECT removed_at FROM rules WHERE id=%s", (rule_id,))
        assert bool(row["removed_at"]) is removed
    assert (await one(conn, "SELECT removed_at FROM rules WHERE id=%s", (already_removed["id"],)))[
        "removed_at"
    ] == old_time
    assert await migrate(conn) == []
