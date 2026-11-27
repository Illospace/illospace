import pytest

from illo.db import one
from illo.ledger import asks, rules
from tests.mcp.client import AgentClient

ASK = {
    "lane": {"source": "t3", "external_id": "acme-1"},
    "question": "Which test target should I use?",
    "options": [{"id": "preview", "label": "Preview"}, {"id": "local", "label": "Local"}],
    "recommended": "preview",
    "source_ref": "acme-1:question",
}


async def test_collector_can_omit_a_recommendation(app, http):
    agent = await AgentClient.create(app, http)
    args = {key: value for key, value in ASK.items() if key != "recommended"}
    created = await agent.call("ask_create", args)
    assert created["state"] == "open"
    async with app.state.ctx.pool.connection() as conn:
        assert (await one(conn, "SELECT recommended_option FROM asks"))[
            "recommended_option"
        ] is None


async def test_ask_creation_idempotency_and_cancel(app, http):
    agent = await AgentClient.create(app, http)
    created = await agent.call("ask_create", ASK)
    again = await agent.call("ask_create", ASK)
    assert created["ask_id"] == again["ask_id"] and again["state"] == "open"
    async with app.state.ctx.pool.connection() as conn:
        row = await one(conn, "SELECT * FROM asks")
        lane = await one(conn, "SELECT * FROM lanes")
        assert row["requester_token_id"] == agent.token_id
        assert row["requester_person_id"] == row["owner_person_id"] == agent.person_id
        assert lane["external_id"] == "acme-1" and lane["machine"] == "acme-laptop"
    result = await agent.call("ask_cancel", {"source_ref": ASK["source_ref"]})
    assert result["state"] == "cancelled"
    result = await agent.call("ask_cancel", {"ask_id": created["ask_id"]})
    assert result["state"] == "cancelled"


@pytest.mark.parametrize(
    "updates",
    [
        {"options": [{"id": "a", "label": "A"}]},
        {"options": [{"id": str(i), "label": str(i)} for i in range(6)]},
        {"recommended": "unknown"},
        {"question": "First question? Second question?"},
        {"question": ""},
        {"question": "x" * 241},
        {"options": [{"id": "a", "label": "A"}, {"id": "a", "label": "B"}]},
        {"options": [{"id": "a"}, {"id": "b", "label": "B"}]},
        {"due_at": "2026-11-10"},
        {"expires_at": "bad-date"},
    ],
)
async def test_ask_validation(app, http, updates):
    agent = await AgentClient.create(app, http)
    await agent.call("ask_create", {**ASK, **updates}, error=True)
    async with app.state.ctx.pool.connection() as conn:
        assert (await one(conn, "SELECT count(*) AS n FROM asks"))["n"] == 0
        assert (await one(conn, "SELECT count(*) AS n FROM lanes"))["n"] == 0


async def test_pull_redelivery_person_scope_and_ack(app, http):
    agent = await AgentClient.create(app, http)
    other_machine = await AgentClient.create(app, http, label="acme-desktop")
    same_machine = await AgentClient.create(app, http)
    created = await agent.call("ask_create", ASK)
    ask_id = created["ask_id"]
    async with app.state.ctx.pool.connection() as conn:
        await asks.answer(
            conn, ask_id, person_id=agent.person_id, option="preview", text="Use the preview."
        )
    assert (await other_machine.call("ask_pull"))["answers"] == []
    pulled = await same_machine.call("ask_pull")
    answer = pulled["answers"][0]
    assert (
        answer["answer_option"]["id"] == "preview" and answer["answer_option"]["label"] == "Preview"
    )
    assert answer["answered_by"] == "ada" and answer["answered_via"] == "person"
    assert answer["lane"] == ASK["lane"] and answer["source_ref"] == ASK["source_ref"]
    assert (await agent.call("ask_pull"))["answers"] == pulled["answers"]
    assert (await other_machine.call("ask_pull", {"scope": "person"}))["answers"] == pulled[
        "answers"
    ]
    async with app.state.ctx.pool.connection() as conn:
        assert (await one(conn, "SELECT delivery_attempts FROM asks"))["delivery_attempts"] == 3
    ack = await other_machine.call("ask_ack", {"ask_id": ask_id})
    assert ack == {"ask_id": ask_id, "delivery_state": "acked"}
    assert await other_machine.call("ask_ack", {"ask_id": ask_id}) == ack
    assert (await agent.call("ask_pull"))["answers"] == []
    assert (await agent.call("ask_cancel", {"ask_id": ask_id}))["state"] == "answered"
    replay = await agent.call("ask_create", ASK)
    assert replay["state"] == "answered" and replay["answer_option"]["id"] == "preview"


