"""Build task snapshots for the personal and team briefs."""

from datetime import datetime, timedelta

from illo.db import all_rows, one
from illo.ledger import kv, tasks


def web_link(links):
    """Slack shows only a web address as a link. Prefer it to an app address."""
    values = [
        link.get("url") or link.get("href") if isinstance(link, dict) else link
        for link in links or []
    ]
    return next((v for v in values if isinstance(v, str) and v.startswith(("http://", "https://"))), "")


def card_link(row):
    if row.get("slack_channel") and row.get("slack_ts"):
        # Slack documents this address. It opens the conversation that holds the card.
        return f"https://slack.com/app_redirect?channel={row['slack_channel']}"
    return web_link(row.get("lane_links"))


def fault_text(key, *, silent=False, names=None):
    """Keep sensor names out of text that a person reads."""
    family = key.split(":", 1)[0]
    if family == "chatgpt":
        name = (names or {}).get(key.partition(":")[2], "a team member")
        return f"The ChatGPT sign-in of {name} failed."
    if family == "job":
        return 'One of my background jobs stopped.' if silent else 'One of my background jobs failed.'
    return {
        "brain": "The model service failed its last run.",
        "draft": 'I could not confirm the delivery of a draft.',
        "drafts": 'I could not send or update a draft card.',
        "asks": 'I could not send or update an ask card.',
        "github": "The last GitHub request failed.",
        "slack": 'I could not confirm the Slack connection or channel access.',
        "heartbeat": 'My regular health report stopped.',
        "collector": "A collector stopped its regular report."
        if silent
        else "A collector failed its last report.",
        "connector": "A connected service failed its last request.",
        "email": 'I could not read email.',
        "notes": 'I could not update a note.',
        "workspace": 'I could not save a workspace change.',
    }.get(family, 'One of my services failed its last check.')


def sensor_fault(row, now, names=None, since=None):
    ok, error = row["last_ok_at"], row["last_error_at"]
    failed = error is not None and (ok is None or error > ok)
    interval = row.get("expected_every_s")
    # Report an event failure one time. A timer sensor stays a fault until it recovers.
    if failed and not interval and since and error <= since:
        failed = False
    last = max((value for value in (ok, error) if value is not None), default=None)
    silent = bool(interval and last and now - last > timedelta(seconds=max(1800, 3 * interval)))
    if not failed and not silent:
        return None
    return {
        "key": row["key"],
        "what": fault_text(row["key"], silent=not failed, names=names),
        "since": error if failed else last + timedelta(seconds=max(1800, 3 * interval)),
    }


TEAM_STATE_KEY = "digest-team"


def thread_link(row):
    if row.get("slack_channel") and row.get("slack_ts"):
        return (f"https://slack.com/archives/{row['slack_channel']}/p"
                + row["slack_ts"].replace(".", ""))
    return ""


def task_link(workspace, row, *, on_board=False):
    if workspace.config.pages.enabled:
        from illo.tasks.pages import workspace_url

        return workspace_url(workspace, row["id"], on_board=on_board)
    return thread_link(row)


def hours(now, at):
    return round(max(0, (now - at).total_seconds() / 3600), 1)


def wait_record(row, events):
    """Find the current wait period. Later messages do not reset it."""
    period = []
    for event in reversed(events):
        if event["data"].get("waits_for") != row["waits_for"]:
            break
        period.append(event)
    return (period[-1]["at"] if period else row["created_at"]), period


def wait_check(event):
    return event["data"].get("source") == "wait_check" and event["kind"] in {"reminded", "message"}


def wait_reason(row, period, name):
    for event in period:
        # The last wait check read the live state. Its text is the best reason.
        if wait_check(event):
            return event["text"][:600]
    for event in period:
        if event["kind"] == "question":
            return event["text"][:200]
        if event["kind"] == "wake_failed" and event["data"].get("final"):
            return "the wake failed: " + event["text"]
        if event["text"] == "The ticket waits for an owner.":
            return "the ticket waits for an owner; choose a holder"
        if event["data"].get("escalated"):
            return f"{name} stays silent after {event['data']['restarts']} restarts; decide the next step"
    if row["state"] == "review":
        return f"returned by {name}, confirm or send back"
    if row["holder"] == row["waits_for"]:
        due = f"; due {row['due_at'].isoformat()}" if row["due_at"] else ""
        return "you hold it" + due
    if not row["holder"]:
        return "the ticket waits for an owner; choose a holder"
    return "choose the next step"


