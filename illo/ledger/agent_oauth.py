"""Agent sign-in state. Callers commit each short step before any outside call."""

import base64
import hashlib
import hmac
import secrets
from urllib.parse import urlsplit

from illo.db import one
from illo.ledger import audit
from illo.ledger.tokens import digest

REGISTRATIONS_PER_HOUR = 60
MAX_CLIENTS = 1000
MAX_PENDING = 200
MAX_PENDING_PER_CLIENT = 10
PAIR_ATTEMPTS = 10
REFRESH_RETRY_S = 60
PAIR_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def challenge(verifier):
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


async def record(conn, event, *, client_id=None, request_id=None, token_id=None, person_id=None):
    await audit.write(
        conn, principal=f"person-id:{person_id}" if person_id else "oauth:agent",
        tool=f"agent_oauth.{event}", args={}, decision="deny" if event in {"deny", "reuse"} else "allow",
        reason="The agent sign-in state changed", token_id=token_id,
        target=f"request:{request_id}" if request_id else f"client:{client_id}",
    )


async def limit(conn, kind, key, maximum, *, seconds=3600):
    row = await one(conn, """
        INSERT INTO agent_oauth_limits (kind,key) VALUES (%s,%s)
        ON CONFLICT (kind,key) DO UPDATE SET
          attempts=CASE WHEN agent_oauth_limits.started_at <= clock_timestamp()-make_interval(secs=>%s)
            THEN 1 ELSE agent_oauth_limits.attempts+1 END,
          started_at=CASE WHEN agent_oauth_limits.started_at <= clock_timestamp()-make_interval(secs=>%s)
            THEN clock_timestamp() ELSE agent_oauth_limits.started_at END
        RETURNING attempts
    """, (kind, key, seconds, seconds))
    return row["attempts"] <= maximum


async def cleanup(conn):
    # Keep used code and refresh hashes. A replay must still revoke its connection.
    await conn.execute("""DELETE FROM agent_oauth_requests
        WHERE token_id IS NULL AND expires_at < clock_timestamp()-interval '1 day'""")
    await conn.execute("""DELETE FROM agent_oauth_clients
        WHERE approved_at IS NULL AND created_at < clock_timestamp()-interval '24 hours'""")
    await conn.execute("DELETE FROM agent_oauth_access WHERE expires_at <= clock_timestamp()")
    await conn.execute("""DELETE FROM agent_oauth_limits
        WHERE started_at < clock_timestamp()-interval '1 day'""")


async def register(conn, name, redirects):
    await conn.execute("SELECT pg_advisory_xact_lock(697637150)")
    await cleanup(conn)
    if not await limit(conn, "register", "all", REGISTRATIONS_PER_HOUR):
        return {"error": "temporarily_unavailable"}
    if (await one(conn, "SELECT count(*) AS n FROM agent_oauth_clients"))["n"] >= MAX_CLIENTS:
        return {"error": "temporarily_unavailable"}
    row = await one(conn, """INSERT INTO agent_oauth_clients (id,name,redirect_uris)
        VALUES (%s,%s,%s) RETURNING *""", (secrets.token_urlsafe(24), name, redirects))
    await record(conn, "registration", client_id=row["id"])
    return row


