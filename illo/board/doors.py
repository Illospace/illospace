"""Route non-board agent doors into the single Chief-of-Staff board pipeline."""

import json

from psycopg.types.json import Jsonb

from illo.db import one
from illo.ledger import board
from illo.ledger import tasks as task_ledger


def coordinator_active(workspace) -> bool:
    cfg = workspace.config.board
    return bool(cfg.enabled and cfg.coordinator.enabled)


def _format_proof_value(value, *, is_links: bool = False) -> str:
    if value is None or value == "" or value == [] or value == {}:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        lines = []
        for item in value[:20]:
            if isinstance(item, dict):
                url = item.get("url") or item.get("href")
                title = "" if is_links else (item.get("title") or item.get("label") or "")
                if url:
                    lines.append(f"- {title}: {url}" if title else f"- {url}")
                else:
                    lines.append("- " + json.dumps(item, ensure_ascii=True, separators=(",", ":")))
            else:
                lines.append(f"- {item}")
        return "\n".join(lines)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=True, separators=(",", ":"))
    return str(value)


def proof_text(question: str, proof: dict | None) -> str:
    """Include agent proof fields in the board post so Chief of Staff has full context."""
    parts = [question.strip()]
    if proof:
        for key, value in proof.items():
            if key == "area":
                continue
            rendered = _format_proof_value(value, is_links=(key == "links"))
            if not rendered:
                continue
            if key in {"text", "body", "summary"}:
                parts.append(rendered)
            else:
                parts.append(f"{key}: {rendered}")
    return "\n\n".join(part for part in parts if part)[:12000]


def _task_ticket_url(workspace, task: dict) -> str | None:
    for link in task.get("links") or []:
        if link.get("kind") == "ticket" and link.get("url"):
            try:
                return board.canonical_task_ticket(workspace, link["url"])
            except ValueError:
                continue
    return None


async def _task_for_lane(conn, workspace, lane: dict | None, *, requester_person_id: int | None, owner_person_id: int):
    if not lane:
        return None
    urls = []
    for link in lane.get("links") or []:
        raw = link if isinstance(link, str) else (link.get("url") if isinstance(link, dict) else None)
        if not isinstance(raw, str):
            continue
        try:
            urls.append(board.canonical_task_ticket(workspace, raw))
        except ValueError:
            continue
    if not urls:
        return None
    task = await one(
        conn,
        f"""SELECT t.* FROM task_tickets tt JOIN tasks t ON t.id=tt.task_id
        WHERE {board.TICKET_MATCH}=ANY(%s) AND t.state NOT IN ('done','cancelled')
        ORDER BY t.id LIMIT 1""",
        (urls,),
    )
    if not task:
        return None
    if task["visibility"] == "private" and not (
        task["owner_person_id"] == owner_person_id
        and (requester_person_id is None or task["owner_person_id"] == requester_person_id)
    ):
        return None
    return task


async def _open_task_thread(conn, workspace, task: dict, *, actor: str) -> dict:
    visibility = "private" if task["visibility"] == "private" else "team"
    owner = task["owner_person_id"] if visibility == "private" else None
    ticket = _task_ticket_url(workspace, task)
    while True:
        existing = await one(
            conn,
            f"""SELECT * FROM board_threads b
            WHERE b.state='open' AND b.visibility=%s
              AND (b.visibility='team' OR b.owner_person_id=%s)
              AND (b.task_id=%s OR (%s::text IS NOT NULL AND b.ticket_url=%s) OR (
                  b.task_id IS NULL AND b.ticket_url IN (
                      SELECT {board.TICKET_MATCH} FROM task_tickets tt WHERE tt.task_id=%s
                  )))
            ORDER BY (b.task_id=%s) DESC NULLS LAST, b.id LIMIT 1 FOR UPDATE OF b""",
            (visibility, owner, task["id"], ticket, ticket, task["id"], task["id"]),
        )
        if existing:
            await board._attach_links(conn, existing, task["id"], ticket)
            return existing
        created = await one(
            conn,
            """INSERT INTO board_threads (title,visibility,owner_person_id,created_by,task_id,ticket_url)
            VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING *""",
            (task["title"][:200], visibility, owner, actor, task["id"], ticket),
        )
        if created:
            return created


