"""Build one personal snapshot for briefs and attention tools."""

from datetime import timedelta
from textwrap import shorten

from psycopg.types.json import Jsonb

from illo.db import all_rows, one
from illo.ledger.lanes import DEFAULT_STALE_MINUTES


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


async def build(conn, person, now, stale_minutes=None):
    """Return only the person's work. Keep full titles and explicit state decisions."""
    limits = {**DEFAULT_STALE_MINUTES, **(stale_minutes or {})}
    asks = await all_rows(
        conn,
        """
        SELECT a.*,l.title AS lane_title,l.links AS lane_links FROM asks a
        LEFT JOIN lanes l ON l.id=a.lane_id
        WHERE a.owner_person_id=%s AND (a.state='open' OR a.delivery_state='failed')
          AND (l.id IS NULL OR l.visibility='team' OR l.owner_person_id=%s)
        ORDER BY CASE WHEN a.delivery_state='failed' THEN 1 ELSE 0 END,a.due_at NULLS LAST,a.id
    """,
        (person["id"], person["id"]),
    )
    needs = []
    for ask in asks:
        needs.append(
            {
                "kind": "failed_answer" if ask["delivery_state"] == "failed" else "ask",
                "question": ask["question"],
                "title": ask["lane_title"] or ask["question"],
                "lane_title": ask["lane_title"],
                "link": card_link(ask),
                "age_hours": max(0, (now - ask["created_at"]).total_seconds() / 3600),
            }
        )
    lanes = await active_lanes(conn, now, limits, owner=person["id"])
    stuck, running, your_moves = [], 0, []
    for lane in lanes:
        age = max(0, (now - lane["last_signal_at"]).total_seconds() / 3600)
        item = {
            "title": lane["title"],
            "link": web_link(lane["links"]),
            "state": lane["state"],
            "hours_since_signal": age,
            "last_event": lane["last_event"] or "",
        }
        if lane["state"] == "review":
            if lane["source"] == "t3":
                text = shorten(lane["summary"], width=200, placeholder="…")
                # Each collector report refreshes the signal time. The state change is the true age.
                since = lane["state_since"] or lane["last_signal_at"]
                age = max(0, (now - since).total_seconds() / 3600)
                your_moves.append(
                    {**item, "kind": "your_move", "summary": text, "age_hours": age,
                     "hours_since_signal": age}
                )
            else:
                needs.append({**item, "kind": "review", "age_hours": age})
        elif lane["state"] == "blocked" or lane["stale"]:
            stuck.append(item)
        else:
            running += 1
    your_moves.sort(key=lambda item: item["hours_since_signal"])
    needs.extend(your_moves[:10])
    last_brief = await brief_window(conn, f"digest:{person['key']}", now)
    finished, finished_total, finished_steps, closed_not_done = await closed_work(
        conn, last_brief, owner=person["id"]
    )
    faults = await read_faults(conn, now, last_brief, person["key"])
    return {
        "name": person["name"],
        "needs_you": needs,
        "your_move_total": len(your_moves),
        "stuck": stuck,
        "in_progress": running,
        "finished_total": finished_total,
        "finished_steps": finished_steps,
        "closed_not_done": closed_not_done,
        "recent_openings": await recent_openings(conn, "digest", person["id"]),
        "finished": [
            {"title": row["title"], "link": web_link(row["links"]), "state": row["state"]}
            for row in finished
        ],
        "faults": faults,
    }


async def brief_window(conn, key, now):
    row = await one(conn, "SELECT last_run_at FROM routine_state WHERE key=%s", (key,))
    return (row["last_run_at"] if row else None) or now - timedelta(days=1)


async def active_lanes(conn, now, limits, *, owner=None, team_only=False):
    return await all_rows(
        conn,
        """
        SELECT l.*,(l.last_signal_at<%s-COALESCE((%s::jsonb->>l.source)::integer,30)
          *interval '1 minute') AS stale,
          (SELECT e.text FROM lane_events e WHERE e.lane_id=l.id ORDER BY e.at DESC,e.id DESC LIMIT 1) AS last_event,
          (SELECT max(e.at) FROM lane_events e WHERE e.lane_id=l.id AND e.kind='state') AS state_since
        FROM lanes l WHERE (%s::bigint IS NULL OR owner_person_id=%s)
          AND (NOT %s OR l.visibility='team') AND state IN ('review','working','blocked')
        ORDER BY last_signal_at,id
        """,
        (now, Jsonb(limits), owner, owner, team_only),
    )


