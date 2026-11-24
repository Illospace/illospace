"""Store board posts, reader acknowledgements, and authenticated decisions."""

import hashlib
import json
import re
import unicodedata
from dataclasses import asdict, dataclass
from typing import Annotated, Literal

from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb
from pydantic import Field, TypeAdapter, ValidationError, field_validator

from illo.db import all_rows, one
from illo.ledger import agent_oauth, asks, audit, mcp
from illo.tasks.access import token_viewer
from illo.tasks.pages.markdown import safe_url
from illo.tasks.pages.schema import Checks, TextPreview
from illo.tools.registry import Arguments

NOT_FOUND = "The board item was not found."
POST_KINDS = Literal["message", "finding", "question", "answer", "handoff", "decision_needed"]
STATES = Literal["open", "resolved", "archived"]
ISSUE = re.compile(r"https://github\.com/([a-zA-Z0-9-]+)/([a-zA-Z0-9_.-]+)/issues/([0-9]+)")
MUTATION = "board_mutation"

# Task links predate the board and can use variants of the same issue URL.
_TICKET = r"""regexp_replace(split_part(split_part(lower(tt.url), '#', 1), '?', 1),
    '/issues/0*([1-9][0-9]*)/?$', '/issues/\1')"""
TICKET_MATCH = f"COALESCE(tt.canonical_url, {_TICKET})"
PRIVATE_TASKS = f"""SELECT 1 FROM tasks t WHERE t.visibility='private'
    AND (t.id=b.task_id OR t.id IN (
        SELECT tt.task_id FROM task_tickets tt WHERE b.ticket_url={TICKET_MATCH}
    ))"""


def visible_to(person_id="%(person_id)s"):
    """Build the thread ACL for a SQL expression that identifies the viewer."""
    return f"""(b.visibility='team' OR b.owner_person_id={person_id})
        AND NOT EXISTS ({PRIVATE_TASKS} AND (t.owner_person_id IS NULL
            OR t.owner_person_id IS DISTINCT FROM {person_id}))"""


VISIBLE = visible_to()
_OPEN_DECISIONS = """SELECT d.id FROM board_posts d
    WHERE d.thread_id=b.id AND d.kind='decision_needed'
      AND NOT EXISTS (
          SELECT 1 FROM board_posts r
          WHERE r.thread_id=b.id AND r.reply_to_post_id=d.id AND r.kind='decision'
      )"""
_UNREAD = """FROM board_mentions m JOIN board_threads b ON b.id=m.thread_id
    WHERE m.recipient_person_id=%(person_id)s AND m.target=ANY(%(targets)s)
      AND NOT EXISTS (
        SELECT 1 FROM board_mention_acks a
        WHERE a.post_id=m.post_id AND a.target=m.target AND a.reader_key=%(reader_key)s
      ) AND """ + VISIBLE


class Link(Arguments):
    title: str = Field(default="", max_length=200)
    url: str = Field(max_length=2000)

    @field_validator("url")
    @classmethod
    def address(cls, value):
        if not safe_url(value):
            raise ValueError("Use a safe link address.")
        return value


class Links(Arguments):
    kind: Literal["links"]
    title: str = Field(default="", max_length=80)
    items: list[Link] = Field(min_length=1, max_length=12)


Block = Annotated[TextPreview | Checks | Links, Field(discriminator="kind")]
_BLOCKS = TypeAdapter(list[Block])


@dataclass(frozen=True)
class TurnCapReached:
    thread_id: int


@dataclass(frozen=True)
class PersonReader:
    person: dict

    def params(self):
        actor = "person:" + self.person["key"]
        return {"person_id": self.person["id"], "reader_key": actor, "targets": [actor]}


class BoardError(ValueError):
    """A board action could not be completed."""


@dataclass(frozen=True)
class Reader:
    token_id: int
    person_id: int
    person: str
    actor: str
    label: str
    agent: str | None

    def params(self):
        person = f"person:{self.person}"
        return {
            "person_id": self.person_id,
            "targets": [person, self.actor] if self.agent else [person],
            "reader_key": self.actor if self.agent else f"token:{self.token_id}",
        }


async def _reader(conn, workspace, token):
    if not workspace.config.board.enabled or token["scopes"] != ["agent"]:
        raise ValueError("This token cannot use the board.")
    current = await one(conn, """SELECT t.id,t.person_id,t.label,t.scopes,t.agent_key,p.key AS person_key
        FROM tokens t JOIN people p ON p.id=t.person_id
        WHERE t.id=%s AND t.person_id=%s AND t.revoked_at IS NULL AND p.active""",
        (token["id"], token["person_id"]))
    if not current or current["scopes"] != ["agent"]:
        raise ValueError("This token cannot use the board.")
    viewer = await token_viewer(conn, workspace, current)
    return Reader(current["id"], current["person_id"], viewer.person, viewer.actor,
                  current["label"], current["agent_key"])