async def attach_ask_to_board(
    conn, workspace, ask: dict, *, token=None, lane=None, owner=None, via: str | None = None
) -> dict | None:
    """Mirror an agent ask onto a board thread so Chief of Staff triages and delivers it."""
    if not coordinator_active(workspace):
        return None
    owner = owner or await one(conn, "SELECT * FROM people WHERE id=%s AND active", (ask["owner_person_id"],))
    if not owner:
        return None
    requester_person_id = ask.get("requester_person_id") or (token["person_id"] if token else None) or owner["id"]
    requester = await one(conn, "SELECT key FROM people WHERE id=%s", (requester_person_id,))
    requester_key = requester["key"] if requester else owner["key"]
    agent_key = token.get("agent_key") if token else None
    token_id = token["id"] if token else ask.get("requester_token_id")
    if agent_key:
        actor = f"agent:{agent_key}"
    elif token_id:
        actor = f"agent:unnamed:{token_id}"
    else:
        actor = f"agent:request:{ask['id']}"
    provenance = {
        "agent_key": agent_key,
        "person_key": requester_key,
        "token_label": token["label"] if token else "",
        "via": via or ("ask_create" if token else "asks_create"),
    }
    task = await _task_for_lane(
        conn, workspace, lane, requester_person_id=requester_person_id, owner_person_id=owner["id"]
    )
    if task:
        thread = await _open_task_thread(conn, workspace, task, actor=actor)
    else:
        visibility = "team" if (lane and lane.get("visibility") == "team") else "private"
        thread_owner = requester_person_id if visibility == "private" else None
        title = ((lane.get("title") if lane else None) or ask["question"])[:200]
        thread = await one(
            conn,
            """INSERT INTO board_threads (title,visibility,owner_person_id,created_by)
            VALUES (%s,%s,%s,%s) RETURNING *""",
            (title, visibility, thread_owner, actor),
        )
        if visibility == "private" and owner["id"] != requester_person_id:
            await board.add_guest(conn, thread["id"], owner["id"])

    target = f"person:{owner['key']}"
    decision_spec = {
        "question": ask["question"],
        "options": ask["options"],
        "recommended": ask["recommended_option"],
        "owner_person_id": owner["id"],
        "allow_rule": bool(ask["allow_rule"]),
    }
    post = await one(
        conn,
        """INSERT INTO board_posts
        (thread_id,actor,at,person_id,token_id,provenance,kind,text,blocks,mentions,
         reply_to_post_id,ask_id,decision_spec,source_ref,request_hash)
        VALUES (%s,%s,clock_timestamp(),%s,%s,%s,'decision_needed',%s,'[]'::jsonb,%s,NULL,%s,%s,NULL,NULL)
        RETURNING id,thread_id,ask_id""",
        (
            thread["id"],
            actor,
            requester_person_id,
            token_id,
            Jsonb(provenance),
            proof_text(ask["question"], ask.get("proof")),
            [target],
            ask["id"],
            Jsonb(decision_spec),
        ),
    )
    await conn.execute(
        "UPDATE board_threads SET last_post_at=clock_timestamp(),updated_at=clock_timestamp() WHERE id=%s",
        (thread["id"],),
    )
    return {"thread_id": thread["id"], "post_id": post["id"]}


async def attach_task_question_to_board(
    conn,
    workspace,
    task: dict,
    event: dict,
    *,
    target: str,
    actor: str,
    person_id: int | None = None,
    token_id: int | None = None,
    provenance: dict | None = None,
) -> dict | None:
    """Mirror a blocking task question onto the task's canonical board thread for Chief-of-Staff triage."""
    if not coordinator_active(workspace) or not event or event.get("data", {}).get("routed_to_board"):
        return None
    if not target or not target.startswith("person:"):
        return None
    person = await one(conn, "SELECT * FROM people WHERE key=%s AND active", (target[7:],))
    if not person:
        return None
    if task["visibility"] == "private" and task["owner_person_id"] != person["id"]:
        return None
    thread = await _open_task_thread(conn, workspace, task, actor=actor)
    post_actor = actor if actor and actor != "illo" else "agent:task"
    meta = dict(provenance or {})
    meta.setdefault("task_id", task["id"])
    meta.setdefault("via", "task_post")
    if "agent_key" not in meta and post_actor.startswith("agent:"):
        key = post_actor[6:]
        if any(a.key == key for a in workspace.agents):
            meta["agent_key"] = key
    if "person_key" not in meta:
        if meta.get("agent_key"):
            agent = next((a for a in workspace.agents if a.key == meta["agent_key"]), None)
            if agent:
                meta["person_key"] = agent.owner
        elif person_id:
            row = await one(conn, "SELECT key FROM people WHERE id=%s", (person_id,))
            if row:
                meta["person_key"] = row["key"]
    post = await one(
        conn,
        """INSERT INTO board_posts
        (thread_id,actor,at,person_id,token_id,provenance,kind,text,blocks,mentions,
         reply_to_post_id,ask_id,decision_spec,source_ref,request_hash)
        VALUES (%s,%s,clock_timestamp(),%s,%s,%s,'question',%s,'[]'::jsonb,%s,NULL,NULL,NULL,NULL,NULL)
        RETURNING id,thread_id""",
        (
            thread["id"],
            post_actor,
            person_id,
            token_id,
            Jsonb(meta),
            (event["text"] or "The task needs a person's answer.")[:16000],
            [target],
        ),
    )
    await conn.execute(
        "INSERT INTO board_mentions (thread_id,post_id,target,recipient_person_id) VALUES (%s,%s,%s,%s)",
        (thread["id"], post["id"], target, person["id"]),
    )
    await conn.execute(
        "UPDATE board_threads SET last_post_at=clock_timestamp(),updated_at=clock_timestamp() WHERE id=%s",
        (thread["id"],),
    )
    await conn.execute(
        "UPDATE task_events SET data=data || %s WHERE id=%s",
        (Jsonb({"routed_to_board": True, "board_thread_id": thread["id"], "board_post_id": post["id"]}), event["id"]),
    )
    return {"thread_id": thread["id"], "post_id": post["id"]}


