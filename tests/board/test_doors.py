"""End-to-end tests for PR 2: tainted team task reads and unified Chief-of-Staff doors."""

from datetime import UTC, datetime, timedelta

from illo.asks import actions, service
from illo.board import coordinator, escalate
from illo.channels.slack import tasks as slack_tasks
from illo.connectors import github_tasks
from illo.db import all_rows, one
from illo.ledger import ask_workflow, asks, board, digest_snapshot, lanes, people, rules, runs
from illo.ledger import tasks as task_ledger
from illo.tasks import release
from illo.tasks.access import Viewer
from illo.workspace import TaskList
from tests.board.test_board import DECISION, TICKET
from tests.board.test_coordinator import admission, last_run, triage
from tests.board.test_escalate import escalation
from tests.tool_results import read_result


async def test_tainted_internal_run_reads_team_tasks_only_and_outsider_gets_none(app, rpc):
    ctx = app.state.ctx
    ctx.workspace = ctx.workspace.model_copy(deep=True)
    ctx.workspace.lists.append(TaskList(key="private", name="Private", owner="ada"))
    async with ctx.pool.connection() as conn:
        ada = await people.get(conn, "ada")
        team_task = await task_ledger.create(
            conn,
            workspace=ctx.workspace,
            viewer=Viewer("ada", "person:ada", team=True, human=True),
            list_key="bugs",
            title="Public staging outage",
            brief="Team bug brief visible to tainted internal runs.",
            holder="agent:build",
        )
        private_task = await task_ledger.create(
            conn,
            workspace=ctx.workspace,
            viewer=Viewer("ada", "person:ada", team=True, human=True),
            list_key="private",
            title="Secret personal errand",
            brief="Private brief must never leak to tainted runs.",
            holder="person:ada",
        )
        tainted_run = await runs.create(
            conn,
            principal="person:ada",
            person_id=ada["id"],
            profile="tainted",
            tainted=True,
            provider="fake",
            tool_limit=12,
            model="fake",
        )
        tainted_token = await runs.mint_token(conn, tainted_run["id"], 60)

        outsider_run = await runs.create(
            conn,
            principal="outsider",
            person_id=None,
            profile="tainted",
            tainted=True,
            provider="fake",
            tool_limit=12,
            model="fake",
        )
        outsider_token = await runs.mint_token(conn, outsider_run["id"], 60)

    names = {t["name"] for t in (await rpc(tainted_token, "tools/list")).json()["result"]["tools"]}
    assert {"lists", "task_list", "task_get"} <= names
    assert not ({"task_create", "task_post", "task_write"} & names)

    lists_out = read_result((await rpc(tainted_token, "tools/call", {"name": "lists"})).json()["result"])
    assert "bugs" in str(lists_out)
    assert "private" not in str(lists_out).lower()

    task_list_out = read_result((await rpc(tainted_token, "tools/call", {"name": "task_list"})).json()["result"])
    assert "Public staging outage" in str(task_list_out)
    assert "Secret personal errand" not in str(task_list_out)

    team_get = read_result(
        (await rpc(tainted_token, "tools/call", {"name": "task_get", "arguments": {"id": team_task["id"]}})).json()[
            "result"
        ]
    )
    assert "Team bug brief visible to tainted internal runs." in str(team_get)

    private_get = read_result(
        (await rpc(tainted_token, "tools/call", {"name": "task_get", "arguments": {"id": private_task["id"]}})).json()[
            "result"
        ]
    )
    assert "Secret personal errand" not in str(private_get)
    assert "Private brief must never leak" not in str(private_get)

    # Mutations remain blocked on tainted runs.
    write_attempt = (
        await rpc(
            tainted_token,
            "tools/call",
            {"name": "task_post", "arguments": {"id": team_task["id"], "text": "Injected comment"}},
        )
    ).json()["result"]
    assert write_attempt["isError"]

    # Outsider tainted runs get zero task tools.
    outsider_names = [t["name"] for t in (await rpc(outsider_token, "tools/list")).json()["result"]["tools"]]
    assert outsider_names == ["web_read_public_page"]
    assert (await rpc(outsider_token, "tools/call", {"name": "task_list"})).json()["result"]["isError"]


