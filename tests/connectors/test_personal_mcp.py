"""Each member uses a separate grant on one shared MCP connector."""

# ruff: noqa: F811

from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import pytest
from pydantic import ValidationError

from illo.asks import cards
from illo.connectors import chat, mcp_oauth, store
from illo.connectors.config import MCP_RESERVED, Connector
from illo.db import all_rows, one
from illo.ledger import runs
from illo.tools.connect import status
from illo.tools.registry import ToolContext
from tests.connectors.test_chat import approve, connect_ctx, fake_mcp, person, propose  # noqa: F401
from tests.connectors.test_mcp_oauth import (
    ACCESS,
    CLIENT_SECRET,
    browser,
    finish_browser,
    server,  # noqa: F401
    signin_link,
)


def personal_config(**changes):
    return Connector.model_validate(
        {
            "key": "acme-crm",
            "kind": "mcp",
            "scope": "personal",
            "url": "https://mcp.example.com/mcp",
            "auth": {"type": "oauth"},
            **changes,
        }
    )


def test_personal_config_has_no_customer_data_default():
    assert personal_config().customer_data is None


@pytest.mark.parametrize("customer_data", [True, False])
def test_personal_config_refuses_customer_data(customer_data):
    with pytest.raises(ValidationError, match="A personal connector uses the personal data rules"):
        personal_config(customer_data=customer_data)


@pytest.mark.parametrize(
    "auth",
    [
        None,
        {"type": "bearer", "secret": "acme-key"},
        {"type": "access_key"},
        {"type": "service_account"},
    ],
)
def test_personal_config_needs_oauth(auth):
    with pytest.raises(ValidationError, match="browser sign-in with OAuth"):
        personal_config(auth=auth)


@pytest.mark.parametrize("key", sorted(MCP_RESERVED))
def test_personal_config_keeps_builtin_names(key):
    with pytest.raises(ValidationError, match="built-in connector"):
        personal_config(key=key)


async def personal_begin(ctx):
    draft = await propose(ctx)
    await approve(ctx, draft, choice="personal")
    return draft, signin_link(ctx)


async def add_personal(ctx):
    _, link = await personal_begin(ctx)
    assert (await finish_browser(ctx, link)).status_code == 200
    return chat.find_config(ctx, "acme-crm")


async def test_oauth_card_offers_both_scopes(server):
    ctx, _, _ = server
    draft = await propose(ctx)
    card = cards.draft_card(draft)
    assert [b["text"]["text"] for b in card[-1]["elements"]] == [
        "For the team",
        "For each person",
        "Do not add",
    ]
    assert card[0]["text"]["text"].endswith(
        "For the team: you sign in one time. All team work uses your sign-in.\n"
        "For each person: each person signs in with their own account. Illo uses it only for that person."
    )


async def test_key_card_keeps_two_buttons(connect_ctx):
    fake_mcp(connect_ctx)
    draft = await propose(connect_ctx, auth="bearer")
    assert [b["text"]["text"] for b in cards.draft_card(draft)[-1]["elements"]] == [
        "Add",
        "Do not add",
    ]


async def test_personal_add_saves_owner_grant_and_empty_permissions(server):
    ctx, _, _ = server
    cfg = await add_personal(ctx)
    owner = await person(ctx)
    assert cfg.scope == "personal" and cfg.customer_data is None and cfg.tools == []
    assert "customer_data:" not in (ctx.workspace.root / "connectors/acme-crm.yaml").read_text()
    async with ctx.pool.connection() as conn:
        row, token = await store.load(conn, ctx, cfg.key, owner["id"])
        assert row["person_id"] == owner["id"] and token["access_token"] == ACCESS
        assert not await one(conn, "SELECT * FROM connector_grants WHERE person_id IS NULL")
    allowed = await propose(
        ctx, kind="connect_allow_tools", tools=[{"name": "search", "risk": "read"}]
    )
    await approve(ctx, allowed)
    messages = [c for c in ctx.slack_client.calls if c["method"] == "chat_postMessage"]
    assert messages[-1]["text"].endswith(
        "Each person now writes `connect my acme-crm` in a DM to me."
    )


async def test_personal_add_supports_private_client_form(server):
    ctx, _, settings = server
    settings["registration"] = False
    draft = await propose(ctx)
    await approve(ctx, draft, choice="personal")
    async with ctx.pool.connection() as conn:
        saved = await one(conn, "SELECT * FROM drafts WHERE id=%s", (draft["id"],))
        await mcp_oauth.with_client(
            ctx, conn, await person(ctx), saved, "acme-client", CLIENT_SECRET
        )
    assert (await finish_browser(ctx, signin_link(ctx))).status_code == 200
    assert chat.find_config(ctx, "acme-crm").scope == "personal"