async def test_rule_answer_has_its_rule_text(app, http):
    agent = await AgentClient.create(app, http)
    created = await agent.call("ask_create", ASK)
    async with app.state.ctx.pool.connection() as conn:
        rule = await rules.create(
            conn,
            owner_person_id=agent.person_id,
            text="Use the preview for test checks.",
            answer_option_label="Preview",
            match={"lane_source": "t3"},
        )
        await asks.answer_with_rule(conn, created["ask_id"], rule["id"])
    result = await agent.call("ask_pull")
    assert result["answers"][0]["rule_text"] == rule["text"]
    assert result["answers"][0]["answered_via"] == "rule"


async def test_expired_answers_do_not_redeliver(app, http):
    agent = await AgentClient.create(app, http)
    created = await agent.call("ask_create", ASK)
    async with app.state.ctx.pool.connection() as conn:
        await asks.answer(conn, created["ask_id"], person_id=agent.person_id, option="preview")
        await conn.execute("UPDATE asks SET expires_at=now()-interval '1 minute'")
    assert (await agent.call("ask_pull"))["answers"] == []


async def test_personal_agent_answers_board_decision_while_autonomous_worker_is_blocked(app, http):
    from illo.workspace import Agent, Wake

    ws = app.state.ctx.workspace.model_copy(deep=True)
    ws.agents = [
        Agent(
            key="worker",
            name="Server worker",
            owner="ada",
            wake=Wake(type="github_issue", assignee="ada", repos=["acme/app"]),
        ),
        Agent(key="ada-desk", name="Ada's desk agent", owner="ada", wake=Wake(type="poll")),
        Agent(key="ben-desk", name="Ben's desk agent", owner="ben", wake=Wake(type="poll")),
    ]
    app.state.ctx.workspace = ws

    worker = await AgentClient.create(app, http, person="ada", label="srv-worker", agent_key="worker")
    ada_desk = await AgentClient.create(app, http, person="ada", label="ada-mac", agent_key="ada-desk")
    ben_desk = await AgentClient.create(app, http, person="ben", label="ben-mac", agent_key="ben-desk")

    # Autonomous worker does not see ask_answer / ask_reassign and cannot call them.
    worker_tools = {t["name"] for t in (await worker.rpc("tools/list")).json()["result"]["tools"]}
    assert "ask_answer" not in worker_tools and "ask_reassign" not in worker_tools
    worker_caps = await worker.call("illo_read", {"capability": "capabilities"})
    assert "ask_answer" not in worker_caps["mcp_tools"]
    assert next(a for a in worker_caps["agents"] if a["key"] == "worker")["acts_for_owner"] is False
    assert next(a for a in worker_caps["agents"] if a["key"] == "ada-desk")["acts_for_owner"] is True

    # Worker opens a board decision_needed post for Ada.
    posted = await worker.call(
        "board_post",
        {
            "title": "Design choice",
            "text": "Pick v1 or v2.",
            "kind": "decision_needed",
            "mentions": ["person:ada"],
            "question": "Which version should we ship?",
            "options": [
                {"id": "v1", "label": "Version 1"},
                {"id": "v2", "label": "Version 2"},
            ],
            "recommended": "v1",
        },
    )
    ask_id = posted["ask_id"]
    thread_id = posted["thread_id"]

    # Autonomous worker cannot answer Ada's ask.
    err_worker = await worker.call("ask_answer", {"ask_id": ask_id, "option": "v1"}, error=True)
    assert "does not allow" in err_worker["error"]["message"]

    # Another person's personal agent cannot answer Ada's ask.
    err_ben = await ben_desk.call("ask_answer", {"ask_id": ask_id, "option": "v2"}, error=True)
    assert "assigned to your person" in err_ben["error"]["message"]

    # Secrets in answer text are blocked deterministically.
    err_secret = await ada_desk.call(
        "ask_answer",
        {"ask_id": ask_id, "text": "Use ghp_abcdefghijklmnopqrstuvwxyz123456"},
        error=True,
    )
    assert "secret" in err_secret["error"]["message"].lower()

    # Ada's personal interactive agent answers the board decision on Ada's behalf.
    answered = await ada_desk.call(
        "ask_answer",
        {"ask_id": ask_id, "option": "v2", "text": "Ship v2 with clean defaults."},
    )
    assert answered["answered"] is True
    assert answered["state"] == "answered"
    assert answered["answer_option"]["id"] == "v2"
    assert answered["board_post_id"] > posted["post_id"]

    async with app.state.ctx.pool.connection() as conn:
        post = await one(conn, "SELECT * FROM board_posts WHERE id=%s", (answered["board_post_id"],))
        assert post["kind"] == "decision" and post["is_human"] is True
        assert post["actor"] == "person:ada" and post["reply_to_post_id"] == posted["post_id"]
        assert post["provenance"]["via"] == "personal_agent"
        assert post["provenance"]["agent_key"] == "ada-desk"

    # The thread can now be resolved because its decision has an answer.
    resolved = await worker.call(
        "board_resolve", {"thread_id": thread_id, "state": "resolved", "summary": "Done."}
    )
    assert resolved["state"] == "resolved"