async def test_ask_create_routes_through_chief_of_staff_stage0_rule_and_ask_pull(
    coordinator_ctx, slack, client, fake_brain
):
    ctx = coordinator_ctx
    async with ctx.pool.connection() as conn:
        ben = await people.get(conn, "ben")
        rule = await rules.create(
            conn,
            owner_person_id=ben["id"],
            text="Always pick the first staging option.",
            answer_option_label="Ship to staging",
        )

    worker = await client(person="ada", agent="build")
    created = await worker.call(
        "ask_create",
        {
            "owner": "ben",
            "question": "Should we ship the release to staging now?",
            "options": [
                {"id": "staging", "label": "Ship to staging"},
                {"id": "wait", "label": "Wait"},
            ],
            "proof": {"summary": "CI is green on branch release-12."},
        },
    )
    ask_id = created["ask_id"]

    # Legacy asks/service.py and reminders.py (due_asks) must not touch board-shimmed asks.
    await service.tick(ctx)
    assert slack.roots() == []
    async with ctx.pool.connection() as conn:
        await conn.execute(
            "UPDATE asks SET due_at = now() - interval '1 hour', slack_ts = '1.0' WHERE id=%s", (ask_id,)
        )
        due = await ask_workflow.due_asks(conn)
        assert all(row["id"] != ask_id for row in due)
        await conn.execute("UPDATE asks SET slack_ts = NULL WHERE id=%s", (ask_id,))
        ask_row = await one(conn, "SELECT state, slack_ts, delivery_state FROM asks WHERE id=%s", (ask_id,))
        assert ask_row == {"state": "open", "slack_ts": None, "delivery_state": "none"}

    # Chief of Staff Stage 0 resolves via standing rule with zero LLM calls and zero Slack DMs.
    await coordinator.run_coordinator(ctx)
    await escalate.send_due(ctx)
    assert fake_brain.requests == []
    assert slack.roots() == []

    # Answer is available through ask_pull and cleanly acked via ask_ack.
    pulled = await worker.call("ask_pull", {})
    assert any(
        item["ask_id"] == ask_id and item["answer_option"]["id"] == "staging"
        for item in pulled["answers"]
    )
    await worker.call("ask_ack", {"ask_id": ask_id})
    async with ctx.pool.connection() as conn:
        final_ask = await one(conn, "SELECT state, rule_id, delivery_state FROM asks WHERE id=%s", (ask_id,))
        assert final_ask == {"state": "answered", "rule_id": rule["id"], "delivery_state": "acked"}


