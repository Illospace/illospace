"""An owner confirms whether a team MCP server holds private data."""

# ruff: noqa: F811

import pytest

from illo.asks import cards
from illo.connectors import chat, files
from illo.connectors.config import Connector
from illo.db import all_rows
from tests.connectors.test_chat import approve, connect_ctx, fake_mcp, propose  # noqa: F401
from tests.connectors.test_mcp_oauth import begin, finish_browser, server  # noqa: F401


async def add_team(ctx):
    fake_mcp(ctx)
    await approve(ctx, await propose(ctx))
    return chat.find_config(ctx, "acme-crm")


@pytest.mark.parametrize("public", [True, False])
async def test_data_policy_card_saves_only_after_approval(connect_ctx, replay, public):
    ctx = connect_ctx
    cfg = await add_team(ctx)
    async with ctx.pool.connection() as conn:
        await files.save(ctx, conn, cfg.model_copy(update={"customer_data": public}))
    phrase = "acme-crm reads only public data" if public else "acme-crm holds private data"
    result = await replay(text=phrase.upper() + "!")
    assert result["decision"] == "claimed"
    async with ctx.pool.connection() as conn:
        drafts = await all_rows(conn, "SELECT * FROM drafts WHERE kind='connect_data_policy'")
    draft = drafts[0]
    card = cards.draft_card(draft)
    expected = (
        "After a call to acme-crm, Illo can still read the web in the same run. "
        "Choose this only if acme-crm has no customer data and no private data."
        if public
        else "After a call to acme-crm, the web is closed for that run."
    )
    assert card[0]["text"]["text"] == expected
    assert [b["text"]["text"] for b in card[-1]["elements"]] == ["Change", "Do not change"]
    assert chat.find_config(ctx, "acme-crm").customer_data is public
    await approve(ctx, draft)
    assert chat.find_config(ctx, "acme-crm").customer_data is not public
    saved = (ctx.workspace.root / "connectors/acme-crm.yaml").read_text()
    assert ("customer_data: false" in saved) is public


async def test_rejected_data_policy_keeps_the_file(connect_ctx, replay):
    ctx = connect_ctx
    await add_team(ctx)
    path = ctx.workspace.root / "connectors/acme-crm.yaml"
    before = path.read_text()
    await replay(text="acme-crm reads only public data")
    async with ctx.pool.connection() as conn:
        draft = (await all_rows(conn, "SELECT * FROM drafts WHERE kind='connect_data_policy'"))[0]
    await approve(ctx, draft, choice="reject")
    assert path.read_text() == before


async def test_member_cannot_change_data_policy(connect_ctx, replay):
    ctx = connect_ctx
    await add_team(ctx)
    result = await replay(sender="U_ACME_BEN", text="acme-crm reads only public data")
    assert "Ada" in result["text"]
    async with ctx.pool.connection() as conn:
        assert not await all_rows(conn, "SELECT * FROM drafts WHERE kind='connect_data_policy'")


@pytest.mark.parametrize("kind", ["personal", "aws", "google"])
async def test_data_policy_refuses_other_connector_kinds(connect_ctx, replay, kind):
    ctx = connect_ctx
    key = "acme-crm" if kind == "personal" else kind
    cfg = Connector.model_validate(
        {
            "key": key,
            "kind": "mcp" if kind == "personal" else kind,
            "scope": "personal" if kind != "aws" else "team",
            **(
                {"url": "https://mcp.example.com/mcp", "auth": {"type": "oauth"}}
                if kind == "personal"
                else {}
            ),
            **({"auth": {"type": "access_key", "secret": "fake-aws"}} if kind == "aws" else {}),
        }
    )
    ctx.workspace = ctx.workspace.model_copy(update={"connectors": [cfg]})
    await replay(text=key + " reads only public data")
    async with ctx.pool.connection() as conn:
        assert not await all_rows(conn, "SELECT * FROM drafts WHERE kind='connect_data_policy'")


async def test_team_key_add_explains_private_data_default(connect_ctx):
    ctx = connect_ctx
    await add_team(ctx)
    messages = [c for c in ctx.slack_client.calls if c["method"] == "chat_postMessage"]
    assert messages[-1]["text"].endswith(chat.private_data_text("acme-crm"))


async def test_team_oauth_add_explains_private_data_default(server):
    ctx, _, _ = server
    _, link = await begin(ctx)
    await finish_browser(ctx, link)
    assert ctx.slack_client.calls[-1]["text"].endswith(chat.private_data_text("acme-crm"))
