"""Queue board wakes with posts and deliver opaque notices from the outbox."""

import asyncio
import hmac
import json
import time
from datetime import UTC, datetime
from uuid import uuid4

import httpx
from psycopg.types.json import Jsonb

from illo.connectors import github_tasks
from illo.connectors.addresses import PublicTransport
from illo.connectors.github_client import GitHubAppRequired, GitHubError
from illo.connectors.http import private_http_logs
from illo.connectors.store import vault
from illo.db import one
from illo.ledger import board, sensors

MAX_ATTEMPTS = 3
APP_REQUIRED = "Board GitHub wakes need the GitHub App."
THREAD_UNAVAILABLE = "The thread no longer permits this wake."


class RetryWake(RuntimeError):
    """A temporary failure with a message that is safe to store."""


async def _pending_worker_post(conn, thread_id, post_id, reply_id, kind=None):
    return await one(conn, """
        SELECT p.id,p.kind,p.actor FROM board_posts p
        WHERE p.thread_id=%s AND p.id=%s AND p.id<%s
          AND p.kind IN ('question','decision_needed') AND (%s::text IS NULL OR p.kind=%s)
          AND (p.kind<>'decision_needed' OR EXISTS (
              SELECT 1 FROM asks a WHERE a.id=p.ask_id AND a.state<>'cancelled'
          ))
          AND NOT p.is_human AND left(p.actor,6)='agent:'
          AND NOT EXISTS (
              SELECT 1 FROM board_posts r
              WHERE r.thread_id=p.thread_id AND r.reply_to_post_id=p.id AND r.id<%s
                AND r.actor<>p.actor
                AND ((p.kind='question' AND r.kind='answer')
                  OR (p.kind='decision_needed' AND r.kind='decision' AND r.is_human))
          )
    """, (thread_id, reply_id, post_id, kind, kind, post_id))


async def _episode(conn, thread_id, post, prior_pending_post_id, prior_turn_cap_at):
    reply_id = post["reply_to_post_id"]
    reply_kind = {"answer": "question", "decision": "decision_needed"}.get(post["kind"])
    if reply_id and reply_kind and (post["kind"] == "answer" or post["is_human"]):
        pending = await _pending_worker_post(conn, thread_id, post["id"], reply_id, reply_kind)
        if pending and pending["actor"] != post["actor"]:
            return f"post:{pending['id']}"
    if post["is_human"]:
        if prior_pending_post_id is not None and (
            await _pending_worker_post(conn, thread_id, post["id"], prior_pending_post_id)
        ):
            return f"post:{prior_pending_post_id}"
        if prior_turn_cap_at is not None:
            return f"stall:{int(prior_turn_cap_at.timestamp() * 1000)}"
    return None


def _github_assignees(workspace, repo):
    return {
        agent.wake.assignee.lower() for agent in workspace.agents
        if agent.wake.type == "github_issue" and repo in {r.lower() for r in agent.wake.repos}
    }


def _ticket(workspace, value):
    if value:
        try:
            if board.canonical_ticket(workspace, value) == value:
                return github_tasks.parse(value)
        except ValueError:
            pass
    return None


async def _github_thread(conn, thread_id):
    return await one(conn, f"""
        SELECT b.ticket_url FROM board_threads b
        WHERE b.id=%s AND b.state='open' AND b.visibility='team'
          AND NOT EXISTS ({board.PRIVATE_TASKS})
    """, (thread_id,))


def _headstart(post, target):
    provenance = post.get("provenance") or {}
    return (post["actor"] == "illo" and not post["is_human"]
            and post.get("token_id") is None and post.get("person_id") is None
            and provenance.get("via") == "coordinator" and provenance.get("purpose") == "headstart"
            and list(post["mentions"]) == [target] and post.get("reply_actor") != target)


def _webhook_agent(workspace, target):
    return next((agent for agent in workspace.agents if f"agent:{agent.key}" == target
                 and agent.wake.type == "webhook"), None)