def short_action(reason):
    action = " ".join(reason.split())
    if len(action) <= 160:
        return action
    end = action.rfind(" ", 0, 160)
    return action[:end if end != -1 else 159] + "…"


async def task_records(conn, workspace, now, since, person=None):
    """Read the ledger's visibility boundary before any task text or event."""
    rows = await all_rows(conn, """SELECT * FROM tasks
        WHERE (visibility='team' OR owner_person_id=%s)
        AND (state NOT IN ('done','cancelled') OR done_at>%s OR updated_at>%s)
        ORDER BY id""", (person["id"] if person else None, since, since))
    events = await all_rows(conn, """SELECT * FROM task_events
        WHERE task_id=ANY(%s) ORDER BY id""", ([r["id"] for r in rows],))
    by_task = {}
    for event in events:
        by_task.setdefault(event["task_id"], []).append(event)
    members = await all_rows(conn, "SELECT key,name FROM people")
    names = {"person:" + p["key"]: p["name"] for p in members}
    names.update({"agent:" + a.key: a.name for a in workspace.agents})
    board_ids = set()
    if person and workspace.config.pages.enabled and workspace.config.pages.open == 'board':
        from illo.tasks.pages.board import needs

        frames, _ = await needs(conn, person)
        board_ids = {row['id'] for _, kind, row in frames if kind == 't'}
    records = []
    for row in rows:
        history = by_task.get(row["id"], [])
        since_wait, period = wait_record(row, history)
        holder = names.get(row["holder"], row["holder"] or "No holder")
        last_wake = [e["at"] for e in history if e["kind"] == "woken"]
        base = max(t for t in [row["last_activity_at"], row["alive_at"], *last_wake] if t)
        restarts, previous_holder = [], None
        for event in history:
            if event["kind"] == "handed":
                previous_holder = event["data"].get("holder")
            if event["kind"] == "released" and event["actor"] == "code" and not event["data"].get("requested"):
                restarts.append((event["data"].get("holder", previous_holder or row["holder"]), event["at"]))
        records.append({
            "row": row, "events": history, "period": period, "wait_since": since_wait, "restarts": restarts,
            "reviewer": await tasks.reviewer(conn, workspace, row),
            "item": {"id": row["id"], "title": row["title"], "link": task_link(workspace, row, on_board=row["id"] in board_ids),
                     "holder": holder, "reason": wait_reason(row, period, holder),
                     "age_hours": hours(now, since_wait), "due_at": row["due_at"],
                     "silent_hours": hours(now, base),
                     # The wait started after the last brief. An old wait needs no second explanation.
                     "new": since_wait > since},
        })
    return records, names


def done_item(record, names):
    row = record["row"]
    event = next((e for e in reversed(record["events"]) if e["kind"] == "done"), None)
    proof = [c["text"] for c in (event or {}).get("data", {}).get("checks", []) if c["ok"]]
    links = [{"kind": link["kind"], "url": link["url"]} for link in row["links"]]
    actor = row["done_by"]
    return {k: record["item"][k] for k in ("id", "title", "link", "holder")} | {
        "proof": proof or [link["kind"] for link in links] or [(event or {}).get("text", "A person confirms the work.")],
        "proof_links": links, "done_by": "the Done rule" if actor == "code" else names.get(actor, actor),
    }


