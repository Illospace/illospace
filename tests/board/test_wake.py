"""Exercise the outbox through board writes and mocked external requests."""

import asyncio
import hmac
import json
import logging
import socket
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet
from psycopg.types.json import Jsonb
from pydantic import SecretStr

from illo.asks import actions, service
from illo.board import wake
from illo.connectors.github_client import GitHubAppRequired, GitHubClient
from illo.db import all_rows, migrate, one
from illo.ledger import kv
from illo.routines import scheduler
from illo.routines.scheduler import Scheduler
from illo.vault import Vault
from illo.workspace import Agent
from tests.board.test_board import DECISION, TICKET

REPO = "acme-example/engine"
ASSIGNEE = "acme-ada-example"
STUDIO = "agent:studio"
BOARD_JOB = "job:illo.routines.scheduler.check_board_wakes"


@pytest.fixture
def manual_wakes(monkeypatch):
    original = Scheduler.every

    def every(self, seconds, fn):
        if fn.__name__ == "check_board_wakes":
            self.names.add(BOARD_JOB)
            assert seconds == 2
            return None
        return original(self, seconds, fn)

    monkeypatch.setattr(Scheduler, "every", every)


@pytest.fixture
def wake_ctx(manual_wakes, app):
    ctx = app.state.ctx
    ctx.workspace = ctx.workspace.model_copy(deep=True)
    ctx.workspace.config.board.enabled = True
    assert BOARD_JOB in ctx.scheduler.names
    return ctx


@pytest.fixture
def queue_github(wake_ctx, client):
    async def queue(*, visibility="team", task_id=None):
        worker = await client(agent="build")
        responder = await client("ben") if visibility == "team" else await client()
        question = await worker.call("board_post", {
            "ticket_url": TICKET, "text": "Worker's private draft", "kind": "question",
            "visibility": visibility, **({"task_id": task_id} if task_id else {}),
        })
        reply = await responder.call("board_post", {
            "thread_id": question["thread_id"], "kind": "answer", "text": "The confidential answer",
            "reply_to_post_id": question["post_id"],
        })
        return question, reply

    return queue


@pytest.fixture
def github_app(wake_ctx, monkeypatch):
    @asynccontextmanager
    async def configure(*, issue=None, lose_receipt=False, app=True):
        data = {"state": "open", "labels": [{"name": "agent:needs-input"}],
                "assignees": [{"login": ASSIGNEE.upper()}], **(issue or {})}
        comments = []
        requests = []

        async def respond(request):
            requests.append(request)
            path = request.url.path
            if path.endswith("/installation"):
                assert request.headers["Authorization"] == "Bearer app-jwt"
                return httpx.Response(200, json={"id": 456})
            if path.endswith("/access_tokens"):
                assert request.headers["Authorization"] == "Bearer app-jwt"
                return httpx.Response(201, json={
                    "token": "installation-token", "expires_at": "2100-01-01T00:00:00Z",
                })
            assert request.headers["Authorization"] == "Bearer installation-token"
            if request.method == "POST":
                comments.append({
                    "body": json.loads(request.content)["body"],
                    "user": {"type": "Bot", "login": "example-app[bot]"},
                    "performed_via_github_app": {"id": 123},
                })
                if lose_receipt:
                    raise httpx.ReadTimeout("Sensitive response from GitHub")
                return httpx.Response(201, json=comments[-1])
            if path.endswith("/comments"):
                return httpx.Response(200, json=comments)
            assert path == f"/repos/{REPO}/issues/12"
            return httpx.Response(200, json=data)

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            app_args = {"app_id": "123", "private_key": "example", "installation_id": "456"} if app else {}
            client = GitHubClient(token="personal-token", http=http, **app_args)
            monkeypatch.setattr(client, "_jwt", lambda: "app-jwt")
            wake_ctx.github_client = client
            yield requests, comments

    return configure


async def outbox(ctx):
    async with ctx.pool.connection() as conn:
        return await all_rows(conn, "SELECT * FROM board_wakes ORDER BY id")


async def make_due(ctx):
    async with ctx.pool.connection() as conn:
        await conn.execute("UPDATE board_wakes SET due_at=now() WHERE finished_at IS NULL")


def notice(post):
    return ("Illo board update available for this issue.\n"
            f"<!-- illo:wake board_thread={post['thread_id']} post={post['post_id']} -->")