async def _check_limit(conn, reader):
    row = await one(conn, """SELECT count(*) AS n FROM audit WHERE token_id=%s AND tool=%s
        AND at>clock_timestamp()-interval '1 hour'""", (reader.token_id, MUTATION))
    if row["n"] >= 60:
        raise ValueError("The token has reached its limit of 60 board changes per hour.")


async def _record_mutation(conn, reader):
    # The token lock and this audit row share the transaction with the change.
    await conn.execute("""INSERT INTO audit (token_id,principal,tool,args_digest,decision,reason,at)
        VALUES (%s,%s,%s,'unavailable','allow','The board changed.',clock_timestamp())""",
        (reader.token_id, f"person:{reader.person}", MUTATION))


def canonical_ticket(workspace, value):
    match = ISSUE.fullmatch(value)
    if not match:
        raise ValueError("Give a GitHub issue URL.")
    owner, repo, digits = match.groups()
    number = digits.lstrip("0")
    name = f"{owner}/{repo}".lower()
    if not number or repo in {".", ".."}:
        raise ValueError("Give a GitHub issue URL.")
    if not any(name == configured.lower() for configured in workspace.config.github.repositories):
        raise ValueError("The GitHub repository is not in the workspace.")
    return f"https://github.com/{name}/issues/{number}"


async def _task(conn, reader, task_id, ticket_url):
    mapped = []
    if ticket_url:
        mapped = await all_rows(conn, f"""SELECT t.* FROM task_tickets tt
            JOIN tasks t ON t.id=tt.task_id WHERE {TICKET_MATCH}=%s FOR SHARE OF t""", (ticket_url,))
        if any(t["visibility"] == "private" and t["owner_person_id"] != reader.person_id
               for t in mapped):
            raise ValueError(NOT_FOUND)
        ids = {t["id"] for t in mapped}
        if len(ids) > 1 or (task_id is not None and ids != {task_id}):
            raise ValueError("The ticket does not identify this task.")
    if mapped:
        return mapped[0]
    if task_id is not None:
        task = await one(conn, """SELECT * FROM tasks
            WHERE id=%s AND (visibility='team' OR owner_person_id=%s) FOR SHARE""",
            (task_id, reader.person_id))
        if not task:
            raise ValueError(NOT_FOUND)
        return task
    return None


async def _thread(conn, params, *, lock=False):
    suffix = " FOR UPDATE OF b" if lock else ""
    row = await one(conn, f"""SELECT b.*, EXISTS ({PRIVATE_TASKS}) AS private_task
        FROM board_threads b WHERE b.id=%(thread_id)s AND {VISIBLE}{suffix}""", params)
    if not row:
        raise ValueError(NOT_FOUND)
    return row


async def _open_thread(conn, reader, title, visibility, task_id, ticket_url):
    owner = reader.person_id if visibility == "private" else None
    args = {**reader.params(), "title": title, "actor": reader.actor, "visibility": visibility,
            "owner": owner, "task_id": task_id, "ticket": ticket_url}
    while True:
        if task_id is not None or ticket_url is not None:
            row = await one(conn, f"""SELECT b.*, ({VISIBLE}) AS visible,
                    EXISTS ({PRIVATE_TASKS}) AS private_task
                FROM board_threads b WHERE b.state='open' AND b.visibility=%(visibility)s
                  AND (b.visibility='team' OR b.owner_person_id=%(owner)s)
                  AND (b.task_id=%(task_id)s OR b.ticket_url=%(ticket)s OR (
                      b.task_id IS NULL AND b.ticket_url IN (
                        SELECT {TICKET_MATCH} FROM task_tickets tt WHERE tt.task_id=%(task_id)s
                      )))
                ORDER BY (b.task_id=%(task_id)s) DESC NULLS LAST,
                  (b.ticket_url=%(ticket)s) DESC NULLS LAST,b.id LIMIT 1 FOR UPDATE OF b""",
                args)
            if row:
                if not row["visible"]:
                    raise ValueError(NOT_FOUND)
                return row
        row = await one(conn, """INSERT INTO board_threads
            (title,visibility,owner_person_id,created_by,task_id,ticket_url)
            VALUES (%(title)s,%(visibility)s,%(owner)s,%(actor)s,%(task_id)s,%(ticket)s)
            ON CONFLICT DO NOTHING RETURNING *""", args)
        if row:
            return row
        # A concurrent insert took the canonical key. The next statement can see it.


async def _attach_links(conn, thread, task_id, ticket_url):
    if (thread["task_id"] is not None or task_id is None) and (
        thread["ticket_url"] is not None or ticket_url is None
    ):
        return
    try:
        async with conn.transaction():
            await conn.execute("""UPDATE board_threads SET task_id=COALESCE(task_id,%s),
                ticket_url=COALESCE(ticket_url,%s) WHERE id=%s""",
                (task_id, ticket_url, thread["id"]))
    except UniqueViolation:
        # A ticket can gain a task after both already have a thread.
        pass


