"""Schedule and apply one Chief-of-Staff triage action for a live board item."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from psycopg.types.json import Jsonb

from illo.board import acl, decision, doors, episodes, packet, policy
from illo.board.labels import actor_label
from illo.brain.routes import resolve_route
from illo.db import all_rows, one
from illo.gateway.events import InboundEvent
from illo.gateway.principal import resolve
from illo.ledger import asks, audit, board, people, rules, sensors
from illo.tools.registry import Refusal

log = logging.getLogger(__name__)
board_pages_ready = policy.board_pages_ready

_NEEDS_SCAN = """((b.state='open' AND (
    (SELECT max(id) FROM board_posts WHERE thread_id=b.id)
        IS DISTINCT FROM b.coordinator_scanned_post_id
    OR b.reopened_at>b.coordinator_scanned_at
    OR (b.stalled_reason='turn_cap' AND (b.coordinator_scanned_at IS NULL
        OR b.stalled_at IS NULL OR b.stalled_at>b.coordinator_scanned_at))))
    OR (b.state<>'open' AND EXISTS (SELECT 1 FROM board_episodes e WHERE
        e.thread_id=b.id AND e.state=ANY(%(states)s))))"""

_EPISODE_READY = """e.state=ANY(%s) AND e.next_check_at<=clock_timestamp()
    AND NOT EXISTS (SELECT 1 FROM board_escalations x WHERE x.episode_id=e.id
        AND x.state IN ('pending','sending') AND x.due_at>clock_timestamp())"""


async def _tick_lock(conn):
    return (await one(conn, "SELECT pg_try_advisory_xact_lock(12000010) AS acquired"))["acquired"]


async def scan(ctx):
    cfg = ctx.workspace.config.board.coordinator
    states = list(episodes.ACTIVE)
    async with ctx.pool.connection() as conn:
        refs = await all_rows(conn, f"""SELECT b.id FROM board_threads b WHERE {_NEEDS_SCAN}
            ORDER BY b.coordinator_scanned_at NULLS FIRST,b.id LIMIT 100""", {"states": states})
    for ref in refs:
        async with ctx.pool.connection() as conn:
            if not await _tick_lock(conn):
                return
            thread = await one(conn, f"""SELECT b.* FROM board_threads b
                WHERE b.id=%(id)s AND {_NEEDS_SCAN} FOR UPDATE OF b SKIP LOCKED""",
                {"id": ref["id"], "states": states})
            if thread:
                await _scan_thread(conn, thread, cfg.settle_seconds)


async def _scan_thread(conn, thread, settle):
    thread_id = thread["id"]
    await board.lock_children(conn, thread_id)
    if thread["state"] != "open":
        await episodes.close_items(conn, thread, reason="thread_closed")
        return
    if thread["stalled_reason"] == "turn_cap" and not thread["stalled_at"]:
        await conn.execute("UPDATE board_threads SET stalled_at=clock_timestamp() WHERE id=%s", (thread_id,))
    latest = await one(conn, """SELECT max(id) AS id,max(at) FILTER (WHERE actor<>'illo') AS at
        FROM board_posts WHERE thread_id=%s""", (thread_id,))
    await conn.execute("""UPDATE board_threads SET coordinator_scanned_post_id=%s,
        coordinator_scanned_at=clock_timestamp() WHERE id=%s""", (latest["id"], thread_id))
    if not await acl.thread_access(conn, thread_id):
        await episodes.close_items(conn, thread, reason="acl_lost")
        return
    await episodes.close_items(conn, thread, reason="no_longer_pending")
    previous = await all_rows(conn, """SELECT DISTINCT ON (reason,post_id,mention_target,stalled_at) *
        FROM board_episodes WHERE thread_id=%s
        ORDER BY reason,post_id,mention_target,stalled_at,episode_seq DESC""", (thread_id,))
    by_key = {episodes.item_key(e): e for e in previous}
    for item in await episodes.pending_items(conn, thread_id):
        last = by_key.get(episodes.item_key(item))
        if last and (
            last["state"] in episodes.ACTIVE or
            max(at for at in (latest["at"], thread["reopened_at"], thread["created_at"]) if at)
                <= last["closed_at"]
        ):
            continue
        now = datetime.now(UTC)
        immediate = item["reason"] in {"turn_cap", "decision_needed"}
        if not immediate and item["post_id"] is not None:
            post_meta = await one(conn, "SELECT provenance FROM board_posts WHERE id=%s", (item["post_id"],))
            if post_meta and (post_meta["provenance"] or {}).get("via") in {"task_post", "github_needs_input"}:
                immediate = True
        due = now if immediate else (latest["at"] or now) + timedelta(seconds=settle)
        await conn.execute("""INSERT INTO board_episodes
            (thread_id,reason,post_id,mention_target,stalled_at,next_check_at,episode_seq)
            VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
            (thread_id, *episodes.item_key(item), due, last["episode_seq"] + 1 if last else 1))