@pytest.mark.parametrize(("enabled", "github", "webhooks", "registered"), [
    (False, True, True, False),
    (True, False, False, False),
    (True, True, False, True),
    (True, False, True, True),
])
async def test_scheduler_registers_only_enabled_wakes(
    wake_ctx, monkeypatch, enabled, github, webhooks, registered,
):
    config = wake_ctx.workspace.config.board
    config.enabled, config.wake_github, config.wake_webhooks = enabled, github, webhooks
    jobs = []
    monkeypatch.setattr(wake_ctx, "every", lambda seconds, fn: jobs.append((seconds, fn.__name__)))
    await scheduler.setup(wake_ctx)
    assert ((2, "check_board_wakes") in jobs) == registered


async def test_github_answer_is_opaque_and_matches_one_open_episode(wake_ctx, client, github_app):
    worker = await client(agent="build")
    responder = await client("ben")
    question = await worker.call("board_post", {
        "ticket_url": TICKET, "kind": "question", "text": "Release secret question",
    })
    answer = {"thread_id": question["thread_id"], "reply_to_post_id": question["post_id"],
              "kind": "answer", "text": "Release secret answer"}
    await worker.call("board_post", answer)
    await responder.call("board_post", {**answer, "kind": "message"})
    assert await outbox(wake_ctx) == []
    first, second = await asyncio.gather(
        responder.call("board_post", {**answer, "source_ref": "reply-one"}),
        responder.call("board_post", {**answer, "source_ref": "reply-two"}),
    )
    [row] = await outbox(wake_ctx)
    assert row["episode_key"] == f"post:{question['post_id']}"
    assert row["post_id"] == min(first["post_id"], second["post_id"])
    assert row["payload"] == {}
    async with github_app() as (_, comments):
        await wake.drain(wake_ctx)
    assert len(comments) == 1
    assert comments[0]["body"] == notice({**question, "post_id": row["post_id"]})
    [row] = await outbox(wake_ctx)
    assert row["finished_at"] and row["attempts"] == 1 and row["last_error"] is None


async def test_only_authenticated_decision_wakes_a_decision_needed_post(wake_ctx, client, github_app):
    wake_ctx.workspace.config.board.max_consecutive_agent_posts = 2
    worker, responder = await client(agent="build"), await client("ben")
    pending = await worker.call("board_post", {"ticket_url": TICKET, **DECISION})
    await responder.call("board_post", {
        "thread_id": pending["thread_id"], "reply_to_post_id": pending["post_id"],
        "kind": "answer", "text": "I think this is the answer.",
    })
    await worker.call("board_post", {"thread_id": pending["thread_id"], "text": "Wait"}, error=True)
    assert await outbox(wake_ctx) == []
    await service.tick(wake_ctx)
    async with wake_ctx.pool.connection() as conn:
        thread = await one(conn, "SELECT * FROM board_threads WHERE id=%s", (pending["thread_id"],))
        assert thread["stalled_reason"] == "turn_cap" and thread["stalled_at"] == thread["updated_at"]
        ask = await one(conn, "SELECT * FROM asks WHERE id=%s", (pending["ask_id"],))
        person = await one(conn, "SELECT * FROM people WHERE id=%s", (responder.person_id,))
    click = {"action_id": f"ask:{ask['id']}:opt:first",
             "channel": ask["slack_channel"], "message_ts": ask["slack_ts"]}
    await actions.handle(wake_ctx, person, click)
    async with wake_ctx.pool.connection() as conn:
        thread = await one(conn, "SELECT * FROM board_threads WHERE id=%s", (pending["thread_id"],))
        assert thread["stalled_reason"] is None and thread["stalled_at"] is None
    [row] = await outbox(wake_ctx)
    assert row["episode_key"] == f"post:{pending['post_id']}"
    async with github_app() as (_, comments):
        await wake.drain(wake_ctx)
    assert [item["body"] for item in comments] == [notice({**pending, "post_id": row["post_id"]})]


async def add_human_post(ctx, thread_id, person_id, *, pending=None, stalled_at=None, reply_to=None):
    async with ctx.pool.connection() as conn:
        thread = await one(conn, "SELECT * FROM board_threads WHERE id=%s FOR UPDATE", (thread_id,))
        post = await one(conn, """
            INSERT INTO board_posts (thread_id,actor,person_id,is_human,kind,text,reply_to_post_id)
            VALUES (%s,'person:ben',%s,true,'message','Human reply',%s) RETURNING *
        """, (thread_id, person_id, reply_to))
        await conn.execute("UPDATE board_threads SET stalled_reason=NULL,stalled_at=NULL WHERE id=%s",
                           (thread_id,))
        await wake.enqueue_for_post(conn, ctx.workspace, thread=thread, post=post,
                                    prior_pending_post_id=pending, prior_turn_cap_at=stalled_at)
    return post