async def unblock_task_from_board(
    conn,
    workspace,
    task_id: int | None,
    *,
    actor: str,
    text: str,
    board_post_id: int,
    person_key: str | None = None,
) -> bool:
    """Propagate a board answer back to a task waiting on a routed question so its agent wakes.

    Callers must take advisory lock 12000001 before locking board_threads when task_id is set.
    """
    if not task_id:
        return False
    row = await one(conn, "SELECT * FROM tasks WHERE id=%s FOR UPDATE", (task_id,))
    if not row or row["state"] not in {"todo", "working"}:
        return False
    waits = row["waits_for"] or ""
    if not waits.startswith("person:"):
        return False
    routed = await one(
        conn,
        """SELECT id FROM task_events WHERE task_id=%s AND kind='question'
        AND coalesce((data->>'routed_to_board')::boolean, false)
        AND id > coalesce((
            SELECT max(b.id) FROM task_events b
            WHERE b.task_id=%s AND b.data->>'waits_for' IS DISTINCT FROM %s
        ), 0)
        ORDER BY id DESC LIMIT 1""",
        (task_id, task_id, waits),
    )
    if person_key is not None:
        if waits != f"person:{person_key}" and not routed:
            return False
    elif not routed:
        return False
    await conn.execute("UPDATE tasks SET waits_for=holder WHERE id=%s", (task_id,))
    row["waits_for"] = row["holder"]
    await task_ledger._event(
        conn,
        row,
        actor,
        "message",
        text[:16000],
        {"wake": True, "source": "board", "board_post_id": board_post_id},
    )
    return True


async def close_board_for_ask(conn, ask_id: int, *, reason: str = "no_longer_pending") -> None:
    """Close board episodes and escalations linked to a cancelled ask."""
    from illo.board.episodes import close_items

    thread_id = await board.ask_thread_id(conn, ask_id)
    if thread_id is None:
        return
    thread = await board.lock_thread(conn, thread_id)
    if thread:
        await close_items(conn, thread, reason=reason)


SCOUT_OPTIONS = [
    {"id": "task", "label": "Make a task"},
    {"id": "dismiss", "label": "Not useful"},
]