async def _recipients(conn, workspace, reader, targets, *, private):
    owners = {"agent:" + a.key: a.owner for a in workspace.agents}
    recipients = {}
    for target in dict.fromkeys(targets):
        if target == reader.actor:
            continue
        key = target.removeprefix("person:") if target.startswith("person:") else owners.get(target)
        person = await one(conn, "SELECT id FROM people WHERE key=%s AND active", (key,))
        if not person or (private and person["id"] != reader.person_id):
            raise ValueError("A mention recipient was not found.")
        recipients[target] = person["id"]
    if len(recipients) > 10:
        raise ValueError("Use at most 10 mention recipients.")
    return recipients


def _clean_hint(value):
    return "".join(c for c in value if not unicodedata.category(c).startswith("C")).strip()[:80]


def _safe_blocks(blocks):
    try:
        clean = [block.model_dump() for block in _BLOCKS.validate_python(blocks)]
    except ValidationError:
        raise ValueError("Use only text preview, checks, and links blocks.") from None
    if len(json.dumps(clean).encode()) > 32 * 1024:
        raise ValueError("Board blocks must fit in 32 KB.")
    return clean


def _receipt(row):
    return {"thread_id": row["thread_id"], "post_id": row["id"], "ask_id": row["ask_id"]}


async def _replay(conn, reader, source_ref, request_hash):
    row = await one(conn, f"""SELECT p.id,p.thread_id,p.ask_id,p.request_hash,({VISIBLE}) AS visible
        FROM board_posts p JOIN board_threads b ON b.id=p.thread_id
        WHERE p.token_id=%(token_id)s AND p.source_ref=%(source_ref)s""",
        {**reader.params(), "token_id": reader.token_id, "source_ref": source_ref})
    if not row:
        return None
    if not row["visible"]:
        raise ValueError(NOT_FOUND)
    if row["request_hash"] != request_hash:
        raise ValueError("This source_ref belongs to a different request.")
    return _receipt(row)


async def post(conn, *, workspace, token, text, kind="message", thread_id=None, title=None,
               task_id=None, ticket_url=None, visibility=None, reply_to_post_id=None,
               source_ref=None, blocks=(), mentions=(), question=None, options=None,
               recommended=None, client_name="", model=""):
    """Append one agent post. The caller commits a turn cap outcome."""
    from illo.board.wake import enqueue_for_post

    reader = await _reader(conn, workspace, token)
    await mcp.lock_token(conn, reader.token_id, "board")
    request = {
        "text": text, "kind": kind, "thread_id": thread_id, "title": title,
        "task_id": task_id, "ticket_url": ticket_url, "visibility": visibility,
        "reply_to_post_id": reply_to_post_id, "blocks": blocks, "mentions": mentions,
        "question": question, "options": options, "recommended": recommended,
        "client_name": client_name, "model": model,
    }
    request_hash = None
    if source_ref is not None:
        request_hash = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
        replay = await _replay(conn, reader, source_ref, request_hash)
        if replay:
            return replay
    await _check_limit(conn, reader)

    if kind == "decision":
        raise ValueError("A person must answer the linked ask.")
    blocks = _safe_blocks(blocks)
    if thread_id is not None:
        thread = await _thread(conn, {**reader.params(), "thread_id": thread_id}, lock=True)
        if task_id is not None or ticket_url is not None or title is not None:
            raise ValueError("Use thread_id by itself to select an existing thread.")
    else:
        ticket_url = canonical_ticket(workspace, ticket_url) if ticket_url is not None else None
        task = await _task(conn, reader, task_id, ticket_url)
        if task and task["visibility"] == "private":
            visibility = "private"
        if not (title or task or ticket_url):
            raise ValueError("Give a title, task ID, or ticket URL for the thread.")
        task_id = task["id"] if task else None
        thread = await _open_thread(conn, reader, title or (task["title"] if task else ticket_url)[:200],
                                    visibility or "team", task_id, ticket_url)
    private = thread["visibility"] == "private" or thread.get("private_task", False)
    if visibility == "private" and not private:
        raise ValueError("Use a private thread for a private post.")
    if thread["state"] != "open":
        raise ValueError("The thread is closed.")
    if reply_to_post_id is not None:
        await _require_posts(conn, reader, [reply_to_post_id], thread["id"])
    if thread["consecutive_agent_posts"] >= workspace.config.board.max_consecutive_agent_posts:
        if thread["stalled_reason"] is None:
            await conn.execute("""UPDATE board_threads SET stalled_reason='turn_cap',
                stalled_at=statement_timestamp(),updated_at=statement_timestamp()
                WHERE id=%s""", (thread["id"],))
            await _record_mutation(conn, reader)
        return TurnCapReached(thread["id"])

    if thread_id is None:
        await _attach_links(conn, thread, task_id, ticket_url)
    recipients = await _recipients(conn, workspace, reader, mentions, private=private)
    ask = None
    if kind == "decision_needed":
        people = [person for target, person in recipients.items() if target.startswith("person:")]
        if len(people) != 1 or not question or not 1 <= len(question) <= 240:
            raise ValueError("Give one person mention and a question of up to 240 characters.")
        if not options or not 2 <= len(options) <= 5 or not recommended:
            raise ValueError("Give two to five options and a recommended option ID.")
        ask = await asks.create(conn, owner_person_id=people[0], question=question, options=options,
                                recommended_option=recommended, requester_token_id=reader.token_id,
                                requester_person_id=reader.person_id, allow_rule=False)
    provenance = {"agent_key": reader.agent, "person_key": reader.person, "token_label": reader.label}
    provenance.update({key: clean for key, value in (("client_name", client_name), ("model", model))
                       if (clean := _clean_hint(value))})
    row = await one(conn, """INSERT INTO board_posts
        (thread_id,actor,at,person_id,token_id,provenance,kind,text,blocks,mentions,
         reply_to_post_id,ask_id,source_ref,request_hash)
        VALUES (%s,%s,clock_timestamp(),%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        RETURNING id,thread_id,ask_id,actor,is_human,kind,mentions,reply_to_post_id""",
        (thread["id"], reader.actor, reader.person_id, reader.token_id, Jsonb(provenance), kind,
         text, Jsonb(blocks), list(recipients), reply_to_post_id, ask["id"] if ask else None,
         source_ref, request_hash))
    if ask:
        await conn.execute("UPDATE asks SET source_ref=%s WHERE id=%s AND source_ref IS NULL",
                           (f"board:{thread['id']}:{row['id']}:decision", ask["id"]))
    if recipients:
        await conn.execute("""INSERT INTO board_mentions (thread_id,post_id,target,recipient_person_id)
            SELECT %s,%s,target,person_id FROM unnest(%s::text[],%s::bigint[]) AS m(target,person_id)""",
            (thread["id"], row["id"], list(recipients), list(recipients.values())))
    if kind == "answer" and reply_to_post_id is not None:
        await _ack(conn, reader, "m.post_id=%(reply)s", {"reply": reply_to_post_id})
    await conn.execute("""UPDATE board_threads SET last_post_at=clock_timestamp(),
        updated_at=clock_timestamp(),consecutive_agent_posts=consecutive_agent_posts+1
        WHERE id=%s""", (thread["id"],))
    await enqueue_for_post(conn, workspace, thread=thread, post=row)
    await _record_mutation(conn, reader)
    return _receipt(row)