async def test_ask_create_cross_person_private_guest_rich_blocks_and_single_card(
    coordinator_ctx, slack, client
):
    ctx = coordinator_ctx
    worker = await client(person="ben")
    created = await worker.call(
        "ask_create",
        {
            "owner": "ada",
            "question": "Approve the partner launch copy?",
            "options": [
                {"id": "approve", "label": "Approve"},
                {"id": "revise", "label": "Needs edits"},
            ],
            "blocks": [{"kind": "text", "title": "Draft", "text": "Launch copy preview."}],
            "proof": {"summary": "Legal signed off on the revised wording."},
        },
    )
    ask_id = created["ask_id"]

    async with ctx.pool.connection() as conn:
        post = await one(conn, "SELECT * FROM board_posts WHERE ask_id=%s", (ask_id,))
        assert post is not None
        thread = await one(conn, "SELECT * FROM board_threads WHERE id=%s", (post["thread_id"],))
        ada = await people.get(conn, "ada")
        ben = await people.get(conn, "ben")
        guest = await one(
            conn,
            "SELECT * FROM board_thread_guests WHERE thread_id=%s AND person_id=%s",
            (thread["id"], ada["id"]),
        )
        assert thread["visibility"] == "private"
        assert thread["owner_person_id"] == ben["id"]
        assert guest is not None
        # Shimmed ask_create does not create unread board_mentions noise.
        assert not await one(conn, "SELECT 1 FROM board_mentions WHERE post_id=%s", (post["id"],))

    event = await admission(ctx)
    res = await triage(
        ctx,
        event,
        "need_ask_person",
        person="person:ada",
        format="card",
        summary="Ben's agent prepared the partner launch copy after legal sign-off. Approve or request edits?",
        recommended=0,
    )
    assert res["decision"] == "allow"
    assert (await last_run(ctx))["outcome"] == "applied"

    await escalate.send_due(ctx)
    [root] = slack.roots()
    rendered = str(root)
    # Rich blocks ask keeps the ask page link ("Open the page" / web-open-ask:{id}).
    assert f"web-open-ask:{ask_id}" in rendered
    assert "Open the page" in rendered

    # Human clicks Approve -> ONE board decision post, ask answered with delivery_state='pending' for ask_pull.
    await actions.handle(
        ctx,
        ada,
        {"action_id": f"ask:{ask_id}:opt:approve", "channel": root["channel"], "message_ts": root["ts"]},
    )
    async with ctx.pool.connection() as conn:
        ask_after = await one(conn, "SELECT state, answer_option, delivery_state FROM asks WHERE id=%s", (ask_id,))
        esc = await escalation(ctx)
        decision_posts = await all_rows(
            conn,
            "SELECT actor, kind, text, is_human FROM board_posts WHERE thread_id=%s AND kind='decision'",
            (thread["id"],),
        )
    assert ask_after == {"state": "answered", "answer_option": "approve", "delivery_state": "pending"}
    assert esc["state"] == "answered"
    assert decision_posts == [{"actor": "person:ada", "kind": "decision", "text": "Approve", "is_human": True}]

    pulled = await worker.call("ask_pull", {})
    assert any(
        item["ask_id"] == ask_id and item["answer_option"]["id"] == "approve"
        for item in pulled["answers"]
    )
    await worker.call("ask_ack", {"ask_id": ask_id})


async def test_ask_create_reuses_task_thread_and_ask_cancel_closes_board(
    coordinator_ctx, slack, client, task
):
    ctx = coordinator_ctx
    task_id = await task(title="Fix payment webhook", ticket=TICKET)
    worker = await client(person="ada", agent="build")
    first = await worker.call("board_post", {"ticket_url": TICKET, "kind": "message", "text": "Investigating."})

    async with ctx.pool.connection() as conn:
        await lanes.upsert(
            conn,
            source="codex",
            external_id="webhook-run-1",
            title="Webhook run",
            visibility="team",
            links=[{"kind": "ticket", "url": TICKET}],
        )

    created = await worker.call(
        "ask_create",
        {
            "owner": "ben",
            "lane": {"source": "codex", "external_id": "webhook-run-1"},
            "question": "Retry failed webhook events from the last hour?",
            "options": [{"id": "yes", "label": "Retry"}, {"id": "no", "label": "Skip"}],
        },
    )
    ask_id = created["ask_id"]
    async with ctx.pool.connection() as conn:
        post = await one(conn, "SELECT thread_id FROM board_posts WHERE ask_id=%s", (ask_id,))
        thread = await one(conn, "SELECT id, task_id FROM board_threads WHERE id=%s", (post["thread_id"],))
    assert thread == {"id": first["thread_id"], "task_id": task_id}

    # Scan creates an open episode; cancelling the ask closes the episode so nothing is ever escalated.
    await coordinator.scan(ctx)
    await worker.call("ask_cancel", {"ask_id": ask_id})
    async with ctx.pool.connection() as conn:
        ep = await one(conn, "SELECT state, closed_reason FROM board_episodes WHERE thread_id=%s", (thread["id"],))
        ask_row = await one(conn, "SELECT state FROM asks WHERE id=%s", (ask_id,))
    assert ask_row == {"state": "cancelled"}
    assert ep == {"state": "closed", "closed_reason": "no_longer_pending"}
    await coordinator.run_coordinator(ctx)
    await escalate.send_due(ctx)
    assert slack.roots() == []


