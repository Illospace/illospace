"""A private, read-only list page. It has no script, cookie, or outside asset."""

import hashlib
import hmac
import html
import logging
import secrets
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo

from fastapi import Request
from fastapi.responses import HTMLResponse

from illo.connectors.oauth import public_url
from illo.db import one
from illo.ledger import tasks

HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Robots-Tag": "noindex",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
}
STATES = {
    "todo": "to do",
    "working": "in work",
    "review": "in review",
    "done": "done",
    "cancelled": "cancelled",
}
STYLE = """
:root {color-scheme:light dark; font:1rem/1.5 system-ui,sans-serif; background:#fff; color:#17202a}
body {max-width:60rem; margin:auto; padding:1rem; overflow-wrap:anywhere}
a {color:#145db5} nav {display:flex; flex-wrap:wrap; gap:1rem; margin-block:1rem}
ul {list-style:none; padding-left:1rem} main>ul {padding:0}
li {border-left:2px solid #bec7cf; padding-left:.75rem; margin-block:1rem}
h1,h2,h3,p {margin-block:.5rem} h3 {font-size:1rem} .facts {font-size:.9rem}
@media (prefers-color-scheme:dark) {:root {background:#121820; color:#edf1f5} a {color:#9dc8ff} li {border-color:#586677}}
"""


def escape(value):
    return html.escape(str(value), quote=True)


def digest(key):
    return hashlib.sha256(key.encode()).hexdigest()


async def issue(conn, person_id):
    person = await one(conn, "SELECT id FROM people WHERE id=%s AND active", (person_id,))
    if not person:
        raise ValueError("The list link needs an active person.")
    key = secrets.token_urlsafe(32)
    await conn.execute(
        "INSERT INTO list_links(key_hash,person_id,expires_at) VALUES (%s,%s,clock_timestamp()+interval '24 hours')",
        (digest(key), person_id),
    )
    return key


async def revoke(conn, person_id):
    await conn.execute("DELETE FROM list_links WHERE person_id=%s", (person_id,))


async def person_for_key(conn, key):
    if not key or len(key) > 100:
        return None
    hashed = digest(key)
    row = await one(
        conn,
        """SELECT l.key_hash,p.* FROM list_links l JOIN people p ON p.id=l.person_id
        WHERE l.key_hash=%s AND l.expires_at>clock_timestamp() AND p.active""",
        (hashed,),
    )
    if not hmac.compare_digest(hashed, row["key_hash"] if row else "0" * 64):
        return None
    return row


def safe_link(url):
    try:
        parts = urlsplit(url)
        return bool(
            parts.hostname
            and not parts.username
            and not parts.password
            and (
                url.startswith("https://")
                or (
                    url.startswith("http://")
                    and parts.hostname in {"localhost", "127.0.0.1", "::1"}
                )
            )
        )
    except ValueError:
        return False


def actor_name(workspace, ref):
    if not ref:
        return "nobody"
    kind, _, key = ref.partition(":")
    entries = workspace.agents if kind == "agent" else workspace.people
    return next((p.name for p in entries if p.key == key), ref)


def document(body):
    return (
        '<!doctype html><html lang="en"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>The task list</title><style>" + STYLE + "</style><body>" + body + "</body></html>"
    )


