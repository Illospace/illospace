"""Read board access from live task links and active identities."""

from illo.db import all_rows, one
from illo.ledger import board

LINKED_TASKS = f"""SELECT t.* FROM tasks t WHERE t.id=b.task_id OR EXISTS (
    SELECT 1 FROM task_tickets tt WHERE tt.task_id=t.id AND b.ticket_url={board.TICKET_MATCH}
)"""
SCOPE_FIELDS = ("visibility", "owner_person_id", "task_id", "ticket_url",
                "effective_owner", "task_visibility", "linked_task_ids", "guest_person_ids")


async def thread_access(conn, thread_id):
    row = await one(conn, "SELECT * FROM board_threads WHERE id=%s", (thread_id,))
    if not row:
        return None
    tasks = await all_rows(conn, f"""SELECT t.* FROM board_threads b
        CROSS JOIN LATERAL ({LINKED_TASKS}) t WHERE b.id=%s ORDER BY t.id""", (thread_id,))
    guests = [
        r["person_id"]
        for r in await all_rows(
            conn, "SELECT person_id FROM board_thread_guests WHERE thread_id=%s ORDER BY person_id", (thread_id,)
        )
    ]
    private_task_owners = {t["owner_person_id"] for t in tasks if t["visibility"] == "private"}
    if guests and private_task_owners:
        return None
    owners = set(private_task_owners)
    owner = row["owner_person_id"] if row["visibility"] == "private" else None
    if owner is not None:
        owners.add(owner)
    if None in owners or len(owners) > 1 or (row["task_id"] and not tasks):
        return None
    task = next((t for t in tasks if t["id"] == row["task_id"]), tasks[0] if tasks else None)
    # A 2-person private thread (with guests) never gets single-owner personal_read authority,
    # so the owner's private notes/DMs cannot leak to the guest.
    effective_owner = None if guests else next(iter(owners), None)
    return row | {
        "effective_owner": effective_owner,
        "task_visibility": "private" if any(t["visibility"] == "private" for t in tasks) else (
            "team" if tasks else None),
        "linked_task_ids": [t["id"] for t in tasks],
        "guest_person_ids": guests,
        "task": task,
    }


def scope(thread):
    return {key: thread[key] for key in SCOPE_FIELDS}


async def thread_ok(conn, thread_id, snapshot=None):
    live = await thread_access(conn, thread_id)
    return live if live and live["state"] == "open" and (not snapshot or scope(live) == snapshot) else None


async def recipient_ok(conn, thread, person, *, slack=True):
    if not person or not person.get("active") or (slack and not person.get("slack_id")):
        return False
    return bool(await one(conn, f"""SELECT 1 FROM board_threads b JOIN people p ON p.id=%s
        WHERE b.id=%s AND p.active AND {board.visible_to('p.id')}
        AND (%s::boolean=false OR p.slack_id=%s)""",
        (person["id"], thread["id"], slack, person.get("slack_id"))))


async def target_ok(conn, workspace, thread, target, asker):
    named = target.startswith("agent:")
    key = target.split(":", 1)[1]
    author = (asker or {}).get("provenance", {})
    if named:
        agent = next((a for a in workspace.agents if a.key == key), None)
        if not agent or author.get("agent_key") == key:
            return None
        owner = agent.owner
    else:
        if not author.get("agent_key") and author.get("person_key") == key:
            return None
        owner = key
    row = await one(conn, """SELECT p.*, tok.last_used_at FROM people p JOIN tokens tok ON tok.person_id=p.id
        WHERE p.key=%s AND p.active AND tok.agent_key IS NOT DISTINCT FROM %s
          AND tok.revoked_at IS NULL AND tok.scopes=ARRAY['agent']::text[]
        ORDER BY tok.last_used_at DESC NULLS LAST LIMIT 1""", (owner, key if named else None))
    return row if await recipient_ok(conn, thread, row, slack=False) else None