async def test_agent_request_asks_create_routes_through_chief_of_staff(
    coordinator_ctx, slack, client, fake_brain
):
    ctx = coordinator_ctx
    await ctx.scheduler.close()
    worker = await client(person="ada", agent="build")
    fake_brain.script = [
        {
            "name": "asks_create",
            "arguments": {
                "owner": "ben",
                "question": "Roll out the cache fix tonight?",
                "options": [{"id": "yes", "label": "Yes"}, {"id": "no", "label": "No"}],
            },
        }
    ]
    await worker.call("illo_submit", {"message": "Ask Ben whether to roll out the cache fix tonight."})
    await worker.settle()

    # Legacy asks worker must leave it alone; board has a decision_needed post for Chief of Staff.
    await service.tick(ctx)
    assert slack.roots() == []
    async with ctx.pool.connection() as conn:
        ask = await one(conn, "SELECT id FROM asks ORDER BY id DESC LIMIT 1")
        assert ask is not None
        post = await one(conn, "SELECT kind, ask_id, provenance FROM board_posts WHERE ask_id=%s", (ask["id"],))
    assert post is not None
    assert post["kind"] == "decision_needed"
    assert post["provenance"]["via"] == "asks_create"


async def test_task_post_question_routes_to_board_immediate_scan_and_chief_of_staff_unblocks_task(
    coordinator_ctx, slack, client, fake_brain
):
    ctx = coordinator_ctx
    # Even with a long settle delay, routed task questions scan immediately.
    ctx.workspace.config.board.coordinator.settle_seconds = 300
    async with ctx.pool.connection() as conn:
        await task_ledger.create(
            conn,
            workspace=ctx.workspace,
            viewer=Viewer("ada", "person:ada", team=True, human=True),
            list_key="bugs",
            title="Staging database facts",
            brief="The staging replica runs Postgres 16 in eu-central-1.",
            holder="person:ada",
        )
        t = await task_ledger.create(
            conn,
            workspace=ctx.workspace,
            viewer=Viewer("ada", "person:ada", team=True, human=True),
            list_key="bugs",
            title="Run staging index check",
            brief="Verify index bloat on staging.",
            holder="agent:build",
        )

    worker = await client(person="ada", agent="build")
    await worker.call(
        "task_post",
        {
            "id": t["id"],
            "text": "Which Postgres version and region is the staging replica in?",
        },
    )

    # Raw Slack task sender and task release person-reminder must stay quiet while board handles it.
    await slack_tasks.tick(ctx)
    assert slack.calls == []
    await release.tick(ctx, datetime.now(UTC) + timedelta(hours=30))
    async with ctx.pool.connection() as conn:
        reminded = await all_rows(conn, "SELECT * FROM task_events WHERE task_id=%s AND kind='reminded'", (t["id"],))
        assert reminded == []

    # Chief of Staff scans immediately (ignoring settle_seconds=300), reads the task board, and answers.
    fake_brain.script = [
        {"name": "task_list", "arguments": {"list_key": "bugs"}},
        {
            "name": "need_answer",
            "arguments": {
                "answer": "The staging replica runs Postgres 16 in eu-central-1.",
                "sources": ["task:Staging database facts"],
            },
        },
    ]
    await coordinator.run_coordinator(ctx)
    await escalate.send_due(ctx)

    assert len(fake_brain.requests) == 1
    assert slack.roots() == []
    async with ctx.pool.connection() as conn:
        updated = await one(conn, "SELECT state, holder, waits_for FROM tasks WHERE id=%s", (t["id"],))
        assert updated == {"state": "todo", "holder": "agent:build", "waits_for": "agent:build"}
        msg = await one(
            conn,
            "SELECT actor, text, data FROM task_events WHERE task_id=%s AND kind='message' ORDER BY id DESC LIMIT 1",
            (t["id"],),
        )
        assert msg["actor"] == "illo"
        assert "Postgres 16" in msg["text"]
        assert msg["data"]["source"] == "board"
        assert msg["data"]["wake"] is True
        wakes = await all_rows(conn, "SELECT holder, reason FROM task_wakes WHERE task_id=%s", (t["id"],))
        assert any(w["holder"] == "agent:build" and w["reason"] == "task.message" for w in wakes)