async def render(ctx, conn, person, key, view, list_key):
    workspace = ctx.workspace
    viewer = tasks.Viewer(person["key"], "person:" + person["key"], human=True)
    visible = tasks.visible_lists(workspace, viewer)
    allowed = {entry.key for entry in visible}
    # Use the ledger's identity and visibility checks for every page of results.
    rows, offset = [], 0
    while True:
        page = await tasks.search(conn, viewer=viewer, limit=100, offset=offset)
        rows.extend(row for row in page if row["list_key"] in allowed)
        if len(page) < 100:
            break
        offset += len(page)
    now = datetime.now(UTC)
    rows = [
        row
        for row in rows
        if (not list_key or row["list_key"] == list_key)
        and row["state"] != "cancelled"
        and (row["state"] != "done" or row["done_at"] and row["done_at"] >= now - timedelta(days=7))
    ]
    by_id = {row["id"]: row for row in rows}
    if view == "mine":
        refs = {"person:" + person["key"]} | {
            "agent:" + a.key for a in workspace.agents if a.owner == person["key"]
        }
        selected = {
            row["id"]
            for row in rows
            if row["holder"] in refs or row["waits_for"] == "person:" + person["key"]
        }
        for task_id in list(selected):
            parent = by_id[task_id]["parent_id"]
            while parent in by_id and parent not in selected:
                selected.add(parent)
                parent = by_id[parent]["parent_id"]
        rows = [row for row in rows if row["id"] in selected]
    children = defaultdict(list)
    ids = {row["id"] for row in rows}
    roots = []
    for row in sorted(rows, key=lambda r: r["id"]):
        if row["parent_id"] in ids:
            children[row["parent_id"]].append(row)
        else:
            roots.append(row)

    def link(label, **params):
        return (
            '<a href="/list?'
            + escape(urlencode({"k": key, **params}))
            + '">'
            + escape(label)
            + "</a>"
        )

    nav = link("All tasks") + link("Mine", view="mine")
    nav += "".join(
        link(entry.name, **({"view": "mine"} if view == "mine" else {}), list=entry.key)
        for entry in visible
    )

    def task_html(row):
        facts = [
            "Holder: " + actor_name(workspace, row["holder"]),
            "State: " + STATES[row["state"]],
            "Act now: " + actor_name(workspace, row["waits_for"]),
            f"Open pieces: {row['open_pieces']}",
        ]
        if row["due_at"]:
            facts.append(
                "Due: "
                + row["due_at"]
                .astimezone(ZoneInfo(person["timezone"]))
                .strftime("%Y-%m-%d %H:%M %Z")
            )
        if (row["holder"] or "").startswith("agent:") and (row.get("silent_hours") or 0) > 0:
            facts.append(f"Silent hours: {row['silent_hours']}")
        links = " ".join(
            '<a rel="noopener noreferrer nofollow" href="'
            + escape(item["url"])
            + '">'
            + escape(item.get("kind", "link"))
            + ": "
            + escape(item["url"])
            + "</a>"
            for item in row["links"]
            if safe_link(item.get("url", ""))
        )
        body = (
            f'<li><h3>#{row["id"]} {escape(row["title"])}</h3><p class="facts">'
            + escape(" · ".join(facts))
            + "</p>"
        )
        if links:
            body += "<p>" + links + "</p>"
        if children[row["id"]]:
            body += "<ul>" + "".join(task_html(child) for child in children[row["id"]]) + "</ul>"
        return body + "</li>"

    def groups(done):
        text = ""
        for entry in visible:
            group = [
                row
                for row in roots
                if row["list_key"] == entry.key and (row["state"] == "done") == done
            ]
            if group:
                text += (
                    "<h2>"
                    + escape(entry.name)
                    + "</h2><ul>"
                    + "".join(task_html(row) for row in group)
                    + "</ul>"
                )
        return text or "<p>No tasks.</p>"

    return document(
        "<header><h1>The task list</h1><nav>"
        + nav
        + "</nav></header><main>"
        + groups(False)
        + "<h1>Done in the last 7 days</h1>"
        + groups(True)
        + "</main>"
    )


class ListLogFilter(logging.Filter):
    def filter(self, record):
        return "/list?" not in record.getMessage()


async def setup(ctx):
    for name in ("uvicorn.access", "httpx", "httpcore"):
        logging.getLogger(name).addFilter(ListLogFilter())

    async def route(request: Request):
        key = request.query_params.get("k", "")
        async with ctx.pool.connection() as conn:
            person = await person_for_key(conn, key)
            if not person:
                return HTMLResponse(
                    document("<p>This list link is invalid or expired.</p>"),
                    status_code=404,
                    headers=HEADERS,
                )
            body = await render(
                ctx,
                conn,
                person,
                key,
                request.query_params.get("view", ""),
                request.query_params.get("list", ""),
            )
        return HTMLResponse(body, headers=HEADERS)

    ctx.app.add_api_route("/list", route, methods=["GET"])

    async def expire():
        async with ctx.pool.connection() as conn:
            await conn.execute("DELETE FROM list_links WHERE expires_at<clock_timestamp()")

    ctx.every(300, expire)


async def list_link(tc, args):
    from illo.connectors.chat import active_person, dm
    from illo.tools._person import run_person_id

    person = await active_person(tc.conn, await run_person_id(tc))
    if args["revoke"]:
        await revoke(tc.conn, person["id"])
        return "Your list links are revoked."
    key = await issue(tc.conn, person["id"])
    url = public_url(tc.app) + "/list?" + urlencode({"k": key})
    if tc.run["profile"] == "team_channel":
        await dm(tc.app, person, "Your task list: " + url + "\nThis link expires in 24 hours.")
        return "The list link is in your DM. It expires in 24 hours."
    await tc.conn.execute("UPDATE runs SET personal_read=true WHERE id=%s", (tc.run["id"],))
    tc.run["personal_read"] = True
    return url
