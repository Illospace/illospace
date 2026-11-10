"""Exercise each ledger area with a real Postgres connection."""

from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet

from illo.db import one
from illo.ledger import (
    asks,
    audit,
    claims,
    drafts,
    items,
    kv,
    lanes,
    people,
    rules,
    runs,
    sensors,
    signals,
    tokens,
)
from illo.vault import Vault, delete, get_names


async def test_people_lookups(conn, ada):
    assert (await people.by_slack_id(conn, "U_ACME_ADA"))["id"] == ada["id"]
    assert (await people.by_github_login(conn, "ACME-ADA-EXAMPLE"))["id"] == ada["id"]
    assert (await people.by_email(conn, "ADA@EXAMPLE.COM"))["id"] == ada["id"]
    assert len(await people.list_people(conn)) == 3
    assert await people.get(conn, "missing") is None


async def test_token_lifecycle(conn, ada):
    raw = await tokens.create(conn, ada["id"], "test machine", ["lanes:read"])
    stored = await one(conn, "SELECT * FROM tokens")
    assert stored["token_hash"] == tokens.digest(raw)
    assert raw not in str(stored)
    assert (await tokens.verify(conn, raw))["person_id"] == ada["id"]
    assert "token_hash" not in (await tokens.list_tokens(conn))[0]
    assert await tokens.revoke(conn, stored["id"])
    assert await tokens.verify(conn, raw) is None
    assert not await tokens.revoke(conn, stored["id"])


async def test_inactive_person_token(conn, ada):
    raw = await tokens.create(conn, ada["id"], "test")
    await conn.execute("UPDATE people SET active=false WHERE id=%s", (ada["id"],))
    assert await tokens.verify(conn, raw) is None


async def test_run_lifecycle(conn, run):
    token = await runs.mint_token(conn, run["id"], 60)
    assert (await runs.verify_token(conn, token))["principal"] == "person:ada"
    assert "token_hash" not in await runs.get(conn, run["id"])
    await runs.finish(conn, run["id"], usage={"input_tokens": 10})
    assert await runs.verify_token(conn, token) is None
    assert (await runs.get(conn, run["id"]))["status"] == "completed"


async def test_expire_run_token(conn, run):
    token = await runs.mint_token(conn, run["id"], 60)
    await runs.expire_token(conn, run["id"])
    assert await runs.verify_token(conn, token) is None


async def test_audit_redacts_values(conn, run):
    await audit.write(
        conn,
        run_id=run["id"],
        principal=run["principal"],
        tool="test",
        args={"secret": "hidden-value"},
        decision="deny",
        reason="test",
    )
    row = await one(conn, "SELECT * FROM audit")
    assert row["args"] == {"secret": "[redacted]"}
    assert len(row["args_digest"]) == 64
    assert "hidden-value" not in str(row)


async def test_lane_upsert_events_search_stale(conn, ada):
    lane = await lanes.upsert(
        conn,
        source="t3",
        external_id="thread-1",
        title="Fix billing",
        state="working",
        owner_person_id=ada["id"],
    )
    same = await lanes.upsert(
        conn,
        source="t3",
        external_id="thread-1",
        title="Fix billing",
        state="working",
        owner_person_id=ada["id"],
    )
    assert same["id"] == lane["id"]
    await lanes.add_event(conn, lane["id"], "progress", "Investigate retries")
    assert len(await lanes.search(conn, "retries", owner="ada", state="working")) == 1
    await conn.execute(
        "UPDATE lanes SET last_signal_at=now()-interval '31 minutes' WHERE id=%s", (lane["id"],)
    )
    row = await lanes.get(conn, lane["id"])
    assert row["stale"] and row["state"] == "working"
    assert not (await lanes.get(conn, lane["id"], stale_minutes={"t3": 60}))["stale"]


async def test_private_lane_filter(conn, ada, ben):
    lane = await lanes.upsert(
        conn,
        source="manual",
        external_id="private",
        title="Private work",
        owner_person_id=ada["id"],
        visibility="private",
    )
    assert await lanes.get(conn, lane["id"], principal="person:ben") is None
    assert await lanes.search(conn, "Private", principal="team") == []
    assert (await lanes.get(conn, lane["id"], principal="person:ada"))["id"] == lane["id"]