@pytest.mark.parametrize("failure", ["token", "catalog"])
async def test_failed_personal_add_saves_nothing(server, failure):
    ctx, _, settings = server
    _, link = await personal_begin(ctx)
    if failure == "token":
        settings["token_error"] = "invalid_grant"
    else:
        ctx.mcp_client.discover = AsyncMock(side_effect=ValueError("The catalog failed"))
    assert (await finish_browser(ctx, link)).status_code == 400
    assert not (ctx.workspace.root / "connectors/acme-crm.yaml").exists()
    async with ctx.pool.connection() as conn:
        assert not await all_rows(conn, "SELECT * FROM connector_grants")
        assert not await all_rows(conn, "SELECT * FROM secrets")


@pytest.mark.parametrize(
    "phrase",
    [
        "connect my acme-crm",
        "sign in to my acme-crm",
        "connect acme-crm",
        "connect acme-crm again",
    ],
)
async def test_member_can_sign_in_with_personal_phrases(server, replay, fake_brain, phrase):
    ctx, _, _ = server
    cfg = await add_personal(ctx)
    path = ctx.workspace.root / "connectors/acme-crm.yaml"
    before = path.read_text()
    result = await replay(sender="U_ACME_BEN", text=phrase.upper() + "!")
    assert result["decision"] == "claimed" and not fake_brain.requests
    assert (await finish_browser(ctx, signin_link(ctx))).status_code == 200
    member = await person(ctx, "ben")
    async with ctx.pool.connection() as conn:
        row, _ = await store.load(conn, ctx, cfg.key, member["id"])
        assert row["person_id"] == member["id"]
        assert len(await all_rows(conn, "SELECT * FROM connector_grants")) == 2
    assert ctx.slack_client.calls[-1]["text"] == "acme-crm is connected for you."
    assert path.read_text() == before


@pytest.mark.parametrize("revocation_fails", [False, True])
async def test_disconnect_deletes_only_the_members_grant(server, replay, revocation_fails):
    ctx, _, _ = server
    cfg = await add_personal(ctx)
    member = await person(ctx, "ben")
    async with ctx.pool.connection() as conn:
        await store.save(
            conn,
            ctx,
            cfg.key,
            member["id"],
            {
                "access_token": "fake-ben-access",
                "revocation_endpoint": "https://auth.example.com/revoke",
            },
            [],
        )
    ctx.mcp_client.http.request = AsyncMock(
        side_effect=ValueError("The server is unavailable") if revocation_fails else None,
        return_value={},
    )
    result = await replay(sender="U_ACME_BEN", text="DISCONNECT MY ACME-CRM!")
    assert result["decision"] == "claimed"
    assert ("did not confirm revocation" in result["text"]) is revocation_fails
    assert ctx.mcp_client.http.request.call_args.args == ("POST", "https://auth.example.com/revoke")
    async with ctx.pool.connection() as conn:
        grants = await all_rows(conn, "SELECT * FROM connector_grants")
        assert len(grants) == 1 and grants[0]["person_id"] != member["id"]
    assert chat.find_config(ctx, cfg.key).scope == "personal"


async def test_remove_deletes_every_persons_grant(server):
    ctx, _, _ = server
    cfg = await add_personal(ctx)
    member = await person(ctx, "ben")
    async with ctx.pool.connection() as conn:
        await store.save(conn, ctx, cfg.key, member["id"], {"access_token": "fake-ben-access"}, [])
    await approve(ctx, await propose(ctx, kind="connect_remove"))
    assert not (ctx.workspace.root / "connectors/acme-crm.yaml").exists()
    async with ctx.pool.connection() as conn:
        assert not await all_rows(conn, "SELECT * FROM connector_grants")
        assert not await all_rows(conn, "SELECT * FROM secrets")


async def test_personal_callback_refuses_inactive_member(server, replay):
    ctx, _, _ = server
    await add_personal(ctx)
    await replay(sender="U_ACME_BEN", text="connect my acme-crm")
    async with ctx.pool.connection() as conn:
        await conn.execute("UPDATE people SET active=false WHERE key='ben'")
    async with browser(ctx) as client:
        response = await client.get(signin_link(ctx))
    assert response.status_code == 400
    async with ctx.pool.connection() as conn:
        assert len(await all_rows(conn, "SELECT * FROM connector_grants")) == 1


