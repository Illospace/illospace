"""End-to-end Chief-of-Staff tests for the single triage and escalation path."""

from illo.asks import actions, service
from illo.board import acl, coordinator, escalate
from illo.db import all_rows, one
from illo.ledger import people, rules
from tests.board.test_board import DECISION, TICKET
from tests.board.test_coordinator import admission, last_run, triage
from tests.board.test_escalate import escalation


async def test_chief_of_staff_answers_factual_question_and_wakes_agent_without_slack(
    coordinator_ctx, slack, client, fake_brain
):
    ctx = coordinator_ctx
    async with ctx.pool.connection() as conn:
        await conn.execute(
            """INSERT INTO tasks (list_key, title, brief, created_by, visibility)
               VALUES ('bugs', 'Staging cache setup',
                       'The staging cache runs on Redis 7.2 in eu-west-1.',
                       'person:ada', 'team')"""
        )
    worker = await client(agent="build")
    q = await worker.call(
        "board_post",
        {
            "ticket_url": TICKET,
            "kind": "question",
            "text": "Which Redis version and region does the staging cache use?",
        },
    )
    fake_brain.script = [
        {"name": "task_list", "arguments": {"list_key": "bugs"}},
        {
            "name": "need_answer",
            "arguments": {
                "answer": "The staging cache runs on Redis 7.2 in eu-west-1.",
                "sources": ["task:Staging cache setup"],
            },
        },
    ]

    await coordinator.run_coordinator(ctx)
    await escalate.send_due(ctx)

    assert len(fake_brain.requests) == 1
    assert slack.roots() == []
    assert (await last_run(ctx))["outcome"] == "applied"
    async with ctx.pool.connection() as conn:
        assert not await one(conn, "SELECT 1 FROM board_escalations")
        ep = await one(conn, "SELECT state, closed_reason FROM board_episodes WHERE post_id=%s", (q["post_id"],))
        assert ep == {"state": "closed", "closed_reason": "answered"}
        answer = await one(
            conn,
            "SELECT actor, kind, is_human, reply_to_post_id, text, provenance "
            "FROM board_posts WHERE thread_id=%s AND actor='illo'",
            (q["thread_id"],),
        )
        assert answer["kind"] == "answer"
        assert answer["is_human"] is False
        assert answer["reply_to_post_id"] == q["post_id"]
        assert "Redis 7.2" in answer["text"]
        assert answer["provenance"]["sources"] == ["task:Staging cache setup"]
        wakes = await all_rows(conn, "SELECT episode_key, target_ref FROM board_wakes WHERE thread_id=%s",
                               (q["thread_id"],))
        assert len(wakes) == 1 and wakes[0]["episode_key"] == f"post:{q['post_id']}"


async def test_stage0_standing_rule_auto_answers_decision_needed_without_llm_or_slack(
    coordinator_ctx, slack, client, fake_brain
):
    ctx = coordinator_ctx
    async with ctx.pool.connection() as conn:
        ben = await people.get(conn, "ben")
        rule = await rules.create(
            conn,
            owner_person_id=ben["id"],
            text="Always choose the first release date when asked.",
            answer_option_label="First date",
        )
    worker = await client(agent="build")
    d = await worker.call("board_post", {"ticket_url": TICKET, **DECISION})

    # The asks worker must never touch board decision asks directly.
    await service.tick(ctx)
    async with ctx.pool.connection() as conn:
        still_open = await one(conn, "SELECT state, slack_ts FROM asks WHERE id=%s", (d["ask_id"],))
    assert still_open == {"state": "open", "slack_ts": None}

    # Stage 0 resolves the decision deterministically with zero LLM calls and zero Slack DMs.
    await coordinator.run_coordinator(ctx)
    await escalate.send_due(ctx)

    assert fake_brain.requests == []
    assert slack.roots() == []
    async with ctx.pool.connection() as conn:
        ask = await one(
            conn,
            "SELECT state, answer_option, rule_id, delivery_state FROM asks WHERE id=%s",
            (d["ask_id"],),
        )
        assert ask == {
            "state": "answered",
            "answer_option": "first",
            "rule_id": rule["id"],
            "delivery_state": "acked",
        }
        post = await one(
            conn,
            "SELECT actor, kind, is_human, text, reply_to_post_id, provenance "
            "FROM board_posts WHERE thread_id=%s AND kind='decision'",
            (d["thread_id"],),
        )
        assert post["actor"] == "illo"
        assert post["is_human"] is False
        assert post["text"] == "First date"
        assert post["reply_to_post_id"] == d["post_id"]
        assert post["provenance"]["via"] == "rule"
        assert post["provenance"]["rule_id"] == rule["id"]
        assert not await one(conn, "SELECT 1 FROM board_escalations")
        wakes = await all_rows(conn, "SELECT episode_key FROM board_wakes WHERE thread_id=%s", (d["thread_id"],))
        assert [w["episode_key"] for w in wakes] == [f"post:{d['post_id']}"]


