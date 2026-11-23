"""Check the person, save the file, and record each memory change."""

import re
import uuid
from datetime import UTC, datetime

from illo import notes
from illo.db import one
from illo.ledger import audit, people
from illo.memory import files
from illo.notes import gitstore
from illo.notes.files import file_path
from illo.tools.checks import Decision, effective_profile
from illo.tools.registry import Refusal
from illo.workspace import WorkspaceError, load_workspace

OWNER_FIELDS = {"name", "holders", "default_holder", "done", "reviewer", "slack_channel"}


async def actor(ctx):
    run = ctx.run
    if effective_profile(run) not in {"person_dm", "team_channel"}:
        return None
    if run["profile"] == "team_channel":
        return await one(ctx.conn, """SELECT p.* FROM people p JOIN events e
            ON p.key=e.payload->>'task_actor' JOIN runs r ON r.event_id=e.id
            WHERE r.id=%s AND p.active AND e.channel='slack'""", (run["id"],))
    if not run["principal"].startswith("person:"):
        return None
    person = await people.get(ctx.conn, run["principal"][7:])
    return person if person and person["id"] == run["person_id"] else None


async def limit(conn, person):
    # The person lock also covers saves from two runs at once.
    active = await one(conn, "SELECT id FROM people WHERE id=%s AND active FOR UPDATE", (person["id"],))
    if not active:
        raise Refusal("Memory needs a known active person.")
    count = await one(conn, """SELECT count(*) AS n FROM memory_changes
        WHERE person_id=%s AND created_at > now() - interval '1 hour'""", (person["id"],))
    pending = await one(conn, """SELECT count(*) AS n FROM note_proposals
        WHERE owner_person_id=%s AND created_at > now() - interval '1 hour'""", (person["id"],))
    if count["n"] + pending["n"] >= 20:
        raise Refusal("You reached 20 memory saves in one hour. Try again later.")


async def note_check(ctx, args):
    if not await actor(ctx):
        return Decision("deny", "Memory needs a known active person's untainted request.")
    return Decision("draft" if ctx.run["personal_read"] else "allow",
                    "A Keep card waits after a personal read." if ctx.run["personal_read"]
                    else "The person can save team notes.")


def list_permission(workspace, person, args):
    entry = next((e for e in workspace.lists if e.key == args["key"]), None)
    if entry and entry.owner and entry.owner != person["key"] and person["role"] != "owner":
        return "This list belongs to another person."
    if workspace.config.memory.list_changes == "everyone" or person["role"] == "owner" or (
            entry and entry.owner == person["key"]):
        return None
    if entry and not (OWNER_FIELDS & args.keys()):
        return None
    owner_key = entry.owner if entry and entry.owner else workspace.config.default_owner
    owner = next(p for p in workspace.people if p.key == owner_key)
    return f"Only an owner can change who holds this work. Ask {owner.name}."


async def list_check(ctx, args):
    person = await actor(ctx)
    if not person:
        return Decision("deny", "Memory needs a known active person's untainted request.")
    if ctx.run["personal_read"]:
        return Decision("deny", "This run read personal data. Ask again in a new conversation to change a list.")
    reason = list_permission(ctx.app.workspace, person, args)
    return Decision("deny", reason) if reason else Decision("allow", "The person can change this list.")