async def test_ask_person_answer_delivery_ack(conn, ada, ben):
    token = await tokens.create(conn, ada["id"], "test")
    token_id = (await tokens.verify(conn, token))["id"]
    ask = await asks.create(
        conn,
        owner_person_id=ada["id"],
        requester_token_id=token_id,
        question="Use the cache?",
        options=[{"id": "yes", "label": "Yes", "detail": "Use it"}],
        recommended_option="yes",
    )
    assert (await asks.get(conn, ask["id"], principal="person:ada"))["state"] == "open"
    assert len(await asks.list_open(conn, owner_person_id=ada["id"], principal="person:ada")) == 1
    assert await asks.answer(conn, ask["id"], person_id=ben["id"], option="yes") is None
    assert await asks.answer(conn, ask["id"], person_id=ada["id"], option="invented") is None
    answer = await asks.answer(conn, ask["id"], person_id=ada["id"], option="yes")
    assert answer["answered_via"] == "person"
    assert len(await asks.pull(conn, token_id)) == 1
    assert len(await asks.pull(conn, token_id)) == 1
    assert not await asks.ack(conn, ask["id"], token_id + 1)
    assert await asks.ack(conn, ask["id"], token_id)
    assert await asks.pull(conn, token_id) == []


async def test_rule_match_use_remove(conn, ada, ben):
    rule = await rules.create(
        conn,
        owner_person_id=ada["id"],
        text="Use the cache",
        match={"ask_kind": "decision", "question_pattern": "*cache*"},
        answer_option_label="Yes",
    )
    ask = await asks.create(
        conn,
        owner_person_id=ada["id"],
        question="Use cache?",
        options=[{"id": "ok", "label": "Yes"}],
    )
    assert (await rules.match_ask(conn, ask))["id"] == rule["id"]
    answer = await asks.answer_with_rule(conn, ask["id"], rule["id"])
    assert answer["answer_option"] == "ok" and answer["answered_via"] == "rule"
    assert answer["rule_id"] == rule["id"]
    assert answer["answer_text"] == 'Answered by the rule "Use the cache" of Ada.'
    assert (await rules.list_rules(conn, principal="person:ada"))[0]["uses"] == 1
    assert await rules.list_rules(conn, principal="person:ben") == []
    assert not await rules.remove(conn, rule["id"], ben["id"])
    assert await rules.remove(conn, rule["id"], ada["id"])
    assert await rules.list_rules(conn, principal="person:ada") == []


async def test_rule_cannot_invent_an_option_or_answer_for_another_owner(conn, ada, ben):
    rule = await rules.create(
        conn, owner_person_id=ada["id"], text="Do it", answer_option_label="Other"
    )
    ask = await asks.create(
        conn,
        owner_person_id=ada["id"],
        question="Proceed?",
        options=[{"id": "yes", "label": "Yes"}],
    )
    assert await asks.answer_with_rule(conn, ask["id"], rule["id"]) is None
    await conn.execute(
        "UPDATE rules SET answer_option_label='Yes',owner_person_id=%s WHERE id=%s",
        (ben["id"], rule["id"]),
    )
    assert await asks.answer_with_rule(conn, ask["id"], rule["id"]) is None