async def test_decision_needed_fact_vs_consent_and_single_decision_card_flow(
    coordinator_ctx, slack, client
):
    ctx = coordinator_ctx
    worker = await client(agent="build")
    d = await worker.call("board_post", {"title": "Release date", **DECISION})

    # Fact-vs-consent boundary: need_answer and need_route must be refused on decision_needed.
    event1 = await admission(ctx)
    refused_answer = await triage(
        ctx, event1, "need_answer", answer="Pick the first date.", sources=["note:release"]
    )
    assert refused_answer["decision"] == "deny"
    assert "decision_needed" in refused_answer["reason"]

    async with ctx.pool.connection() as conn:
        await conn.execute("UPDATE board_episodes SET errors=0, next_check_at=now()")
    event2 = await admission(ctx)
    refused_route = await triage(ctx, event2, "need_route", target="agent:studio", minutes=15)
    assert refused_route["decision"] == "deny"

    # Now Chief of Staff sends one context-rich card bound to the original ask_id.
    async with ctx.pool.connection() as conn:
        await conn.execute("UPDATE board_episodes SET errors=0, next_check_at=now()")
    event3 = await admission(ctx)
    res = await triage(
        ctx,
        event3,
        "need_ask_person",
        person="person:ben",
        format="card",
        summary=(
            "The build agent finished staging prep in https://github.com/acme-example/engine/pull/42 "
            "and needs you to pick the release window. I recommend the second date to leave buffer."
        ),
        recommended=1,
    )
    assert res["decision"] == "allow"
    assert (await last_run(ctx))["outcome"] == "applied"

    await escalate.send_due(ctx)
    [root] = slack.roots()
    esc = await escalation(ctx)
    assert esc["state"] == "sent"
    assert esc["ask_id"] == d["ask_id"] == esc["card_ask_id"]

    # Verify single asks row, rich summary with https link, primary recommended button, Always, and Open the thread.
    async with ctx.pool.connection() as conn:
        ask_rows = await all_rows(conn, "SELECT * FROM asks")
        ben = await people.get(conn, "ben")
    assert len(ask_rows) == 1
    ask = ask_rows[0]
    assert ask["id"] == d["ask_id"]
    assert ask["slack_ts"] == root["ts"] and ask["slack_channel"] == root["channel"]
    assert ask["recommended_option"] == "second"
    assert ask["allow_rule"] is True

    rendered = str(root)
    assert "https://github.com/acme-example/engine/pull/42" in rendered
    assert "Which date works for the release?" in rendered
    assert f"web-open-board:{d['thread_id']}" in rendered
    assert f"ask:{ask['id']}:always" in rendered
    action_blocks = [b for b in root["blocks"] if b["type"] == "actions"]
    opt_buttons = [el for el in action_blocks[0]["elements"] if el["action_id"].startswith(f"ask:{ask['id']}:opt:")]
    assert [b["text"]["text"] for b in opt_buttons] == ["Second date", "First date"]
    assert opt_buttons[0].get("style") == "primary"
    assert "style" not in opt_buttons[1]

    # Clicking the option writes ONE decision post (no duplicate human_post) and closes escalation + acks ask.
    await actions.handle(
        ctx,
        ben,
        {"action_id": f"ask:{ask['id']}:opt:second", "channel": ask["slack_channel"], "message_ts": ask["slack_ts"]},
    )
    async with ctx.pool.connection() as conn:
        posts = await all_rows(
            conn, "SELECT kind, actor, text, is_human FROM board_posts WHERE thread_id=%s ORDER BY id",
            (d["thread_id"],),
        )
        final_ask = await one(conn, "SELECT state, delivery_state FROM asks WHERE id=%s", (ask["id"],))
        final_esc = await one(conn, "SELECT state, answer_post_id FROM board_escalations WHERE id=%s", (esc["id"],))
    assert [p["kind"] for p in posts] == ["decision_needed", "message", "decision"]
    assert posts[1]["actor"] == "illo" and posts[1]["is_human"] is False
    assert sum(p["is_human"] for p in posts) == 1
    assert posts[-1] == {"kind": "decision", "actor": "person:ben", "text": "Second date", "is_human": True}
    assert final_ask == {"state": "answered", "delivery_state": "acked"}
    assert final_esc["state"] == "answered" and final_esc["answer_post_id"] is not None