async def build(conn, person, now, workspace):
    """Build the person's task sections and keep their asks and owner faults."""
    since = await brief_window(conn, f"digest:{person['key']}", now)
    records, names = await task_records(conn, workspace, now, since, person)
    ref = "person:" + person["key"]
    open_records = [r for r in records if r["row"]["state"] not in {"done", "cancelled"}]
    needs = [r for r in open_records if r["row"]["waits_for"] == ref]
    needs.sort(key=lambda r: (not bool(r["row"]["due_at"] and r["row"]["due_at"] < now),
                              r["wait_since"], r["row"]["id"]))
    owned = {"agent:" + a.key: a for a in workspace.agents if a.owner == person["key"]}
    agents = []
    for holder, agent in owned.items():
        held = [r for r in open_records if r["row"]["holder"] == holder]
        silent = []
        for r in held:
            entry = next((e for e in workspace.lists if e.key == r["row"]["list_key"]), None)
            if entry and r["item"]["silent_hours"] > entry.silence.agent_hours / 2:
                silent.append(r["row"]["id"])
        restarts = [r["row"]["id"] for r in records for event_holder, at in r["restarts"]
                    if at > since and event_holder == holder]
        if held or restarts:
            agents.append({"name": agent.name, "count": len(held), "silent": silent,
                           "restarts": len(restarts), "restart_tasks": sorted(set(restarts))})
    finished = [r for r in records if r["row"]["state"] == "done" and r["row"]["done_at"] > since
                and (ref in {r["row"]["created_by"], r["row"]["handed_by"], r["reviewer"]}
                     or r["row"]["holder"] in owned)]
    finished.sort(key=lambda r: (r["row"]["done_at"], r["row"]["id"]), reverse=True)
    due = [r["item"] for r in open_records if r not in needs and r["row"]["due_at"]
           and r["row"]["due_at"] <= now + timedelta(hours=24)
           and (r["row"]["holder"] == ref or r["reviewer"] == ref)]
    due.sort(key=lambda r: (r["due_at"], r["id"]))
    from illo.ledger import asks as ask_ledger

    asks = await all_rows(conn, f"""SELECT a.*,l.links AS lane_links FROM asks a
        LEFT JOIN lanes l ON l.id=a.lane_id
        WHERE a.owner_person_id=%s
          AND (a.state='open' OR (a.delivery_state='failed' AND NOT {ask_ledger.BOARD_NATIVE_SQL}))
        AND (l.id IS NULL OR l.visibility='team' OR l.owner_person_id=%s)
        ORDER BY a.due_at NULLS LAST,a.id""", (person["id"], person["id"]))
    ask_links = {}
    if workspace.config.pages.enabled and workspace.config.pages.open == 'board':
        from illo.config import Config
        from illo.tasks.pages import path
        from illo.tasks.pages.board import needs as board_needs

        base = (workspace.config.pages.url or workspace.config.connect.public_url or Config.from_env().public_url).rstrip('/')
        frames, _ = await board_needs(conn, person)
        ids = {row['id'] for _, kind, row in frames if kind == 'a'}
        ask_links = {a['id']: base + path(workspace, a['id'], ask=True, on_board=a['id'] in ids) for a in asks}
    return {
        "name": person["name"], "needs_you": [r["item"] for r in needs[:7]],
        "needs_you_total": len(needs), "your_agents": agents,
        "finished_total": len(finished), "finished": [done_item(r, names) for r in finished[:5]],
        "due": due, "asks": [{"title": a["question"], "link": ask_links.get(a["id"], card_link(a)),
            "reason": "The answer did not reach its agent." if a["delivery_state"] == "failed" else "Please answer.",
            "age_hours": hours(now, a["created_at"])} for a in asks],
        "handled_without_you": await handled_without_you(conn, since, person=person),
        "faults": await read_faults(conn, now, since, person["key"]) if person["role"] == "owner" else [],
        "recent_openings": await recent_openings(conn, "digest", person["id"]),
    }


async def handled_without_you(conn, since, *, person=None) -> int:
    """Count Chief-of-Staff board answers and standing-rule decisions since the last brief."""
    if person is None:
        row = await one(
            conn,
            """SELECT count(*) AS n
            FROM board_posts a
            JOIN board_threads t ON t.id = a.thread_id
            WHERE a.actor = 'illo' AND NOT a.is_human
              AND a.kind IN ('answer', 'decision')
              AND a.at > %s AND t.visibility = 'team'""",
            (since,),
        )
    else:
        row = await one(
            conn,
            """SELECT count(*) AS n
            FROM board_posts a
            JOIN board_threads t ON t.id = a.thread_id
            LEFT JOIN board_posts q ON q.id = a.reply_to_post_id
            WHERE a.actor = 'illo' AND NOT a.is_human
              AND a.kind IN ('answer', 'decision')
              AND a.at > %s
              AND (
                (t.visibility = 'team' AND (%s = ANY(COALESCE(q.mentions, '{}')) OR COALESCE(cardinality(q.mentions), 0) = 0))
                OR (t.visibility = 'private' AND (t.owner_person_id = %s OR EXISTS (
                    SELECT 1 FROM board_thread_guests g WHERE g.thread_id = t.id AND g.person_id = %s
                )))
              )""",
            (since, f"person:{person['key']}", person["id"], person["id"]),
        )
    return int(row["n"]) if row else 0


