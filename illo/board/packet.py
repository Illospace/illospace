"""Build the bounded data packet and its text-free validation snapshot."""

import json
import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from illo.board import acl
from illo.board.labels import actor_label
from illo.db import all_rows, one
from illo.ledger import board
from illo.tasks.release import in_window
from illo.tools._format import data_text


async def group_counts(conn, thread):
    return await all_rows(conn, f"""SELECT b.group_key AS key,
        count(*) FILTER (WHERE b.state='open') AS open_threads
        FROM board_threads b WHERE b.group_key IS NOT NULL
        AND (b.state='open' OR b.closed_at>now()-interval '30 days')
        AND ((b.visibility='team' AND NOT EXISTS ({board.PRIVATE_TASKS}))
            OR (%(owner)s::bigint IS NOT NULL AND {board.visible_to('%(owner)s')}))
        GROUP BY b.group_key ORDER BY open_threads DESC,b.group_key""",
        {"owner": thread["effective_owner"]})


async def asker_post(conn, episode):
    if episode["post_id"] is not None:
        return await one(conn, "SELECT * FROM board_posts WHERE id=%s", (episode["post_id"],))
    return await one(conn, """SELECT * FROM board_posts WHERE thread_id=%s
        AND NOT is_human AND actor<>'illo' ORDER BY id LIMIT 1""", (episode["thread_id"],))


def age_minutes(now, at):
    return max(0, int((now - at).total_seconds() / 60)) if at else None


def _people_keys(workspace, thread, author, people):
    if thread["effective_owner"] is not None:
        return [p["key"] for p in people.values() if p["id"] == thread["effective_owner"]]
    task = thread["task"] or {}
    owners = {f"agent:{a.key}": f"person:{a.owner}" for a in workspace.agents}
    author = author or {}
    mentions = [owners.get(key, key) for key in author.get("mentions", [])]
    asker = author.get("provenance", {}).get("person_key")
    list_entry = next((row for row in workspace.lists if row.key == task.get("list_key")), None)
    title = thread["title"] + " " + (list_entry.name if list_entry else "")
    words = set(re.findall(r"\w+", title.casefold()))
    areas = [f"person:{p['key']}" for p in people.values()
             if words.intersection(re.findall(r"\w+", " ".join(p["areas"]).casefold()))]
    return [key[7:] for key in dict.fromkeys([
        *mentions, task.get("holder"), task.get("created_by"), f"person:{asker}",
        *areas, f"person:{workspace.config.default_owner}",
    ]) if key and key.startswith("person:")]


def wakeable(workspace, target):
    return workspace.config.board.wake_webhooks and any(
        f"agent:{a.key}" == target and a.wake.type == "webhook" for a in workspace.agents)


async def _agents(conn, workspace, thread, person, author, now, posted):
    cfg = workspace.config.board.coordinator
    targets = [(f"agent:{a.key}", a.name) for a in workspace.agents if a.owner == person["key"]]
    targets.append((f"person:{person['key']}", f"{person['name'][:80]}'s agents"))
    result = []
    for target, name in targets:
        row = await acl.target_ok(conn, workspace, thread, target, author)
        if row:
            seen = age_minutes(now, row["last_used_at"])
            result.append({"target": target, "label": name[:100],
                           "online": bool(row["last_used_at"] and
                                          row["last_used_at"] >= now - timedelta(minutes=cfg.presence_minutes)),
                           "last_seen_minutes": seen, "posted_here": target in posted,
                           "wakeable": wakeable(workspace, target)})
    result.sort(key=lambda row: (not (row["online"] or row["wakeable"]), row["target"]))
    return result[:5]


async def _candidates(conn, workspace, thread, author, people, posts, now):
    posted = {p["actor"] if p["provenance"].get("agent_key")
              else "person:" + p["provenance"].get("person_key", "") for p in posts if not p["is_human"]}
    result = []
    for key in _people_keys(workspace, thread, author, people):
        person = people.get(key)
        if not await acl.recipient_ok(conn, thread, person):
            continue
        n = await one(conn, """SELECT count(*) AS n FROM board_escalations
            WHERE person_id=%s AND created_at>now()-interval '24 hours'""", (person["id"],))
        result.append({"target": f"person:{key}", "name": person["name"][:100],
                       "areas": [area[:40] for area in person["areas"][:6]],
                       "local_time": now.astimezone(ZoneInfo(person["timezone"])).strftime("%H:%M"),
                       "in_hours": in_window(workspace, person, now), "escalations_today": n["n"],
                       "agents": await _agents(conn, workspace, thread, person, author, now, posted)})
        if len(result) == 5:
            break
    return result