async def test_decision_needed_mandatory_verbatim_fallback_delivers_actionable_card(
    coordinator_ctx, slack, client, fake_brain
):
    ctx = coordinator_ctx
    worker = await client(agent="build")
    d = await worker.call(
        "board_post",
        {
            "title": "Model staging rollout",
            **DECISION,
            "text": "Staging migration is ready; need go/no-go before Friday promo.",
        },
    )

    # Two consecutive runs where the model produces no tool action trigger immediate verbatim fallback.
    fake_brain.script = []
    fake_brain.final_text = "I forgot to call a tool."
    await coordinator.run_coordinator(ctx)
    async with ctx.pool.connection() as conn:
        ep1 = await one(conn, "SELECT errors, next_check_at FROM board_episodes WHERE post_id=%s", (d["post_id"],))
        assert ep1["errors"] == 1
        await conn.execute("UPDATE board_episodes SET next_check_at=now() WHERE post_id=%s", (d["post_id"],))

    # On the 2nd consecutive error, the same coordinator pass triggers immediate verbatim fallback (zero delay).
    await coordinator.run_coordinator(ctx)
    assert len(fake_brain.requests) == 2
    async with ctx.pool.connection() as conn:
        ep2 = await one(conn, "SELECT state, errors FROM board_episodes WHERE post_id=%s", (d["post_id"],))
    assert ep2 == {"state": "escalated", "errors": 2}
    esc = await escalation(ctx)
    assert esc["origin"] == "fallback"
    assert esc["format"] == "card"
    assert esc["card_ask_id"] == d["ask_id"]
    assert "Staging migration is ready" in esc["text"]

    await escalate.send_due(ctx)
    [root] = slack.roots()
    async with ctx.pool.connection() as conn:
        ask = await one(conn, "SELECT id, slack_ts, slack_channel FROM asks WHERE id=%s", (d["ask_id"],))
    assert ask["slack_ts"] == root["ts"] and ask["slack_channel"] == root["channel"]
    assert "Staging migration is ready" in str(root)
    assert "First date" in str(root) and "Second date" in str(root)


async def test_private_thread_with_guest_isolates_owner_private_data(
    coordinator_ctx, slack, client, fake_brain
):
    ctx = coordinator_ctx
    ada = await client("ada")
    ben = await client("ben")
    chloe = await client("chloe")

    # Ada creates a private thread and has a private task that must NEVER leak when a guest joins.
    q = await ada.call(
        "board_post",
        {"title": "Private sync", "text": "Can Ben confirm the launch checklist?", "kind": "question",
         "visibility": "private"},
    )
    async with ctx.pool.connection() as conn:
        await conn.execute(
            """INSERT INTO tasks (list_key, title, brief, created_by, owner_person_id, visibility)
               VALUES ('bugs', 'Personal secret', 'ADA_PRIVATE_SECRET_999', 'person:ada', %s, 'private')""",
            (ada.person_id,),
        )
        await conn.execute(
            "INSERT INTO board_thread_guests (thread_id, person_id) VALUES (%s, %s)",
            (q["thread_id"], ben.person_id),
        )
        access = await acl.thread_access(conn, q["thread_id"])
    assert access["visibility"] == "private"
    assert access["effective_owner"] is None
    assert access["guest_person_ids"] == [ben.person_id]

    # Owner and guest can read the thread; non-guest 3rd party cannot.
    assert (await ada.call("board_read", {"thread_id": q["thread_id"]}))["thread"]["id"] == q["thread_id"]
    assert (await ben.call("board_read", {"thread_id": q["thread_id"]}))["thread"]["id"] == q["thread_id"]
    assert await chloe.call("board_read", {"thread_id": q["thread_id"]}, error=True)

    # Chief of Staff runs with team-only read authority (personal_read=False) so Ada's private task is invisible,
    # and candidates are strictly owner + guest (never Chloe).
    fake_brain.script = [
        {"name": "task_list", "arguments": {"list_key": "bugs"}},
        {"name": "search_all", "arguments": {"query": "ADA_PRIVATE_SECRET_999"}},
        {
            "name": "need_ask_person",
            "arguments": {
                "person": "person:ben",
                "format": "dm",
                "summary": "Ada asked if you can confirm the launch checklist.",
            },
        },
    ]
    await coordinator.run_coordinator(ctx)

    run = await last_run(ctx)
    assert run["outcome"] == "applied"
    assert set(run["snapshot"]["candidates"]) == {"person:ada", "person:ben"}
    assert "person:chloe" not in run["snapshot"]["candidates"]
    assert "ADA_PRIVATE_SECRET_999" not in str(fake_brain.results)
    async with ctx.pool.connection() as conn:
        ledger = await one(conn, "SELECT principal, person_id, personal_read FROM runs WHERE id=%s", (run["run_id"],))
    assert ledger == {"principal": "team", "person_id": None, "personal_read": False}

    await escalate.send_due(ctx)
    [root] = slack.roots()
    assert root["channel"] == "DU_ACME_BEN"