async def _claim(ctx):
    timeout = resolve_route(ctx.workspace, "board_coordinator").timeout_s
    async with ctx.pool.connection() as conn:
        if not await _tick_lock(conn):
            return None
        row = await one(conn, f"""SELECT t.id FROM board_threads t WHERE t.state='open'
            AND (t.coordinator_lease_until IS NULL OR t.coordinator_lease_until<clock_timestamp())
            AND EXISTS (SELECT 1 FROM board_episodes e WHERE e.thread_id=t.id AND {_EPISODE_READY})
            ORDER BY (SELECT min(e.next_check_at) FROM board_episodes e WHERE e.thread_id=t.id
                AND {_EPISODE_READY}),t.id LIMIT 1 FOR UPDATE OF t SKIP LOCKED""",
            (list(episodes.ACTIVE), list(episodes.ACTIVE)))
        if not row:
            return None
        lease = uuid4()
        await conn.execute("""UPDATE board_threads SET coordinator_lease_id=%s,
            coordinator_lease_until=clock_timestamp()+make_interval(secs=>%s) WHERE id=%s""",
            (lease, timeout + 60, row["id"]))
        return row["id"], lease


def _leased(thread, lease):
    return (thread and str(thread["coordinator_lease_id"]) == str(lease)
            and thread["coordinator_lease_until"] > datetime.now(UTC))


async def _release(conn, thread_id, lease):
    await conn.execute("""UPDATE board_threads SET coordinator_lease_id=NULL,coordinator_lease_until=NULL
        WHERE id=%s AND coordinator_lease_id=%s AND coordinator_lease_until>clock_timestamp()""", (thread_id, lease))


async def _keep_lease(ctx, thread_id, lease):
    timeout = resolve_route(ctx.workspace, "board_coordinator").timeout_s
    while True:
        await asyncio.sleep(30)
        async with ctx.pool.connection() as conn:
            row = await one(conn, """UPDATE board_threads
                SET coordinator_lease_until=clock_timestamp()+make_interval(secs=>%s)
                WHERE id=%s AND coordinator_lease_id=%s AND coordinator_lease_until>clock_timestamp() RETURNING id""",
                (timeout + 60, thread_id, lease))
            if not row:
                return


async def _stage0_rule(conn, workspace, episode):
    """Stage 0 deterministic check: apply a matching standing rule before calling the model."""
    if episode["reason"] != "decision_needed":
        return False
    author = await packet.asker_post(conn, episode)
    if not author or not author.get("ask_id"):
        return False
    ask = await one(conn, "SELECT * FROM asks WHERE id=%s", (author["ask_id"],))
    if not ask or ask["state"] != "open" or not ask["allow_rule"]:
        return False
    rule = await rules.match_ask(conn, ask)
    if not rule:
        return False
    answered = await asks.answer_with_rule(conn, ask["id"], rule["id"], allow_board=True)
    if not answered:
        return False
    post_id = await board.record_decision(conn, answered, workspace=workspace)
    await audit.write(
        conn, principal="illo", tool="board_coordinate_rule", decision="allow", reason="rule",
        args={"thread": episode["thread_id"], "episode": episode["id"], "ask_id": ask["id"],
              "rule_id": rule["id"], "post_id": post_id},
    )
    return True