def _thread_result(row, workspace):
    return {key: row[key] for key in (
        "id", "title", "state", "visibility", "task_id", "ticket_url", "created_by", "created_at",
        "updated_at", "last_post_at", "resolved_at", "resolved_by", "resolution_summary", "stalled_reason"
    )} | {"turn_budget_remaining": max(0, workspace.config.board.max_consecutive_agent_posts
                                       - row["consecutive_agent_posts"])}


async def _recent(conn, workspace, reader, *, state, task_id=None, ticket_url=None, query=None, group_key=None, limit):
    rows = await all_rows(conn, f"""SELECT b.* FROM board_threads b WHERE {VISIBLE}
        AND (%(state)s::text IS NULL OR b.state=%(state)s)
        AND (%(task_id)s::bigint IS NULL OR b.task_id=%(task_id)s OR (
          b.task_id IS NULL AND b.ticket_url IN (
            SELECT {TICKET_MATCH} FROM task_tickets tt WHERE tt.task_id=%(task_id)s)))
        AND (%(ticket)s::text IS NULL OR b.ticket_url=%(ticket)s OR b.task_id IN (
          SELECT tt.task_id FROM task_tickets tt WHERE {TICKET_MATCH}=%(ticket)s))
        AND (%(group)s::text IS NULL OR b.group_key=%(group)s)
        AND (%(query)s::text IS NULL OR b.tsv @@ websearch_to_tsquery('english',%(query)s))
        ORDER BY b.last_post_at DESC,b.id DESC LIMIT %(limit)s""",
        {**reader.params(), "state": state, "task_id": task_id, "ticket": ticket_url,
         "query": query or None, "group": group_key, "limit": limit})
    return [await _thread_result_with_episode(conn, row, workspace) for row in rows]


async def read(conn, *, workspace, token, thread_id=None, after_post_id=0, state="open",
               task_id=None, ticket_url=None, query=None, group_key=None, limit=50):
    reader = await _reader(conn, workspace, token)
    if thread_id is None:
        ticket_url = canonical_ticket(workspace, ticket_url) if ticket_url is not None else None
        return {"threads": await _recent(conn, workspace, reader, state=state, task_id=task_id,
                                         ticket_url=ticket_url, query=query, group_key=group_key, limit=limit)}
    params = {**reader.params(), "thread_id": thread_id, "after": after_post_id, "limit": limit}
    row = await one(conn, f"""SELECT b.*,ARRAY({_OPEN_DECISIONS} ORDER BY d.id) AS open_decision_ids,
        ARRAY(SELECT row_to_json(page) FROM (
            SELECT p.id,p.thread_id,p.at,p.actor,p.is_human,p.provenance,p.kind,p.text,
                p.blocks,p.mentions,p.reply_to_post_id,p.ask_id
            FROM board_posts p WHERE p.thread_id=b.id AND p.id>%(after)s
            ORDER BY p.id LIMIT %(limit)s
        ) page) AS posts
        FROM board_threads b WHERE b.id=%(thread_id)s AND {VISIBLE}""", params)
    if not row or (group_key is not None and row["group_key"] != group_key):
        raise ValueError(NOT_FOUND)
    from illo.board.labels import actor_label

    people = {p["key"]: p for p in await all_rows(conn, "SELECT key,name FROM people")}
    for post in row["posts"]:
        post["actor_label"] = asdict(actor_label(workspace, people, post))
    return {"thread": await _thread_result_with_episode(conn, row, workspace), "posts": row["posts"],
            "open_decision_ids": row["open_decision_ids"]}