@pytest.mark.parametrize("episode", ["pending", "stall"])
async def test_human_post_uses_prior_episode_once(wake_ctx, client, episode):
    worker, person = await client(agent="build"), await client("ben")
    question = await worker.call("board_post", {
        "ticket_url": TICKET, "kind": "question" if episode == "pending" else "message",
        "text": "The worker needs input",
    })
    pending, stalled_at = None, None
    if episode == "pending":
        pending = question["post_id"]
        key = f"post:{pending}"
    else:
        async with wake_ctx.pool.connection() as conn:
            row = await one(conn, """
                UPDATE board_threads SET stalled_reason='turn_cap',stalled_at=statement_timestamp()
                WHERE id=%s RETURNING stalled_at
            """, (question["thread_id"],))
        stalled_at = row["stalled_at"]
        key = f"stall:{int(stalled_at.timestamp() * 1000)}"
    first = await add_human_post(wake_ctx, question["thread_id"], person.person_id,
                                 pending=pending, stalled_at=stalled_at, reply_to=pending)
    await add_human_post(wake_ctx, question["thread_id"], person.person_id,
                         pending=pending, stalled_at=stalled_at)
    [row] = await outbox(wake_ctx)
    assert (row["episode_key"], row["post_id"]) == (key, first["id"])


async def test_private_thread_does_not_enqueue_github(wake_ctx, queue_github):
    await queue_github(visibility="private")
    assert await outbox(wake_ctx) == []


async def test_private_task_link_prevents_enqueue_even_for_its_owner(wake_ctx, client, task):
    worker, owner = await client(agent="build"), await client()
    question = await worker.call("board_post", {
        "ticket_url": TICKET, "kind": "question", "text": "Question",
    })
    await task(ticket=f"{TICKET}/#issuecomment-1", owner=owner.person_id)
    await owner.call("board_post", {
        "thread_id": question["thread_id"], "reply_to_post_id": question["post_id"],
        "kind": "answer", "text": "Answer from the private task owner",
    })
    assert await outbox(wake_ctx) == []


@pytest.mark.parametrize("link", ["late_ticket", "task_id", "closed_thread", "disallowed_repo"])
async def test_github_rechecks_current_thread_and_ticket(wake_ctx, queue_github, github_app, client, task, link):
    task_id = await task(ticket=TICKET) if link == "task_id" else None
    question, _ = await queue_github(task_id=task_id)
    assert len(await outbox(wake_ctx)) == 1
    if link == "late_ticket":
        owner = await client()
        await task(ticket="HTTPS://GitHub.com/Acme-Example/ENGINE/issues/012/#issuecomment-1",
                   owner=owner.person_id)
    elif link == "task_id":
        owner = await client()
        async with wake_ctx.pool.connection() as conn:
            await conn.execute("DELETE FROM task_tickets WHERE task_id=%s", (task_id,))
            await conn.execute("UPDATE tasks SET visibility='private',owner_person_id=%s WHERE id=%s",
                               (owner.person_id, task_id))
    elif link == "closed_thread":
        worker = await client(agent="build")
        await worker.call("board_resolve", {"thread_id": question["thread_id"]})
    else:
        wake_ctx.workspace.config.github.repositories = []
    async with github_app() as (requests, _):
        await wake.drain(wake_ctx)
        assert not requests
    [row] = await outbox(wake_ctx)
    assert row["finished_at"] and row["last_error"] == wake.THREAD_UNAVAILABLE


@pytest.mark.parametrize("issue", [
    {"state": "closed"},
    {"labels": []},
    {"labels": ["agent:needs-input", "agent:hold"]},
    {"labels": ["agent:needs-input", "agent:queued"]},
    {"assignees": [{"login": "unrelated-worker"}]},
    {"pull_request": {}},
])
async def test_github_checks_live_issue(wake_ctx, queue_github, github_app, issue):
    await queue_github()
    async with github_app(issue=issue) as (requests, comments):
        await wake.drain(wake_ctx)
        assert requests and not comments
    [row] = await outbox(wake_ctx)
    assert row["finished_at"] and row["last_error"]


