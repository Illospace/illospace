"""Keep confirmed team notes in files and pending proposals in the ledger."""

import os
import re
import tempfile
from datetime import UTC
from pathlib import Path

import yaml

from illo.db import all_rows, one
from illo.ledger import items, people, sensors
from illo.notes import gitstore
from illo.slackapi import dm_person
from illo.workspace import load_workspace


async def sync(ctx, conn=None) -> None:
    """The folder is the truth. Delete index rows when their note file is gone."""
    if conn is None:
        async with ctx.pool.connection() as connection:
            await sync(ctx, connection)
        return
    notes = load_workspace(ctx.workspace.root).notes
    refs = []
    for note in notes:
        ref = "note:" + Path(note.path).name
        refs.append(ref)
        author = await people.get(conn, note.author) if note.author else None
        await items.upsert(
            conn,
            source_ref=ref,
            kind="note",
            title=note.title,
            body=note.body,
            summary=note.about,
            visibility="team",
            owner_person_id=author["id"] if author else None,
            source_at=note.created,
            meta={"path": note.path, "about": note.about, "author": note.author},
        )
    await conn.execute(
        "DELETE FROM items WHERE kind='note' AND NOT (source_ref=ANY(%s::text[]))", (refs,)
    )


def card(row):
    text = "I noted: " + row["body"]
    blocks = [
        {"type": "section", "text": {"type": "plain_text", "text": text[:3000]}},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": label},
                    "action_id": f"note:{row['id']}:{action}",
                    "value": str(row["id"]),
                }
                for action, label in [
                    ("keep", "Keep"),
                    ("correct", "Correct…"),
                    ("remove", "Remove"),
                ]
            ],
        },
    ]
    return text, blocks


async def send_card(ctx, conn, row, owner_key):
    text, blocks = card(row)
    try:
        sent = await dm_person(ctx, owner_key, text, blocks)
    except Exception:
        await sensors.error(conn, "notes", "The note card could not be sent. Illo will try again.")
        return None
    if sent:
        await conn.execute(
            "UPDATE note_proposals SET slack_channel=%s,slack_ts=%s WHERE id=%s",
            (sent["channel"], sent["ts"], row["id"]),
        )
    return sent


async def propose(ctx, conn, run, title, text, about):
    """Only a person's untainted run can start a note proposal."""
    if run["tainted"] or not run["principal"].startswith("person:") or not run["person_id"]:
        raise ValueError("A note proposal needs an untainted person")
    person = await people.get(conn, run["principal"][7:])
    if not person or person["id"] != run["person_id"]:
        raise ValueError("The person is inactive")
    row = await one(
        conn,
        """
        INSERT INTO note_proposals(owner_person_id,run_id,title,body,about)
        VALUES (%s,%s,%s,%s,%s) RETURNING *
    """,
        (person["id"], run["id"], title, text, about),
    )
    sent = await send_card(ctx, conn, row, person["key"])
    return {
        "proposal_id": row["id"],
        "state": "pending",
        "card_sent": bool(sent),
        "message": "The note waits for Keep.",
    }