async def inbox(conn, *, workspace, token, include_recent_open=False, limit=30):
    reader = await _reader(conn, workspace, token)
    rows = await all_rows(conn, """SELECT m.thread_id,m.post_id,m.target,m.created_at,b.title """
                          + _UNREAD + " ORDER BY m.post_id DESC,m.target LIMIT %(limit)s",
                          {**reader.params(), "limit": limit})
    result = {"mentions": rows}
    if include_recent_open:
        result["recent_open"] = await _recent(conn, workspace, reader, state="open", limit=10)
    return result


async def unread_count(conn, *, workspace, token):
    reader = await _reader(conn, workspace, token)
    row = await one(conn, "SELECT count(*) AS n " + _UNREAD, reader.params())
    return row["n"]


async def _require_posts(conn, reader, post_ids, thread_id=None):
    row = await one(conn, f"""SELECT count(*) AS n FROM board_posts p
        JOIN board_threads b ON b.id=p.thread_id
        WHERE p.id=ANY(%(post_ids)s) AND (%(thread_id)s::bigint IS NULL OR b.id=%(thread_id)s)
          AND {VISIBLE}""",
        {**reader.params(), "post_ids": post_ids, "thread_id": thread_id})
    if row["n"] != len(set(post_ids)):
        raise ValueError(NOT_FOUND)


async def _ack(conn, reader, where, params):
    cursor = await conn.execute("""INSERT INTO board_mention_acks (post_id,target,reader_key)
        SELECT m.post_id,m.target,%(reader_key)s """ + _UNREAD + " AND " + where
        + " ON CONFLICT DO NOTHING", {**reader.params(), **params})
    return cursor.rowcount


async def ack(conn, *, workspace, token, post_ids=(), thread_id=None, up_to_post_id=None):
    reader = await _reader(conn, workspace, token)
    await mcp.lock_token(conn, reader.token_id, "board")
    if post_ids and thread_id is None and up_to_post_id is None and len(post_ids) <= 50:
        await _require_posts(conn, reader, list(post_ids))
        changed = await _ack(conn, reader, "m.post_id=ANY(%(ids)s)", {"ids": list(post_ids)})
    elif not post_ids and thread_id is not None and up_to_post_id is not None:
        await _require_posts(conn, reader, [up_to_post_id], thread_id)
        changed = await _ack(conn, reader, "m.thread_id=%(thread)s AND m.post_id<=%(up_to)s",
                             {"thread": thread_id, "up_to": up_to_post_id})
    else:
        raise ValueError("Give 1 to 50 post IDs, or a thread ID and an upper post ID.")
    if changed:
        await _check_limit(conn, reader)
        await _record_mutation(conn, reader)
    return {"acked": changed}


async def resolve(conn, *, workspace, token, thread_id, state="resolved", summary=""):
    reader = await _reader(conn, workspace, token)
    await mcp.lock_token(conn, reader.token_id, "board")
    params = {**reader.params(), "thread_id": thread_id}
    thread = await _thread(conn, params, lock=True)
    if thread["state"] == "archived":
        raise ValueError("The thread is archived.")
    if state != "open" and await one(conn, f"""SELECT 1 FROM board_threads b
        WHERE b.id=%(thread_id)s AND {VISIBLE} AND EXISTS ({_OPEN_DECISIONS})""", params):
        raise ValueError("A person must answer each open decision before you close this thread.")
    if thread["state"] == state and thread["resolution_summary"] == summary:
        return await _thread_result_with_episode(conn, thread, workspace)
    await _check_limit(conn, reader)
    from illo.board.episodes import close_items

    if state == "open" and thread["state"] != "open":
        await close_items(conn, thread, reason="thread_closed")
    try:
        async with conn.transaction():
            row = await one(conn, """UPDATE board_threads SET state=%s,updated_at=clock_timestamp(),
                resolved_at=CASE WHEN %s='open' THEN NULL ELSE clock_timestamp() END,
                resolved_by=CASE WHEN %s='open' THEN NULL ELSE %s END,resolution_summary=%s,
                closed_at=CASE WHEN %s='open' THEN NULL ELSE clock_timestamp() END,
                reopened_at=CASE WHEN %s='open' AND state<>'open' THEN clock_timestamp() ELSE reopened_at END
                WHERE id=%s RETURNING *""", (state, state, state, reader.actor, summary, state, state, thread_id))
    except UniqueViolation:
        raise ValueError("This task or ticket already has an open thread in this scope.") from None
    if state != "open":
        await close_items(conn, row, reason="thread_closed")
    await _record_mutation(conn, reader)
    return await _thread_result_with_episode(conn, row, workspace)