async def _fallback_trigger(conn, cfg, episode):
    last = await one(conn, """SELECT state,origin FROM board_escalations
        WHERE episode_id=%s ORDER BY id DESC LIMIT 1""", (episode["id"],))
    if last and last["state"] in {"pending", "sending"}:
        await _delay(conn, episode["id"], 5)
        return "defer"
    if last and last["state"] == "failed":
        return "second_failure" if last["origin"] == "fallback" else "delivery"
    if episode["attempts"] >= cfg.max_attempts_per_episode:
        return "attempts"
    if episode["errors"] >= 2:
        return "errors"
    clock = datetime.now(UTC)
    if episode["waits"] >= 3 and clock - episode["created_at"] >= timedelta(hours=4):
        return "waits"
    if episode["stalled_at"] and clock - episode["stalled_at"] >= timedelta(minutes=cfg.stall_fallback_minutes):
        if not await one(conn, """SELECT 1 FROM board_escalations WHERE episode_id=%s
            AND state IN ('pending','sending','sent')""", (episode["id"],)):
            return "turn_cap"
    return None


async def _fallback(conn, workspace, thread, episode, trigger):
    task = thread["task"] or {}
    author = await packet.asker_post(conn, episode)
    asker = (author or {}).get("provenance", {}).get("person_key")
    targets = [episode["mention_target"], task.get("holder"),
               f"person:{asker}", f"person:{workspace.config.default_owner}"]
    if episode["reason"] == "decision_needed" and author and author.get("ask_id"):
        ask = await one(conn, "SELECT owner_person_id FROM asks WHERE id=%s", (author["ask_id"],))
        owner_row = (await one(conn, "SELECT key FROM people WHERE id=%s", (ask["owner_person_id"],))
                     if ask else None)
        targets = [f"person:{owner_row['key']}"] if owner_row else []
    elif thread["effective_owner"] is not None:
        person = await one(conn, "SELECT key FROM people WHERE id=%s", (thread["effective_owner"],))
        targets = [f"person:{person['key']}"] if person else []
    elif thread["visibility"] == "private":
        ids = [thread["owner_person_id"], *thread.get("guest_person_ids", ())]
        rows = await all_rows(conn, "SELECT id,key FROM people WHERE id=ANY(%s)", (ids,))
        by_id = {r["id"]: r["key"] for r in rows}
        targets = [f"person:{by_id[pid]}" for pid in dict.fromkeys(ids) if pid in by_id]
    if trigger != "second_failure":
        for target in dict.fromkeys(targets):
            if not target or not target.startswith("person:"):
                continue
            person = await people.get(conn, target[7:])
            if not await acl.recipient_ok(conn, thread, person):
                continue
            if await one(conn, "SELECT 1 FROM board_escalations WHERE episode_id=%s AND person_id=%s",
                         (episode["id"], person["id"])):
                continue
            if not await policy.reserve(conn, workspace, thread, episode, person):
                await audit.write(conn, principal="illo", tool="board_coordinate_fallback", decision="allow",
                                  reason="fallback", args={"thread": thread["id"], "episode": episode["id"],
                                                           "person": person["key"], "trigger": trigger})
                return
            break
    await conn.execute("""UPDATE board_episodes SET state='stuck',closed_at=clock_timestamp(),
        closed_reason='stuck',version=version+1 WHERE id=%s""", (episode["id"],))
    await board.system_post(conn, thread["id"], "I could not reach a person for this thread. "
                            "Someone needs to open it on the board.", purpose="stuck",
                            episode_id=episode["id"], source_ref=f"coord:{episode['id']}:stuck")
    await sensors.error(conn, "board:coordinator",
                        f"Board thread {thread['id']} needs a person, and Illo could not reach one.")


async def _delay(conn, episode_id, minutes):
    await conn.execute("""UPDATE board_episodes SET next_check_at=clock_timestamp()+make_interval(mins=>%s)
        WHERE id=%s""", (minutes, episode_id))