@pytest.mark.parametrize("pat", [True, False])
async def test_github_requires_app_and_reports_sensor(wake_ctx, queue_github, github_app, pat):
    await queue_github()
    async with github_app(app=False) as (requests, _):
        if pat:
            with pytest.raises(GitHubAppRequired):
                await wake_ctx.github_client.comment_issue(REPO, 12, "Refuse", app_only=True)
        else:
            wake_ctx.github_client = None
        await wake.drain(wake_ctx)
        assert not requests
    [row] = await outbox(wake_ctx)
    assert row["finished_at"] and row["attempts"] == 1 and row["last_error"] == wake.APP_REQUIRED
    async with wake_ctx.pool.connection() as conn:
        sensor = await one(conn, "SELECT * FROM sensors WHERE key='board:wake'")
    assert sensor["last_error_at"] and sensor["detail"] == wake.APP_REQUIRED


async def test_github_recovers_lost_receipt_and_ignores_forged_marker(wake_ctx, queue_github, github_app):
    _, reply = await queue_github()
    async with github_app(lose_receipt=True) as (_, comments):
        comments.append({"body": notice(reply), "user": {"type": "User", "login": "someone"}})
        await wake.drain(wake_ctx)
        [row] = await outbox(wake_ctx)
        assert not row["finished_at"] and row["attempts"] == 1
        assert len(comments) == 2
        await make_due(wake_ctx)
        await wake.drain(wake_ctx)
    assert len(comments) == 2
    [row] = await outbox(wake_ctx)
    assert row["finished_at"] and row["attempts"] == 2 and row["last_error"] is None


async def test_github_shares_task_write_limit(wake_ctx, queue_github, github_app):
    wake_ctx.workspace.config.tasks.github_writes_per_hour = 1
    async with wake_ctx.pool.connection() as conn:
        await kv.set(conn, "tasks:github-writes", [datetime.now(UTC).timestamp()])
    await queue_github()
    async with github_app() as (_, comments):
        await wake.drain(wake_ctx)
        assert not comments
    [row] = await outbox(wake_ctx)
    assert not row["finished_at"] and row["attempts"] == 1
    assert "GitHub writes per hour" in row["last_error"]


@pytest.fixture
def queue_webhook(wake_ctx, client, monkeypatch):
    async def resolve(host, port):
        assert host == "agents.example.com" and port == 443
        return ["8.8.8.8"]

    monkeypatch.setattr("illo.connectors.addresses.resolve_public", resolve)

    async def queue(*, secret=None, url="https://agents.example.com/wake?key=private-query"):
        studio = next(agent for agent in wake_ctx.workspace.agents if agent.key == "studio")
        studio.wake = Agent.model_validate({
            "key": "studio", "name": "Studio", "owner": studio.owner,
            "wake": {"type": "webhook", "url": url, **({"secret": secret} if secret else {})},
        }).wake
        sender = await client()
        return await sender.call("board_post", {
            "title": "Private draft title", "text": "Private draft body", "mentions": [STUDIO],
        })

    return queue


async def test_webhook_sends_only_opaque_fields_and_signs_raw_body(wake_ctx, queue_webhook, caplog):
    ctx = wake_ctx
    ctx.config.vault_key = SecretStr(Fernet.generate_key().decode())
    key = "this-is-a-private-signing-key"
    async with ctx.pool.connection() as conn:
        await Vault(ctx.config.vault_key.get_secret_value()).set(conn, "board-signing-key", key)
    result = await queue_webhook(secret="board-signing-key")
    requests = []

    async def receive(request):
        requests.append(request)
        # A pool transaction should see the committed lease during the HTTP call.
        [row] = await outbox(ctx)
        assert row["lease_until"] and row["attempts"] == 1
        return httpx.Response(204)

    ctx.wake_transport = httpx.MockTransport(receive)
    with caplog.at_level(logging.DEBUG, logger="httpx"):
        await wake.drain(ctx)
    [request] = requests
    [row] = await outbox(ctx)
    assert row["target_ref"] == STUDIO and row["payload"] == {}
    assert row["finished_at"] and row["last_error"] is None
    assert request.url.host == "8.8.8.8" and request.headers["host"] == "agents.example.com"
    data = json.loads(request.content)
    assert data == {
        "event": "board.mention", "thread_id": result["thread_id"], "post_id": result["post_id"],
        "kind": "message", "at": data["at"],
    }
    assert datetime.fromisoformat(data["at"]).tzinfo is not None
    assert request.headers["X-Illo-Event-Id"] == f"board_wake:{row['id']}"
    assert request.headers["X-Illo-Signature"] == (
        "sha256=" + hmac.digest(key.encode(), request.content, "sha256").hex()
    )
    signed = request.headers["X-Illo-Timestamp"].encode() + b"." + request.content
    assert request.headers["X-Illo-Signature-256"] == (
        "sha256=" + hmac.digest(key.encode(), signed, "sha256").hex()
    )
    assert "Authorization" not in request.headers
    assert "private-query" not in caplog.text and key not in caplog.text