async def team_snapshot(conn, workspace, now):
    """Read team tasks only. Keep private tasks, asks, and service faults out."""
    since = await brief_window(conn, TEAM_STATE_KEY, now)
    records, names = await task_records(conn, workspace, now, since)
    finished = [r for r in records if r["row"]["state"] == "done" and r["row"]["done_at"] > since]
    finished.sort(key=lambda r: (r["row"]["done_at"], r["row"]["id"]), reverse=True)
    waiting, stuck, agents, people_count, new_waits = {}, [], 0, 0, []
    for r in records:
        row, item = r["row"], r["item"]
        if row["state"] in {"done", "cancelled"}:
            continue
        target = row["waits_for"] or ""
        if target.startswith("person:"):
            if item["new"]:
                new_waits.append({k: item[k] for k in ("id", "title", "link", "reason", "age_hours")}
                                 | {"action_by": names.get(target, target)})
            oldest = {k: item[k] for k in ("id", "title", "link")} | {"action": short_action(item["reason"])}
            entry = waiting.setdefault(target, {"name": names.get(target, target), "count": 0,
                                                "oldest_hours": 0, "oldest": oldest})
            entry["count"] += 1
            if item["age_hours"] >= entry["oldest_hours"]:
                entry.update(oldest_hours=item["age_hours"], oldest=oldest)
        facts = []
        check_text = None
        stuck_at = None
        for event in r["period"]:
            if event["kind"] == "wake_failed" and event["data"].get("final"):
                fact = "the wake failed: " + event["text"]
            elif event["data"].get("escalated"):
                fact = f"the holder stays silent after {event['data']['restarts']} restarts"
            else:
                if check_text is None and wait_check(event):
                    check_text = event["text"][:600]
                continue
            facts.append(check_text if check_text is not None else fact)
            stuck_at = event["at"]
            break
        if not row["holder"]:
            unheld = next((e["at"] for e in reversed(r["events"])
                           if e["kind"] == "handed" and not e["data"].get("holder")), row["created_at"])
            if now - unheld > timedelta(hours=24):
                facts.append("no holder for more than 24 hours")
                became_stuck = unheld + timedelta(hours=24)
                stuck_at = max(stuck_at, became_stuck) if stuck_at else became_stuck
        if row["due_at"] and row["due_at"] < now:
            facts.append("past the due time")
            stuck_at = max(stuck_at, row["due_at"]) if stuck_at else row["due_at"]
        if facts:
            stuck.append({k: item[k] for k in ("id", "title", "link")} | {
                "fact": "; ".join(facts), "action_by": names.get(target or r["reviewer"], "the list reviewer"),
                "new": bool(stuck_at and stuck_at > since)})
        agents += (row["holder"] or "").startswith("agent:")
        people_count += (row["holder"] or "").startswith("person:")
    return {"team": workspace.config.team_name, "finished_total": len(finished),
            "finished": [done_item(r, names) for r in finished[:7]],
            "waiting": sorted(waiting.values(), key=lambda r: -r["oldest_hours"]),
            "new_waits": new_waits, "stuck": stuck, "in_work": {"agents": agents, "people": people_count},
            "handled_without_you": await handled_without_you(conn, since),
            "recent_openings": await recent_openings(conn, "team_digest")}


async def brief_window(conn, key, now):
    saved = await kv.get(conn, "brief:sent:" + key)
    if saved:
        return datetime.fromisoformat(saved)
    row = await one(conn, "SELECT last_run_at FROM routine_state WHERE key=%s AND last_status IS DISTINCT FROM 'skipped'", (key,))
    return (row["last_run_at"] if row else None) or now - timedelta(days=1)


async def recent_openings(conn, channel, person_id=None):
    """Read the first line of at most five completed briefs, newest first."""
    person_filter = "person_id=%s" if person_id is not None else "person_id IS NULL"
    rows = await all_rows(
        conn,
        f"""SELECT left(split_part(payload->>'final_answer',E'\\n',1),160) AS opening
        FROM events WHERE channel=%s AND {person_filter} AND status='handled'
          AND jsonb_typeof(payload->'final_answer')='string' AND payload->>'final_answer'<>''
        ORDER BY id DESC LIMIT 5""",
        (channel, person_id) if person_id is not None else (channel,),
    )
    return [row["opening"] for row in rows]


async def read_faults(conn, now, since, credential_key):
    names = {r["key"]: r["name"] for r in await all_rows(conn, "SELECT key,name FROM people")}
    faults = []
    for row in await all_rows(conn, "SELECT * FROM sensors ORDER BY key"):
        if row["key"].startswith("chatgpt:") and row["key"] != f"chatgpt:{credential_key}":
            continue
        if fault := sensor_fault(row, now, names, since):
            faults.append(fault)
    return faults