async def _admit(ctx, thread_id, lease):
    cfg = ctx.workspace.config.board.coordinator
    async with ctx.pool.connection() as conn:
        thread = await board.lock_thread(conn, thread_id)
        if not _leased(thread, lease):
            return None
        await board.lock_children(conn, thread_id)
        live = await acl.thread_ok(conn, thread_id)
        if not live:
            await episodes.close_items(conn, thread, reason="thread_closed" if thread["state"] != "open" else "acl_lost")
            return None
        await episodes.close_items(conn, thread, reason="no_longer_pending")
        episode = await one(conn, f"""SELECT e.* FROM board_episodes e WHERE e.thread_id=%s
            AND {_EPISODE_READY} ORDER BY e.next_check_at,e.id LIMIT 1""",
            (thread_id, list(episodes.ACTIVE)))
        if not episode:
            return None
        if await _stage0_rule(conn, ctx.workspace, episode):
            return None
        trigger = await _fallback_trigger(conn, cfg, episode)
        if trigger:
            if trigger != "defer":
                await _fallback(conn, ctx.workspace, live, episode, trigger)
            return None
        text, snapshot = await packet.build(conn, ctx.workspace, live, episode)
        if not text:
            await _fallback(conn, ctx.workspace, live, episode, "packet_limit")
            return None
        await policy.lock_budget(conn)
        stats = await one(conn, """SELECT count(*) FILTER (WHERE created_at>now()-interval '1 hour') AS hourly,
            count(*) FILTER (WHERE thread_id=%s) AS daily FROM board_coordinator_runs
            WHERE created_at>now()-interval '24 hours'""", (thread_id,))
        if stats["hourly"] >= cfg.max_runs_per_hour or stats["daily"] >= cfg.max_runs_per_thread_per_day:
            await _delay(conn, episode["id"], 15)
            waiting = await one(conn, """SELECT count(DISTINCT thread_id) AS n
                FROM board_episodes WHERE state=ANY(%s)""", (list(episodes.ACTIVE),))
            await sensors.error(conn, "board:coordinator",
                                f"The board coordinator reached its run limit. {waiting['n']} threads wait.")
            return None
        await conn.execute("""UPDATE board_coordinator_runs SET outcome='stale',error_code='lease',
            finished_at=clock_timestamp() WHERE thread_id=%s AND outcome='running' AND lease_id<>%s""",
            (thread_id, lease))
        attempt = episode["attempts"] + 1
        row = await one(conn, """INSERT INTO board_coordinator_runs
            (thread_id,episode_id,attempt,lease_id,snapshot) VALUES (%s,%s,%s,%s,%s) RETURNING id""",
            (thread_id, episode["id"], attempt, lease, Jsonb(snapshot)))
        await conn.execute("UPDATE board_episodes SET attempts=%s WHERE id=%s", (attempt, episode["id"]))
    return InboundEvent(channel="board", external_id=f"board:{episode['id']}:{attempt}", kind="coordinate",
                        text=text, payload={"thread_id": thread_id, "episode_id": episode["id"],
                                            "coordinator_run_id": row["id"], "lease_id": str(lease)})


async def authorized_packet(conn, ctx, event, authority):
    """Recheck the packet after the run queue grants capacity."""
    if not (ctx.workspace.config.board.enabled and ctx.workspace.config.board.coordinator.enabled):
        return False
    run = await _run(conn, event)
    if not run or run["outcome"] != "running":
        return False
    thread = await board.lock_thread(conn, run["thread_id"])
    live = await acl.thread_ok(conn, run["thread_id"], run["snapshot"]["acl"])
    current = await resolve(conn, event, ctx.workspace)
    episode = await one(conn, "SELECT * FROM board_episodes WHERE id=%s", (run["episode_id"],))
    return bool(_leased(thread, run["lease_id"]) and live and current == authority
                and episode["version"] == run["snapshot"]["version"] and await episodes.is_pending(conn, episode))


async def run_coordinator(ctx):
    if not await policy.ready(ctx):
        return
    await scan(ctx)
    for _ in range(3):
        claimed = await _claim(ctx)
        if not claimed:
            return
        thread_id, lease = claimed
        renew = asyncio.create_task(_keep_lease(ctx, thread_id, lease))
        try:
            event = await _admit(ctx, thread_id, lease)
            if event:
                result = await ctx.gateway.receive(event)
                if result["decision"] == "ignore":
                    await apply(ctx, event, error="authority")
        except Exception:
            log.error("Could not coordinate board thread %s.", thread_id)
        finally:
            renew.cancel()
            await asyncio.gather(renew, return_exceptions=True)
            async with ctx.pool.connection() as conn:
                await _release(conn, thread_id, lease)