async def test_task_post_question_human_board_reply_unblocks_task_and_wakes_agent(
    coordinator_ctx, slack, client
):
    ctx = coordinator_ctx
    async with ctx.pool.connection() as conn:
        ada = await people.get(conn, "ada")
        t = await task_ledger.create(
            conn,
            workspace=ctx.workspace,
            viewer=Viewer("ada", "person:ada", team=True, human=True),
            list_key="bugs",
            title="Confirm retry policy",
            brief="Check retry limit before deploying.",
            holder="agent:build",
        )

    worker = await client(person="ada", agent="build")
    await worker.call(
        "task_post",
        {"id": t["id"], "text": "Should the retry cap be 3 or 5 attempts?"},
    )
    async with ctx.pool.connection() as conn:
        thread = await one(conn, "SELECT id FROM board_threads WHERE task_id=%s", (t["id"],))
        assert thread is not None

    # Human replies on the board thread -> task unblocks and agent receives a wake.
    async with ctx.pool.connection() as conn:
        await board.human_post(
            conn,
            workspace=ctx.workspace,
            person=ada,
            thread_id=thread["id"],
            text="Use 5 attempts.",
            reply_to_post_id=None,
            source_ref="web:test-unblock-1",
            via="web",
        )

    async with ctx.pool.connection() as conn:
        updated = await one(conn, "SELECT holder, waits_for FROM tasks WHERE id=%s", (t["id"],))
        assert updated == {"holder": "agent:build", "waits_for": "agent:build"}
        wakes = await all_rows(conn, "SELECT holder, reason FROM task_wakes WHERE task_id=%s", (t["id"],))
        assert any(w["holder"] == "agent:build" and w["reason"] == "task.message" for w in wakes)


async def test_github_waiting_person_label_mirrors_to_board_and_suppresses_raw_slack(
    coordinator_ctx, slack
):
    ctx = coordinator_ctx
    ctx.workspace.config.board.coordinator.settle_seconds = 300
    issue_url = "https://github.com/acme-example/engine/issues/77"
    async with ctx.pool.connection() as conn:
        t = await task_ledger.create(
            conn,
            workspace=ctx.workspace,
            viewer=Viewer("ada", "person:ada", team=True, human=True),
            list_key="bugs",
            title="Fix queue timeout",
            brief="Investigate worker timeout.",
            holder="agent:build",
            links=[{"kind": "ticket", "url": issue_url}],
        )
        await github_tasks.signal(
            ctx,
            conn,
            "acme-example/engine",
            {
                "number": 77,
                "html_url": issue_url,
                "state": "open",
                "labels": [{"name": "agent:needs-input"}],
                "comments_data": [
                    {
                        "id": 901,
                        "body": "Need confirmation on whether to raise the queue timeout to 60s.\n<!-- worker -->",
                        "user": {"login": "acme-bot"},
                    }
                ],
            },
        )
        ev = await one(
            conn,
            "SELECT kind, text, data FROM task_events WHERE task_id=%s AND kind='question' ORDER BY id DESC LIMIT 1",
            (t["id"],),
        )
        post = await one(
            conn,
            "SELECT kind, text, provenance FROM board_posts WHERE id=%s",
            (ev["data"]["board_post_id"],),
        )
    assert ev["data"]["routed_to_board"] is True
    assert post["kind"] == "question"
    assert post["provenance"]["via"] == "github_needs_input"
    assert "60s" in post["text"]

    # Raw Slack task sender sends nothing; coordinator scans immediately despite settle_seconds=300.
    await slack_tasks.tick(ctx)
    assert slack.calls == []
    await coordinator.scan(ctx)
    async with ctx.pool.connection() as conn:
        ep = await one(
            conn,
            "SELECT state FROM board_episodes WHERE post_id=%s",
            (ev["data"]["board_post_id"],),
        )
    assert ep == {"state": "open"}