async def record_decision(conn, ask, *, workspace=None) -> int | None:
    """Append the answer while the authenticated ask service still holds its lock."""
    from illo.board.episodes import close_items
    from illo.board.wake import enqueue_for_post

    if (ask["answered_via"] != "person" or ask["answered_by_person_id"] != ask["owner_person_id"]):
        return None
    linked = await one(conn, """SELECT p.id,p.thread_id,owner.key FROM board_posts p
        JOIN people owner ON owner.id=%s AND owner.active
        WHERE p.ask_id=%s AND p.kind='decision_needed'""", (ask["owner_person_id"], ask["id"]))
    if not linked:
        return None
    params = {"person_id": ask["owner_person_id"], "thread_id": linked["thread_id"]}
    thread = await _thread(conn, params, lock=True)
    option = next((o["label"] for o in ask["options"] if o["id"] == ask["answer_option"]), "")
    answer = "\n".join(value for value in (option, ask["answer_text"]) if value)[:16000]
    post = await one(conn, """INSERT INTO board_posts
        (thread_id,actor,is_human,person_id,provenance,kind,text,reply_to_post_id)
        VALUES (%s,%s,true,%s,%s,'decision',%s,%s)
        RETURNING id,actor,is_human,kind,mentions,reply_to_post_id""",
        (linked["thread_id"], f"person:{linked['key']}", ask["owner_person_id"],
         Jsonb({"person_key": linked["key"]}), answer, linked["id"]))
    if workspace is not None:
        stall_at = None
        if thread["stalled_reason"] == "turn_cap":
            stall_at = thread.get("stalled_at") or thread["updated_at"]
        await enqueue_for_post(conn, workspace, thread=thread, post=post,
                               prior_pending_post_id=linked["id"], prior_turn_cap_at=stall_at)
    person = {"id": ask["owner_person_id"], "key": linked["key"]}
    await _reset_after_human(conn, linked["thread_id"], person, post["id"])
    await close_items(conn, thread, reason="answered", person_id=person["id"], post_id=post["id"])
    return post["id"]


def canonical_task_ticket(workspace, value):
    return canonical_ticket(workspace, value.split("#", 1)[0].rstrip("/").lower())


async def backfill_tickets(conn, workspace):
    rows = await all_rows(conn, "SELECT url FROM task_tickets WHERE canonical_url IS NULL")
    for row in rows:
        try:
            canonical = canonical_task_ticket(workspace, row["url"])
        except ValueError:
            continue
        await conn.execute("UPDATE task_tickets SET canonical_url=%s WHERE url=%s AND canonical_url IS NULL",
                           (canonical, row["url"]))


async def lock_thread(conn, thread_id):
    return await one(conn, "SELECT * FROM board_threads WHERE id=%s FOR UPDATE", (thread_id,))


async def lock_children(conn, thread_id):
    await conn.execute("SELECT id FROM board_episodes WHERE thread_id=%s ORDER BY id FOR UPDATE", (thread_id,))
    await conn.execute("SELECT id FROM board_escalations WHERE thread_id=%s ORDER BY id FOR UPDATE", (thread_id,))


def is_coordinator_ask(ask):
    ref = ask.get("source_ref") or ""
    return ref.startswith("board:") and ":esc:" in ref


async def _ask_thread_ids(conn, ask_id):
    rows = await all_rows(conn, """SELECT thread_id FROM board_posts WHERE ask_id=%s
        UNION SELECT thread_id FROM board_escalations WHERE ask_id=%s""", (ask_id, ask_id))
    linked = {row["thread_id"] for row in rows}
    row = await one(conn, "SELECT source_ref FROM asks WHERE id=%s", (ask_id,))
    ref = ((row or {}).get("source_ref") or "").split(":")
    if len(ref) >= 3 and ref[0] == "board" and re.fullmatch(r"[0-9]{1,19}", ref[1]):
        thread_id = int(ref[1])
        if 0 < thread_id < 2**63:
            linked.add(thread_id)
    return sorted(linked)


async def ask_thread_id(conn, ask_id):
    linked = await _ask_thread_ids(conn, ask_id)
    return linked[0] if len(linked) == 1 else None


class _AskLinkChanged(RuntimeError):
    """Retry the lock order when a link changes before the ask is locked."""


async def lock_for_ask(conn, ask_id):
    from illo.ledger.ask_workflow import lock_ask

    thread_ids = await _ask_thread_ids(conn, ask_id)
    for _ in range(2):
        try:
            async with conn.transaction():
                if thread_ids:
                    await conn.execute("""SELECT id FROM board_threads WHERE id=ANY(%s)
                        ORDER BY id FOR UPDATE""", (thread_ids,))
                    await conn.execute("""SELECT id FROM board_episodes WHERE thread_id=ANY(%s)
                        ORDER BY id FOR UPDATE""", (thread_ids,))
                    await conn.execute("""SELECT id FROM board_escalations WHERE thread_id=ANY(%s)
                        ORDER BY id FOR UPDATE""", (thread_ids,))
                row = await lock_ask(conn, ask_id)
                if thread_ids != await _ask_thread_ids(conn, ask_id):
                    raise _AskLinkChanged
                return row if len(thread_ids) <= 1 else None
        except _AskLinkChanged:
            thread_ids = await _ask_thread_ids(conn, ask_id)
    return None


