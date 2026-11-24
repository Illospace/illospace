"""Browser pairing uses Slack consent but has no agent token or OAuth grant."""

import hashlib
import hmac
import re
import secrets
from urllib.parse import urlencode
from uuid import NAMESPACE_URL, uuid5

from illo.asks.cards import button, escape, section
from illo.channels.slack.pairing import sent_by_app
from illo.connectors.http import private_http_logs
from illo.db import one
from illo.ledger import ask_workflow, tasks
from illo.ledger.agent_oauth import PAIR_ALPHABET, limit
from illo.ledger.tokens import digest
from illo.tasks.pages import base_url, enabled, person_path

REQUEST_COOKIE = "illo_web_request"
SESSION_COOKIE = "illo_web_session"
MAX_PENDING = 200
STARTS_PER_HOUR = 60
STARTS_PER_HOST = 30
LINKS_PER_HOUR = 20


def next_path(value):
    # Only local pages and board frame targets can follow sign-in.
    return value if re.fullmatch(r"(?:/(?:t|web/a)/[1-9][0-9]*/?|/web/board(?:#[ta][1-9][0-9]*|/t/[1-9][0-9]{0,18})?)", value or "") else ""


def pairing_line(handle):
    raw = hashlib.sha256(("pair:" + handle).encode()).digest()
    code = "".join(PAIR_ALPHABET[n % len(PAIR_ALPHABET)] for n in raw[:8])
    return code[:4] + "-" + code[4:]


async def start(conn, host, destination):
    await conn.execute("SELECT pg_advisory_xact_lock(18000001)")
    allowed = await limit(conn, "web_start", "all", STARTS_PER_HOUR)
    host_allowed = await limit(conn, "web_start_host", digest(host), STARTS_PER_HOST)
    count = await one(conn, """SELECT count(*) AS n FROM web_signins
        WHERE expires_at>clock_timestamp() AND continued_at IS NULL AND decision='pending'""")
    if not allowed or not host_allowed or count["n"] >= MAX_PENDING:
        return None
    # The request handle carries 256 random bits. Only this browser can exchange the approval.
    handle = secrets.token_urlsafe(32)
    await conn.execute("""INSERT INTO web_signins(pairing_hash,browser_hash,next_path)
        VALUES (%s,%s,%s)""", (digest(pairing_line(handle).replace("-", "")),
                              digest(handle), next_path(destination)))
    return handle


async def request(conn, handle, *, lock=False):
    if not handle or len(handle) > 100:
        return None
    return await one(conn, """SELECT *,expires_at<=clock_timestamp() AS expired FROM web_signins
        WHERE browser_hash=%s""" + (" FOR UPDATE" if lock else ""), (digest(handle),))


async def session(conn, token):
    if not token or len(token) > 100:
        return None
    row = await one(conn, """UPDATE web_sessions s SET last_seen_at=clock_timestamp()
        FROM people p WHERE s.token_hash=%s AND s.person_id=p.id AND p.active
        AND s.expires_at>clock_timestamp() RETURNING p.*,s.token_hash""", (digest(token),))
    return row


def csrf(token):
    return digest("csrf:" + token)


def valid_csrf(token, value):
    return bool(token and hmac.compare_digest(csrf(token).encode(), value.encode()))


async def continue_session(conn, handle):
    row = await request(conn, handle, lock=True)
    if not row or row["expired"] or row["decision"] != "approved" or row["continued_at"]:
        return None
    person = await one(conn, "SELECT id FROM people WHERE id=%s AND active FOR SHARE", (row["person_id"],))
    if not person:
        return None
    token = secrets.token_urlsafe(32)
    await conn.execute("INSERT INTO web_sessions(token_hash,person_id) VALUES (%s,%s)",
                       (digest(token), person["id"]))
    await conn.execute("UPDATE web_signins SET continued_at=clock_timestamp() WHERE id=%s", (row["id"],))
    return token, row["next_path"]