async def test_board_native_and_legacy_board_asks_never_fail_delivery_or_pollute_digest(
    coordinator_ctx, client
):
    ctx = coordinator_ctx
    worker = await client(person="ada", agent="build")
    native = await worker.call("board_post", {"title": "Native board decision", **DECISION})
    shimmed = await worker.call(
        "ask_create",
        {
            "owner": "ben",
            "question": "Should we rotate the staging key?",
            "options": [{"id": "yes", "label": "Yes"}, {"id": "no", "label": "No"}],
        },
    )

    old_answered_at = datetime.now(UTC) - timedelta(hours=2)
    async with ctx.pool.connection() as conn:
        ben = await people.get(conn, "ben")
        # Simulate a legacy M1 board_post ask (no 'board:' prefix, provenance via='board_post').
        legacy_ask = await asks.create(
            conn,
            owner_person_id=ben["id"],
            question="Legacy M1 board decision?",
            options=[{"id": "yes", "label": "Yes"}, {"id": "no", "label": "No"}],
            source_ref="legacy-m1-decision-1",
        )
        legacy_thread = await one(
            conn,
            "INSERT INTO board_threads (title,visibility,created_by) VALUES ('Legacy M1','team','agent:build') RETURNING id",
        )
        await conn.execute(
            """INSERT INTO board_posts
            (thread_id,actor,at,provenance,kind,text,blocks,mentions,ask_id)
            VALUES (%s,'agent:build',now(),'{"via":"board_post"}'::jsonb,'decision_needed','Legacy M1','[]'::jsonb,%s,%s)""",
            (legacy_thread["id"], ["person:ben"], legacy_ask["id"]),
        )

        # Mark all three answered 2 hours ago with delivery_state='pending'.
        for aid in (native["ask_id"], legacy_ask["id"], shimmed["ask_id"]):
            await conn.execute(
                """UPDATE asks SET state='answered', answer_option='yes', answered_at=%s, delivery_state='pending'
                WHERE id=%s""",
                (old_answered_at, aid),
            )

        await asks.expire(conn)

        states = await all_rows(
            conn,
            "SELECT id, delivery_state FROM asks WHERE id = ANY(%s)",
            ([native["ask_id"], legacy_ask["id"], shimmed["ask_id"]],),
        )
        by_id = {row["id"]: row["delivery_state"] for row in states}
        assert by_id[native["ask_id"]] == "pending"
        assert by_id[legacy_ask["id"]] == "pending"
        assert by_id[shimmed["ask_id"]] == "failed"

        snap = await digest_snapshot.build(conn, ben, datetime.now(UTC), ctx.workspace)
        failed_titles = {
            item["title"]
            for item in snap["asks"]
            if item["reason"] == "The answer did not reach its agent."
        }
        assert DECISION["question"] not in failed_titles
        assert "Legacy M1 board decision?" not in failed_titles
        assert "Should we rotate the staging key?" in failed_titles