async def test_ask_cancel_and_expire(conn, ada):
    a = await asks.create(conn, owner_person_id=ada["id"], question="Cancel?")
    assert await asks.cancel(conn, a["id"])
    assert not await asks.cancel(conn, a["id"])
    old = await asks.create(
        conn,
        owner_person_id=ada["id"],
        question="Expired?",
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    assert await asks.expire(conn) == 1
    assert (await asks.get(conn, old["id"], principal="person:ada"))["state"] == "expired"


async def test_items_full_text_upsert_and_visibility(conn, ada, ben):
    shared = await items.upsert(
        conn,
        source_ref="note:cache",
        title="Caching decisions",
        body="We fixed retries",
        owner_person_id=ada["id"],
    )
    same = await items.upsert(
        conn,
        source_ref="note:cache",
        title="Caching decisions",
        body="We fixed retries",
        owner_person_id=ada["id"],
    )
    assert same["id"] == shared["id"]
    assert len(await items.search(conn, "retry")) == 1
    await items.upsert(
        conn,
        source_ref="note:private",
        title="Caching private",
        owner_person_id=ada["id"],
        visibility="private",
    )
    assert len(await items.search(conn, "Caching", principal="person:ada")) == 2
    assert len(await items.search(conn, "Caching", principal="person:ben")) == 1
    assert await items.get(conn, "note:private", principal="team") is None
    assert await items.get(conn, "note:private", principal="person:ada")


async def test_draft_owner_dm_and_send_state(conn, run, ada, ben):
    row = await drafts.create(
        conn,
        run_id=run["id"],
        approver_person_id=ada["id"],
        kind="email_draft",
        destination="outside:recipient@example.com",
        content={"text": "Hello"},
    )
    assert not await drafts.mark_sent(conn, row["id"])
    assert not await drafts.decide(conn, row["id"], person_id=ben["id"], approve=True, in_dm=True)
    assert not await drafts.decide(conn, row["id"], person_id=ada["id"], approve=True, in_dm=False)
    assert await drafts.decide(conn, row["id"], person_id=ada["id"], approve=True, in_dm=True)
    assert await drafts.mark_sent(conn, row["id"])
    assert not await drafts.mark_sent(conn, row["id"])


async def test_signal_dedupe_and_claim(conn, run):
    args = dict(
        source="monitor",
        dedupe_key="outage-1",
        severity="error",
        title="Job failed",
        body="See the job",
    )
    assert await signals.insert(conn, **args)
    assert await signals.insert(conn, **args) is None
    assert await claims.take(conn, "alert-1", run["id"])
    assert not await claims.take(conn, "alert-1", run["id"])


async def test_sensors_and_kv(conn):
    await sensors.ok(conn, "collector")
    await sensors.error(conn, "collector", "Not available")
    row = await one(conn, "SELECT * FROM sensors WHERE key='collector'")
    assert row["last_ok_at"] and row["last_error_at"] and row["detail"] == "Not available"
    await kv.set(conn, "cursor", {"position": 1})
    await kv.set(conn, "cursor", {"position": 2})
    assert await kv.get(conn, "cursor") == {"position": 2}
    assert await kv.delete(conn, "cursor")
    assert await kv.get(conn, "cursor", 0) == 0


async def test_vault_authentication_and_storage(conn):
    vault = Vault(Fernet.generate_key().decode())
    await vault.set(conn, "example", "private-value")
    row = await one(conn, "SELECT secret FROM secrets")
    assert b"private-value" not in bytes(row["secret"])
    assert await vault.get(conn, "example") == "private-value"
    assert await get_names(conn) == ["example"]
    with pytest.raises(ValueError, match="cannot be decrypted"):
        Vault(Fernet.generate_key().decode()).decrypt(row["secret"])
    assert await delete(conn, "example")
    assert await get_names(conn) == []


@pytest.mark.parametrize(
    "options",
    [
        [{"label": "Missing id"}],
        [{"id": "x"}],
        [1],
        [{"id": "x", "label": "X"}, {"id": "x", "label": "Duplicate"}],
    ],
)
async def test_ask_options_have_clear_validation(conn, ada, options):
    with pytest.raises(ValueError):
        await asks.create(conn, owner_person_id=ada["id"], question="Proceed?", options=options)


async def test_answer_delivery_stops_after_expiry(conn, ada):
    raw = await tokens.create(conn, ada["id"], "test")
    token_id = (await tokens.verify(conn, raw))["id"]
    ask = await asks.create(
        conn, owner_person_id=ada["id"], requester_token_id=token_id, question="Proceed?"
    )
    await asks.answer(conn, ask["id"], person_id=ada["id"], text="Yes")
    await conn.execute(
        "UPDATE asks SET expires_at=now()-interval '1 second' WHERE id=%s", (ask["id"],)
    )
    await asks.expire(conn)
    assert await asks.pull(conn, token_id) == []
    assert (await asks.get(conn, ask["id"], principal="person:ada"))["delivery_state"] == "failed"


async def test_claim_is_atomic_across_connections(pool):
    import asyncio

    async with pool.connection() as conn:
        row = await runs.create(
            conn, principal="team", person_id=None, profile="routine", runtime="fake", model="fake"
        )

    async def take():
        async with pool.connection() as conn:
            return await claims.take(conn, "one-alert", row["id"])

    results = await asyncio.gather(take(), take(), take())
    assert results.count(True) == 1