async def attach_scout_finding_to_board(conn, workspace, row: dict) -> dict | None:
    """Route a scout finding into the single Chief-of-Staff board pipeline."""
    from illo.ledger import asks

    if not coordinator_active(workspace):
        return None
    source_ref = f"board:scout:{row['id']}"
    existing_ask = await one(conn, "SELECT id FROM asks WHERE source_ref=%s", (source_ref,))
    if existing_ask:
        await conn.execute(
            "UPDATE scout_findings SET state='sent' WHERE id=%s AND state='new'",
            (row["id"],),
        )
        post = await one(conn, "SELECT id, thread_id FROM board_posts WHERE ask_id=%s LIMIT 1", (existing_ask["id"],))
        return {
            "thread_id": post["thread_id"] if post else None,
            "post_id": post["id"] if post else None,
            "ask_id": existing_ask["id"],
        }

    owner = None
    if row["scope"] == "person" and row.get("person_id"):
        owner = await one(conn, "SELECT * FROM people WHERE id=%s AND active", (row["person_id"],))
    else:
        for key in (row.get("for_person"), workspace.config.default_owner):
            if key:
                owner = await one(conn, "SELECT * FROM people WHERE key=%s AND active", (key,))
                if owner:
                    break
    if not owner:
        return None

    visibility = "private" if row["scope"] == "person" else "team"
    thread_owner = owner["id"] if visibility == "private" else None
    thread = await one(
        conn,
        """INSERT INTO board_threads (title,visibility,owner_person_id,created_by)
        VALUES (%s,%s,%s,'agent:scout') RETURNING *""",
        (row["title"][:200], visibility, thread_owner),
    )
    proof = {"text": row["message"], "links": row.get("links") or []}
    ask = await asks.create(
        conn,
        owner_person_id=owner["id"],
        question=row["title"][:240],
        kind="review",
        options=SCOUT_OPTIONS,
        proof=proof,
        source_ref=source_ref,
        allow_rule=False,
    )
    target = f"person:{owner['key']}"
    decision_spec = {
        "question": ask["question"],
        "options": ask["options"],
        "recommended": None,
        "owner_person_id": owner["id"],
        "allow_rule": False,
    }
    provenance = {
        "agent_key": None,
        "person_key": owner["key"],
        "token_label": "scout",
        "via": "scout_finding",
        "finding_id": row["id"],
        "scope": row["scope"],
    }
    post = await one(
        conn,
        """INSERT INTO board_posts
        (thread_id,actor,at,person_id,token_id,provenance,kind,text,blocks,mentions,
         reply_to_post_id,ask_id,decision_spec,source_ref,request_hash)
        VALUES (%s,'agent:scout',clock_timestamp(),NULL,NULL,%s,'decision_needed',%s,'[]'::jsonb,%s,NULL,%s,%s,NULL,NULL)
        RETURNING id,thread_id,ask_id""",
        (
            thread["id"],
            Jsonb(provenance),
            proof_text(row["message"], {"links": row.get("links") or []}),
            [target],
            ask["id"],
            Jsonb(decision_spec),
        ),
    )
    await conn.execute(
        "UPDATE board_threads SET last_post_at=clock_timestamp(),updated_at=clock_timestamp() WHERE id=%s",
        (thread["id"],),
    )
    await conn.execute(
        "UPDATE scout_findings SET state='sent' WHERE id=%s AND state='new'",
        (row["id"],),
    )
    return {"thread_id": thread["id"], "post_id": post["id"], "ask_id": ask["id"]}


async def sync_scout_finding_answer(conn, answered: dict) -> dict | None:
    """Update scout_findings when a board-routed scout ask is answered; return row if task should be created."""
    source_ref = answered.get("source_ref") or ""
    if not source_ref.startswith("board:scout:") or not source_ref[12:].isdigit():
        return None
    finding_id = int(source_ref[12:])
    choice = "task" if answered.get("answer_option") == "task" else "dismissed"
    updated = await one(
        conn,
        """UPDATE scout_findings SET state=%s,decided_by=%s,decided_at=now()
        WHERE id=%s AND state IN ('new','sent') RETURNING *""",
        (choice, answered.get("answered_by_person_id"), finding_id),
    )
    return updated if (updated and choice == "task") else None