@pytest.mark.parametrize("url", ["https://127.0.0.1/wake", "https://agents.example.com/wake"])
async def test_webhook_ssrf_blocks_private_ip_and_private_dns(wake_ctx, queue_webhook, monkeypatch, url):
    from illo.tools.web import resolve_public

    async def dns(host, port, **kwargs):
        assert host == "agents.example.com"
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.8", port)),
        ]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", dns)
    monkeypatch.setattr("illo.connectors.addresses.resolve_public", resolve_public)
    await queue_webhook(url=url)
    wake_ctx.wake_transport = httpx.MockTransport(lambda request: pytest.fail("Made an HTTP request"))
    await wake.drain(wake_ctx)
    [row] = await outbox(wake_ctx)
    assert row["finished_at"] and row["attempts"] == 1
    assert row["last_error"] == "The webhook address or redirect is not allowed."


@pytest.mark.parametrize("change", ["transfer", "inactive", "private", "deleted", "late_task"])
async def test_webhook_rechecks_owner_and_visibility(wake_ctx, queue_webhook, client, task, change):
    result = await queue_webhook()
    studio = next(agent for agent in wake_ctx.workspace.agents if agent.key == "studio")
    if change == "transfer":
        studio.owner = "ada"
    elif change == "deleted":
        wake_ctx.workspace.agents.remove(studio)
    elif change == "late_task":
        ada = await client()
        async with wake_ctx.pool.connection() as conn:
            await conn.execute("UPDATE board_threads SET ticket_url=%s WHERE id=%s",
                               (TICKET, result["thread_id"]))
        await task(ticket=TICKET, owner=ada.person_id)
    else:
        async with wake_ctx.pool.connection() as conn:
            if change == "inactive":
                await conn.execute("UPDATE people SET active=false WHERE key='ben'")
            else:
                ada = await client()
                await conn.execute("""
                    UPDATE board_threads SET visibility='private',owner_person_id=%s WHERE id=%s
                """, (ada.person_id, result["thread_id"]))
    wake_ctx.wake_transport = httpx.MockTransport(lambda request: pytest.fail("Made an HTTP request"))
    await wake.drain(wake_ctx)
    [row] = await outbox(wake_ctx)
    assert row["finished_at"] and row["last_error"] == wake.THREAD_UNAVAILABLE


async def test_webhook_uses_current_url(wake_ctx, queue_webhook):
    await queue_webhook()
    studio = next(agent for agent in wake_ctx.workspace.agents if agent.key == "studio")
    studio.wake.url = httpx.URL("https://agents.example.com/new")
    requests = []

    def receive(request):
        requests.append(request)
        return httpx.Response(204)

    wake_ctx.wake_transport = httpx.MockTransport(receive)
    await wake.drain(wake_ctx)
    [request] = requests
    assert request.url.path == "/new"
    assert "X-Illo-Signature" not in request.headers


async def test_webhook_allows_private_thread_owner_but_skips_self(wake_ctx, client, queue_webhook):
    studio = await client("ben", agent="studio")
    await studio.call("board_post", {"title": "Studio", "text": "Own post", "mentions": [STUDIO]})
    assert await outbox(wake_ctx) == []
    ben = await client("ben")
    await ben.call("board_post", {"title": "Studio", "text": "Ben's post", "mentions": [STUDIO],
                                 "visibility": "private"})
    [row] = await outbox(wake_ctx)
    assert row["target_ref"] == STUDIO