async def record(ctx, conn, *, kind, target, title, before, after, person, run_id,
                 state="saved", tool=None):
    row = await one(conn, """INSERT INTO memory_changes
        (kind,target,title,before_text,after_text,person_id,run_id,state)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
        (kind, target, title, before, after, person["id"], run_id, state))
    if tool:
        await audit.write(conn, principal="person:" + person["key"], tool=tool,
                          args={}, decision="allow", reason="The memory file is saved.",
                          run_id=run_id, target=f"memory:{row['id']}")
    return row


async def persist(ctx, conn, kind, target, text, message):
    folder, name = ("notes", target) if kind == "note" else ("lists", target + ".md")
    try:
        path = files.replace(ctx.workspace.root, folder, name, text)
    except (WorkspaceError, ValueError) as exc:
        raise Refusal(str(exc)) from None
    ctx.workspace = load_workspace(ctx.workspace.root)
    if kind == "note":
        await notes.sync(ctx, conn)
    return await gitstore.store(ctx, conn, path, target, commit=True, message=message)


async def note_draft(ctx, args):
    person = await actor(ctx)
    await limit(ctx.conn, person)
    run = {**ctx.run, "principal": "person:" + person["key"], "person_id": person["id"]}
    replaces = args.get("replaces")
    if replaces:
        path = file_path(ctx.app.workspace.root, "notes", replaces[5:])
        if not path.exists():
            raise Refusal("replaces: The note was not found.")
    result = await notes.propose(ctx.app, ctx.conn, run, args["title"], args["text"], args["about"])
    if replaces:
        await ctx.conn.execute("UPDATE note_proposals SET file_name=%s WHERE id=%s",
                               (replaces[5:], result["proposal_id"]))
    return result


async def note_save(ctx, args):
    person = await actor(ctx)
    if not person or ctx.run["personal_read"]:
        raise Refusal("Memory needs a known active person's untainted request without personal reads.")
    await limit(ctx.conn, person)
    async with ctx.app.reload_lock:
        target = args.get("replaces")
        slug = re.sub(r"[^a-z0-9]+", "-", args["title"].lower()).strip("-")[:60] or "note"
        target = target[5:] if target else f"{slug}-{uuid.uuid4().hex[:12]}.md"
        path = file_path(ctx.app.workspace.root, "notes", target)
        if args.get("replaces") and not path.exists():
            raise Refusal("replaces: The note was not found.")
        before = path.read_text() if path.exists() else None
        after = files.render({"title": args["title"], "about": args["about"],
                              "author": person["key"], "created": datetime.now(UTC).isoformat(),
                              "visibility": "team"}, args["text"])
        committed = await persist(ctx.app, ctx.conn, "note", target, after, "memory: note " + args["title"])
        row = await record(ctx.app, ctx.conn, kind="note", target=target, title=args["title"],
                           before=before, after=after, person=person, run_id=ctx.run["id"])
    return {"ref": "note:" + target, "change_id": row["id"], "committed": committed,
            "line": "I will remember: " + " ".join(args["text"].split())[:300]}


async def list_save(ctx, args):
    person = await actor(ctx)
    if not person or ctx.run["personal_read"]:
        raise Refusal("Memory needs a known active person's untainted request without personal reads.")
    await limit(ctx.conn, person)
    async with ctx.app.reload_lock:
        reason = list_permission(ctx.app.workspace, person, args)
        if reason:
            raise Refusal(reason)
        path = file_path(ctx.app.workspace.root, "lists", args["key"] + ".md", create=False)
        before = path.read_text() if path.exists() else None
        if before is not None:
            front, body = files.split(before)
        else:
            entry = next((e for e in ctx.app.workspace.lists if e.key == args["key"]), None)
            if not entry and not args.get("name"):
                raise Refusal("name: A new list needs a name.")
            front = entry.model_dump(exclude={"key", "body", "path"}, exclude_defaults=True) if entry else {}
            if entry:
                front["name"] = entry.name
            body = entry.body if entry else ""
        for field in args.keys() - {"key", "instructions", "add"}:
            front[field] = args[field]
        if "instructions" in args:
            body = args["instructions"]
        if "add" in args:
            body = body.rstrip() + ("\n\n" if body.strip() else "") + args["add"].strip()
        if len(body) > 20000:
            raise Refusal("instructions: Use at most 20000 characters.")
        after = files.render(front, body)
        committed = await persist(ctx.app, ctx.conn, "list", args["key"], after, "memory: list " + args["key"])
        row = await record(ctx.app, ctx.conn, kind="list", target=args["key"], title=front["name"],
                           before=before, after=after, person=person, run_id=ctx.run["id"])
    line = args.get("add") or body or front.get("description") or front["name"]
    return {"ref": "list:" + args["key"], "change_id": row["id"], "committed": committed,
            "line": "I will remember: " + " ".join(line.split())[:300]}


async def forget_check(ctx, args):
    if not await actor(ctx):
        return Decision("deny", "Memory needs a known active person's untainted request.")
    if ctx.run["personal_read"]:
        return Decision("deny", "This run read personal data. Ask again in a new conversation to forget a note.")
    return Decision("allow", "The person can forget team notes.")


async def note_forget(ctx, args):
    """Delete one team note. The record keeps its last text."""
    person = await actor(ctx)
    if not person or ctx.run["personal_read"]:
        raise Refusal("Memory needs a known active person's untainted request without personal reads.")
    await limit(ctx.conn, person)
    async with ctx.app.reload_lock:
        target = args["ref"][5:]
        path = file_path(ctx.app.workspace.root, "notes", target, create=False)
        if not path.exists():
            raise Refusal("ref: The note was not found.")
        before = path.read_text()
        front, _ = files.split(before)
        title = (front or {}).get("title") or target
        if person["role"] != "owner" and (front or {}).get("author") != person["key"]:
            raise Refusal("Only the author of the note or an owner can forget it.")
        committed = await persist(ctx.app, ctx.conn, "note", target, None, "memory: forget note " + title)
        row = await record(ctx.app, ctx.conn, kind="note", target=target, title=title,
                           before=before, after="", person=person, run_id=ctx.run["id"],
                           state="forgotten")
    return {"ref": "note:" + target, "change_id": row["id"], "committed": committed, "forgotten": True}