async def _human_thread(conn, person, thread_id):
    try:
        return await _thread(conn, {"person_id": person["id"], "thread_id": thread_id})
    except ValueError:
        raise BoardError("The thread was not found.") from None


async def _human_replay(conn, actor, source_ref, thread_id):
    row = await one(conn, """SELECT id,thread_id,kind FROM board_posts WHERE
        actor=%s AND source_ref=%s AND token_id IS NULL""", (actor, source_ref))
    if row and row["thread_id"] != thread_id:
        raise BoardError("This reply key belongs to another thread.")
    return row


async def _human_receipt(conn, row, person, *, replayed):
    args = {**PersonReader(person).params(), "thread_id": row["thread_id"]}
    waiting = await one(conn, """SELECT 1 """ + _UNREAD + """
        AND m.thread_id=%(thread_id)s AND EXISTS (
            SELECT 1 FROM board_posts p WHERE p.id=m.post_id AND NOT p.is_human AND p.actor<>'illo'
              AND p.kind<>'decision_needed')
        UNION ALL SELECT 1 FROM board_escalations WHERE thread_id=%(thread_id)s
          AND person_id=%(person_id)s AND state='sent' AND format<>'card' LIMIT 1""", args)
    return {"post_id": row["id"], "thread_id": row["thread_id"], "kind": row["kind"],
            "replayed": replayed, "still_waits_for_viewer": bool(waiting)}


async def _human_limit(conn, person):
    if not await agent_oauth.limit(conn, "board_human", str(person["id"]), 30):
        raise BoardError("You have reached the limit of 30 board changes per hour.")


async def _reset_after_human(conn, thread_id, person, post_id):
    await conn.execute("""UPDATE board_threads SET consecutive_agent_posts=0,stalled_reason=NULL,
        stalled_at=NULL,last_post_at=clock_timestamp(),updated_at=clock_timestamp() WHERE id=%s""",
        (thread_id,))
    await _ack(conn, PersonReader(person), "m.thread_id=%(thread)s AND m.post_id<=%(post)s",
               {"thread": thread_id, "post": post_id})