async def _webhook_post(conn, workspace, thread_id, post_id, target, agent, *, holder=False, owner_id=None,
                        delivery=False):
    post = await one(conn, f"""
        SELECT p.id,p.kind,p.at,p.actor,p.is_human,p.person_id,p.token_id,p.provenance,p.mentions,
            owner.id AS owner_id,
            r.actor AS reply_actor,
            EXISTS (
                SELECT 1 FROM tokens tok WHERE tok.person_id=owner.id AND tok.agent_key=%(agent)s
                  AND tok.revoked_at IS NULL AND tok.scopes=ARRAY['agent']::text[]
            ) AS agent_active
        FROM board_posts p JOIN board_threads b ON b.id=p.thread_id
        JOIN people owner ON owner.key=%(owner)s AND owner.active
        LEFT JOIN board_posts r ON r.thread_id=p.thread_id AND r.id=p.reply_to_post_id
        WHERE b.id=%(thread_id)s AND p.id=%(post_id)s AND b.state='open'
          AND p.actor<>%(target)s AND (%(owner_id)s::bigint IS NULL OR owner.id=%(owner_id)s)
          AND (
              EXISTS (
                  SELECT 1 FROM board_mentions m WHERE m.thread_id=b.id AND m.post_id=p.id
                    AND m.target=%(target)s AND m.recipient_person_id=owner.id
              ) OR (%(holder)s AND p.actor<>'illo' AND EXISTS (
                  SELECT 1 FROM tasks t WHERE t.id=b.task_id AND t.holder=%(target)s
              ))
          )
          AND {board.visible_to("owner.id")}
    """, {"thread_id": thread_id, "post_id": post_id, "target": target,
          "owner": agent.owner, "agent": agent.key, "holder": holder, "owner_id": owner_id})
    if post and post["actor"] == "illo":
        from illo.board import acl, episodes, packet

        if not post["agent_active"] or not _headstart(post, target):
            return None
        if episode_id := post["provenance"].get("episode_id"):
            episode = await one(conn, "SELECT * FROM board_episodes WHERE id=%s AND thread_id=%s",
                                (episode_id, thread_id))
            thread = await acl.thread_access(conn, thread_id)
            if (not episode or not thread or thread["state"] != "open"
                    or (delivery and (episode["state"] != "headstart"
                                      or not episode["headstart_until"]
                                      or episode["headstart_until"] <= datetime.now(UTC)))
                    or not await episodes.is_pending(conn, episode)
                    or not await acl.target_ok(conn, workspace, thread, target, await packet.asker_post(conn, episode))):
                return None
    return post


async def enqueue_for_post(conn, workspace, *, thread, post, prior_pending_post_id=None,
                           prior_turn_cap_at=None, headstart_target=None):
    """Queue wakes in the transaction that writes the post and its mentions."""
    config = workspace.config.board
    if not config.enabled or thread["state"] != "open":
        return
    thread_id = thread["id"]
    if config.wake_github and post["actor"] != "illo" and thread["visibility"] == "team":
        episode = await _episode(conn, thread_id, post, prior_pending_post_id, prior_turn_cap_at)
        if episode and (current := await _github_thread(conn, thread_id)):
            ticket = _ticket(workspace, current["ticket_url"])
            if ticket and _github_assignees(workspace, ticket[0]):
                await conn.execute("""
                    INSERT INTO board_wakes (thread_id,post_id,target_kind,target_ref,episode_key)
                    VALUES (%s,%s,'github_issue',%s,%s) ON CONFLICT DO NOTHING
                """, (thread_id, post["id"], current["ticket_url"], episode))
    if not config.wake_webhooks:
        return
    targets = dict.fromkeys(post["mentions"], False)
    if post["actor"] == "illo":
        mentions = post["mentions"]
        if len(mentions) != 1 or (headstart_target is not None and mentions[0] != headstart_target):
            return
        if not _headstart(post, mentions[0]):
            return
    elif thread["task_id"] is not None or thread["ticket_url"]:
        task = await one(conn, """
            SELECT t.holder FROM board_threads b JOIN tasks t ON t.id=b.task_id WHERE b.id=%s
        """, (thread_id,))
        if task and task["holder"]:
            targets.setdefault(task["holder"], True)
    for target, holder in targets.items():
        agent = _webhook_agent(workspace, target)
        if agent and (visible := await _webhook_post(conn, workspace, thread_id, post["id"], target, agent, holder=holder)):
            payload = {"holder_owner_id": visible["owner_id"]} if holder else {}
            await conn.execute("""
                INSERT INTO board_wakes (thread_id,post_id,target_kind,target_ref,payload)
                VALUES (%s,%s,'webhook',%s,%s) ON CONFLICT DO NOTHING
            """, (thread_id, post["id"], target, Jsonb(payload)))