async def test_personal_agent_cannot_self_answer_and_can_reassign_ask(app, http):
    from illo.workspace import Agent, Wake

    ws = app.state.ctx.workspace.model_copy(deep=True)
    ws.agents = [
        Agent(key="ada-desk", name="Ada's desk agent", owner="ada", wake=Wake(type="poll")),
        Agent(key="ben-desk", name="Ben's desk agent", owner="ben", wake=Wake(type="poll")),
    ]
    app.state.ctx.workspace = ws

    ada_desk_1 = await AgentClient.create(app, http, person="ada", label="mac-1", agent_key="ada-desk")
    ada_desk_2 = await AgentClient.create(app, http, person="ada", label="mac-2", agent_key="ada-desk")
    ben_desk = await AgentClient.create(app, http, person="ben", label="ben-mac", agent_key="ben-desk")

    created = await ada_desk_1.call("ask_create", ASK)
    ask_id = created["ask_id"]

    # Same token cannot self-answer.
    err_same = await ada_desk_1.call("ask_answer", {"ask_id": ask_id, "option": "preview"}, error=True)
    assert "created itself" in err_same["error"]["message"]

    # Another token with the same agent_key cannot self-answer either.
    err_same_agent = await ada_desk_2.call(
        "ask_answer", {"ask_id": ask_id, "option": "preview"}, error=True
    )
    assert "same agent" in err_same_agent["error"]["message"]

    # Private-lane ask cannot be reassigned to another teammate.
    err_private = await ada_desk_1.call(
        "ask_reassign", {"ask_id": ask_id, "owner": "ben"}, error=True
    )
    assert "lane is not visible" in err_private["error"]["message"]

    # Reassigning a team-visible ask to Ben transfers ownership so Ben's personal agent can answer it.
    team_ask = {key: value for key, value in ASK.items() if key != "lane"} | {"source_ref": "team-q"}
    team_ask_id = (await ada_desk_1.call("ask_create", team_ask))["ask_id"]
    reassigned = await ada_desk_1.call("ask_reassign", {"ask_id": team_ask_id, "owner": "ben"})
    assert reassigned["reassigned"] is True
    assert reassigned["ask_id"] == team_ask_id
    assert reassigned["owner"] == "ben"
    assert isinstance(reassigned.get("thread_id"), int)

    answered = await ben_desk.call(
        "ask_answer", {"ask_id": team_ask_id, "option": "local", "text": "Run local checks."}
    )
    assert answered["answered"] is True and answered["answer_option"]["id"] == "local"

