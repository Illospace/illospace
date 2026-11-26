"""End-to-end tests for Chief-of-Staff policy rollout asks, 20k blocks, saved sharing, and ask_list."""

from illo.db import one
from illo.ledger import asks, items, people
from tests.asks.test_request_pages import ASK
from tests.mcp.client import AgentClient
from tests.tool_results import read_result


async def _rpc_call(rpc, token, name, arguments=None):
    response = await rpc(token, "tools/call", {"name": name, "arguments": arguments or {}})
    return read_result(response.json()["result"])


async def _save_item(ctx, *, source_ref: str, owner: str, visibility: str = "private", title: str = "Policy", body: str = "Standard procedure text."):
    async with ctx.pool.connection() as conn:
        who = await people.get(conn, owner)
        return await items.upsert(
            conn,
            source_ref=source_ref,
            title=title,
            body=body,
            owner_person_id=who["id"],
            visibility=visibility,
        )


async def test_long_policy_preview_and_text_blocks_up_to_20000_chars(app, http):
    app.state.ctx.workspace.config.pages.enabled = True
    agent = await AgentClient.create(app, http)
    long_doc = "Section 1: Device Policy.\n" + ("Follow the standard procedure carefully. " * 300)
    assert 10000 < len(long_doc) <= 20000

    created = await agent.call(
        "ask_create",
        {
            **ASK,
            "lane": None,
            "blocks": [
                {"kind": "preview", "name": "Device policy", "text": long_doc},
                {"kind": "text", "text": long_doc[:12000]},
            ],
        },
    )
    assert created["state"] == "open"
    assert created["page_url"].endswith(f"/web/a/{created['ask_id']}")

    too_long = await agent.call(
        "ask_create",
        {
            **ASK,
            "lane": None,
            "blocks": [{"kind": "preview", "name": "Too big", "text": "x" * 20001}],
        },
        error=True,
    )
    assert "20000" in too_long["error"]["message"]


async def test_saved_publish_enforces_ownership_secrets_and_personal_read(app, rpc, rpc_token):
    ctx = app.state.ctx
    run, token = rpc_token
    private_item = await _save_item(ctx, source_ref="saved:101", owner="ada", visibility="private")
    other_item = await _save_item(ctx, source_ref="saved:102", owner="ben", visibility="private")

    # Non-owner cannot publish another person's private saved item.
    denied = await _rpc_call(rpc, token, "saved_publish", {"source_ref": other_item["source_ref"]})
    assert denied["decision"] == "deny"

    # Owner can publish their own private saved item.
    ok = await _rpc_call(rpc, token, "saved_publish", {"source_ref": private_item["source_ref"]})
    assert ok["decision"] == "allow"
    assert ok["result"]["visibility"] == "team"
    assert ok["result"]["already_shared"] is False

    # Re-publishing is idempotent.
    again = await _rpc_call(rpc, token, "saved_publish", {"source_ref": private_item["source_ref"]})
    assert again["decision"] == "allow"
    assert again["result"]["already_shared"] is True

    # Secret-containing saved items cannot be published automatically.
    secret_item = await _save_item(
        ctx,
        source_ref="saved:103",
        owner="ada",
        visibility="private",
        title="Key backup",
        body="Do not share sk-ant-1234567890abcdefghij1234567890",
    )
    blocked = await _rpc_call(rpc, token, "saved_publish", {"source_ref": secret_item["source_ref"]})
    assert blocked["decision"] == "deny"
    async with ctx.pool.connection() as conn:
        still_private = await one(conn, "SELECT visibility FROM items WHERE id=%s", (secret_item["id"],))
        assert still_private["visibility"] == "private"

    # After a personal read, saved_publish must become a draft rather than auto-publishing.
    another = await _save_item(
        ctx,
        source_ref="saved:104",
        owner="ada",
        visibility="private",
        title="Draft policy",
        body="Clean internal policy text.",
    )
    async with ctx.pool.connection() as conn:
        await conn.execute("UPDATE runs SET personal_read=true WHERE id=%s", (run["id"],))
    drafted = await _rpc_call(rpc, token, "saved_publish", {"source_ref": another["source_ref"]})
    assert drafted["decision"] == "draft"
    async with ctx.pool.connection() as conn:
        still_private = await one(conn, "SELECT visibility FROM items WHERE id=%s", (another["id"],))
        assert still_private["visibility"] == "private"


async def test_mcp_ask_create_share_saved_and_multi_owners(app, http):
    ctx = app.state.ctx
    ctx.workspace.config.pages.enabled = True
    agent = await AgentClient.create(app, http)

    # Private saved item owned by ada.
    private_item = await _save_item(ctx, source_ref="saved:201", owner="ada", visibility="private")
    ref = private_item["source_ref"]

    # Default share_saved=False preserves refusal for non-owner recipient.
    refused = await agent.call(
        "ask_create",
        {**ASK, "lane": None, "owner": "ben", "proof": {"procedure": ref}},
        error=True,
    )
    assert refused["error"]["message"] == f"The person who must answer cannot see {ref}."

    # Passing both owner and owners is rejected with a clear message.
    both = await agent.call(
        "ask_create",
        {**ASK, "lane": None, "owner": "ada", "owners": ["ben"], "proof": {}},
        error=True,
    )
    assert "not both" in both["error"]["message"]

    # Secret-containing private item cannot be auto-shared via share_saved.
    secret_item = await _save_item(
        ctx,
        source_ref="saved:202",
        owner="ada",
        visibility="private",
        body="Secret token ghp_abcdefghijklmnopqrstuvwxyz123456",
    )
    secret_refused = await agent.call(
        "ask_create",
        {**ASK, "lane": None, "owner": "ben", "proof": {"procedure": secret_item["source_ref"]}, "share_saved": True},
        error=True,
    )
    assert "sensitive credentials" in secret_refused["error"]["message"]

    # Multi-recipient broadcast with share_saved=True publishes caller's item and deduplicates owners.
    created = await agent.call(
        "ask_create",
        {
            **ASK,
            "lane": None,
            "owner": None,
            "owners": ["ada", "ben", "ben"],
            "proof": {"procedure": ref},
            "share_saved": True,
            "source_ref": "policy-v1",
        },
    )
    assert created["count"] == 2
    assert [a["owner"] for a in created["asks"]] == ["ada", "ben"]
    assert [a["source_ref"] for a in created["asks"]] == ["policy-v1:ada", "policy-v1:ben"]

    # Retrying with the same source_ref returns existing asks idempotently.
    retried = await agent.call(
        "ask_create",
        {
            **ASK,
            "lane": None,
            "owner": None,
            "owners": ["ada", "ben"],
            "proof": {"procedure": ref},
            "share_saved": True,
            "source_ref": "policy-v1",
        },
    )
    assert [a["ask_id"] for a in retried["asks"]] == [a["ask_id"] for a in created["asks"]]

    # Both recipients can now read the shared item.
    async with ctx.pool.connection() as conn:
        assert await items.get(conn, ref, principal="person:ben") is not None