async def test_personal_state_requires_its_browser_and_one_use(server, replay):
    ctx, _, _ = server
    await add_personal(ctx)
    await replay(sender="U_ACME_BEN", text="connect my acme-crm")
    link = signin_link(ctx)
    async with browser(ctx) as client:
        assert (await client.get(link)).status_code == 307
        state = parse_qs(urlsplit(link).query)["state"][0]
        assert (await client.get(link)).status_code == 400
        client.cookies.clear()
        for _ in range(2):
            response = await client.get(
                mcp_oauth.callback_url(ctx), params={"state": state, "code": "fake-code"}
            )
            assert response.status_code == 400
    async with ctx.pool.connection() as conn:
        assert len(await all_rows(conn, "SELECT * FROM connector_grants")) == 1


async def test_personal_status_uses_only_the_readers_grant(server):
    ctx, _, _ = server
    cfg = await add_personal(ctx)
    async with ctx.pool.connection() as conn:
        await conn.execute(
            "UPDATE sensors SET last_error_at=clock_timestamp() WHERE key=%s",
            ("connector:" + cfg.key,),
        )
        for key, expected in [
            ("ada", "connected for you"),
            ("ben", "not connected for you: write `connect my acme-crm`"),
        ]:
            reader = await person(ctx, key)
            run = await runs.create(
                conn,
                principal="person:" + key,
                person_id=reader["id"],
                profile="person_dm",
                runtime="fake",
                model="fake",
            )
            result = await status(ToolContext(conn, run, ctx), {})
            entry = next(c for c in result if c["name"] == cfg.key)
            assert entry["state"] == expected
            assert "signed_in_by" not in str(entry) and "account" not in str(entry)


async def test_personal_callback_rejects_file_removal_during_discovery(server, replay):
    from illo.connectors import files

    ctx, _, _ = server
    cfg = await add_personal(ctx)
    await replay(sender="U_ACME_BEN", text="connect my acme-crm")
    original = ctx.mcp_client.discover

    async def removed(*args, **kwargs):
        found = await original(*args, **kwargs)
        async with ctx.pool.connection() as conn:
            await files.remove(ctx, conn, cfg)
            await conn.execute("DELETE FROM connector_grants WHERE connector_key=%s", (cfg.key,))
        return found

    ctx.mcp_client.discover = removed
    assert (await finish_browser(ctx, signin_link(ctx))).status_code == 400
    async with ctx.pool.connection() as conn:
        assert not await all_rows(conn, "SELECT * FROM connector_grants")


async def test_personal_state_expires_after_ten_minutes(server, replay):
    ctx, _, _ = server
    await add_personal(ctx)
    await replay(sender="U_ACME_BEN", text="connect my acme-crm")
    async with ctx.pool.connection() as conn:
        state = await one(conn, "SELECT * FROM connector_oauth_states WHERE consumed_at IS NULL")
        await conn.execute(
            "UPDATE connector_oauth_states SET expires_at=clock_timestamp()-interval '1 second' WHERE state_hash=%s",
            (state["state_hash"],),
        )
    async with browser(ctx) as client:
        assert (await client.get(signin_link(ctx))).status_code == 400


async def test_personal_add_keeps_credentials_out_of_shared_surfaces(server, caplog, fake_brain):
    from tests.connectors.test_mcp_oauth import CODE, REFRESH

    ctx, _, _ = server
    _, link = await personal_begin(ctx)
    state = parse_qs(urlsplit(link).query)["state"][0]
    assert (await finish_browser(ctx, link)).status_code == 200
    async with ctx.pool.connection() as conn:
        values = []
        for table in ("events", "audit", "drafts", "kv", "sensors"):
            values.extend(await all_rows(conn, f"SELECT * FROM {table}"))
    shared = str(values) + caplog.text + str(fake_brain.requests)
    shared += (ctx.workspace.root / "connectors/acme-crm.yaml").read_text()
    for value in (ACCESS, REFRESH, CLIENT_SECRET, CODE, state):
        assert value not in shared


async def test_member_uses_the_registered_client_method_without_the_owners_grant(server, replay):
    ctx, seen, _ = server
    cfg = await add_personal(ctx)
    assert cfg.auth.token_endpoint_auth_method == "none"
    async with ctx.pool.connection() as conn:
        await conn.execute("DELETE FROM connector_grants")
    await replay(sender="U_ACME_BEN", text="connect my acme-crm")
    assert (await finish_browser(ctx, signin_link(ctx))).status_code == 200
    request = [r for r in seen if r.url.path.endswith("/token")][-1]
    assert "client_secret" not in parse_qs(request.content.decode())
    assert "authorization" not in request.headers


async def test_existing_personal_key_cannot_also_be_added_for_the_team(server):
    ctx, seen, _ = server
    await add_personal(ctx)
    seen.clear()
    result = await propose(ctx)
    assert result["decision"] == "deny" and not seen
