"""Store tasks and their record. All authority checks run before a write."""

from dataclasses import dataclass
from datetime import UTC, datetime
from functools import wraps

from psycopg.types.json import Jsonb

from illo.db import all_rows, one
from illo.ledger import kv
from illo.tasks import checks


class TaskError(ValueError):
    """A fixed reason that a caller can read."""


@dataclass(frozen=True)
class Viewer:
    person: str | None
    actor: str
    human: bool = False
    team: bool = False
    tainted: bool = False
    team_only: bool = False
    holder_refs: tuple[str, ...] = ()


async def _identity(conn, viewer):
    if viewer.tainted or viewer.actor == "outsider":
        raise TaskError("This caller cannot read tasks.")
    if viewer.person:
        row = await one(conn, "SELECT * FROM people WHERE key=%s AND active", (viewer.person,))
        if row:
            return row
    if viewer.team and not viewer.person:
        return None
    raise TaskError("The person is missing or inactive.")


def list_config(workspace, key, viewer):
    entry = next((entry for entry in workspace.lists if entry.key == key), None)
    if not entry or (entry.owner and (entry.owner != viewer.person or viewer.team_only)) or viewer.tainted:
        raise TaskError("The list was not found.")
    return entry


def visible_lists(workspace, viewer):
    if viewer.tainted or viewer.actor == "outsider":
        return []
    return [entry for entry in workspace.lists if not entry.owner or (entry.owner == viewer.person and not viewer.team_only)]


def _mutation(fn):
    @wraps(fn)
    async def call(conn, *args, **kwargs):
        async with conn.transaction():
            # One graph lock prevents a parent and its pieces from taking opposite row locks.
            await conn.execute("SELECT pg_advisory_xact_lock(12000001)")
            return await fn(conn, *args, **kwargs)
    return call


async def get(conn, task_id, *, viewer, lock=False):
    person = await _identity(conn, viewer)
    row = await one(conn, """SELECT * FROM tasks WHERE id=%s
        AND (visibility='team' OR owner_person_id=%s)""" + (" FOR UPDATE" if lock else ""),
        (task_id, person["id"] if person and not viewer.team_only else None))

    return await timing(conn, row) if row else None


async def _require(conn, task_id, viewer):
    row = await get(conn, task_id, viewer=viewer, lock=True)
    if not row:
        raise TaskError("The task was not found.")
    return row


async def search(conn, *, viewer, list_key=None, holder=None, state=None, waits_for=None,
                 parent_id=None, query="", open=False, limit=30, mine_refs=None, offset=0):
    person = await _identity(conn, viewer)
    rows = await all_rows(conn, """SELECT t.*,
        (SELECT count(*) FROM tasks c WHERE c.parent_id=t.id
          AND c.state NOT IN ('done','cancelled')) AS open_pieces,
        (SELECT actor FROM task_events e WHERE e.task_id=t.id ORDER BY id DESC LIMIT 1) AS last_actor
        FROM tasks t WHERE (t.visibility='team' OR t.owner_person_id=%s)
        AND (%s::text IS NULL OR list_key=%s) AND (%s::text IS NULL OR holder=%s)
        AND (%s::text IS NULL OR state=%s) AND (%s::text IS NULL OR waits_for=%s)
        AND (%s::bigint IS NULL OR parent_id=%s)
        AND (NOT %s OR state NOT IN ('done','cancelled'))
        AND (%s::text[] IS NULL OR holder=ANY(%s) OR waits_for=%s)
        AND (%s='' OR tsv @@ websearch_to_tsquery('english',%s)
             OR title ILIKE %s)
        ORDER BY updated_at DESC,id DESC LIMIT %s OFFSET %s""",
        (person["id"] if person and not viewer.team_only else None, list_key, list_key, holder, holder, state, state,
         waits_for, waits_for, parent_id, parent_id, open, mine_refs, mine_refs,
         "person:" + viewer.person if viewer.person else None, query, query, f"%{query}%", min(100, max(1, limit)), max(0, offset)))

    return [await timing(conn, row) for row in rows]


async def events(conn, task_id, *, viewer, limit=20):
    if not await get(conn, task_id, viewer=viewer):
        raise TaskError("The task was not found.")
    return await all_rows(conn, """SELECT * FROM (SELECT * FROM task_events WHERE task_id=%s
        ORDER BY id DESC LIMIT %s) e ORDER BY id""", (task_id, min(100, max(1, limit))))


