"""One personal brief uses one snapshot and one delivery key."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from illo.brain.client import BrainEvent, ProviderAuth
from illo.db import one
from illo.gateway.principal import Authority
from illo.ledger import lanes, people, sensors
from illo.ledger.digest_snapshot import sensor_fault
from illo.routines.digest import DEFAULT_INSTRUCTIONS, fallback_text, send_due, snapshot
from illo.routines.scheduler import tick_routines
from illo.tools._format import short
from illo.workspace import Routine

NOW = datetime(2026, 11, 10, 9, tzinfo=UTC)


def empty(**changes):
    return {
        "name": "Ada",
        "needs_you": [],
        "stuck": [],
        "finished": [],
        "finished_total": 0,
        "faults": [],
        **changes,
    }


def test_sensor_fault_rules():
    base = {"key": "asks:slack", "last_ok_at": NOW - timedelta(days=2), "last_error_at": None}
    assert sensor_fault(base, NOW) is None
    assert sensor_fault({**base, "last_ok_at": None}, NOW) is None
    failed = sensor_fault({**base, "last_error_at": NOW}, NOW)
    assert failed["since"] == NOW
    assert "asks:slack" not in failed["what"]
    assert "ask card" in failed["what"]
    assert sensor_fault({**base, "last_error_at": NOW - timedelta(days=3)}, NOW) is None
    # An event failure appears in one brief only.
    old = {**base, "last_error_at": NOW - timedelta(hours=30)}
    assert sensor_fault(old, NOW, since=NOW - timedelta(hours=24)) is None
    assert sensor_fault(old, NOW, since=NOW - timedelta(hours=48))["since"] == old["last_error_at"]
    timer = {**base, "key": "job:example", "expected_every_s": 60}
    stopped = {**timer, "last_error_at": NOW - timedelta(hours=30)}
    assert sensor_fault(stopped, NOW, since=NOW - timedelta(hours=24)) is not None
    assert sensor_fault(timer, NOW)["what"] == "A background job of Illo stopped."
    assert sensor_fault({**timer, "last_ok_at": NOW - timedelta(minutes=29)}, NOW) is None
    assert sensor_fault({**timer, "expected_every_s": 86400}, NOW) is None
    assert (
        sensor_fault({**base, "key": "chatgpt:ada", "last_error_at": NOW}, NOW, {"ada": "Ada"})[
            "what"
        ]
        == "The ChatGPT sign-in of Ada failed."
    )


@pytest.mark.parametrize("group", ["needs_you", "stuck"])
def test_fallback_never_says_nothing_when_work_waits(group):
    item = {
        "title": "Review the plan",
        "link": "https://example.com/plan",
        "kind": "review",
        "hours_since_signal": 11,
    }
    text = fallback_text(empty(**{group: [item]}))
    assert "Nothing needs you" not in text
    assert "1 thing needs you." in text
    assert "<https://example.com/plan|Review the plan>" in text
    if group == "stuck":
        assert "no signal for 11 h" in text


def test_fallback_groups_limits_and_escaping():
    text = fallback_text(
        empty(
            needs_you=[{"kind": "ask", "title": "A & B", "question": "Can <Ada> proceed?"}] * 7,
            stuck=[{"title": "Blocked", "hours_since_signal": 11}] * 5,
            finished_total=23,
            finished=[{"title": f"Full title {i}"} for i in range(20)],
            faults=[{"what": "The last GitHub request failed."}],
        )
    )
    assert "12 things need you." in text
    assert text.count("and 2 more") == 2
    assert "Finished: 23." in text and "Full title 3" not in text
    assert "A &amp; B" in text and "&lt;Ada&gt;" in text
    assert "Not working:\nThe last GitHub request failed." in text
    assert fallback_text(empty()) == "Morning, Ada. Nothing needs you."
    assert short("one two three four", 12) == "one two…"
    assert short("abcdefghijk", 8) == "…"


def test_fallback_your_move_counts_all_items():
    text = fallback_text(empty(
        needs_you=[{"kind": "your_move", "title": f"Task {i}"} for i in range(10)],
        your_move_total=14,
    ))
    assert "14 things need you." in text
    assert "Task 0: the agent stopped and waits for you." in text
    assert "and 9 more" in text
    assert "Finished" not in text


async def test_snapshot_separates_steps_and_orders_your_move(conn, ada):
    from illo.ledger import asks, digest_snapshot

    # The failed answer has an earlier due time. Open asks must still come first.
    failed = await asks.create(conn, owner_person_id=ada["id"], question="Retry the answer?",
                               due_at=NOW - timedelta(days=2))
    await conn.execute("UPDATE asks SET state='answered',delivery_state='failed' WHERE id=%s",
                       (failed["id"],))
    await asks.create(conn, owner_person_id=ada["id"], question="Which plan?", due_at=NOW)
    summary = "Please check this plan before the next step. " * 8
    for i in range(13):
        lane = await lanes.upsert(conn, source="t3", external_id=f"thread-{i}",
                                  title=f"Task {i}", state="review", summary=summary,
                                  owner_person_id=ada["id"])
        await conn.execute("UPDATE lanes SET last_signal_at=%s WHERE id=%s",
                           (NOW - timedelta(hours=i + 1), lane["id"]))
    await lanes.upsert(conn, source="github", external_id="review-a", title="Review the change",
                       state="review", owner_person_id=ada["id"])
    for i, (step, state) in enumerate([(True, "done"), (True, "failed"), (False, "done")]):
        lane = await lanes.upsert(conn, source="t3", external_id=f"closed-{i}", title=f"Closed {i}",
                                  state=state, owner_person_id=ada["id"], is_step=step)
        await conn.execute("UPDATE lanes SET closed_at=%s WHERE id=%s",
                           (NOW - timedelta(hours=1), lane["id"]))
    data = await digest_snapshot.build(conn, ada, NOW)
    assert data["finished_steps"] == 2
    assert data["finished_total"] == 1
    assert [item["title"] for item in data["finished"]] == ["Closed 2"]
    assert data["your_move_total"] == 13
    assert [item["kind"] for item in data["needs_you"]] == [
        "ask", "failed_answer", "review", *(["your_move"] * 10),
    ]
    moves = data["needs_you"][3:]
    assert [item["title"] for item in moves] == [f"Task {i}" for i in range(10)]
    assert moves[0]["hours_since_signal"] == 1
    assert moves[0]["summary"] == (
        "Please check this plan before the next step. " * 4 + "Please check this…"
    )


def test_t3_review_format_names_next_move():
    from illo.tools._format import lane

    base = {"id": 1, "title": "Task", "state": "review", "source": "t3",
            "last_event": "The lane state is review.", "summary": "Please check the plan."}
    text = lane(base)
    assert "your_move" in text and "the agent stopped and waits for you" in text
    assert "Please check the plan." in text and "The lane state is review" not in text
    assert "your_move" not in lane({**base, "source": "github"})


async def only_ada(ctx, timezone="UTC"):
    async with ctx.pool.connection() as conn:
        await conn.execute("UPDATE people SET active=(key='ada'),timezone=%s", (timezone,))


def posts(slack_client):
    return [call for call in slack_client.calls if call["method"] == "chat_postMessage"]


async def test_one_model_brief_with_digest_file_and_person_credentials(
    app, fake_brain, slack_client, monkeypatch
):
    from illo.brain import run_manager
    from illo.connectors import chatgpt

    ctx = app.state.ctx
    await only_ada(ctx, "America/New_York")
    ctx.workspace = ctx.workspace.model_copy(deep=True)
    ctx.workspace.config.brain.auth = "chatgpt"
    ctx.workspace.config.brain.runtime = "openai_responses"
    ctx.workspace.config.brain.team_credential = "ben"
    owners = []

    async def access(ctx, person, **kwargs):
        owners.append(person["key"])
        return ProviderAuth(
            kind="chatgpt", access_token="example-token", account_id="example-account"
        )

    monkeypatch.setattr(chatgpt, "access", access)
    assert (
        run_manager.credential_person(
            Authority("person:ada", "person_dm", 1),
            ctx.workspace,
        )
        == "ada"
    )
    fake_brain.script = [{"name": "digest_snapshot", "arguments": {}}]
    fake_brain.final_text = "Morning, Ada. Please [review the plan](https://example.com/plan)."
    ctx.workspace.routines.append(
        Routine(
            name="daily-digest",
            schedule="* * * * *",
            principal="person:ada",
            destination="person:ada",
            body="Do not send twice.",
        )
    )
    due = NOW + timedelta(hours=5)
    await tick_routines(ctx, due)
    await asyncio.gather(send_due(ctx, due), send_due(ctx, due))
    await send_due(ctx, due + timedelta(minutes=1))
    assert owners == ["ada"]
    assert len(fake_brain.requests) == 1
    request = fake_brain.requests[0]
    assert request.user_message == DEFAULT_INSTRUCTIONS.replace("{name}", "Ada")
    assert request.provider_auth.kind == "chatgpt"
    assert "Morning, Ada." in request.user_message and "{name}" not in request.user_message
    from tests.tool_results import read_result

    result = read_result(fake_brain.results[0])
    assert result["decision"] == "allow" and result["result"]["needs_you"] == []
    assert len(posts(slack_client)) == 1
    assert (
        posts(slack_client)[0]["text"]
        == "Morning, Ada. Please <https://example.com/plan|review the plan>."
    )
    async with ctx.pool.connection() as conn:
        run = await one(conn, "SELECT * FROM runs WHERE id=%s", (request.run_id,))
        assert run["principal"] == "person:ada" and run["profile"] == "person_dm"
        assert run["personal_read"]
        assert not await one(conn, "SELECT 1 FROM events WHERE channel='routine'")


@pytest.mark.parametrize("failure", ["failed", "timeout", "missing", "temporary"])
async def test_fallback_on_model_failure_once(app, fake_brain, slack_client, monkeypatch, failure):
    from illo.brain import run_manager
    from illo.connectors import chatgpt

    ctx = app.state.ctx
    await only_ada(ctx)
    if failure in {"missing", "temporary"}:

        async def auth(*args, **kwargs):
            if failure == "temporary":
                raise chatgpt.TemporaryRefresh(chatgpt.TEMPORARY)
            return None

        monkeypatch.setattr(run_manager, "provider_auth", auth)
    else:

        async def broken(request):
            yield BrainEvent(type="started")
            if failure == "timeout":
                raise TimeoutError()
            raise RuntimeError("Example provider failure")

        monkeypatch.setattr(fake_brain, "run", broken)
    await send_due(ctx, NOW)
    await send_due(ctx, NOW)
    assert len(posts(slack_client)) == 1
    assert posts(slack_client)[0]["text"] == "Morning, Ada. Nothing needs you."


async def test_brief_queue_timeout_uses_fallback(app, slack_client, monkeypatch):
    ctx = app.state.ctx
    await only_ada(ctx)
    ctx.workspace = ctx.workspace.model_copy(deep=True)
    ctx.workspace.config.brain.timeout_s = 0.01

    async def wait(*args):
        await asyncio.Event().wait()

    monkeypatch.setattr(ctx.gateway, "receive", wait)
    await send_due(ctx, NOW)
    await send_due(ctx, NOW)
    assert len(posts(slack_client)) == 1
    assert posts(slack_client)[0]["text"] == "Morning, Ada. Nothing needs you."


async def test_snapshot_full_titles_limits_reviews_and_source_staleness(app, seed):
    ctx = app.state.ctx
    full = "Review the complete example plan with all its details and the final acceptance criteria for every step"
    ask = await seed()
    async with ctx.pool.connection() as conn:
        ada = await people.get(conn, "ada")
        for state in ["working", "blocked", "review"]:
            await lanes.upsert(
                conn,
                source="github",
                external_id=state,
                title=full,
                state=state,
                owner_person_id=ada["id"],
                links=["https://github.com/example-org/example-repo/issues/1"],
            )
        await conn.execute(
            "UPDATE lanes SET last_signal_at=%s WHERE source='github'", (NOW - timedelta(hours=11),)
        )
        stale = await lanes.upsert(
            conn,
            source="t3",
            external_id="stale",
            title="Old work",
            state="working",
            owner_person_id=ada["id"],
        )
        await lanes.add_event(conn, stale["id"], "update", "The agent waits for access.")
        await conn.execute(
            "UPDATE lanes SET last_signal_at=%s WHERE id=%s",
            (NOW - timedelta(hours=11), stale["id"]),
        )
        for i in range(23):
            row = await lanes.upsert(
                conn,
                source="github",
                external_id=f"done-{i}",
                title=full,
                state="done" if i % 2 else "failed",
                owner_person_id=ada["id"],
                links=["https://example.com/finished"],
            )
            await conn.execute(
                "UPDATE lanes SET closed_at=%s WHERE id=%s", (NOW - timedelta(hours=1), row["id"])
            )
        await sensors.ok(conn, "asks:slack")
        await sensors.error(conn, "brain", "Example failure")
        data = await snapshot(conn, ada, ctx, NOW)
        assert len(data["needs_you"]) == 2
        assert data["needs_you"][1]["title"] == full
        assert len(data["stuck"]) == 2 and data["in_progress"] == 1
        assert any(item["last_event"] == "The agent waits for access." for item in data["stuck"])
        assert len(data["finished"]) == 20 and data["finished_total"] == 23
        assert all(item["title"] == full and item["link"] for item in data["finished"])
        assert [item["key"] for item in data["faults"]] == ["brain"]
        await conn.execute(
            "UPDATE asks SET state='answered',delivery_state='failed' WHERE id=%s", (ask["id"],)
        )
        data = await snapshot(conn, ada, ctx, NOW)
        assert data["needs_you"][0]["kind"] == "failed_answer"
        assert "did not reach its agent" in fallback_text(data)
        # A lower GitHub limit changes the same lane to stale.
        ctx.workspace = ctx.workspace.model_copy(deep=True)
        ctx.workspace.config.stale_minutes.github = 600
        assert (await snapshot(conn, ada, ctx, NOW))["in_progress"] == 0


async def test_person_zone_reload_does_not_repeat_brief(app, fake_brain, slack_client):
    ctx = app.state.ctx
    await only_ada(ctx)
    fake_brain.script = [{"name": "digest_snapshot", "arguments": {}}]
    await send_due(ctx, NOW)
    await only_ada(ctx, "America/New_York")
    await send_due(ctx, NOW + timedelta(hours=5))
    assert len(posts(slack_client)) == 1
    await send_due(ctx, NOW + timedelta(days=1, hours=5))
    assert len(posts(slack_client)) == 2


async def test_brief_cannot_send_through_a_tool(app, fake_brain, slack_client):
    from tests.tool_results import read_result

    ctx = app.state.ctx
    await only_ada(ctx)
    fake_brain.script = [
        {"name": "slack_send_dm", "arguments": {"person": "ada", "text": "Extra message"}}
    ]
    await send_due(ctx, NOW)
    assert read_result(fake_brain.results[0])["decision"] == "deny"
    assert len(posts(slack_client)) == 1


async def test_tool_scope_and_snapshot_agree(app, rpc, rpc_token, monkeypatch):
    from tests.tool_results import read_result

    class Clock:
        @staticmethod
        def now(tz):
            return NOW

    monkeypatch.setattr("illo.tools.digest.datetime", Clock)
    _, token = rpc_token
    ctx = app.state.ctx
    async with ctx.pool.connection() as conn:
        ada = await people.get(conn, "ada")
        for i in range(21):
            await lanes.upsert(
                conn,
                source="manual",
                external_id=f"finished-{i}",
                title="Complete example title " * 50,
                state="done",
                owner_person_id=ada["id"],
            )
        await lanes.upsert(conn, source="t3", external_id="your-move", title="Check the plan",
                           state="review", summary="Please check the plan.", owner_person_id=ada["id"])
    results = []
    for name in ("digest_snapshot", "asks_digest", "lanes_attention"):
        response = await rpc(token, "tools/call", {"name": name, "arguments": {}})
        result = read_result(response.json()["result"])
        assert result["decision"] == "allow"
        results.append(result["result"])
    assert results[0] == results[1] == results[2]
    assert len(results[0]["finished"]) == 20 and results[0]["finished_total"] == 21
    assert results[0]["your_move_total"] == 1
    assert results[0]["needs_you"][0]["kind"] == "your_move"
    assert all(item["title"] == "Complete example title " * 50 for item in results[0]["finished"])
    response = await rpc(
        token, "tools/call", {"name": "digest_snapshot", "arguments": {"person": "ben"}}
    )
    assert read_result(response.json()["result"])["decision"] == "deny"


@pytest.mark.parametrize("model", [True, False])
async def test_delivery_retry_keeps_key_text_and_snapshot(
    app, fake_brain, slack_client, seed, monkeypatch, model
):
    from illo.brain import run_manager
    from illo.routines import digest

    ctx = app.state.ctx
    await only_ada(ctx)
    fake_brain.script = [{"name": "digest_snapshot", "arguments": {}}]
    if not model:

        async def no_auth(*args, **kwargs):
            return None

        monkeypatch.setattr(run_manager, "provider_auth", no_auth)
    send = digest.post_dm
    attempts = []

    async def post(ctx, person, text, blocks, kind, key):
        attempts.append((text, kind, key))
        if len(attempts) == 1:
            raise RuntimeError("Example send failure")
        return await send(ctx, person, text, blocks, kind, key)

    monkeypatch.setattr(digest, "post_dm", post)
    with pytest.raises(RuntimeError):
        await send_due(ctx, NOW)
    await seed()
    await send_due(ctx, NOW)
    await send_due(ctx, NOW)
    assert attempts[0] == attempts[1]
    assert attempts[0][1] == "digest"
    assert len(fake_brain.requests) == (1 if model else 0)
    assert len(posts(slack_client)) == 1


async def test_custom_instructions_name_and_frozen_snapshot(
    app, fake_brain, slack_client, monkeypatch
):
    ctx = app.state.ctx
    await only_ada(ctx)
    ctx.workspace = ctx.workspace.model_copy(deep=True)
    ctx.workspace.digest_instructions = "Write a brief for {name}. Call digest_snapshot."
    fake_brain.script = [{"name": "digest_snapshot", "arguments": {}}]
    original = fake_brain.run

    async def change_after_snapshot(request):
        async with ctx.pool.connection() as conn:
            ada = await people.get(conn, "ada")
            await lanes.upsert(
                conn,
                source="manual",
                external_id="new-review",
                title="New review",
                state="review",
                owner_person_id=ada["id"],
            )
        async for item in original(request):
            yield item

    monkeypatch.setattr(fake_brain, "run", change_after_snapshot)
    await send_due(ctx, NOW)
    from tests.tool_results import read_result

    assert read_result(fake_brain.results[0])["result"]["needs_you"] == []
    assert fake_brain.requests[0].user_message == "Write a brief for Ada. Call digest_snapshot."
    assert len(posts(slack_client)) == 1


@pytest.mark.parametrize(
    "profile,tainted",
    [("routine", False), ("team_channel", False), ("agent_request", False), ("person_dm", True)],
)
async def test_snapshot_personal_scope(app, rpc, profile, tainted):
    from illo.ledger import runs
    from tests.tool_results import read_result

    async with app.state.ctx.pool.connection() as conn:
        ada = await people.get(conn, "ada")
        run = await runs.create(
            conn,
            principal="person:ada",
            person_id=ada["id"],
            profile=profile,
            tainted=tainted,
            runtime="fake",
            model="fake",
        )
        token = await runs.mint_token(conn, run["id"], 60)
    response = await rpc(token, "tools/call", {"name": "digest_snapshot", "arguments": {}})
    assert read_result(response.json()["result"])["decision"] == "deny"


async def test_model_lane_tools_use_github_stale_config(app):
    from illo.ledger import runs
    from illo.tools.registry import ToolContext

    ctx = app.state.ctx
    async with ctx.pool.connection() as conn:
        ada = await people.get(conn, "ada")
        row = await lanes.upsert(
            conn,
            source="github",
            external_id="example-org/example-repo#1",
            title="Review the example plan",
            state="working",
            owner_person_id=ada["id"],
        )
        await conn.execute(
            "UPDATE lanes SET last_signal_at=now()-interval '11 hours' WHERE id=%s", (row["id"],)
        )
        run = await runs.create(
            conn,
            principal="person:ada",
            person_id=ada["id"],
            profile="person_dm",
            runtime="fake",
            model="fake",
        )
        tool_ctx = ToolContext(conn, run, ctx)
        for name, args in (
            ("lanes_search", {}),
            ("lanes_mine", {}),
            ("lanes_get", {"id": row["id"]}),
        ):
            result = await ctx.registry.call(tool_ctx, name, args)
            assert result["decision"] == "allow" and "working stale" not in result["result"]
        for name in ("asks_digest", "lanes_attention", "digest_snapshot"):
            assert (await ctx.registry.call(tool_ctx, name, {}))["result"]["stuck"] == []
        ctx.workspace = ctx.workspace.model_copy(deep=True)
        ctx.workspace.config.stale_minutes.github = 600
        result = await ctx.registry.call(tool_ctx, "lanes_search", {})
        assert "working stale" in result["result"]
        assert (
            len((await ctx.registry.call(tool_ctx, "digest_snapshot", {}))["result"]["stuck"]) == 1
        )


async def test_repeated_report_keeps_the_first_closing_time(app, seed):
    """A restart reports each closed thread again. The brief must not count it as new."""
    ctx = app.state.ctx
    async with ctx.pool.connection() as conn:
        ada = await people.get(conn, "ada")
        base = {
            "source": "t3",
            "external_id": "closed-1",
            "title": "Example",
            "owner_person_id": ada["id"],
        }
        first = await lanes.upsert(conn, **base, state="done")
        await conn.execute(
            "UPDATE lanes SET closed_at=%s WHERE id=%s", (NOW - timedelta(days=2), first["id"])
        )
        again = await lanes.upsert(conn, **base, state="done")
        assert again["closed_at"] == NOW - timedelta(days=2)
        failed = await lanes.upsert(conn, **base, state="failed")
        assert failed["closed_at"] == NOW - timedelta(days=2)
        opened = await lanes.upsert(conn, **base, state="working")
        assert opened["closed_at"] is None
        closed = await lanes.upsert(conn, **base, state="done")
        assert closed["closed_at"] > NOW - timedelta(days=2)


async def test_snapshot_links_are_web_addresses(app, seed):
    ctx = app.state.ctx
    async with ctx.pool.connection() as conn:
        ada = await people.get(conn, "ada")
        for name, links in (
            ("app-only", ["example-app://thread/1"]),
            ("plain", ["http://example.com/review"]),
            ("both", [{"url": "example-app://thread/2"}, {"url": "https://example.com/pull/2"}]),
        ):
            await lanes.upsert(
                conn,
                source="t3",
                external_id=name,
                title=name,
                state="done",
                owner_person_id=ada["id"],
                links=links,
            )
        data = await snapshot(conn, ada, ctx, datetime.now(UTC))
    found = {item["title"]: item["link"] for item in data["finished"]}
    assert found == {
        "app-only": "",
        "plain": "http://example.com/review",
        "both": "https://example.com/pull/2",
    }