def has_picture(post):
    return any(block["kind"] == "image" or block.get("image") for block in post["blocks"])


def _encode(packet, limit):
    def encode():
        return "Decide the next step for this board item.\n" + data_text(
            "board-thread", json.dumps(packet, ensure_ascii=True, separators=(",", ":")))

    text = encode()
    pending_id = packet["pending"][0]["post_id"]
    while len(text) > limit and packet["posts"]:
        if len(packet["posts"]) == 1 and packet["posts"][0]["id"] == pending_id:
            packet["posts"][0]["text"] = ""
            break
        index = int(packet["posts"][0]["id"] == pending_id)
        packet["posts"].pop(index)
        text = encode()
    while len(text := encode()) > limit and packet["groups"]:
        packet["groups"].pop()
    while len(text) > limit and packet["candidates"]:
        packet["candidates"].pop()
        text = encode()
    return text if len(text) <= limit else None


async def build(conn, workspace, thread, episode):
    now = datetime.now(UTC)
    cfg = workspace.config.board.coordinator
    author = await asker_post(conn, episode)
    posts = await all_rows(conn, """SELECT * FROM (
        (SELECT * FROM board_posts WHERE thread_id=%s ORDER BY id DESC LIMIT 12)
        UNION SELECT * FROM board_posts WHERE id=%s
        ) p ORDER BY id""", (thread["id"], episode["post_id"]))
    people = {p["key"]: p for p in await all_rows(conn, "SELECT * FROM people ORDER BY key")}
    candidates = await _candidates(conn, workspace, thread, author, people, posts, now)
    notice = (not cfg.slack_summaries or any(has_picture(p) for p in posts[-12:]) or
              any(b["kind"] == "preview" for b in (author or {}).get("blocks", [])))
    allowed = {"formats": ["link"] if notice else ["dm", "card", "link"],
               "headstart": episode["reason"] != "turn_cap" and not episode["headstart_target"]
               and any(a["online"] or a["wakeable"] for c in candidates for a in c["agents"]),
               "max_headstart_minutes": cfg.max_headstart_minutes}
    groups = (await group_counts(conn, thread))[:30]
    task = thread["task"]
    n = await one(conn, "SELECT count(*) AS n,max(id) AS last FROM board_posts WHERE thread_id=%s",
                  (thread["id"],))
    packet = {
        "now_utc": now.isoformat(),
        "thread": {**{key: thread[key] for key in ("id", "title", "state", "stalled_reason")},
                   "visibility": "private" if thread["effective_owner"] is not None else "team",
                   "group_key": thread["group_key"], "posts_total": n["n"],
                   "task": {"id": task["id"], "title": task["title"][:200]} if task else None,
                   "ticket": (thread["ticket_url"] or "").removeprefix("https://github.com/").replace("/issues/", "#"),
                   "headstart_tried": [episode["headstart_target"]] if episode["headstart_target"] else []},
        "pending": [{"item": episode["id"], "reason": episode["reason"], "post_id": episode["post_id"],
                     "age_minutes": age_minutes(now, episode["stalled_at"] or (author or {}).get("at")),
                     "from": actor_label(workspace, people, author).name[:100] if author else "Agent",
                     "mentions": (author or {}).get("mentions", []), "attempt": episode["attempts"] + 1,
                     "state": episode["state"]}],
        "posts": [{"id": p["id"], "from": label.name[:100], "from_kind": label.kind,
                   "kind": p["kind"], "reply_to": p["reply_to_post_id"],
                   "age_minutes": age_minutes(now, p["at"]), "text": p["text"][:600],
                   "has_pictures": has_picture(p)}
                  for p in posts for label in (actor_label(workspace, people, p),)],
        "candidates": candidates, "allowed": allowed, "groups": groups,
    }
    text = _encode(packet, min(12000, workspace.config.run_limits.max_message_chars))
    targets = {a["target"]: {k: a[k] for k in ("online", "wakeable", "last_seen_minutes")}
               for c in candidates for a in c["agents"] if a["online"] or a["wakeable"]}
    return text, {"acl": acl.scope(thread), "episode_id": episode["id"], "version": episode["version"],
                  "reason": episode["reason"], "candidates": [c["target"] for c in candidates],
                  "targets": targets, "allowed": allowed, "groups": [g["key"] for g in groups],
                  "max_post_id": n["last"]}