@pytest.mark.parametrize("change_holder", [True, False])
async def test_webhook_checks_the_linked_task_holder(wake_ctx, client, task, queue_webhook, change_holder):
    task_id = await task()
    studio = next(agent for agent in wake_ctx.workspace.agents if agent.key == "studio")
    studio.wake.secret = None
    async with wake_ctx.pool.connection() as conn:
        await conn.execute("UPDATE tasks SET holder=%s WHERE id=%s", (STUDIO, task_id))
    sender = await client()
    post = await sender.call("board_post", {"task_id": task_id, "text": "Task update"})
    [row] = await outbox(wake_ctx)
    assert row["post_id"] == post["post_id"] and row["target_ref"] == STUDIO
    if change_holder:
        async with wake_ctx.pool.connection() as conn:
            await conn.execute("UPDATE tasks SET holder='person:ada' WHERE id=%s", (task_id,))
    requests = []

    def receive(request):
        requests.append(request)
        return httpx.Response(204)

    wake_ctx.wake_transport = httpx.MockTransport(receive)
    await wake.drain(wake_ctx)
    assert len(requests) == (0 if change_holder else 1)


@pytest.mark.parametrize(("purpose", "mentions", "target", "active", "count"), [
    ("headstart", [STUDIO], STUDIO, True, 1),
    ("headstart", [STUDIO], STUDIO, False, 0),
    ("headstart", [STUDIO], "agent:build", True, 0),
    ("headstart", [STUDIO, "agent:build"], STUDIO, True, 0),
    ("escalated", [STUDIO], STUDIO, True, 0),
])
async def test_system_posts_only_wake_a_single_headstart_target(
    wake_ctx, client, task, purpose, mentions, target, active, count,
):
    worker = await client(agent="build")
    studio = await client("ben", agent="studio")
    task_id = await task(ticket=TICKET)
    question = await worker.call("board_post", {
        "task_id": task_id, "ticket_url": TICKET, "kind": "question", "text": "Help",
    })
    wake_ctx.workspace.agents.append(Agent.model_validate({
        "key": "other", "name": "Other webhook agent", "owner": "ada",
        "wake": {"type": "webhook", "url": "https://agents.example.com/other"},
    }))
    async with wake_ctx.pool.connection() as conn:
        await conn.execute("UPDATE tasks SET holder='agent:other' WHERE id=%s", (task_id,))
        if not active:
            await conn.execute("UPDATE tokens SET revoked_at=now() WHERE id=%s", (studio.token_id,))
        thread = await one(conn, "SELECT * FROM board_threads WHERE id=%s FOR UPDATE", (question["thread_id"],))
        post = await one(conn, """
            INSERT INTO board_posts (thread_id,actor,kind,text,mentions,reply_to_post_id,provenance)
            VALUES (%s,'illo','answer','Headstart',%s,%s,%s) RETURNING *
        """, (thread["id"], mentions, question["post_id"],
              Jsonb({"via": "coordinator", "purpose": purpose})))
        await conn.execute("""
            INSERT INTO board_mentions (thread_id,post_id,target,recipient_person_id)
            VALUES (%s,%s,%s,%s)
        """, (thread["id"], post["id"], STUDIO, studio.person_id))
        await wake.enqueue_for_post(conn, wake_ctx.workspace, thread=thread, post=post,
                                    headstart_target=target)
    rows = await outbox(wake_ctx)
    assert len(rows) == count
    if count:
        assert [r["target_kind"] for r in rows] == ["webhook"]
        async with wake_ctx.pool.connection() as conn:
            await conn.execute("UPDATE tokens SET revoked_at=now() WHERE id=%s", (studio.token_id,))
        wake_ctx.wake_transport = httpx.MockTransport(lambda request: pytest.fail("Made an HTTP request"))
        await wake.drain(wake_ctx)
        [row] = await outbox(wake_ctx)
        assert row["finished_at"] and row["last_error"] == wake.THREAD_UNAVAILABLE


async def test_retry_backoff_and_max_attempts(wake_ctx, queue_webhook):
    await queue_webhook()
    event_ids = []

    def fail(request):
        event_ids.append(request.headers["X-Illo-Event-Id"])
        return httpx.Response(503)

    wake_ctx.wake_transport = httpx.MockTransport(fail)
    for attempt in range(1, 4):
        assert await wake.run_once(wake_ctx)
        [row] = await outbox(wake_ctx)
        assert row["attempts"] == attempt and row["last_error"] == "The webhook returned status 503."
        assert row["lease_id"] is None and row["lease_until"] is None
        if attempt < 3:
            assert row["finished_at"] is None
            wait = (row["due_at"] - datetime.now(UTC)).total_seconds()
            assert 5 * attempt - 1 < wait <= 5 * attempt
            assert not await wake.run_once(wake_ctx)
            await make_due(wake_ctx)
        else:
            assert row["finished_at"]
    assert not await wake.run_once(wake_ctx)
    assert event_ids == [f"board_wake:{row['id']}"] * 3