async def test_reassigning_board_decision_ask_preserves_thread_link_and_wakes_agent(
    coordinator_ctx, slack, client, fake_brain
):
    from illo.ledger import runs
    from illo.tools.registry import ToolContext

    ctx = coordinator_ctx
    worker = await client(agent="build")
    decision_for_ada = {**DECISION, "mentions": ["person:ada"]}
    d = await worker.call("board_post", {"ticket_url": TICKET, **decision_for_ada})

    # Coordinator escalates the board-native decision card to Ada.
    fake_brain.script = [
        {
            "name": "need_ask_person",
            "arguments": {
                "person": "person:ada",
                "format": "card",
                "summary": "Build needs a release date decision for ticket #12.",
            },
        }
    ]
    await coordinator.run_coordinator(ctx)
    await escalate.send_due(ctx)
    [ada_root] = slack.roots()
    assert ada_root["channel"] == "DU_ACME_ADA"

    # Ada reassigns the board-native decision ask to Ben (even after personal_read=true).
    async with ctx.pool.connection() as conn:
        ada = await people.get(conn, "ada")
        run = await runs.create(
            conn,
            principal="person:ada",
            person_id=ada["id"],
            profile="person_dm",
            provider="fake",
            tool_limit=12,
            model="fake",
        )
        await conn.execute("UPDATE runs SET personal_read=true WHERE id=%s", (run["id"],))
        run["personal_read"] = True
        res = await ctx.registry.call(
            ToolContext(conn, run, ctx),
            "asks_reassign",
            {"id": d["ask_id"], "owner": "ben"},
        )
    assert res["decision"] == "allow"
    assert res["result"] == {"reassigned": True, "ask_id": d["ask_id"], "owner": "ben", "thread_id": d["thread_id"]}

    # Old card in Ada's DM was updated; escalate.send_due delivers the card to Ben's DM.
    await escalate.send_due(ctx)
    roots = slack.roots()
    assert len(roots) == 2
    ben_root = roots[-1]
    assert ben_root["channel"] == "DU_ACME_BEN"

    # Ben clicks an option on the reassigned card -> decision posts to the original board thread & wakes worker.
    async with ctx.pool.connection() as conn:
        ben = await people.get(conn, "ben")
    await actions.handle(
        ctx,
        ben,
        {
            "action_id": f"ask:{d['ask_id']}:opt:first",
            "channel": ben_root["channel"],
            "message_ts": ben_root["ts"],
        },
    )
    async with ctx.pool.connection() as conn:
        ask = await one(conn, "SELECT state, owner_person_id, answer_option FROM asks WHERE id=%s", (d["ask_id"],))
        post = await one(
            conn,
            "SELECT actor, kind, reply_to_post_id, text FROM board_posts WHERE thread_id=%s AND kind='decision'",
            (d["thread_id"],),
        )
        wakes = await all_rows(conn, "SELECT target_ref FROM board_wakes WHERE thread_id=%s", (d["thread_id"],))
    assert ask == {"state": "answered", "owner_person_id": ben["id"], "answer_option": "first"}
    assert post == {
        "actor": "person:ben",
        "kind": "decision",
        "reply_to_post_id": d["post_id"],
        "text": "First date",
    }
    assert len(wakes) >= 1


async def test_ticket_requester_and_their_agent_prioritized_in_board_routing(
    coordinator_ctx, client
):
    from illo.board import packet
    from illo.ledger import github as github_ledger

    ctx = coordinator_ctx
    async with ctx.pool.connection() as conn:
        ben = await people.get(conn, "ben")
        # Sync the GitHub issue authored by Ben (assigned to Ada).
        await github_ledger.sync_item(
            conn,
            "acme-example/engine",
            {
                "number": 12,
                "title": "Export timeout fix",
                "state": "open",
                "body": "Please fix export timeout.",
                "html_url": TICKET,
                "user": {"login": ben["github_login"]},
                "assignees": [{"login": "ada-gh"}],
            },
            {},
        )
    # Ben has an online personal agent token; Ada's build worker asks an unmentioned question on TICKET.
    ben_agent = await client("ben")
    await ben_agent.call("board_inbox", {})
    worker = await client(agent="build")
    q = await worker.call(
        "board_post",
        {"ticket_url": TICKET, "kind": "question", "text": "Should export retry on 504 or fail fast?"},
    )
    await coordinator.scan(ctx)
    async with ctx.pool.connection() as conn:
        thread = await acl.thread_ok(conn, q["thread_id"])
        ep = await one(conn, "SELECT * FROM board_episodes WHERE post_id=%s", (q["post_id"],))
        text, snap = await packet.build(conn, ctx.workspace, thread, ep)

    # Ticket requester (Ben) comes first ahead of worker owner (Ada), and Ben's online agent is routable.
    assert snap["candidates"][0] == "person:ben"
    assert snap["allowed"]["headstart"] is True
    assert "person:ben" in snap["targets"]
    assert '"requester":"person:ben"' in text