async def human_post(conn, *, workspace, person, thread_id, text, reply_to_post_id, source_ref, via,
                     ask_id=None):
    """Write the verified person's own words in the caller's transaction."""
    from illo.board.episodes import close_items
    from illo.board.wake import enqueue_for_post
    from illo.channels.slack.tasks import _visible

    if via not in {"slack_thread", "slack_card", "web"} or not source_ref:
        raise ValueError("Use a verified reply source.")
    if not person or not person.get("active", True):
        raise BoardError("Your account is inactive.")
    actor = "person:" + person["key"]
    await _human_thread(conn, person, thread_id)
    if row := await _human_replay(conn, actor, source_ref, thread_id):
        return await _human_receipt(conn, row, person, replayed=True)
    await lock_thread(conn, thread_id)
    thread = await _human_thread(conn, person, thread_id)
    if row := await _human_replay(conn, actor, source_ref, thread_id):
        return await _human_receipt(conn, row, person, replayed=True)
    if thread["state"] == "archived":
        raise BoardError("The thread is archived. It is read-only.")
    if thread["state"] == "resolved":
        raise BoardError("The thread is resolved. Reopen it to reply.")
    prior = await one(conn, """SELECT p.id FROM board_posts p WHERE p.thread_id=%s
        AND NOT p.is_human AND p.actor LIKE 'agent:%%' AND p.kind IN ('question','decision_needed')
        AND NOT EXISTS (SELECT 1 FROM board_posts r WHERE r.thread_id=p.thread_id AND (
            (r.reply_to_post_id=p.id AND r.kind=CASE p.kind WHEN 'question' THEN 'answer' ELSE 'decision' END)
            OR (p.kind='question' AND r.is_human AND r.id>p.id)))
        ORDER BY (p.id=%s) DESC NULLS LAST,p.id DESC LIMIT 1""", (thread_id, reply_to_post_id))
    stall_at = (thread["stalled_at"] or thread["updated_at"]) if thread["stalled_reason"] == "turn_cap" else None
    text = _visible(text).strip()
    if not 1 <= len(text) <= 4000 or "\x00" in text:
        raise BoardError("Give a reply of 1 to 4,000 characters.")
    reply = None
    if reply_to_post_id is not None:
        reply = await one(conn, "SELECT * FROM board_posts WHERE thread_id=%s AND id=%s",
                          (thread_id, reply_to_post_id))
        if not reply:
            raise BoardError("The board post was not found.")
        if reply["kind"] == "decision_needed":
            raise BoardError("Answer this decision with its buttons.")
    kind = "answer" if reply and reply["kind"] == "question" else "message"
    target = None
    if reply and reply["actor"].startswith("agent:") and reply["person_id"]:
        provenance = reply["provenance"]
        if provenance.get("agent_key"):
            target = "agent:" + provenance["agent_key"]
        elif provenance.get("person_key"):
            target = "person:" + provenance["person_key"]
    await lock_children(conn, thread_id)
    try:
        async with conn.transaction():
            await _human_limit(conn, person)
            row = await one(conn, """INSERT INTO board_posts
                (thread_id,actor,at,is_human,person_id,kind,text,reply_to_post_id,source_ref,provenance,mentions)
                VALUES (%s,%s,clock_timestamp(),true,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                (thread_id, actor, person["id"], kind, text, reply_to_post_id, source_ref,
                 Jsonb({"via": via}), [target] if target else []))
    except UniqueViolation as exc:
        if exc.diag.constraint_name != "board_posts_untokened_source":
            raise
        row = await _human_replay(conn, actor, source_ref, thread_id)
        return await _human_receipt(conn, row, person, replayed=True)
    if target:
        await conn.execute("""INSERT INTO board_mentions (thread_id,post_id,target,recipient_person_id)
            VALUES (%s,%s,%s,%s)""", (thread_id, row["id"], target, reply["person_id"]))
    await _reset_after_human(conn, thread_id, person, row["id"])
    await close_items(conn, thread, reason="answered", person_id=person["id"], post_id=row["id"])
    await enqueue_for_post(conn, workspace, thread=thread, post=row, prior_turn_cap_at=stall_at,
                           prior_pending_post_id=prior["id"] if prior else None)
    await audit.write(conn, principal=actor, tool="board_human_post", args={"thread": thread_id, "via": via},
                      decision="allow", reason="The person replies on the board.", target=f"board thread {thread_id}")
    return await _human_receipt(conn, row, person, replayed=False)


async def human_resolve(conn, *, workspace, person, thread_id, state="open"):
    """Reopen a board thread on behalf of a signed-in person."""
    if state != "open":
        raise ValueError("A person can only reopen a thread here.")
    await lock_thread(conn, thread_id)
    thread = await _human_thread(conn, person, thread_id)
    if thread["state"] == "archived":
        raise BoardError("The thread is archived. It is read-only.")
    if thread["state"] == "open":
        return await _thread_result_with_episode(conn, thread, workspace)
    from illo.board.episodes import close_items

    try:
        async with conn.transaction():
            await close_items(conn, thread, reason="thread_closed")
            await _human_limit(conn, person)
            thread = await one(conn, """UPDATE board_threads SET state='open',closed_at=NULL,
                resolved_at=NULL,resolved_by=NULL,updated_at=clock_timestamp(),reopened_at=clock_timestamp()
                WHERE id=%s RETURNING *""", (thread_id,))
    except UniqueViolation:
        raise BoardError("This task or ticket already has an open thread in this scope.") from None
    await audit.write(conn, principal="person:" + person["key"], tool="board_human_resolve",
                      args={"thread": thread_id, "state": state}, decision="allow",
                      reason="The person reopens the board thread.", target=f"board thread {thread_id}")
    return await _thread_result_with_episode(conn, thread, workspace)


async def system_post(conn, thread_id, text, *, purpose, source_ref, episode_id,
                      mentions=(), workspace=None, reply_to_post_id=None):
    from illo.board.wake import enqueue_for_post

    thread = await lock_thread(conn, thread_id)
    row = await one(conn, """INSERT INTO board_posts
        (thread_id,actor,kind,text,provenance,source_ref,mentions,reply_to_post_id)
        VALUES (%s,'illo','message',%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING *""",
        (thread_id, text, Jsonb({"via": "coordinator", "purpose": purpose, "episode_id": episode_id}),
         source_ref, list(dict(mentions)), reply_to_post_id))
    if not row:
        return None
    for target, person_id in mentions:
        await conn.execute("""INSERT INTO board_mentions (thread_id,post_id,target,recipient_person_id)
            VALUES (%s,%s,%s,%s)""", (thread_id, row["id"], target, person_id))
    await conn.execute("UPDATE board_threads SET last_post_at=clock_timestamp() WHERE id=%s", (thread_id,))
    if purpose == "headstart" and workspace:
        await enqueue_for_post(conn, workspace, thread=thread, post=row)
    return row


async def _thread_result_with_episode(conn, row, workspace):
    from illo.board.episodes import ACTIVE

    episode = await one(conn, """SELECT state,headstart_until,headstart_target
        FROM board_episodes WHERE thread_id=%s AND state=ANY(%s) ORDER BY id DESC LIMIT 1""",
        (row["id"], list(ACTIVE)))
    return _thread_result(row, workspace) | {
        "group_key": row["group_key"],
        "coordinator": (episode | {"escalated_at": row["escalated_at"]}) if episode else None,
    }
