"""Build one personal snapshot for briefs and attention tools."""

from datetime import timedelta

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
        return "A background job of Illo stopped." if silent else "A background job of Illo failed."
    return {
        "brain": "The model service failed its last run.",
        "draft": "Illo could not confirm the delivery of a draft.",
        "drafts": "Illo could not send or update a draft card.",
        "asks": "Illo could not send or update an ask card.",
        "github": "The last GitHub request failed.",
        "slack": "Illo could not confirm the Slack connection or channel access.",
        "heartbeat": "Illo stopped its regular health report.",
        "collector": "A collector stopped its regular report."
        if silent
        else "A collector failed its last report.",
        "connector": "A connected service failed its last request.",
        "email": "Illo could not read email.",
        "notes": "Illo could not update a note.",
        "workspace": "Illo could not save a workspace change.",
    }.get(family, "An Illo service failed its last check.")


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
        ORDER BY a.due_at NULLS LAST,a.id
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
    lanes = await all_rows(
        conn,
        """
        SELECT l.*,(l.last_signal_at<%s-COALESCE((%s::jsonb->>l.source)::integer,30)
          *interval '1 minute') AS stale,
          (SELECT e.text FROM lane_events e WHERE e.lane_id=l.id ORDER BY e.at DESC,e.id DESC LIMIT 1) AS last_event
        FROM lanes l WHERE owner_person_id=%s AND state IN ('review','working','blocked')
        ORDER BY last_signal_at,id
    """,
        (now, Jsonb(limits), person["id"]),
    )
    stuck, running = [], 0
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
            needs.append({**item, "kind": "review", "age_hours": age})
        elif lane["state"] == "blocked" or lane["stale"]:
            stuck.append(item)
        else:
            running += 1
    state = await one(
        conn, "SELECT last_run_at FROM routine_state WHERE key=%s", (f"digest:{person['key']}",)
    )
    finished = await all_rows(
        conn,
        """
        SELECT title,links,state,count(*) OVER () AS total FROM lanes
        WHERE owner_person_id=%s AND state IN ('done','failed')
          AND closed_at>COALESCE(%s,%s-interval '1 day') ORDER BY closed_at DESC,id DESC LIMIT 20
    """,
        (person["id"], state["last_run_at"] if state else None, now),
    )
    last_brief = (state["last_run_at"] if state else None) or now - timedelta(days=1)
    names = {
        row["key"]: row["name"]
        for row in await all_rows(conn, "SELECT key,name FROM people WHERE active")
    }
    faults = []
    for row in await all_rows(conn, "SELECT * FROM sensors ORDER BY key"):
        if row["key"].startswith("chatgpt:") and row["key"] != "chatgpt:" + person["key"]:
            continue
        if fault := sensor_fault(row, now, names, last_brief):
            faults.append(fault)
    return {
        "name": person["name"],
        "needs_you": needs,
        "stuck": stuck,
        "in_progress": running,
        "finished_total": finished[0]["total"] if finished else 0,
        "finished": [
            {"title": row["title"], "link": web_link(row["links"]), "state": row["state"]}
            for row in finished
        ],
        "faults": faults,
    }