async def test_mcp_ask_list_reads_open_and_expired_delivery_answers_with_isolation(app, http):
    ctx = app.state.ctx
    ctx.workspace.config.pages.enabled = True
    ada_agent = await AgentClient.create(app, http, person="ada")
    ben_agent = await AgentClient.create(app, http, person="ben")

    # Create two asks requested by ada: one for ben (answered >30m ago), one open for ada.
    created = await ada_agent.call(
        "ask_create",
        {
            **ASK,
            "lane": None,
            "owner": None,
            "owners": ["ada", "ben"],
            "source_ref": "ack-rollout",
        },
    )
    ada_ask_id = created["asks"][0]["ask_id"]
    ben_ask_id = created["asks"][1]["ask_id"]

    async with ctx.pool.connection() as conn:
        ben = await people.get(conn, "ben")
        await asks.answer(conn, ben_ask_id, person_id=ben["id"], option="yes", text="Read and understood.")
        await conn.execute(
            "UPDATE asks SET answered_at=now()-interval '45 minutes', card_sent_at=now()-interval '50 minutes' WHERE id=%s",
            (ben_ask_id,),
        )
        await asks.expire(conn)
        expired_row = await one(conn, "SELECT delivery_state FROM asks WHERE id=%s", (ben_ask_id,))
        assert expired_row["delivery_state"] == "failed"

    # ask_pull no longer sees the expired delivery, but ask_list still reports the full answer.
    assert (await ada_agent.call("ask_pull"))["answers"] == []

    all_asks = (await ada_agent.call("ask_list", {"state": "all"}))["asks"]
    by_id = {a["ask_id"]: a for a in all_asks}
    assert set(by_id) == {ada_ask_id, ben_ask_id}
    assert by_id[ben_ask_id]["state"] == "answered"
    assert by_id[ben_ask_id]["answer_option"]["id"] == "yes"
    assert by_id[ben_ask_id]["answer_text"] == "Read and understood."
    assert by_id[ben_ask_id]["answered_by"] == "ben"
    assert by_id[ben_ask_id]["delivered_to_slack"] is True
    assert by_id[ada_ask_id]["state"] == "open"

    # Filtering by ask_id works regardless of default state='open'.
    single = (await ada_agent.call("ask_list", {"ask_id": ben_ask_id}))["asks"]
    assert len(single) == 1 and single[0]["state"] == "answered"

    # Isolation: ben sees his own ask but NOT ada's private ask to herself.
    ben_visible = (await ben_agent.call("ask_list", {"state": "all"}))["asks"]
    assert {a["ask_id"] for a in ben_visible} == {ben_ask_id}


async def test_internal_asks_create_and_list_support_blocks_owners_share_saved_and_state(app, rpc, rpc_token):
    ctx = app.state.ctx
    ctx.workspace.config.pages.enabled = True
    _, token = rpc_token
    private_item = await _save_item(ctx, source_ref="saved:301", owner="ada", visibility="private")

    created = await _rpc_call(
        rpc,
        token,
        "asks_create",
        {
            "owners": ["ada", "ben"],
            "kind": "action",
            "question": "Confirm you read the updated device policy.",
            "options": [
                {"id": "confirmed", "label": "Read and understood"},
                {"id": "question", "label": "Have a question"},
            ],
            "proof": {"policy": private_item["source_ref"]},
            "blocks": [{"kind": "preview", "name": "Policy", "text": "Full policy details."}],
            "share_saved": True,
        },
    )
    assert created["decision"] == "allow"
    assert created["result"]["count"] == 2
    assert all(entry["page_url"].endswith(f"/web/a/{entry['ask_id']}") for entry in created["result"]["asks"])

    ben_ask_id = next(e["ask_id"] for e in created["result"]["asks"] if e["owner"] == "ben")
    async with ctx.pool.connection() as conn:
        ben = await people.get(conn, "ben")
        await asks.answer(conn, ben_ask_id, person_id=ben["id"], option="confirmed")

    answered = await _rpc_call(rpc, token, "asks_list", {"state": "answered", "requester": "ada"})
    assert answered["decision"] == "allow"
    assert f"id {ben_ask_id}" in answered["result"]
    assert "answered Read and understood" in answered["result"]

    all_list = await _rpc_call(rpc, token, "asks_list", {"state": "all", "requester": "ada"})
    assert all_list["decision"] == "allow"
    assert f"id {ben_ask_id}" in all_list["result"]