async def test_scout_finding_routes_through_chief_of_staff_and_buttons_create_task_or_dismiss(
    coordinator_ctx, slack, fake_brain
):
    from illo.deep import findings
    from illo.deep.worker import tick as deep_tick
    from illo.routines import scout

    ctx = coordinator_ctx
    finding_payload = {
        "key": "cos-api-retirement",
        "title": "Check deprecated API version",
        "message": "The upstream API retires next month. Verify whether our staging client still uses v1.",
        "links": [
            {"title": "Deprecation notice", "url": "https://example.com/deprecation"},
        ],
        "person": "ada",
    }

    async with ctx.pool.connection() as conn:
        await scout.queue(ctx, conn, person_key=None)
    fake_brain.script = [
        {"name": "findings_list", "arguments": {}},
        {"name": "finding_add", "arguments": finding_payload},
    ]
    await deep_tick(ctx)

    # Zero direct Slack posts from finding_add, findings.retry, or legacy asks/service.
    assert slack.calls == []
    await findings.retry(ctx)
    await service.tick(ctx)
    assert slack.calls == []

    async with ctx.pool.connection() as conn:
        sf = await one(conn, "SELECT * FROM scout_findings WHERE key='cos-api-retirement'")
        assert sf["state"] == "sent"
        assert sf["slack_ts"] is None
        ask = await one(conn, "SELECT * FROM asks WHERE source_ref=%s", (f"board:scout:{sf['id']}",))
        assert ask is not None
        assert ask["kind"] == "review"
        assert ask["allow_rule"] is False
        post = await one(conn, "SELECT * FROM board_posts WHERE ask_id=%s", (ask["id"],))
        assert post["kind"] == "decision_needed"
        assert post["provenance"]["via"] == "scout_finding"
        thread = await one(conn, "SELECT visibility FROM board_threads WHERE id=%s", (post["thread_id"],))
        assert thread["visibility"] == "team"
        ada = await people.get(conn, "ada")

    # Chief of Staff delivers ONE card with Make a task / Not useful buttons.
    event = await admission(ctx)
    res = await triage(
        ctx,
        event,
        "need_ask_person",
        person="person:ada",
        format="card",
        summary="Scout spotted an upstream API retirement affecting v1. Turn this into a task or dismiss?",
    )
    assert res["decision"] == "allow"
    await escalate.send_due(ctx)
    [root] = slack.roots()
    rendered = str(root)
    assert "Make a task" in rendered
    assert "Not useful" in rendered

    # Clicking "Make a task" updates scout_findings, acks the board-native ask, and runs gateway to create a task.
    fake_brain.script = [
        {"name": "task_create", "arguments": {"title": "Check deprecated API version", "list": "bugs"}}
    ]
    await actions.handle(
        ctx,
        ada,
        {"action_id": f"ask:{ask['id']}:opt:task", "channel": root["channel"], "message_ts": root["ts"]},
    )
    async with ctx.pool.connection() as conn:
        sf_after = await one(conn, "SELECT state, decided_by, decided_at FROM scout_findings WHERE id=%s", (sf["id"],))
        ask_after = await one(conn, "SELECT state, delivery_state FROM asks WHERE id=%s", (ask["id"],))
        created_task = await one(conn, "SELECT id, title FROM tasks WHERE title='Check deprecated API version'")
    assert sf_after["state"] == "task"
    assert sf_after["decided_by"] == ada["id"] and sf_after["decided_at"] is not None
    assert ask_after == {"state": "answered", "delivery_state": "acked"}
    assert created_task is not None

    # Personal scout finding stays in a private board thread and "Not useful" marks it dismissed with zero tasks.
    personal_payload = {**finding_payload, "key": "cos-personal-finding", "title": "Personal API note"}
    async with ctx.pool.connection() as conn:
        await scout.queue(ctx, conn, person_key="ada")
    fake_brain.script = [{"name": "finding_add", "arguments": personal_payload}]
    await deep_tick(ctx)
    async with ctx.pool.connection() as conn:
        sf2 = await one(conn, "SELECT * FROM scout_findings WHERE key='cos-personal-finding'")
        ask2 = await one(conn, "SELECT * FROM asks WHERE source_ref=%s", (f"board:scout:{sf2['id']}",))
        post2 = await one(conn, "SELECT thread_id FROM board_posts WHERE ask_id=%s", (ask2["id"],))
        thread2 = await one(conn, "SELECT visibility, owner_person_id FROM board_threads WHERE id=%s", (post2["thread_id"],))
    assert thread2 == {"visibility": "private", "owner_person_id": ada["id"]}

    event2 = await admission(ctx)
    assert (await triage(ctx, event2, "need_ask_person", person="person:ada", format="card", summary="Dismiss?"))["decision"] == "allow"
    await escalate.send_due(ctx)
    root2 = [r for r in slack.roots() if r["ts"] != root["ts"]][0]
    await actions.handle(
        ctx,
        ada,
        {"action_id": f"ask:{ask2['id']}:opt:dismiss", "channel": root2["channel"], "message_ts": root2["ts"]},
    )
    async with ctx.pool.connection() as conn:
        sf2_after = await one(conn, "SELECT state, decided_by FROM scout_findings WHERE id=%s", (sf2["id"],))
        task_count = await one(conn, "SELECT count(*) AS n FROM tasks")
    assert sf2_after == {"state": "dismissed", "decided_by": ada["id"]}
    assert task_count["n"] == 1


