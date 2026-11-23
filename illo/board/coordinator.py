"""Schedule and apply one tool-free decision for a live board item."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from psycopg.types.json import Jsonb

from illo.board import acl, decision, episodes, packet, policy
from illo.board.labels import actor_label
from illo.brain.routes import resolve_route
from illo.db import all_rows, one
from illo.gateway.events import InboundEvent
from illo.gateway.principal import resolve
from illo.ledger import audit, board, people, sensors

log = logging.getLogger(__name__)
board_pages_ready = policy.board_pages_ready


async def _tick_lock(conn):
    return (await one(conn, "SELECT pg_try_advisory_xact_lock(12000010) AS acquired"))["acquired"]


async def scan(ctx):
    cfg = ctx.workspace.config.board.coordinator
    async with ctx.pool.connection() as conn:
        if not await _tick_lock(conn):
            return
        threads = await all_rows(conn, """SELECT b.* FROM board_threads b WHERE
            (b.state='open' AND (
                (SELECT max(id) FROM board_posts WHERE thread_id=b.id)
                    IS DISTINCT FROM b.coordinator_scanned_post_id
                OR b.reopened_at>b.coordinator_scanned_at
                OR (b.stalled_reason='turn_cap' AND (b.coordinator_scanned_at IS NULL
                    OR b.stalled_at IS NULL OR b.stalled_at>b.coordinator_scanned_at))))
            OR (b.state<>'open' AND EXISTS (SELECT 1 FROM board_episodes e WHERE
                e.thread_id=b.id AND e.state=ANY(%s)))
            ORDER BY b.coordinator_scanned_at NULLS FIRST,b.id LIMIT 100
            FOR UPDATE OF b SKIP LOCKED""", (list(episodes.ACTIVE),))
        for thread in threads:
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
        due = now if item["reason"] == "turn_cap" else (latest["at"] or now) + timedelta(seconds=settle)
        await conn.execute("""INSERT INTO board_episodes
            (thread_id,reason,post_id,mention_target,stalled_at,next_check_at,episode_seq)
            VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
            (thread_id, *episodes.item_key(item), due, last["episode_seq"] + 1 if last else 1))