def _write(root, file_name, text):
    folder = root / "notes"
    if folder.is_symlink():
        raise ValueError("The notes folder must not be a link")
    folder.mkdir(exist_ok=True)
    path = folder / file_name
    if path.is_symlink() or path.resolve().parent != folder.resolve():
        raise ValueError("The note path is invalid")
    fd, name = tempfile.mkstemp(prefix=".note-", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    return path


async def keep(ctx, conn, row, person, text=None):
    body = row["body"] if text is None else text
    slug = re.sub(r"[^a-z0-9]+", "-", row["title"].lower()).strip("-")[:60] or "note"
    file_name = row["file_name"] or f"{slug}-{row['id']}.md"
    created = row["created_at"].astimezone(UTC).isoformat()
    front = yaml.safe_dump(
        {
            "title": row["title"],
            "about": row["about"],
            "author": person["key"],
            "created": created,
            "visibility": "team",
        },
        sort_keys=False,
    )
    path = _write(ctx.workspace.root, file_name, f"---\n{front}---\n\n{body}\n")
    await sync(ctx, conn)
    await conn.execute(
        """
        UPDATE note_proposals SET state='kept',body=%s,file_name=%s,kept_at=now() WHERE id=%s
    """,
        (body, file_name, row["id"]),
    )
    pushed = await gitstore.store(ctx, conn, path, row["title"])
    return "The note is saved." if pushed else 'The note is saved, but I could not push it to the workspace repository.'


async def _edit_card(ctx, row, text):
    if ctx.slack_client and row["slack_channel"] and row["slack_ts"]:
        try:
            await ctx.slack_client.chat_update(
                channel=row["slack_channel"], ts=row["slack_ts"], text=text, blocks=[]
            )
        except Exception:
            async with ctx.pool.connection() as conn:
                await sensors.error(conn, "notes", "The note card could not be updated.")


async def action(ctx, person, action):
    match = re.fullmatch(r"note:([1-9][0-9]*):(keep|correct|remove)", action.get("action_id", ""))
    if not match:
        return
    proposal_id, choice = match.groups()
    async with ctx.pool.connection() as conn:
        row = await one(
            conn, "SELECT * FROM note_proposals WHERE id=%s FOR UPDATE", (int(proposal_id),)
        )
        active = await people.get(conn, person["key"])
        if (
            not row
            or not active
            or active["id"] != row["owner_person_id"]
            or row["state"] == "kept"
            or not row["slack_channel"]
            or row["slack_channel"] != action.get("channel")
            or row["slack_ts"] != action.get("message_ts")
            or not row["slack_channel"].startswith("D")
        ):
            return
        if choice == "remove":
            await conn.execute("DELETE FROM note_proposals WHERE id=%s", (row["id"],))
            await _edit_card(ctx, row, "The note proposal is removed.")
        elif choice == "correct":
            if row["state"] == "correcting":
                return
            if not ctx.slack_client:
                return
            await ctx.slack_client.chat_postMessage(
                channel=row["slack_channel"],
                thread_ts=row["slack_ts"],
                text="Reply in this thread with the correct note text.",
            )
            await conn.execute(
                """UPDATE note_proposals SET state='correcting',correction_ts=slack_ts
                                  WHERE id=%s""",
                (row["id"],),
            )
        else:
            text = await keep(ctx, conn, row, active)
            await _edit_card(ctx, row, text)


async def claim(ctx, event, authority):
    """A correction must come from the proposal owner in its DM thread."""
    if (
        event.channel != "slack"
        or event.kind not in {"dm", "message.im"}
        or authority.tainted
        or not authority.principal.startswith("person:")
        or not event.text.strip()
    ):
        return None
    thread = event.payload.get("thread_ts") or event.reply_to
    channel = event.payload.get("channel_id")
    if not thread or not channel or not channel.startswith("D"):
        return None
    async with ctx.pool.connection() as conn:
        row = await one(
            conn,
            """
            SELECT * FROM note_proposals WHERE state='correcting' AND owner_person_id=%s
              AND slack_channel=%s AND correction_ts=%s FOR UPDATE
        """,
            (authority.person_id, channel, thread),
        )
        if not row:
            return None
        person = await people.get(conn, authority.principal[7:])
        if not person or person["id"] != row["owner_person_id"]:
            return None
        if len(event.text) > 20000:
            return "The note is too long. Use at most 20000 characters."
        text = await keep(ctx, conn, row, person, event.text)
        await _edit_card(ctx, row, text)
        return text


async def _perform_draft(ctx, draft):
    """Draft approval permits a proposal. Keep still confirms the team note."""
    async with ctx.pool.connection() as conn:
        row = await one(conn, "SELECT * FROM drafts WHERE id=%s FOR UPDATE", (draft["id"],))
        if not row or row["state"] != "approved" or row["kind"] != "notes_propose":
            return
        run = await one(conn, "SELECT * FROM runs WHERE id=%s", (row["run_id"],))
        if run["person_id"] != row["approver_person_id"]:
            return
        args = row["content"]["arguments"]
        existing = await one(
            conn,
            "SELECT id FROM note_proposals WHERE run_id=%s AND title=%s AND body=%s",
            (run["id"], args["title"], args["text"]),
        )
        if not existing:
            await propose(ctx, conn, run, **args)


async def setup(ctx) -> None:
    await sync(ctx)
    ctx.slack_actions.register("note:", action)
    ctx.gateway.register_claim(claim)
    ctx.draft_performers["notes_propose"] = _perform_draft

    async def refresh():
        await sync(ctx)
        # A failed or absent Slack connection leaves proposals ready for a later card.
        async with ctx.pool.connection() as conn:
            rows = await all_rows(
                conn,
                """
                SELECT n.*,p.key AS owner_key FROM note_proposals n JOIN people p
                  ON p.id=n.owner_person_id WHERE n.state='pending' AND n.slack_ts IS NULL AND p.active
                ORDER BY n.id FOR UPDATE OF n SKIP LOCKED
            """,
            )
            for row in rows:
                await send_card(ctx, conn, row, row["owner_key"])

    ctx.every(ctx.config.workspace_check_seconds, refresh)