async def _run(conn, event):
    return await one(conn, """SELECT * FROM board_coordinator_runs WHERE id=%s AND thread_id=%s
        AND episode_id=%s AND lease_id::text=%s""", tuple(event.payload[key] for key in (
        "coordinator_run_id", "thread_id", "episode_id", "lease_id")))


async def _finish(conn, run, outcome, code=None, action=None):
    await conn.execute("""UPDATE board_coordinator_runs SET outcome=%s,error_code=%s,action=%s,
        finished_at=clock_timestamp() WHERE id=%s AND outcome='running'""", (outcome, code, action, run["id"]))


async def _headstart(conn, workspace, thread, episode, choice, target):
    now = datetime.now(UTC)
    due = now + timedelta(minutes=choice.minutes)
    author = await packet.asker_post(conn, episode)
    person_key = author["provenance"].get("person_key")
    person = await people.get(conn, person_key)
    asker = actor_label(workspace, {person_key: person}, author).name
    agent = next((a for a in workspace.agents if f"agent:{a.key}" == choice.target), None)
    label = agent.name if agent else target["name"] + "'s agents"
    minutes = packet.age_minutes(now, target["last_used_at"])
    was_active = minutes is not None and minutes <= workspace.config.board.coordinator.presence_minutes
    start = f" you were active {minutes} minutes ago. Can" if was_active else " can"
    note = (" " + " ".join(choice.note.split())[:300]) if getattr(choice, "note", "") else ""
    text = (f"{label}:{start} you answer post #{episode['post_id']} from {asker}?{note} "
            f"If no one answers by {due:%H:%M} UTC, I ask a person.")
    await board.system_post(conn, thread["id"], text, mentions=[(choice.target, target["id"])],
                            purpose="headstart", source_ref=f"coord:{episode['id']}:headstart",
                            episode_id=episode["id"], workspace=workspace, reply_to_post_id=episode["post_id"])
    await conn.execute("""UPDATE board_episodes SET state='headstart',headstart_target=%s,
        headstart_until=%s,next_check_at=%s,version=version+1,waits=0 WHERE id=%s""",
        (choice.target, due, due, episode["id"]))


async def _apply_group(conn, thread, run, group_key, group_clear):
    try:
        clean_key = decision.clean_group_key(group_key, group_clear)
    except decision.InvalidDecision:
        raise Refusal("group_key must be 2 to 48 lowercase letters, digits, or hyphens, and cannot be combined with group_clear.") from None
    if clean_key:
        await policy.lock_budget(conn)
        groups = await packet.group_counts(conn, thread)
        offered = clean_key in run["snapshot"]["groups"] and any(g["key"] == clean_key for g in groups)
        if not offered and sum(g["open_threads"] > 0 for g in groups) >= 50:
            raise Refusal("The board already has 50 open groups. Reuse an existing group_key or omit it.")
    if group_clear or clean_key:
        await conn.execute("UPDATE board_threads SET group_key=%s WHERE id=%s", (clean_key, thread["id"]))


async def _reschedule_budget(conn, workspace, thread, episode):
    cfg = workspace.config.board.coordinator
    now = datetime.now(UTC)
    cooldown_until = (
        thread["escalated_at"] + timedelta(minutes=cfg.thread_cooldown_minutes)
        if thread["escalated_at"]
        else None
    )
    resume_at = (
        cooldown_until
        if cooldown_until and cooldown_until > now
        else now + timedelta(minutes=60)
    )
    await conn.execute(
        "UPDATE board_episodes SET version=version+1,next_check_at=%s WHERE id=%s",
        (resume_at, episode["id"]),
    )