async def _claim(ctx):
    async with ctx.pool.connection() as conn:
        lock = await one(conn, "SELECT pg_try_advisory_xact_lock(12000013) AS acquired")
        if not lock["acquired"]:
            return None
        return await one(conn, """
            WITH due AS (
                SELECT id,attempts FROM board_wakes
                WHERE finished_at IS NULL AND due_at<=now()
                  AND (lease_until IS NULL OR lease_until<now())
                ORDER BY due_at,id FOR UPDATE SKIP LOCKED LIMIT 1
            )
            UPDATE board_wakes w SET lease_id=%s, lease_until=now()+interval '60 seconds',
                attempts=LEAST(due.attempts+1,%s)
            FROM due WHERE w.id=due.id
            RETURNING w.*,due.attempts>=%s AS exhausted
        """, (uuid4().hex, MAX_ATTEMPTS, MAX_ATTEMPTS))


async def _finish(ctx, wake, error=None, *, retry=False, app_required=False):
    final = not retry or wake["attempts"] >= MAX_ATTEMPTS
    async with ctx.pool.connection() as conn:
        saved = await one(conn, """
            UPDATE board_wakes SET lease_id=NULL, lease_until=NULL, last_error=%s,
                finished_at=CASE WHEN %s THEN clock_timestamp() ELSE NULL END,
                due_at=CASE WHEN %s THEN due_at
                            ELSE clock_timestamp()+%s*interval '5 seconds' END
            WHERE id=%s AND lease_id=%s AND lease_until>clock_timestamp()
            RETURNING id
        """, (error, final, final, wake["attempts"], wake["id"], wake["lease_id"]))
        if saved and app_required:
            await sensors.error(conn, "board:wake", APP_REQUIRED)
        elif saved and error is None and wake["target_kind"] == "github_issue":
            await sensors.ok(conn, "board:wake")


async def _lease_current(conn, wake):
    return await one(conn, """
        SELECT 1 FROM board_wakes WHERE id=%s AND lease_id=%s AND lease_until>clock_timestamp()
    """, (wake["id"], wake["lease_id"]))


async def _github_target(ctx, conn, wake):
    workspace = ctx.workspace
    if not workspace.config.board.enabled or not workspace.config.board.wake_github:
        return None
    current = await _github_thread(conn, wake["thread_id"])
    if current and current["ticket_url"] == wake["target_ref"]:
        ticket = _ticket(workspace, current["ticket_url"])
        if ticket and (assignees := _github_assignees(workspace, ticket[0])):
            return ticket, assignees
    return None


def _sent_by_app(comment, body, app_id):
    app = comment.get("performed_via_github_app") or {}
    user = comment.get("user") or {}
    return (bool(app_id) and str(app.get("id")) == str(app_id)
            and user.get("type") == "Bot" and comment.get("body", "").strip() == body)


async def _deliver_github(ctx, wake):
    async with ctx.pool.connection() as conn:
        target = await _github_target(ctx, conn, wake)
    if not target:
        return THREAD_UNAVAILABLE
    client = ctx.github_client
    if client is None:
        raise GitHubAppRequired()
    (repo, number), _ = target
    issue = await client.get_issue(repo, number, app_only=True)
    body = ("Illo board update available for this issue.\n"
            f"<!-- illo:wake board_thread={wake['thread_id']} post={wake['post_id']} -->")
    app_id = getattr(client, "app_id", None)
    if any(_sent_by_app(comment, body, app_id) for comment in issue.get("comments_data", [])):
        return None
    labels = {label["name"] if isinstance(label, dict) else label for label in issue.get("labels", [])}
    if (issue.get("state") != "open" or "pull_request" in issue
            or "agent:needs-input" not in labels or labels & {"agent:hold", "agent:queued"}):
        return "The GitHub issue is no longer waiting for input."
    assigned = {user["login"].lower() for user in issue.get("assignees", [])}
    async with ctx.pool.connection() as conn:
        await github_tasks.lock_writes(conn)
        current = await _github_target(ctx, conn, wake)
        if not current:
            return THREAD_UNAVAILABLE
        if not assigned.intersection(current[1]):
            return "The GitHub issue has no configured assignee."
        if not await _lease_current(conn, wake):
            return "The wake lease expired."
        await github_tasks.reserve_write(ctx, conn)
    await client.comment_issue(repo, number, body, app_only=True)
    return None