async def _event(conn, row, actor, kind, text="", data=None):
    active = kind not in {"woken", "wake_failed", "released", "reminded"}
    stamps = await one(conn, """UPDATE tasks SET updated_at=now(),
        last_activity_at=CASE WHEN %s THEN now() ELSE last_activity_at END
        WHERE id=%s RETURNING updated_at,last_activity_at,waits_for""", (active, row["id"]))
    row.update(stamps)
    if actor == row.get("holder") or kind == "reopened":
        await conn.execute("UPDATE tasks SET stopped_at=NULL WHERE id=%s", (row["id"],))
    data = {**(data or {}), "waits_for": row["waits_for"]}
    if kind == "released":
        data["holder"] = row["holder"]
    event = await one(conn, """INSERT INTO task_events (task_id,actor,kind,text,data)
        VALUES (%s,%s,%s,%s,%s) RETURNING *""", (row["id"], actor, kind, text, Jsonb(data)))
    await kv.set(conn, f"task:pending:{row['id']}", {"event": event["id"]})
    reason = {"handed": "task.handed", "reopened": "task.reopened", "released": "task.released"}.get(kind)
    if kind == "message" and data.get("wake"):
        reason = "task.message"
    if reason and (row.get("holder") or "").startswith("agent:"):
        await conn.execute("""INSERT INTO task_wakes(event_id,task_id,holder,reason)
            VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
            (event["id"], row["id"], row["holder"], reason))
    return event


async def timing(conn, row, now=None):
    now = now or datetime.now(UTC)
    last = await one(conn, "SELECT max(at) AS at FROM task_events WHERE task_id=%s AND kind='woken'",
                     (row["id"],))
    base = max(t for t in (row["last_activity_at"], row.get("alive_at"), last["at"]) if t)
    row["silent_hours"] = (round(max(0, (now-base).total_seconds()/3600), 1)
                           if row["holder"] and row["state"] not in {"done", "cancelled"} else None)
    return row


async def alive(conn, task_id, source, at=None):
    await conn.execute("""UPDATE tasks SET alive_at=GREATEST(alive_at,%s),stopped_at=NULL
        WHERE id=%s AND state IN ('todo','working')""", (at or datetime.now(UTC), task_id))


async def _holder(conn, workspace, entry, holder):
    if holder is None:
        return
    if entry.holders and holder not in entry.holders:
        raise TaskError("The holder is not allowed by this list.")
    kind, _, key = holder.partition(":")
    owner = key if kind == "person" else next((a.owner for a in workspace.agents if a.key == key), None)
    if kind not in {"agent", "person"} or not owner:
        raise TaskError("The holder is unknown.")
    if entry.owner and entry.owner != owner:
        raise TaskError("A private task needs a holder of its owner.")
    if not await one(conn, "SELECT id FROM people WHERE key=%s AND active", (owner,)):
        raise TaskError("The holder is inactive.")


async def reviewer(conn, workspace, row):
    entry = next((entry for entry in workspace.lists if entry.key == row["list_key"]), None)
    candidates = [row["handed_by"], row["created_by"], entry.reviewer if entry else None,
                  "person:" + workspace.config.default_owner]
    if row["visibility"] == "private":
        owner = await one(conn, "SELECT key FROM people WHERE id=%s AND active", (row["owner_person_id"],))
        return "person:" + owner["key"] if owner else None
    for ref in candidates:
        if ref and ref.startswith("person:") and await one(
            conn, "SELECT id FROM people WHERE key=%s AND active", (ref[7:],)
        ):
            return ref
    raise TaskError("The task needs an active reviewer.")


def _person(viewer):
    if not viewer.human or not viewer.person or viewer.actor != "person:" + viewer.person:
        raise TaskError("A person must make this change.")


def _working_holder(row, viewer):
    if row["holder"] not in (viewer.actor, *viewer.holder_refs) or row["state"] not in {"todo", "working"}:
        raise TaskError("This task was handed to someone else. Change nothing and end the run.")


async def proof(conn, workspace, row, ctx=None):
    entry = next((entry for entry in workspace.lists if entry.key == row["list_key"]), None)
    rule = entry.done if entry else "person"
    names = ["person"] if rule == "person" else [*rule]
    if "pieces" not in names and await one(
        conn, "SELECT id FROM tasks WHERE parent_id=%s AND state<>'cancelled' LIMIT 1", (row["id"],)
    ):
        names.append("pieces")
    return await checks.evaluate(ctx, conn, row, names)


async def _state(conn, workspace, row, state, actor, kind, text, data=None, waits=None):
    if state in {"todo", "working"}:
        waits = row["holder"]
    elif state == "review":
        waits = await reviewer(conn, workspace, row)
    else:
        waits = None
    result = await one(conn, """UPDATE tasks SET state=%s,waits_for=%s,
        done_at=CASE WHEN %s='done' THEN now() END,
        done_by=CASE WHEN %s='done' THEN %s END WHERE id=%s RETURNING *""",
        (state, waits, state, state, actor, row["id"]))
    await _event(conn, result, actor, kind, text, data)
    return result


async def _parent(conn, workspace, row, ctx=None):
    if not row["parent_id"]:
        return
    parent = await one(conn, "SELECT * FROM tasks WHERE id=%s FOR UPDATE", (row["parent_id"],))
    # A cancelled piece is not part of the whole.
    children = await all_rows(conn, "SELECT state FROM tasks WHERE parent_id=%s AND state<>'cancelled'", (parent["id"],))
    if parent["state"] == "cancelled" or not children:
        return
    if all(child["state"] == "done" for child in children):
        results = await proof(conn, workspace, parent, ctx)
        # A person rule is the final confirmation. Other proof must pass before review.
        ready = all(result["ok"] or result["name"] == "person" for result in results)
        if parent["state"] not in {"review", "done"}:
            parent = await _state(conn, workspace, parent, "review", "code", "state",
                                  ("All pieces are done. A person must confirm the whole task." if ready
                                   else "All pieces are done. The whole task still needs proof."),
                                  {"checks": results})
    elif parent["state"] in {"review", "done"}:
        parent = await _state(conn, workspace, parent, "working" if parent["holder"] else "todo",
                              "code", "state", "A piece is not done. The whole task needs work.")
        await _parent(conn, workspace, parent, ctx)


async def _settle(conn, workspace, row, ctx=None, *, active=False):
    """Apply the Done rule. `active` is a link change on work that the holder did not return."""
    results = await proof(conn, workspace, row, ctx)
    children = await all_rows(conn, "SELECT state FROM tasks WHERE parent_id=%s AND state<>'cancelled'", (row["id"],))
    if active and (children or not (results and all(r["ok"] for r in results))):
        # Only passed checks close active work. A new link never sends it to review.
        return row
    if children:
        if all(child["state"] == "done" for child in children):
            row = await _state(conn, workspace, row, "review", "code", "state",
                               "All pieces are done. A person must confirm the whole task.", {"checks": results})
        elif row["state"] == "review":
            row = await _state(conn, workspace, row, "review", "code", "state",
                               "Some pieces are not done.", {"checks": results})
    elif results and all(r["ok"] for r in results):
        row = await _state(conn, workspace, row, "done", "code", "done",
                           "The checks pass: " + ", ".join(r["name"] for r in results), {"checks": results})
    elif row["state"] not in {"done", "cancelled"}:
        row = await _state(conn, workspace, row, "review", "code", "state",
                           "A person must review the returned work.", {"checks": results})
    await _parent(conn, workspace, row, ctx)
    return row


@_mutation
async def create(conn, *, workspace, viewer, list_key, title, brief="", holder=None,
                 due_at=None, parent_id=None, links=None, use_default=True):
    await _identity(conn, viewer)
    entry = list_config(workspace, list_key, viewer)
    holder = (holder or entry.default_holder) if use_default else holder
    await _holder(conn, workspace, entry, holder)
    parent = await _require(conn, parent_id, viewer) if parent_id else None
    if parent:
        if parent["state"] in {"done", "cancelled"}:
            raise TaskError("Reopen the parent before you add a piece.")
        if parent["list_key"] != list_key:
            raise TaskError("A piece must use its parent's list.")
    if not viewer.human and viewer.actor != "code" and holder and holder not in {viewer.actor, "person:" + (viewer.person or "")}:
        delegation = parent and parent["holder"] == viewer.actor and parent["state"] in {"todo", "working"}
        if not delegation and holder != entry.default_holder:
            raise TaskError("An agent can hand over only a piece of its own task.")
    owner = await one(conn, "SELECT id FROM people WHERE key=%s AND active", (entry.owner,)) if entry.owner else None
    row = await one(conn, """INSERT INTO tasks
        (list_key,title,brief,holder,holder_since,handed_by,waits_for,due_at,parent_id,links,
         created_by,owner_person_id,visibility)
        VALUES (%s,%s,%s,%s,CASE WHEN %s::text IS NOT NULL THEN now() END,%s,%s,%s,%s,%s,%s,%s,%s)
        RETURNING *""", (list_key, title, brief, holder, holder, viewer.actor if holder else None,
                            holder, due_at, parent_id, Jsonb(links or []), viewer.actor,
                            owner["id"] if owner else None, "private" if owner else "team"))
    await _ticket_links(conn, row)
    await _event(conn, row, viewer.actor, "created", title)
    if holder:
        await _event(conn, row, viewer.actor, "handed", "The task has a holder.", {"holder": holder})
    if parent:
        await _parent(conn, workspace, row)
    return row


@_mutation
async def update(conn, task_id, *, workspace, viewer, **changes):
    row = await _require(conn, task_id, viewer)
    if not viewer.human and row["created_by"] != viewer.actor:
        raise TaskError("Only a person or the creator can edit the brief.")
    if not viewer.human and set(changes) - {"brief"}:
        raise TaskError("A person must change these task fields.")
    if set(changes) - {"title", "brief", "due_at", "list_key"}:
        raise TaskError("This task field cannot change here.")
    entry = list_config(workspace, changes.get("list_key", row["list_key"]), viewer)
    if entry.key != row["list_key"]:
        if row["parent_id"] or await one(conn, "SELECT id FROM tasks WHERE parent_id=%s LIMIT 1", (task_id,)):
            raise TaskError("Move only a task with no parent or pieces.")
        if entry.owner or row["visibility"] == "private":
            raise TaskError("A private task cannot move to another list.")
        await _holder(conn, workspace, entry, row["holder"])
    for key, value in changes.items():
        await conn.execute(f"UPDATE tasks SET {key}=%s WHERE id=%s", (value, task_id))
    await _event(conn, row, viewer.actor, "state", "The task details changed.", {"fields": sorted(changes)})
    return await get(conn, task_id, viewer=viewer)


@_mutation
async def hand(conn, task_id, *, workspace, viewer, holder):
    _person(viewer)
    row = await _require(conn, task_id, viewer)
    if row["state"] in {"done", "cancelled"}:
        raise TaskError("Reopen the task before you hand it over.")
    entry = list_config(workspace, row["list_key"], viewer)
    await _holder(conn, workspace, entry, holder)
    row = await one(conn, """UPDATE tasks SET holder=%s,holder_since=CASE WHEN %s::text IS NOT NULL
        THEN now() END,handed_by=%s,alive_at=NULL,stopped_at=NULL WHERE id=%s RETURNING *""", (holder, holder, viewer.actor, task_id))
    return await _state(conn, workspace, row, "todo", viewer.actor, "handed",
                        "The task has a new holder.", {"holder": holder})


@_mutation
async def add_link(conn, task_id, *, workspace, viewer, links, ctx=None, _defer=False):
    row = await _require(conn, task_id, viewer)
    if not viewer.human and viewer.actor != "code":
        _working_holder(row, viewer)
    merged = row["links"] + [link for link in links if link not in row["links"]]
    if merged == row["links"]:
        return row
    row = await one(conn, "UPDATE tasks SET links=%s WHERE id=%s RETURNING *", (Jsonb(merged), task_id))
    await _ticket_links(conn, row)
    await _event(conn, row, viewer.actor, "link", "The proof links changed.", {"links": links})
    if _defer or row["state"] in {"done", "cancelled"}:
        return row
    return await _settle(conn, workspace, row, ctx, active=row["state"] != "review")


@_mutation
async def write_result(conn, task_id, *, workspace, viewer, result=None, append=None,
                       result_version=None, message, links=None, returned=False, ctx=None):
    row = await _require(conn, task_id, viewer)
    _working_holder(row, viewer)
    if not message or not message.strip():
        raise TaskError("Give a message that says what changed and what you need.")
    if (result is None) == (append is None):
        raise TaskError("Give either a result or text to append.")
    if result is not None and result_version != row["result_version"]:
        raise TaskError("The result changed. Read the task again before you replace it.")
    text = result if result is not None else row["result"] + ("\n" if row["result"] else "") + append
    row = await one(conn, """UPDATE tasks SET result=%s,result_version=result_version+1,
        state='working',waits_for=holder WHERE id=%s RETURNING *""", (text, task_id))
    await _event(conn, row, viewer.actor, "result", "The holder changed the result.", {"version": row["result_version"]})
    await _event(conn, row, viewer.actor, "message", message)
    if links:
        row = await add_link(conn, task_id, workspace=workspace, viewer=viewer, links=links, ctx=ctx, _defer=True)
    if returned:
        row = await return_work(conn, task_id, workspace=workspace, viewer=viewer, message=message, ctx=ctx)
    elif links:
        row = await _settle(conn, workspace, row, ctx, active=True)
    return row


@_mutation
async def return_work(conn, task_id, *, workspace, viewer, message="", ctx=None):
    row = await _require(conn, task_id, viewer)
    _working_holder(row, viewer)
    row = await _state(conn, workspace, row, "review", viewer.actor, "returned", message)
    return await _settle(conn, workspace, row, ctx)


@_mutation
async def post(conn, task_id, *, workspace, viewer, text, question=False, to=None):
    row = await _require(conn, task_id, viewer)
    if not viewer.human:
        _working_holder(row, viewer)
    if question:
        if row["holder"] not in (viewer.actor, *viewer.holder_refs):
            raise TaskError("Only the holder can ask a blocking question.")
        target = to or await reviewer(conn, workspace, row)
        if not target or not target.startswith("person:"):
            raise TaskError("A blocking question needs a person.")
        if not await one(conn, "SELECT id FROM people WHERE 'person:'||key=%s AND active", (target,)):
            raise TaskError("The asked person is missing or inactive.")
        if row["visibility"] == "private" and target != await reviewer(conn, workspace, row):
            raise TaskError("A private task can ask only its owner.")
        await conn.execute("UPDATE tasks SET waits_for=%s WHERE id=%s", (target, task_id))
    elif row["state"] in {"todo", "working"} and row["waits_for"] == viewer.actor:
        await conn.execute("UPDATE tasks SET waits_for=holder WHERE id=%s", (task_id,))
    await _event(conn, row, viewer.actor, "question" if question else "message", text,
                 {"to": target} if question else {"wake": viewer.human and row["state"] in {"todo", "working"}
                 and (row["waits_for"] in {viewer.actor, row["holder"]})})
    return await get(conn, task_id, viewer=viewer)


@_mutation
async def done(conn, task_id, *, workspace, viewer, force=False, reason="", ctx=None):
    _person(viewer)
    row = await _require(conn, task_id, viewer)
    if row["state"] in {"done", "cancelled"}:
        raise TaskError("The task is already closed.")
    results = await proof(conn, workspace, row, ctx)
    pieces = await all_rows(conn, "SELECT state FROM tasks WHERE parent_id=%s AND state<>'cancelled'", (task_id,))
    failed = any(not r["ok"] and r["name"] != "person" for r in results) or any(p["state"] != "done" for p in pieces)
    if failed and not (force and reason.strip()):
        raise TaskError("The checks do not pass. A forced confirmation needs a reason.")
    if force and not reason.strip():
        raise TaskError("A forced confirmation needs a reason.")
    row = await _state(conn, workspace, row, "done", viewer.actor, "done", reason or "A person confirms the work.",
                       {"force": force, "checks": results})
    await _parent(conn, workspace, row, ctx)
    return row


@_mutation
async def reopen(conn, task_id, *, workspace, viewer, reason):
    _person(viewer)
    row = await _require(conn, task_id, viewer)
    if not reason.strip():
        raise TaskError("Give a reason to send the task back.")
    row = await _state(conn, workspace, row, "todo", viewer.actor, "reopened", reason)
    await _parent(conn, workspace, row)
    return row


@_mutation
async def cancel(conn, task_id, *, workspace, viewer, reason=""):
    _person(viewer)
    row = await _require(conn, task_id, viewer)
    row = await _state(conn, workspace, row, "cancelled", viewer.actor, "cancelled", reason)
    await _parent(conn, workspace, row)
    return row


@_mutation
async def returned_by_signal(conn, task_id, source, message, *, workspace, ctx=None):
    row = await one(conn, "SELECT * FROM tasks WHERE id=%s FOR UPDATE", (task_id,))
    if not row or row["state"] not in {"todo", "working", "review"}:
        return row
    if row["state"] == "review":
        return await _settle(conn, workspace, row, ctx)
    row = await _state(conn, workspace, row, "review", "code", "returned", message,
                       {"source": source, "holder": row["holder"]})
    return await _settle(conn, workspace, row, ctx)


@_mutation
async def wake(conn, task_id, *, workspace, viewer, reason="A person requests a restart."):
    _person(viewer)
    row = await _require(conn, task_id, viewer)
    if row["state"] not in {"todo", "working"} or not (row["holder"] or "").startswith("agent:"):
        raise TaskError("Wake needs an open task with an agent holder.")
    await conn.execute("UPDATE tasks SET waits_for=holder,stopped_at=NULL WHERE id=%s", (task_id,))
    await _event(conn, row, viewer.actor, "released", reason, {"requested": True})
    return row


async def _ticket_links(conn, row):
    for link in row["links"]:
        if link.get("kind") == "ticket":
            existing = await one(conn, """INSERT INTO task_tickets(url,task_id) VALUES (%s,%s)
                ON CONFLICT(url) DO UPDATE SET url=EXCLUDED.url RETURNING task_id""", (link["url"], row["id"]))
            if existing["task_id"] != row["id"]:
                raise TaskError("The ticket already has a task.")