async def apply_tool_action(conn, ctx, run_row, action, args):
    """Validate and commit one Chief-of-Staff tool action inside the brain tool transaction."""
    coord_run = await one(conn, "SELECT * FROM board_coordinator_runs WHERE run_id=%s", (run_row["id"],))
    if not coord_run:
        raise Refusal("This tool can only be used during a board triage run.")
    if coord_run["outcome"] != "running":
        raise Refusal("An action was already recorded for this board item. Finish without calling another action tool.")
    if action == "answer":
        pre = await one(conn, "SELECT task_id FROM board_threads WHERE id=%s", (coord_run["thread_id"],))
        if pre and pre["task_id"]:
            await conn.execute("SELECT pg_advisory_xact_lock(12000001)")
    thread = await board.lock_thread(conn, coord_run["thread_id"])
    if not _leased(thread, coord_run["lease_id"]):
        await _finish(conn, coord_run, "stale", "lease")
        return {"status": "stale", "reason": "lease", "message": "The triage lease expired. Stop and finish."}
    await board.lock_children(conn, thread["id"])
    episode = await one(conn, "SELECT * FROM board_episodes WHERE id=%s", (coord_run["episode_id"],))
    snapshot = coord_run["snapshot"]
    live = await acl.thread_ok(conn, thread["id"], snapshot["acl"])
    if (not live
            or episode["state"] not in episodes.ACTIVE
            or episode["version"] != snapshot["version"]
            or not (ctx.workspace.config.board.enabled and ctx.workspace.config.board.coordinator.enabled)):
        await _finish(conn, coord_run, "stale", "snapshot")
        return {"status": "stale", "reason": "snapshot",
                "message": "This board item changed while you were reviewing it. Stop and finish."}
    if not await episodes.is_pending(conn, episode):
        await episodes.close_items(conn, thread, reason="no_longer_pending")
        await _finish(conn, coord_run, "stale", "no_longer_pending")
        return {"status": "stale", "reason": "no_longer_pending",
                "message": "This board item is no longer pending. Stop and finish."}

    group_key = args.get("group_key")
    group_clear = bool(args.get("group_clear", False))

    if action == "answer":
        if episode["reason"] == "decision_needed":
            raise Refusal("A decision_needed item requires a person or a standing rule. Use need_ask_person instead.")
        if episode["reason"] == "turn_cap":
            raise Refusal("A turn_cap stall requires a person to continue. Use need_ask_person instead.")
        answer_text = args["answer"].strip()
        if not 1 <= len(answer_text) <= 4000 or "\x00" in answer_text:
            raise Refusal("Provide an answer between 1 and 4,000 characters.")
        sources = [" ".join(str(s).split())[:200] for s in args.get("sources", []) if str(s).strip()][:10]
        if not sources:
            raise Refusal("Cite at least one verified source you read during this run.")
        await _apply_group(conn, live, coord_run, group_key, group_clear)
        post = await board.system_post(
            conn, thread["id"], answer_text, kind="answer", purpose="answer",
            episode_id=episode["id"], source_ref=f"coord:{episode['id']}:answer:{coord_run['id']}",
            workspace=ctx.workspace, reply_to_post_id=episode["post_id"], sources=sources,
        )
        await episodes.close_items(conn, thread, reason="answered", post_id=post["id"])
        if thread["task_id"]:
            await doors.unblock_task_from_board(
                conn, ctx.workspace, thread["task_id"], actor="illo",
                text=answer_text, board_post_id=post["id"],
            )
        await audit.write(
            conn, principal="illo", tool="board_coordinate", decision="allow", reason="answer",
            args={"thread": thread["id"], "episode": episode["id"], "action": "answer",
                  "post_id": post["id"], "sources": sources},
        )
        await _finish(conn, coord_run, "applied", None, "answer")
        return {"status": "answered", "thread_id": thread["id"], "post_id": post["id"]}

    if action == "route":
        allowed = snapshot["allowed"]
        if episode["reason"] in {"decision_needed", "turn_cap"} or not allowed.get("headstart"):
            raise Refusal("Routing to another agent is not allowed for this item. Use need_ask_person or need_wait.")
        target_key = args["target"]
        minutes = args["minutes"]
        if target_key not in snapshot["targets"] or minutes > allowed["max_headstart_minutes"]:
            raise Refusal("Target agent is not an eligible online/wakeable candidate or minutes exceeds the limit.")
        target = await acl.target_ok(conn, ctx.workspace, live, target_key, await packet.asker_post(conn, episode))
        cfg = ctx.workspace.config.board.coordinator
        if not target or not (packet.wakeable(ctx.workspace, target_key) or
                              (target["last_used_at"] and target["last_used_at"] >=
                               datetime.now(UTC) - timedelta(minutes=cfg.presence_minutes))):
            raise Refusal("Target agent is no longer eligible. Use need_ask_person or need_wait.")
        await _apply_group(conn, live, coord_run, group_key, group_clear)
        await _headstart(conn, ctx.workspace, live, episode,
                         decision.Headstart(target=target_key, minutes=minutes, note=args.get("note", "")),
                         target)
        await audit.write(
            conn, principal="illo", tool="board_coordinate", decision="allow", reason="route",
            args={"thread": thread["id"], "episode": episode["id"], "action": "route", "target": target_key},
        )
        await _finish(conn, coord_run, "applied", None, "route")
        return {"status": "routed", "thread_id": thread["id"], "target": target_key, "minutes": minutes}

    if action == "ask_person":
        person_target = args["person"]
        fmt = args["format"]
        if person_target not in snapshot["candidates"]:
            raise Refusal("Recipient is not an allowed candidate for this thread.")
        if fmt not in snapshot["allowed"]["formats"]:
            raise Refusal(f"Format {fmt!r} is not allowed for this item. Allowed: {snapshot['allowed']['formats']}.")
        person = await people.get(conn, person_target[7:])
        if not await acl.recipient_ok(conn, live, person):
            await _finish(conn, coord_run, "stale", "recipient")
            return {"status": "stale", "reason": "recipient",
                    "message": "Recipient can no longer receive this escalation. Stop and finish."}
        summary = decision.clean_summary(args.get("summary", ""), 1800, escaped_limit=2000)
        if episode["reason"] == "decision_needed":
            if not summary:
                raise Refusal("Provide a clear human-readable summary explaining the context and trade-offs.")
            ask_id = snapshot.get("decision_ask_id")
            ask = await one(conn, "SELECT * FROM asks WHERE id=%s", (ask_id,)) if ask_id else None
            if not ask or ask["state"] != "open" or ask["owner_person_id"] != person["id"]:
                await _finish(conn, coord_run, "stale", "no_longer_pending")
                return {"status": "stale", "reason": "no_longer_pending",
                        "message": "The underlying decision is no longer open for this person. Stop and finish."}
            raw_opts = ask["options"]
            rec_index = args.get("recommended")
            if rec_index is not None:
                if not 0 <= rec_index < len(raw_opts):
                    raise Refusal(f"recommended index {rec_index} is out of range for {len(raw_opts)} options.")
                recommended_id = raw_opts[rec_index]["id"]
            else:
                recommended_id = ask["recommended_option"]
            esc = decision.Escalate(
                person=person_target,
                format="card",
                text=summary,
                question=ask["question"],
                options=[decision.Option(id=o["id"], label=o["label"], detail=o.get("detail", "")) for o in raw_opts],
                recommended=None,
                recommended_id=recommended_id,
                source_ask_id=ask["id"],
                allow_rule=bool(ask["allow_rule"]),
                kind=ask["kind"],
                use_ask_page=bool(ask.get("blocks") and not await asks.is_board_native(conn, ask)),
            )
        else:
            if fmt != "link" and not summary:
                raise Refusal("Provide a clear human-readable summary explaining what is needed and why.")
            if fmt == "card":
                question = decision.clean_slack(args.get("question", ""), 150)
                opts = []
                for raw in args.get("options", []):
                    opts.append(decision.Option(
                        label=decision.clean_slack(raw["label"], 40),
                        detail=decision.clean_slack(raw.get("detail", ""), 80),
                    ))
                labels = {o.label.casefold() for o in opts}
                if not question or not 2 <= len(opts) <= 4 or "" in labels or len(labels) != len(opts):
                    raise Refusal("A card requires a question and 2 to 4 options with distinct non-empty labels.")
                rec = args.get("recommended")
                if rec is not None and not 0 <= rec < len(opts):
                    raise Refusal("recommended index is out of range.")
                esc = decision.Escalate(person=person_target, format="card", text=summary,
                                        question=question, options=opts, recommended=rec)
            else:
                esc = decision.Escalate(person=person_target, format=fmt, text=summary)
        await _apply_group(conn, live, coord_run, group_key, group_clear)
        err = await policy.reserve(conn, ctx.workspace, live, episode, person, choice=esc, run_id=coord_run["id"])
        if err == "budget":
            await _reschedule_budget(conn, ctx.workspace, thread, episode)
            await _finish(conn, coord_run, "invalid", "budget", "ask_person")
            return {"status": "deferred", "reason": "budget", "person": person_target}
        if err:
            raise Refusal(f"Could not queue escalation: {err}")
        await audit.write(
            conn, principal="illo", tool="board_coordinate", decision="allow", reason="ask_person",
            args={"thread": thread["id"], "episode": episode["id"], "action": "ask_person",
                  "format": fmt, "person": person_target},
        )
        await _finish(conn, coord_run, "applied", None, "ask_person")
        return {"status": "queued", "thread_id": thread["id"], "person": person_target, "format": fmt}

    if action == "wait":
        minutes = args.get("recheck_minutes", 30)
        await _apply_group(conn, live, coord_run, group_key, group_clear)
        await conn.execute("""UPDATE board_episodes SET state='waiting',waits=waits+1,
            next_check_at=clock_timestamp()+make_interval(mins=>%s),version=version+1 WHERE id=%s""",
            (minutes, episode["id"]))
        await audit.write(
            conn, principal="illo", tool="board_coordinate", decision="allow", reason="wait",
            args={"thread": thread["id"], "episode": episode["id"], "action": "wait", "minutes": minutes},
        )
        await _finish(conn, coord_run, "applied", None, "wait")
        return {"status": "waiting", "thread_id": thread["id"], "recheck_minutes": minutes}

    raise Refusal(f"Unknown triage action: {action}")