async def _deliver_webhook(ctx, wake):
    workspace = ctx.workspace
    agent = _webhook_agent(workspace, wake["target_ref"])
    if not workspace.config.board.enabled or not workspace.config.board.wake_webhooks or not agent:
        return THREAD_UNAVAILABLE
    secret = None
    owner_id = wake["payload"].get("holder_owner_id")
    async with ctx.pool.connection() as conn:
        post = await _webhook_post(conn, workspace, wake["thread_id"], wake["post_id"], wake["target_ref"],
                                   agent, holder=owner_id is not None, owner_id=owner_id, delivery=True)
        if not post:
            return THREAD_UNAVAILABLE
        if agent.wake.secret:
            try:
                secret = await vault(ctx).get(conn, agent.wake.secret)
            except ValueError:
                pass
            if not secret:
                return "The webhook signing key is unavailable."
        if not await _lease_current(conn, wake):
            return "The wake lease expired."

    at = post["at"].isoformat()
    body = json.dumps({"event": "board.mention", "thread_id": wake["thread_id"],
                       "post_id": post["id"], "kind": post["kind"], "at": at}).encode()
    timestamp = str(int(time.time()))
    headers = {"Content-Type": "application/json", "X-Illo-Event-Id": f"board_wake:{wake['id']}",
               "X-Illo-Timestamp": timestamp}
    if secret:
        headers["X-Illo-Signature"] = "sha256=" + hmac.digest(secret.encode(), body, "sha256").hex()
        signed = timestamp.encode() + b"." + body
        headers["X-Illo-Signature-256"] = "sha256=" + hmac.digest(secret.encode(), signed, "sha256").hex()
    transport = PublicTransport(getattr(ctx, "wake_transport", None)
                                or httpx.AsyncHTTPTransport(trust_env=False))
    try:
        with private_http_logs():
            async with httpx.AsyncClient(transport=transport, timeout=10, trust_env=False,
                                         follow_redirects=False) as client:
                async with client.stream("POST", str(agent.wake.url), content=body, headers=headers) as response:
                    status = response.status_code
    except (ValueError, httpx.InvalidURL):
        return "The webhook address or redirect is not allowed."
    if status == 429 or status >= 500:
        raise RetryWake(f"The webhook returned status {status}.")
    if not 200 <= status < 300:
        return f"The webhook returned status {status}."
    return None


async def run_once(ctx):
    """Claim one due wake, then deliver it without a database transaction."""
    wake = await _claim(ctx)
    if not wake:
        return False
    if wake["exhausted"]:
        await _finish(ctx, wake, "The last wake attempt ended without a receipt.")
        return True
    try:
        async with asyncio.timeout(50):
            deliver = _deliver_github if wake["target_kind"] == "github_issue" else _deliver_webhook
            error = await deliver(ctx, wake)
    except GitHubAppRequired:
        await _finish(ctx, wake, APP_REQUIRED, app_required=True)
    except GitHubError as exc:
        await _finish(ctx, wake, str(exc), retry=exc.rate_limited or exc.status >= 500)
    except (github_tasks.GitHubWriteLimit, RetryWake) as exc:
        await _finish(ctx, wake, str(exc), retry=True)
    except (httpx.HTTPError, TimeoutError, OSError):
        await _finish(ctx, wake, "The wake endpoint did not answer.", retry=True)
    except (KeyError, TypeError, ValueError):
        await _finish(ctx, wake, "The wake configuration or response is invalid.")
    else:
        await _finish(ctx, wake, error)
    return True


async def drain(ctx):
    """Process up to 50 due wakes without holding a batch of leases."""
    for _ in range(50):
        if not await run_once(ctx):
            break