async def test_digest_and_team_digest_report_handled_without_you_with_privacy_isolation(
    coordinator_ctx, client
):
    from illo.routines.digest import fallback_text as personal_fallback
    from illo.routines.team_digest import fallback_text as team_fallback

    ctx = coordinator_ctx
    async with ctx.pool.connection() as conn:
        ada = await people.get(conn, "ada")
        ben = await people.get(conn, "ben")
        # 1) Team factual question mentioning Ada, answered by Chief of Staff ('illo').
        t_team_ada = await one(
            conn,
            "INSERT INTO board_threads (title,visibility,created_by) VALUES ('Team Q Ada','team','agent:build') RETURNING id",
        )
        q_team_ada = await one(
            conn,
            """INSERT INTO board_posts (thread_id,actor,at,kind,text,mentions)
            VALUES (%s,'agent:build',now(),'question','What port?',%s) RETURNING id""",
            (t_team_ada["id"], ["person:ada"]),
        )
        await conn.execute(
            """INSERT INTO board_posts (thread_id,actor,at,is_human,kind,text,reply_to_post_id)
            VALUES (%s,'illo',now(),false,'answer','Port 8080.',%s)""",
            (t_team_ada["id"], q_team_ada["id"]),
        )

        # 2) Team decision_needed mentioning Ben, resolved by Stage 0 rule ('illo').
        t_team_ben = await one(
            conn,
            "INSERT INTO board_threads (title,visibility,created_by) VALUES ('Team D Ben','team','agent:build') RETURNING id",
        )
        rule_ask = await asks.create(
            conn,
            owner_person_id=ben["id"],
            question="Ship?",
            options=[{"id": "yes", "label": "Yes"}, {"id": "no", "label": "No"}],
        )
        q_team_ben = await one(
            conn,
            """INSERT INTO board_posts (thread_id,actor,at,kind,text,mentions,ask_id)
            VALUES (%s,'agent:build',now(),'decision_needed','Ship?',%s,%s) RETURNING id""",
            (t_team_ben["id"], ["person:ben"], rule_ask["id"]),
        )
        await conn.execute(
            """INSERT INTO board_posts (thread_id,actor,at,is_human,provenance,kind,text,reply_to_post_id)
            VALUES (%s,'illo',now(),false,'{"via":"rule"}'::jsonb,'decision','Yes',%s)""",
            (t_team_ben["id"], q_team_ben["id"]),
        )

        # 3) Private thread owned by Ada, answered by Chief of Staff ('illo').
        t_priv_ada = await one(
            conn,
            """INSERT INTO board_threads (title,visibility,owner_person_id,created_by)
            VALUES ('Private Ada','private',%s,'agent:build') RETURNING id""",
            (ada["id"],),
        )
        q_priv_ada = await one(
            conn,
            """INSERT INTO board_posts (thread_id,actor,at,kind,text,mentions)
            VALUES (%s,'agent:build',now(),'question','Private question?',%s) RETURNING id""",
            (t_priv_ada["id"], ["person:ada"]),
        )
        await conn.execute(
            """INSERT INTO board_posts (thread_id,actor,at,is_human,kind,text,reply_to_post_id)
            VALUES (%s,'illo',now(),false,'answer','Private answer.',%s)""",
            (t_priv_ada["id"], q_priv_ada["id"]),
        )

        now = datetime.now(UTC)
        ada_snap = await digest_snapshot.build(conn, ada, now, ctx.workspace)
        ben_snap = await digest_snapshot.build(conn, ben, now, ctx.workspace)
        team_snap = await digest_snapshot.team_snapshot(conn, ctx.workspace, now)

    assert ada_snap["handled_without_you"] == 2
    assert ben_snap["handled_without_you"] == 1
    assert team_snap["handled_without_you"] == 2
    assert "Handled without you: 2." in personal_fallback(ada_snap)
    assert "Handled without you: 1." in personal_fallback(ben_snap)
    assert "Handled without you: 2." in team_fallback(team_snap)