async def receive(ctx, raw, code):
    if not enabled(ctx):
        return False
    async with ctx.pool.connection() as conn:
        exists = await one(conn, "SELECT id FROM web_signins WHERE pairing_hash=%s", (digest(code),))
        if not exists:
            return False
        person = await one(conn, "SELECT * FROM people WHERE slack_id=%s AND active", (raw.get("user", ""),))
        row = None
        if person and raw.get("channel", "").startswith("D") and await limit(conn, "web_pair", str(person["id"]), 10):
            prior = await one(conn, """SELECT *,expires_at>clock_timestamp() AS valid FROM web_signins
                WHERE person_id=%s AND slack_channel=%s AND slack_event_ts=%s""",
                (person["id"], raw["channel"], raw["ts"]))
            if prior and (prior["slack_ts"] or not prior["valid"] or prior["decision"] != "pending"):
                return True
            row = prior or await one(conn, """UPDATE web_signins SET person_id=%s,
                paired_at=clock_timestamp(),slack_channel=%s,slack_event_ts=%s
                WHERE id=%s AND paired_at IS NULL AND decision='pending'
                AND expires_at>clock_timestamp() RETURNING *""",
                (person["id"], raw["channel"], raw["ts"], exists["id"]))
    if not row:
        await ctx.slack_client.chat_postMessage(channel=raw["channel"], text="The code is invalid, used, or expired. Start again in the browser.")
        return True
    text = (f"A browser wants to sign in to Illo's pages as {person['name']}.\n"
            + ("An app sent this code in your name.\n" if sent_by_app(raw.get("text", "")) else "")
            + "Approve only if you started this yourself a moment ago.")
    blocks = [{"type": "section", "text": {"type": "plain_text", "text": text}},
              {"type": "actions", "elements": [
                  button("Approve", f"web-signin:{row['id']}:approve", True),
                  button("Deny", f"web-signin:{row['id']}:deny")]}]
    sent = await ctx.slack_client.chat_postMessage(channel=raw["channel"], text=text, blocks=blocks,
        unfurl_links=False, unfurl_media=False,
        client_msg_id=str(uuid5(NAMESPACE_URL, f"illo:web-signin:{row['id']}")))
    async with ctx.pool.connection() as conn:
        await conn.execute("UPDATE web_signins SET slack_ts=%s WHERE id=%s", (sent["ts"], row["id"]))
    return True


async def action(ctx, person, action):
    if not enabled(ctx):
        return
    match = re.fullmatch(r"web-signin:([1-9][0-9]*):(approve|deny)", action.get("action_id", ""))
    channel = action.get("channel", "")
    if not match or not channel.startswith("D"):
        return
    request_id, choice = match.groups()
    async with ctx.pool.connection() as conn:
        row = await one(conn, """UPDATE web_signins SET decision=%s WHERE id=%s AND person_id=%s
            AND slack_channel=%s AND slack_ts=%s AND decision='pending'
            AND expires_at>clock_timestamp() AND paired_at IS NOT NULL
            AND EXISTS (SELECT 1 FROM people WHERE id=%s AND active) RETURNING id""",
            ("approved" if choice == "approve" else "denied", int(request_id), person["id"],
             channel, action.get("message_ts"), person["id"]))
    if row:
        text = "The browser sign-in is approved. Return to the browser." if choice == "approve" else "The browser sign-in is denied."
        await ctx.slack_client.chat_update(channel=channel, ts=action["message_ts"], text=text,
            blocks=[{"type": "section", "text": {"type": "plain_text", "text": text}}])


async def link(conn, ctx, person, task_id, *, ask=False, board=False):
    """Make a key that signs in one browser as this person. The person asked for it in Slack."""
    if not await limit(conn, "web_link", str(person["id"]), LINKS_PER_HOUR):
        return None
    # The click in Slack is the approval, so the row starts as approved. The key works one time,
    # in the same exchange as the handle of a browser that got an approval. No pairing code matches it.
    key = secrets.token_urlsafe(32)
    destination = (f"/web/board/t/{task_id}" if board
                   else await person_path(conn, ctx.workspace, person, task_id, ask=ask))
    await conn.execute("""INSERT INTO web_signins(pairing_hash,browser_hash,next_path,person_id,paired_at,decision)
        VALUES (%s,%s,%s,%s,clock_timestamp(),'approved')""",
        (digest("link:" + key), digest(key), destination, person["id"]))
    return key


