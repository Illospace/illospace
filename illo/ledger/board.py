"""Store board posts, reader acknowledgements, and authenticated decisions."""

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Annotated, Literal

from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb
from pydantic import Field, TypeAdapter, ValidationError, field_validator

from illo.db import all_rows, one
from illo.ledger import asks, mcp
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
PRIVATE_TASKS = f"""SELECT 1 FROM tasks t WHERE t.visibility='private'
    AND (t.id=b.task_id OR t.id IN (
        SELECT tt.task_id FROM task_tickets tt WHERE b.ticket_url={_TICKET}
    ))"""


def visible_to(person_id="%(person_id)s"):
    """Build the thread ACL for a SQL expression that identifies the viewer."""
    return f"""(b.visibility='team' OR b.owner_person_id={person_id})
        AND NOT EXISTS ({PRIVATE_TASKS} AND t.owner_person_id IS DISTINCT FROM {person_id})"""


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
            JOIN tasks t ON t.id=tt.task_id WHERE {_TICKET}=%s FOR SHARE OF t""", (ticket_url,))
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
                        SELECT {_TICKET} FROM task_tickets tt WHERE tt.task_id=%(task_id)s
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
        (thread_id,actor,person_id,token_id,provenance,kind,text,blocks,mentions,
         reply_to_post_id,ask_id,source_ref,request_hash)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        RETURNING id,thread_id,ask_id,actor,is_human,kind,mentions,reply_to_post_id""",
        (thread["id"], reader.actor, reader.person_id, reader.token_id, Jsonb(provenance), kind,
         text, Jsonb(blocks), list(recipients), reply_to_post_id, ask["id"] if ask else None,
         source_ref, request_hash))
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


async def _recent(conn, workspace, reader, *, state, task_id=None, ticket_url=None, query=None, limit):
    rows = await all_rows(conn, f"""SELECT b.* FROM board_threads b WHERE {VISIBLE}
        AND (%(state)s::text IS NULL OR b.state=%(state)s)
        AND (%(task_id)s::bigint IS NULL OR b.task_id=%(task_id)s OR (
          b.task_id IS NULL AND b.ticket_url IN (
            SELECT {_TICKET} FROM task_tickets tt WHERE tt.task_id=%(task_id)s)))
        AND (%(ticket)s::text IS NULL OR b.ticket_url=%(ticket)s OR b.task_id IN (
          SELECT tt.task_id FROM task_tickets tt WHERE {_TICKET}=%(ticket)s))
        AND (%(query)s::text IS NULL OR b.tsv @@ websearch_to_tsquery('english',%(query)s))
        ORDER BY b.last_post_at DESC,b.id DESC LIMIT %(limit)s""",
        {**reader.params(), "state": state, "task_id": task_id, "ticket": ticket_url,
         "query": query or None, "limit": limit})
    return [_thread_result(row, workspace) for row in rows]


async def read(conn, *, workspace, token, thread_id=None, after_post_id=0, state="open",
               task_id=None, ticket_url=None, query=None, limit=50):
    reader = await _reader(conn, workspace, token)
    if thread_id is None:
        ticket_url = canonical_ticket(workspace, ticket_url) if ticket_url is not None else None
        return {"threads": await _recent(conn, workspace, reader, state=state, task_id=task_id,
                                         ticket_url=ticket_url, query=query, limit=limit)}
    params = {**reader.params(), "thread_id": thread_id, "after": after_post_id, "limit": limit}
    row = await one(conn, f"""SELECT b.*,ARRAY({_OPEN_DECISIONS} ORDER BY d.id) AS open_decision_ids,
        ARRAY(SELECT row_to_json(page) FROM (
            SELECT p.id,p.thread_id,p.at,p.actor,p.is_human,p.provenance,p.kind,p.text,
                p.blocks,p.mentions,p.reply_to_post_id,p.ask_id
            FROM board_posts p WHERE p.thread_id=b.id AND p.id>%(after)s
            ORDER BY p.id LIMIT %(limit)s
        ) page) AS posts
        FROM board_threads b WHERE b.id=%(thread_id)s AND {VISIBLE}""", params)
    if not row:
        raise ValueError(NOT_FOUND)
    return {"thread": _thread_result(row, workspace), "posts": row["posts"],
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
        return _thread_result(thread, workspace)
    await _check_limit(conn, reader)
    try:
        async with conn.transaction():
            row = await one(conn, """UPDATE board_threads SET state=%s,updated_at=clock_timestamp(),
                resolved_at=CASE WHEN %s='open' THEN NULL ELSE clock_timestamp() END,
                resolved_by=CASE WHEN %s='open' THEN NULL ELSE %s END,resolution_summary=%s
                WHERE id=%s RETURNING *""", (state, state, state, reader.actor, summary, thread_id))
    except UniqueViolation:
        raise ValueError("This task or ticket already has an open thread in this scope.") from None
    await _record_mutation(conn, reader)
    return _thread_result(row, workspace)


async def record_decision(conn, ask, *, workspace=None):
    """Append the answer while the authenticated ask service still holds its lock."""
    from illo.board.wake import enqueue_for_post

    if (ask["answered_via"] != "person" or ask["answered_by_person_id"] != ask["owner_person_id"]):
        return
    linked = await one(conn, """SELECT p.id,p.thread_id,owner.key FROM board_posts p
        JOIN people owner ON owner.id=%s AND owner.active
        WHERE p.ask_id=%s AND p.kind='decision_needed'""", (ask["owner_person_id"], ask["id"]))
    if not linked:
        return
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
    await conn.execute("""UPDATE board_threads SET last_post_at=clock_timestamp(),
        updated_at=clock_timestamp(),consecutive_agent_posts=0,stalled_reason=NULL,stalled_at=NULL
        WHERE id=%s""",
        (linked["thread_id"],))