async def apply(ctx, event, text=None, *, error=None):
    async with ctx.pool.connection() as conn:
        run = await _run(conn, event)
        if not run:
            return
        if run["outcome"] != "running":
            await _release(conn, run["thread_id"], run["lease_id"])
            return
        thread = await board.lock_thread(conn, run["thread_id"])
        if not _leased(thread, run["lease_id"]):
            await _finish(conn, run, "stale", "lease")
            return
        await board.lock_children(conn, thread["id"])
        episode = await one(conn, "SELECT * FROM board_episodes WHERE id=%s", (run["episode_id"],))
        live = await acl.thread_ok(conn, thread["id"], run["snapshot"]["acl"])
        if (not live
                or episode["state"] not in episodes.ACTIVE or episode["version"] != run["snapshot"]["version"]
                or not (ctx.workspace.config.board.enabled and ctx.workspace.config.board.coordinator.enabled)):
            outcome, code = "stale", "snapshot"
        elif not await episodes.is_pending(conn, episode):
            await episodes.close_items(conn, thread, reason="no_longer_pending")
            outcome, code = "stale", "no_longer_pending"
        elif error == "authority":
            outcome, code = "stale", "authority"
        elif error:
            outcome, code = "failed", error if error in {"provider", "timeout"} else "error"
        else:
            outcome, code = "invalid", "no_action"
        if outcome in {"failed", "invalid"}:
            next_errors = episode["errors"] + 1
            delay = timedelta(0) if next_errors >= 2 else timedelta(minutes=10)
            await conn.execute("""UPDATE board_episodes SET errors=%s,waits=0,version=version+1,
                next_check_at=clock_timestamp()+%s WHERE id=%s""", (next_errors, delay, episode["id"]))
            await sensors.error(conn, "board:coordinator", "The board coordinator could not apply a decision.")
        await _finish(conn, run, outcome, code, None)
        await _release(conn, thread["id"], run["lease_id"])


class BoardAdapter:
    def __init__(self, ctx):
        self.ctx = ctx

    async def show_working(self, event):
        pass

    async def deliver_final(self, event, text):
        await apply(self.ctx, event, text)

    async def deliver_error(self, event, *, category="internal"):
        await apply(self.ctx, event, error=category)