async def test_advisory_lock_and_stale_lease_fence(wake_ctx, queue_webhook):
    await queue_webhook()
    async with wake_ctx.pool.connection() as conn:
        await conn.execute("SELECT pg_advisory_xact_lock(12000013)")
        assert not await wake.run_once(wake_ctx)
    async with wake_ctx.pool.connection() as conn:
        await conn.execute("SELECT pg_advisory_xact_lock(12000002)")
        first = await wake._claim(wake_ctx)
        assert first and not await wake.run_once(wake_ctx)
    async with wake_ctx.pool.connection() as conn:
        await conn.execute("UPDATE board_wakes SET lease_until=now()-interval '1 second'")
    second = await wake._claim(wake_ctx)
    assert second and second["lease_id"] != first["lease_id"]
    await wake._finish(wake_ctx, first)
    [current] = await outbox(wake_ctx)
    assert current["lease_id"] == second["lease_id"] and current["finished_at"] is None
    await wake._finish(wake_ctx, second)
    [current] = await outbox(wake_ctx)
    assert current["finished_at"] and current["attempts"] == 2


async def test_expired_final_lease_closes_wake_without_another_request(wake_ctx, queue_webhook):
    await queue_webhook()
    async with wake_ctx.pool.connection() as conn:
        await conn.execute("""UPDATE board_wakes
            SET attempts=3,lease_id='lost',lease_until=now()-interval '1 second'""")
    wake_ctx.wake_transport = httpx.MockTransport(lambda request: pytest.fail("Made an HTTP request"))
    await wake.drain(wake_ctx)
    [row] = await outbox(wake_ctx)
    assert row["finished_at"] and row["attempts"] == 3
    assert row["last_error"] == "The last wake attempt ended without a receipt."


async def test_m2_schema_accepts_m1_writes_after_rollback(pool, tmp_path):
    migrations = Path(__file__).resolve().parents[2] / "illo/migrations"
    m1 = tmp_path / "m1"
    m1.mkdir()
    for path in migrations.glob("*.sql"):
        if path.name <= "1900_board.sql":
            (m1 / path.name).symlink_to(path)

    async with pool.connection() as conn:
        try:
            await conn.execute("CREATE SCHEMA board_wake_upgrade")
            await conn.execute("SET search_path TO board_wake_upgrade")
            assert "1900_board.sql" in await migrate(conn, m1)
            thread = await one(conn, """
                INSERT INTO board_threads (title,created_by,stalled_reason)
                VALUES ('Topic','agent:build','turn_cap') RETURNING id,updated_at
            """)
            insert_post = """
                INSERT INTO board_posts (thread_id,actor,kind,text)
                VALUES (%s,'agent:build','message','Update') RETURNING id
            """
            # The M1 table can contain wake rows with the original column set.
            insert_wake = """
                INSERT INTO board_wakes (thread_id,post_id,target_kind,target_ref)
                VALUES (%s,%s,'webhook','agent:studio') RETURNING id
            """
            post = await one(conn, insert_post, (thread["id"],))
            await one(conn, insert_wake, (thread["id"], post["id"]))
            await conn.commit()
            assert await migrate(conn) == [
                "1901_board_wakes.sql", "1902_board_coordinator.sql", "1903_board_coordinator_validate.sql",
                "1904_board_files.sql", "1905_board_files_validate.sql",
            ]
            assert await migrate(conn, m1) == []
            saved = await one(conn, "SELECT stalled_at FROM board_threads WHERE id=%s", (thread["id"],))
            assert saved["stalled_at"] == thread["updated_at"]
            post = await one(conn, insert_post, (thread["id"],))
            await one(conn, insert_wake, (thread["id"], post["id"]))
            assert await all_rows(conn, "SELECT episode_key FROM board_wakes ORDER BY id") == [
                {"episode_key": None}, {"episode_key": None},
            ]
        finally:
            await conn.rollback()
            await conn.execute("RESET search_path")
            await conn.execute("DROP SCHEMA IF EXISTS board_wake_upgrade CASCADE")
            await conn.commit()