async def open_action(ctx, person, action):
    """Answer the button "Open the page" with a private link. Slack proved who pressed the button."""
    match = re.fullmatch(r"web-open(-(ask|board))?:([1-9][0-9]{0,18})", action.get("action_id", ""))
    if not enabled(ctx) or not match or not action.get("trigger_id"):
        return
    ask, board_page = match[2] == "ask", match[2] == "board"
    task_id = int(match[3])
    if task_id >= 2**63:
        return
    async with ctx.pool.connection() as conn:
        viewer = tasks.Viewer(person["key"], "person:" + person["key"], human=True)
        if board_page:
            from illo.board import acl, policy

            channel, root = action.get("channel"), action.get("message_ts")
            if not policy.board_pages_ready(ctx) or not channel or not channel.startswith("D") or not root:
                return
            thread = await acl.thread_access(conn, task_id)
            visible = thread and await acl.recipient_ok(conn, thread, person) and await one(conn, """
                SELECT 1 FROM board_escalations e LEFT JOIN asks a ON a.id=e.ask_id
                WHERE e.thread_id=%s AND e.person_id=%s AND e.recipient_slack_user=%s
                AND ((e.slack_channel=%s AND e.slack_ts=%s) OR (a.slack_channel=%s AND a.slack_ts=%s))""",
                (task_id, person["id"], person["slack_id"], channel, root, channel, root))
        else:
            visible = (await ask_workflow.page_read(conn, task_id, person['id']) if ask
                       else await tasks.get(conn, task_id, viewer=viewer))
        if not visible:
            return
        key = await link(conn, ctx, person, task_id, ask=ask, board=board_page)
    address = base_url(ctx) + "/web/signin?" + urlencode({"key": key or ""})
    kind = "board thread" if board_page else "request" if ask else "task"
    label = f"Open board thread {task_id}" if board_page else f"Open the page of {kind} {task_id}"
    text = ("You asked for too many links. Try again in one hour." if not key else
            f"*<{escape(address)}|{label}>*\n"
            f"This link signs in one browser as {escape(person['name'])}. It works one time, for 10 minutes. "
            "Do not give it to another person.")
    try:
        # A modal is private to the person who pressed the button, and it leaves no message with a key.
        with private_http_logs():
            await ctx.slack_client.views_open(trigger_id=action["trigger_id"], view={
                "type": "modal", "title": {"type": "plain_text", "text": "Board thread" if board_page else "Request page" if ask else "Task page"},
                "close": {"type": "plain_text", "text": "Close"}, "blocks": [section(text)]})
    except Exception:
        # Slack accepts a modal for only 3 seconds after the click. End the link that no person got.
        if key:
            async with ctx.pool.connection() as conn:
                await conn.execute("DELETE FROM web_signins WHERE browser_hash=%s", (digest(key),))
        if action.get("channel"):
            await ctx.slack_client.chat_postEphemeral(channel=action["channel"], user=person["slack_id"],
                text="Slack did not show the link in time. Press Open the page again.",
                **({"thread_ts": action["thread_ts"]} if action.get("thread_ts") else {}))


async def cleanup(ctx):
    async with ctx.pool.connection() as conn:
        await conn.execute("DELETE FROM web_signins WHERE expires_at<clock_timestamp()")
        await conn.execute("DELETE FROM web_sessions WHERE expires_at<clock_timestamp()")
        await conn.execute("DELETE FROM agent_oauth_limits WHERE kind LIKE 'web_%%' AND started_at<clock_timestamp()-interval '1 day'")