async def reassign_board_ask(
    conn,
    workspace,
    ask: dict,
    *,
    old_owner_id: int | None,
    new_owner: dict,
    caller: dict,
    slack_connected: bool = False,
) -> dict | None:
    """Reassign a board-linked ask while preserving its thread link and delivery pipeline."""
    from illo.board import acl, episodes

    thread_id = await board.ask_thread_id(conn, ask["id"])
    if thread_id is None:
        return None
    await board.lock_children(conn, thread_id)
    thread = await acl.thread_access(conn, thread_id)
    if not thread or thread["state"] != "open":
        raise ValueError("The board thread is no longer open.")
    if thread["visibility"] == "private" and not await acl.recipient_ok(
        conn, thread, new_owner, slack=False
    ):
        await board.add_guest(conn, thread_id, new_owner["id"])
        thread = await acl.thread_access(conn, thread_id)
    if not await acl.recipient_ok(conn, thread, new_owner, slack=slack_connected):
        raise ValueError("The new owner cannot receive this board ask.")

    old_owner = (
        await one(conn, "SELECT id,key,name FROM people WHERE id=%s", (old_owner_id,))
        if old_owner_id is not None
        else None
    )
    old_target = f"person:{old_owner['key']}" if old_owner else None
    new_target = f"person:{new_owner['key']}"

    post = await one(
        conn,
        "SELECT id,mentions,decision_spec FROM board_posts WHERE ask_id=%s AND kind='decision_needed'",
        (ask["id"],),
    )
    if post:
        spec = dict(post["decision_spec"]) if post.get("decision_spec") else None
        if spec is not None:
            spec["owner_person_id"] = new_owner["id"]
        mentions = [m for m in (post.get("mentions") or []) if m != old_target]
        if new_target not in mentions:
            mentions.append(new_target)
        await conn.execute(
            "UPDATE board_posts SET mentions=%s,decision_spec=%s WHERE id=%s",
            (mentions, Jsonb(spec) if spec is not None else None, post["id"]),
        )
        if old_target and old_target != new_target:
            await conn.execute(
                "DELETE FROM board_mention_acks WHERE post_id=%s AND target=%s",
                (post["id"], old_target),
            )
            await conn.execute(
                "DELETE FROM board_mentions WHERE post_id=%s AND target=%s",
                (post["id"], old_target),
            )
        await conn.execute(
            """INSERT INTO board_mentions (thread_id,post_id,target,recipient_person_id)
            VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
            (thread_id, post["id"], new_target, new_owner["id"]),
        )

    esc = await one(
        conn,
        "SELECT * FROM board_escalations WHERE ask_id=%s ORDER BY id DESC LIMIT 1",
        (ask["id"],),
    )
    queued_escalation = False
    if esc:
        card = dict(esc["card"] or {})
        card["source_ask_id"] = ask["id"]
        await conn.execute(
            """UPDATE board_escalations
            SET state='closed',closed_reason='no_longer_pending',closed_at=clock_timestamp(),
                closed_by_person_id=%s,ask_id=NULL,notice_at=clock_timestamp(),
                lease_id=NULL,lease_until=NULL
            WHERE id=%s""",
            (caller["id"], esc["id"]),
        )
        await conn.execute(
            """INSERT INTO board_escalations
            (thread_id,episode_id,post_id,reason,origin,coordinator_run_id,person_id,
             format,content_mode,text,card,card_ask_id,due_at,acl_snapshot,recipient_slack_user)
            VALUES (%s,%s,%s,%s,'model',%s,%s,'card','summary',%s,%s,%s,clock_timestamp(),%s,%s)
            ON CONFLICT (episode_id,person_id) DO UPDATE SET
                state='pending',closed_reason=NULL,closed_at=NULL,closed_by_person_id=NULL,
                answer_post_id=NULL,ask_id=NULL,card_ask_id=EXCLUDED.card_ask_id,
                text=EXCLUDED.text,card=EXCLUDED.card,due_at=clock_timestamp(),
                attempts=0,send_started_at=NULL,sending_channel=NULL,
                slack_channel=NULL,slack_ts=NULL,sent_at=NULL,notice_at=NULL,
                lease_id=NULL,lease_until=NULL,acl_snapshot=EXCLUDED.acl_snapshot,
                recipient_slack_user=EXCLUDED.recipient_slack_user""",
            (
                thread_id,
                esc["episode_id"],
                esc["post_id"],
                esc["reason"],
                esc["coordinator_run_id"],
                new_owner["id"],
                esc["text"],
                Jsonb(card),
                ask["id"],
                Jsonb(acl.scope(thread)),
                new_owner.get("slack_id"),
            ),
        )
        timeout_hours = workspace.config.board.coordinator.escalation_timeout_hours
        await conn.execute(
            """UPDATE board_episodes
            SET state='escalated',version=version+1,
                next_check_at=clock_timestamp()+make_interval(hours=>%s)
            WHERE id=%s""",
            (timeout_hours, esc["episode_id"]),
        )
        queued_escalation = True
    elif post:
        await conn.execute(
            """UPDATE board_episodes
            SET version=version+1,next_check_at=clock_timestamp()
            WHERE thread_id=%s AND post_id=%s AND state=ANY(%s)""",
            (thread_id, post["id"], list(episodes.ACTIVE)),
        )

    await board.system_post(
        conn,
        thread_id,
        f"{caller['name']} reassigned this question to {new_owner['name']}.",
        purpose="reassigned",
        source_ref=None,
        episode_id=esc["episode_id"] if esc else None,
    )
    return {"thread_id": thread_id, "queued_escalation": queued_escalation}


