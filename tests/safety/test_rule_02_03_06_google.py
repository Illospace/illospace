"""Rules 2, 3, and 6: personal grants stay with their owner's untainted run."""

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from illo.connectors import store
from illo.connectors.config import Connector
from illo.connectors.google import personal_call
from illo.ledger import people, runs
from illo.tools.registry import ToolContext
from tests.fakes import FakeGoogleClient
from tests.tool_results import read_result


async def prepare(app):
    ctx = app.state.ctx
    ctx.config.vault_key = SecretStr(Fernet.generate_key().decode())
    ctx.workspace = ctx.workspace.model_copy(
        update={"connectors": [Connector(key="google", kind="google", scope="personal")]}
    )
    ctx.google_client = FakeGoogleClient()
    async with ctx.pool.connection() as conn:
        ada = await people.get(conn, "ada")
        await store.save(conn, ctx, "google", ada["id"], {"refresh_token": "test-refresh"}, [])
    return ctx


async def call(rpc, token, name, args):
    response = await rpc(token, "tools/call", {"name": name, "arguments": args})
    return read_result(response.json()["result"])


@pytest.mark.parametrize(
    ("principal", "profile", "tainted"),
    [
        ("person:ben", "person_dm", False),
        ("team", "team_channel", False),
        ("person:ada", "person_dm", True),
        ("team", "tainted", True),
    ],
)
async def test_rule_02_06_never_borrows_another_grant(app, rpc, principal, profile, tainted):
    ctx = await prepare(app)
    async with ctx.pool.connection() as conn:
        person = await people.get(conn, principal[7:]) if principal.startswith("person:") else None
        row = await runs.create(
            conn,
            principal=principal,
            person_id=person["id"] if person else None,
            profile=profile,
            tainted=tainted,
            runtime="fake",
            model="fake",
        )
        token = await runs.mint_token(conn, row["id"], 60)
    for name, args in (
        ("mail_search", {"query": "invoice"}),
        ("mail_get", {"id": "a"}),
        ("calendar_list", {"from": "2026-01-01T00:00:00Z", "to": "2026-01-02T00:00:00Z"}),
        ("drive_search", {"query": "plan"}),
        ("drive_get", {"id": "a"}),
    ):
        assert (await call(rpc, token, name, args))["decision"] == "deny"
    assert not ctx.google_client.calls


async def test_rule_02_handler_checks_owner_even_without_registry(app):
    ctx = await prepare(app)
    async with ctx.pool.connection() as conn:
        ada = await people.get(conn, "ada")
        forged = {
            "principal": "person:ben",
            "person_id": ada["id"],
            "profile": "person_dm",
            "tainted": False,
        }
        with pytest.raises(ValueError, match="owner"):
            await personal_call(ToolContext(conn, forged, ctx), "mail_search", {"query": "x"})
    assert not ctx.google_client.calls


async def test_rule_03_mail_read_then_shared_draft_and_owner_dm(app, rpc, rpc_token):
    ctx = await prepare(app)
    row, token = rpc_token
    assert (await call(rpc, token, "mail_search", {"query": "invoice"}))["decision"] == "allow"
    assert (
        await call(
            rpc,
            token,
            "slack_post_channel",
            {"channel": "C_ACME_TEAM", "text": "A private mail summary"},
        )
    )["decision"] == "draft"
    assert not ctx.slack_client.calls
    assert (
        await call(
            rpc, token, "slack_send_dm", {"person": "ada", "text": "Your private mail summary"}
        )
    )["decision"] == "allow"
    assert len(ctx.slack_client.calls) == 2
    async with ctx.pool.connection() as conn:
        result = await runs.get(conn, row["id"])
        assert result["personal_read"]
        drafts = await (await conn.execute("SELECT * FROM drafts")).fetchall()
        assert len(drafts) == 1
        assert drafts[0]["approver_person_id"] == row["person_id"]
        audit = await (await conn.execute("SELECT decision FROM audit ORDER BY id")).fetchall()
        assert [a["decision"] for a in audit] == ["allow", "draft", "allow"]
    assert ctx.google_client.calls[0]["person_id"] == row["person_id"]