async def _claim(ctx):
    timeout = resolve_route(ctx.workspace, "board_coordinator").timeout_s
    async with ctx.pool.connection() as conn:
        if not await _tick_lock(conn):
            return None
        row = await one(conn, """SELECT t.id FROM board_threads t WHERE t.state='open'
            AND (t.coordinator_lease_until IS NULL OR t.coordinator_lease_until<clock_timestamp())
            AND EXISTS (SELECT 1 FROM board_episodes e WHERE e.thread_id=t.id AND e.state=ANY(%s)
                AND e.next_check_at<=clock_timestamp())
            ORDER BY (SELECT min(e.next_check_at) FROM board_episodes e WHERE e.thread_id=t.id
                AND e.state=ANY(%s)),t.id LIMIT 1 FOR UPDATE OF t SKIP LOCKED""",
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
    owner = thread["effective_owner"]
    if owner is not None:
        person = await one(conn, "SELECT key FROM people WHERE id=%s", (owner,))
        targets = [f"person:{person['key']}"] if person else []
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
        episode = await one(conn, """SELECT * FROM board_episodes WHERE thread_id=%s
            AND state=ANY(%s) AND next_check_at<=clock_timestamp()
            ORDER BY next_check_at,id LIMIT 1""", (thread_id, list(episodes.ACTIVE)))
        if not episode:
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
    if not board_pages_ready(ctx) or not (ctx.workspace.config.board.enabled
                                          and ctx.workspace.config.board.coordinator.enabled):
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
    text = (f"{label}:{start} you answer post #{episode['post_id']} from {asker}? "
            f"If no one answers by {due:%H:%M} UTC, I ask a person.")
    await board.system_post(conn, thread["id"], text, mentions=[(choice.target, target["id"])],
                            purpose="headstart", source_ref=f"coord:{episode['id']}:headstart",
                            episode_id=episode["id"], workspace=workspace, reply_to_post_id=episode["post_id"])
    await conn.execute("""UPDATE board_episodes SET state='headstart',headstart_target=%s,
        headstart_until=%s,next_check_at=%s,version=version+1,waits=0 WHERE id=%s""",
        (choice.target, due, due, episode["id"]))


async def _action(conn, workspace, thread, episode, run, choice):
    person = target = None
    if choice.escalate:
        person = await people.get(conn, choice.escalate.person[7:])
        if not await acl.recipient_ok(conn, thread, person):
            return "stale", "recipient"
        if choice.escalate.text and not workspace.config.board.coordinator.slack_summaries:
            return "stale", "content_policy"
    if choice.headstart:
        target = await acl.target_ok(conn, workspace, thread, choice.headstart.target,
                                     await packet.asker_post(conn, episode))
        cfg = workspace.config.board.coordinator
        if not target or not (packet.wakeable(workspace, choice.headstart.target) or
                              (target["last_used_at"] and target["last_used_at"] >=
                               datetime.now(UTC) - timedelta(minutes=cfg.presence_minutes))):
            return "stale", "target"
    if choice.group_key:
        await policy.lock_budget(conn)
        groups = await packet.group_counts(conn, thread)
        offered = choice.group_key in run["snapshot"]["groups"] and any(
            g["key"] == choice.group_key for g in groups)
        if not offered and sum(g["open_threads"] > 0 for g in groups) >= 50:
            return "invalid", "group_limit"
    if choice.escalate:
        error = await policy.reserve(conn, workspace, thread, episode, person, choice=choice.escalate,
                                     run_id=run["id"])
        if error:
            return "invalid", error
    elif choice.headstart:
        await _headstart(conn, workspace, thread, episode, choice.headstart, target)
    else:
        await conn.execute("""UPDATE board_episodes SET state='waiting',waits=waits+1,
            next_check_at=clock_timestamp()+make_interval(mins=>%s),version=version+1 WHERE id=%s""",
            (60 if choice.action == "nothing" else choice.recheck_minutes or 30, episode["id"]))
    if choice.group_clear or choice.group_key:
        await conn.execute("UPDATE board_threads SET group_key=%s WHERE id=%s",
                           (choice.group_key, thread["id"]))
    await audit.write(conn, principal="illo", tool="board_coordinate", decision="allow", reason=choice.action,
                      args={"thread": thread["id"], "episode": episode["id"], "action": choice.action,
                            "format": choice.escalate.format if choice.escalate else None,
                            "person": choice.escalate.person if choice.escalate else None,
                            "target": choice.headstart.target if choice.headstart else None})
    return "applied", None


async def apply(ctx, event, text=None, *, error=None):
    async with ctx.pool.connection() as conn:
        run = await _run(conn, event)
        if not run or run["outcome"] != "running":
            return
        thread = await board.lock_thread(conn, run["thread_id"])
        if not _leased(thread, run["lease_id"]):
            await _finish(conn, run, "stale", "lease")
            return
        await board.lock_children(conn, thread["id"])
        episode = await one(conn, "SELECT * FROM board_episodes WHERE id=%s", (run["episode_id"],))
        live = await acl.thread_ok(conn, thread["id"], run["snapshot"]["acl"])
        choice = None
        if (not live
                or episode["state"] not in episodes.ACTIVE or episode["version"] != run["snapshot"]["version"]
                or not board_pages_ready(ctx) or not ctx.workspace.config.board.coordinator.enabled):
            outcome, code = "stale", "snapshot"
        elif not await episodes.is_pending(conn, episode):
            await episodes.close_items(conn, thread, reason="no_longer_pending")
            outcome, code = "stale", "no_longer_pending"
        elif error == "authority":
            outcome, code = "stale", "authority"
        elif error:
            outcome, code = "failed", error if error in {"provider", "timeout"} else "error"
        else:
            try:
                choice = decision.parse(text, run["snapshot"])
            except decision.InvalidDecision as exc:
                outcome, code = "invalid", str(exc)
            else:
                outcome, code = await _action(conn, ctx.workspace, live, episode, run, choice)
        if outcome in {"failed", "invalid"}:
            await conn.execute("""UPDATE board_episodes SET errors=errors+1,waits=0,version=version+1,
                next_check_at=clock_timestamp()+interval '10 minutes' WHERE id=%s""", (episode["id"],))
            await sensors.error(conn, "board:coordinator", "The board coordinator could not apply a decision.")
        await _finish(conn, run, outcome, code, choice.action if choice else None)
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