async def request(conn, client_id, redirect, resource, state, pkce):
    await conn.execute("SELECT pg_advisory_xact_lock(697637151)")
    counts = await one(conn, """SELECT count(*) AS n,
        count(*) FILTER (WHERE client_id=%s) AS client_n FROM agent_oauth_requests
        WHERE expires_at>clock_timestamp() AND continued_at IS NULL""", (client_id,))
    if counts["n"] >= MAX_PENDING or counts["client_n"] >= MAX_PENDING_PER_CLIENT:
        return {"error": "temporarily_unavailable"}
    code = "".join(secrets.choice(PAIR_ALPHABET) for _ in range(8))
    handle = secrets.token_urlsafe(32)
    row = await one(conn, """INSERT INTO agent_oauth_requests
        (client_id,redirect_uri,resource,state,challenge,pairing_hash,browser_hash)
        VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
        (client_id, redirect, resource, state, pkce, digest(code), digest(handle)))
    await record(conn, "request", request_id=row["id"])
    return {"code": code[:4] + "-" + code[4:], "handle": handle}


async def browser_request(conn, handle, *, lock=False):
    row = await one(conn, """SELECT r.*,r.expires_at<=clock_timestamp() AS expired
        FROM agent_oauth_requests r WHERE browser_hash=%s""" + (" FOR UPDATE" if lock else ""),
        (digest(handle),))
    if row and secrets.compare_digest(row["browser_hash"], digest(handle)):
        if lock:
            row["expired"] = (await one(conn, "SELECT %s::timestamptz<=clock_timestamp() AS expired", (row["expires_at"],)))["expired"]
        return row
    return None


async def pair(conn, code, slack_id, channel, event_ts):
    person = await one(conn, "SELECT * FROM people WHERE slack_id=%s AND active", (slack_id,))
    if not person or not await limit(conn, "pair", str(person["id"]), PAIR_ATTEMPTS):
        return None
    # A duplicate Slack delivery does not consume the code twice or send a second card.
    prior = await one(conn, """SELECT r.*,c.name,r.expires_at>clock_timestamp() AS valid
        FROM agent_oauth_requests r JOIN agent_oauth_clients c ON c.id=r.client_id
        WHERE person_id=%s AND slack_channel=%s AND slack_event_ts=%s""", (person["id"], channel, event_ts))
    if prior:
        return prior if prior["slack_ts"] is None and prior["valid"] and prior["decision"] == "pending" else {"duplicate": True}
    row = await one(conn, """UPDATE agent_oauth_requests SET person_id=%s,paired_at=clock_timestamp(),
        slack_channel=%s,slack_event_ts=%s WHERE pairing_hash=%s AND paired_at IS NULL
        AND decision='pending' AND expires_at>clock_timestamp() RETURNING *""",
        (person["id"], channel, event_ts, digest(code)))
    if not row or not secrets.compare_digest(row["pairing_hash"], digest(code)):
        return None
    client = await one(conn, "SELECT name FROM agent_oauth_clients WHERE id=%s", (row["client_id"],))
    return {**row, "name": client["name"]}


async def decide(conn, request_id, person_id, channel, message_ts, approve):
    row = await one(conn, """SELECT r.*,c.name FROM agent_oauth_requests r
        JOIN agent_oauth_clients c ON c.id=r.client_id
        WHERE r.id=%s AND r.person_id=%s AND r.slack_channel=%s AND r.slack_ts=%s
        AND EXISTS (SELECT 1 FROM people WHERE id=%s AND active) FOR UPDATE OF r""",
        (request_id, person_id, channel, message_ts, person_id))
    if not row:
        return None
    if row["decision"] != "pending":
        return "The connection is approved. Return to the app." if row["decision"] == "approved" else "The connection is denied."
    valid = await one(conn, "SELECT %s::timestamptz>clock_timestamp() AS valid", (row["expires_at"],))
    if not valid["valid"]:
        return "The sign-in request expired. Start again in the app."
    token_id = None
    if approve:
        # SHA-256 produces hex only. This sentinel cannot match any bearer digest.
        token = await one(conn, """INSERT INTO tokens (person_id,label,token_hash,scopes)
            VALUES (%s,%s,%s,ARRAY['agent']) RETURNING id""",
            (person_id, f"{row['name']} ({urlsplit(row['redirect_uri']).hostname})", "!agent-oauth:" + secrets.token_hex(32)))
        token_id = token["id"]
        await conn.execute("""UPDATE agent_oauth_clients SET approved_at=COALESCE(approved_at,clock_timestamp())
            WHERE id=%s""", (row["client_id"],))
    await conn.execute("UPDATE agent_oauth_requests SET decision=%s,token_id=%s WHERE id=%s",
                       ("approved" if approve else "denied", token_id, request_id))
    await record(conn, "approve" if approve else "deny", request_id=request_id,
                 token_id=token_id, person_id=person_id)
    return "The connection is approved. Return to the app." if approve else "The connection is denied."


async def continue_request(conn, handle):
    row = await browser_request(conn, handle, lock=True)
    if not row or row["continued_at"] or row["decision"] == "pending" and not row["expired"]:
        return None
    result = {"error": "access_denied"}
    active = await one(conn, """SELECT t.id FROM tokens t JOIN people p ON p.id=t.person_id
        WHERE t.id=%s AND t.revoked_at IS NULL AND p.active""", (row["token_id"],))
    if row["decision"] == "approved" and not row["expired"] and active:
        code = secrets.token_urlsafe(32)
        await conn.execute("""UPDATE agent_oauth_requests SET code_hash=%s,
            code_expires_at=clock_timestamp()+interval '2 minutes' WHERE id=%s""", (digest(code), row["id"]))
        result = {"code": code}
    await conn.execute("UPDATE agent_oauth_requests SET continued_at=clock_timestamp() WHERE id=%s", (row["id"],))
    if row["state"] is not None:
        result["state"] = row["state"]
    return row["redirect_uri"], result


def successor(parent, seed, kind):
    """Give the same next secret for the same refresh token. Only the token holder and the ledger together can do it."""
    value = hmac.new(parent.encode(), f"{kind}:{seed}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


async def issue(conn, token_id, client_id, resource, *, parent=None, seed=None):
    if parent:
        # A retry of one refresh gets the same pair again. It never starts a second chain.
        access, refresh = successor(parent, seed, "access"), successor(parent, seed, "refresh")
    else:
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    await conn.execute("""INSERT INTO agent_oauth_access (token_hash,token_id,resource,expires_at)
        VALUES (%s,%s,%s,clock_timestamp()+interval '1 hour') ON CONFLICT (token_hash) DO NOTHING""",
        (digest(access), token_id, resource))
    await conn.execute("""INSERT INTO agent_oauth_refresh (token_hash,token_id,client_id,resource,seed,expires_at)
        VALUES (%s,%s,%s,%s,%s,clock_timestamp()+interval '90 days') ON CONFLICT (token_hash) DO NOTHING""",
        (digest(refresh), token_id, client_id, resource, secrets.token_hex(32)))
    # A retry can come up to REFRESH_RETRY_S after the first reply. Report the shorter life.
    return {"access_token": access, "token_type": "Bearer", "expires_in": 3600 - REFRESH_RETRY_S if parent else 3600,
            "refresh_token": refresh, "scope": "agent"}


async def reuse(conn, row):
    await conn.execute("""UPDATE tokens SET revoked_at=clock_timestamp()
        WHERE id=%s AND revoked_at IS NULL""", (row["token_id"],))
    await record(conn, "reuse", token_id=row["token_id"], client_id=row["client_id"])
    return {"error": "invalid_grant"}


async def exchange(conn, form):
    row = await one(conn, """SELECT r.* FROM agent_oauth_requests r
        WHERE code_hash=%s FOR UPDATE""", (digest(form.get("code", "")),))
    if (not row or row["client_id"] != form.get("client_id")
            # PKCE binds the code. An app can leave out the redirect address and the resource.
            or form.get("redirect_uri") not in (None, row["redirect_uri"])
            or form.get("resource") not in (None, row["resource"])
            or not secrets.compare_digest(row["challenge"], challenge(form.get("code_verifier", "")))):
        return {"error": "invalid_grant"}
    token = await one(conn, """SELECT t.id FROM tokens t JOIN people p ON p.id=t.person_id
        WHERE t.id=%s AND t.revoked_at IS NULL AND p.active FOR UPDATE OF t""", (row["token_id"],))
    if row["exchanged_at"]:
        return await reuse(conn, row)
    valid = await one(conn, "SELECT %s::timestamptz>clock_timestamp() AS valid", (row["code_expires_at"],))
    if not valid["valid"] or not token:
        return {"error": "invalid_grant"}
    await conn.execute("UPDATE agent_oauth_requests SET exchanged_at=clock_timestamp() WHERE id=%s", (row["id"],))
    result = await issue(conn, row["token_id"], row["client_id"], row["resource"])
    await record(conn, "exchange", request_id=row["id"], token_id=row["token_id"])
    return result


async def refresh(conn, form):
    # Lock the stable connection first. Concurrent rotations and revocations serialize here.
    row = await one(conn, """SELECT t.id
        FROM agent_oauth_refresh f JOIN tokens t ON t.id=f.token_id
        WHERE f.token_hash=%s FOR UPDATE OF t""", (digest(form.get("refresh_token", "")),))
    if not row:
        return {"error": "invalid_grant"}
    # Check expiry and rotation with a new snapshot after the connection lock.
    row = await one(conn, """SELECT f.*,f.expires_at>clock_timestamp() AS valid,t.revoked_at,p.active,
          f.used_at>clock_timestamp()-make_interval(secs=>%s) AS retry
        FROM agent_oauth_refresh f JOIN tokens t ON t.id=f.token_id JOIN people p ON p.id=t.person_id
        WHERE f.token_hash=%s""", (REFRESH_RETRY_S, digest(form.get("refresh_token", ""))))
    if (not row or row["client_id"] != form.get("client_id")
            or form.get("resource") not in (None, row["resource"])):
        return {"error": "invalid_grant"}
    # Sessions of one app can refresh at the same time, and a reply can get lost.
    # A second use in the retry window gets the same pair again. A later use is a replay.
    if row["used_at"] and not row["retry"]:
        return await reuse(conn, row)
    if not row["valid"] or row["revoked_at"] or not row["active"]:
        return {"error": "invalid_grant"}
    if not row["used_at"]:
        await conn.execute("UPDATE agent_oauth_refresh SET used_at=clock_timestamp() WHERE token_hash=%s", (row["token_hash"],))
    result = await issue(conn, row["token_id"], row["client_id"], row["resource"],
                         parent=form["refresh_token"], seed=row["seed"])
    await record(conn, "refresh", client_id=row["client_id"], token_id=row["token_id"])
    return result