async def closed_work(conn, since, *, owner=None, team_only=False):
    """Count finished work once. Use an issue title with its closing pull request link."""
    rows = await all_rows(
        conn,
        """
        WITH completed AS (
          SELECT * FROM lanes
          WHERE (%s::bigint IS NULL OR owner_person_id=%s OR visibility='team')
            AND (NOT %s OR visibility='team') AND state='done' AND NOT is_step
        ), edges AS (
          SELECT issue.id AS issue_id, pull.id AS pull_id, pull.links,
            pull.owner_person_id AS pull_owner,
            GREATEST(issue.closed_at,pull.closed_at) AS closed_at
          FROM completed pull CROSS JOIN LATERAL jsonb_array_elements(pull.links) link
          JOIN completed issue ON issue.source='github' AND issue.id<>pull.id
            AND (issue.id::text=link->>'lane_id' OR issue.external_id=link->>'external_id')
          WHERE pull.source='github' AND link->>'kind'='closes'
        ), preferred AS (
          SELECT DISTINCT ON (issue_id) * FROM edges ORDER BY issue_id,closed_at DESC,pull_id DESC
        )
        SELECT c.title,COALESCE(p.links,c.links) AS links,c.state,c.source,c.owner_person_id,
          count(*) OVER () AS total
        FROM completed c LEFT JOIN preferred p ON p.issue_id=c.id
        WHERE GREATEST(c.closed_at,p.closed_at)>%s
          AND (%s::bigint IS NULL OR c.owner_person_id=%s OR EXISTS
            (SELECT 1 FROM edges e WHERE e.issue_id=c.id AND e.pull_owner=%s))
          AND NOT EXISTS (SELECT 1 FROM edges e WHERE e.pull_id=c.id)
        ORDER BY GREATEST(c.closed_at,p.closed_at) DESC,c.id DESC LIMIT 20
        """,
        (owner, owner, team_only, since, owner, owner, owner),
    )
    counts = await one(
        conn,
        """SELECT count(*) FILTER (WHERE state='done' AND is_step) AS steps,
          count(*) FILTER (WHERE state='failed') AS closed_not_done FROM lanes
        WHERE (%s::bigint IS NULL OR owner_person_id=%s) AND (NOT %s OR visibility='team')
          AND state IN ('done','failed') AND closed_at>%s""",
        (owner, owner, team_only, since),
    )
    return rows, rows[0]["total"] if rows else 0, counts["steps"], counts["closed_not_done"]


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


TEAM_STATE_KEY = "digest-team"


async def team_snapshot(conn, config, now):
    """Read team lanes and ask counts. Never read an ask question or a private lane."""
    members = await all_rows(conn, "SELECT id,key,name,slack_id FROM people ORDER BY key")
    owners = {p["id"]: p for p in members}
    since = await brief_window(conn, TEAM_STATE_KEY, now)
    shipped, total, steps, closed_not_done = await closed_work(conn, since, team_only=True)
    waiting = {}

    def person_waits(person_id, since):
        person = owners.get(person_id)
        if not person:
            return None
        item = waiting.setdefault(person_id, {
            "name": person["name"], "key": person["key"], "slack_id": person["slack_id"],
            "asks": 0, "reviews": [], "your_moves": 0, "oldest_hours": 0,
        })
        item["oldest_hours"] = max(item["oldest_hours"], hours(since))
        return item

    def hours(at):
        return max(0, (now - at).total_seconds() / 3600)

    asks = await all_rows(
        conn,
        """SELECT a.owner_person_id,count(*) AS total,min(a.created_at) AS oldest
        FROM asks a JOIN lanes l ON l.id=a.lane_id
        WHERE a.state='open' AND l.visibility='team' GROUP BY a.owner_person_id""",
    )
    for ask in asks:
        if item := person_waits(ask["owner_person_id"], ask["oldest"]):
            item["asks"] = ask["total"]
    stuck, running = [], {}
    for lane in await active_lanes(
        conn, now, {**DEFAULT_STALE_MINUTES, **config.stale_minutes.model_dump(by_alias=True)},
        team_only=True,
    ):
        owner = owners.get(lane["owner_person_id"], {}).get("name", "Unassigned")
        age = hours(lane["last_signal_at"])
        item = {"title": lane["title"], "link": web_link(lane["links"]), "owner": owner}
        if lane["state"] == "review":
            if lane["source"] not in {"github", "t3"}:
                continue
            since_review = lane["state_since"] or lane["last_signal_at"]
            entry = person_waits(lane["owner_person_id"], since_review)
            if entry is not None:
                if lane["source"] == "github":
                    entry["reviews"].append({**item, "hours": hours(since_review)})
                else:
                    entry["your_moves"] += 1
        elif lane["state"] == "blocked" or lane["stale"]:
            stuck.append({**item, "hours_since_signal": age, "last_event": lane["last_event"] or ""})
        else:
            running[owner] = running.get(owner, 0) + 1
    return {
        "team": config.team_name,
        "people": {p["name"]: p["slack_id"] for p in members},
        "shipped": [{
            "title": r["title"], "link": web_link(r["links"]), "source": r["source"],
            "state": r["state"],
            "owner": owners.get(r["owner_person_id"], {}).get("name", "Unassigned"),
        } for r in shipped],
        "shipped_total": total, "shipped_steps": steps,
        "closed_not_done": closed_not_done,
        "recent_openings": await recent_openings(conn, "team_digest"),
        "stuck": stuck,
        "waiting": sorted(waiting.values(), key=lambda r: (-r["oldest_hours"], r["key"])),
        "in_progress": running,
        "faults": await read_faults(conn, now, since, config.brain.team_credential),
    }
