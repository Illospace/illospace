"""The AWS private form keeps keys out of the ledger, cards, files, and logs."""

# ruff: noqa: F811

import json
import logging
from unittest.mock import AsyncMock

import pytest
import yaml

from illo.channels.slack import actions, modals
from illo.connectors import aws, chat, store
from illo.db import all_rows, one
from tests.connectors.test_aws import KEYS
from tests.connectors.test_chat import approve, connect_ctx, finish, git, person  # noqa: F401

ARN = "arn:aws:iam::123456789012:user/acme-reader"


async def open_aws(ctx, *, user="U_ACME_ADA", channel="D_EXAMPLE"):
    await actions.dispatch(
        ctx,
        {
            "type": "block_actions",
            "user": {"id": user},
            "channel": {"id": channel},
            "message": {"ts": "1"},
            "trigger_id": "aws-form-trigger",
            "actions": [{"action_id": "connect:aws:form"}],
        },
    )
    return next(
        (c["view"] for c in reversed(ctx.slack_client.calls) if c["method"] == "views_open"), None
    )


def submission(view, *, user="U_ACME_ADA", **values):
    fields = {
        "access_key_id": KEYS["access_key_id"],
        "secret": KEYS["secret_access_key"],
        "region": "us-east-1",
        **values,
    }
    return {
        "type": "view_submission",
        "user": {"id": user},
        "view": {
            **view,
            "state": {"values": {k: {"value": {"value": v}} for k, v in fields.items()}},
        },
    }


async def assert_private(ctx, caplog):
    async with ctx.pool.connection() as conn:
        values = []
        for table in ("events", "audit", "drafts", "kv", "sensors", "runs"):
            values.extend(await all_rows(conn, f"SELECT * FROM {table}"))
        values.extend(await all_rows(conn, "SELECT * FROM secrets"))
    text = str(values) + caplog.text + json.dumps(ctx.slack_client.calls)
    for secret in KEYS.values():
        assert secret not in text
        assert all(secret not in str(r.__dict__) for r in caplog.records)
        assert all(secret not in p.read_text() for p in ctx.workspace.root.rglob("*.yaml"))


async def test_owner_connect_form_save_and_remove(
    connect_ctx, replay, fake_brain, monkeypatch, caplog
):
    ctx = connect_ctx
    git(ctx.workspace.root, "init")
    git(
        ctx.workspace.root,
        "-c",
        "user.name=Illo",
        "-c",
        "user.email=illo@example.com",
        "commit",
        "--allow-empty",
        "-m",
        "Start workspace",
    )
    check = AsyncMock(return_value={"Arn": ARN})
    monkeypatch.setattr(aws, "check_key", check)
    result = await replay(text="CONNECT AWS!")
    assert result["decision"] == "claimed" and not fake_brain.requests
    messages = [c for c in ctx.slack_client.calls if c["method"] == "chat_postMessage"]
    assert len(messages) == 1 and messages[0]["text"] == chat.AWS_SETUP
    assert messages[0]["blocks"][-1]["elements"][0]["text"]["text"] == "Enter the AWS key"
    view = await open_aws(ctx)
    assert [b["label"]["text"] for b in view["blocks"]] == [
        "Access key ID",
        "Secret access key",
        "Region",
    ]
    assert view["blocks"][-1]["element"]["initial_value"] == "us-east-1"
    payload = submission(view)
    caplog.set_level(logging.DEBUG)
    logging.getLogger("slack_sdk.socket_mode.aiohttp").debug("Received private form: %s", payload)
    assert await finish(ctx, payload) == {"response_action": "clear"}
    check.assert_awaited_once_with(KEYS, "us-east-1")
    assert (
        ctx.slack_client.calls[-1]["text"]
        == f"AWS is connected as `{ARN}` in `us-east-1`. I only read."
    )
    path = ctx.workspace.root / "connectors/aws.yaml"
    saved = yaml.safe_load(path.read_text())
    assert saved["kind"] == "aws" and saved["auth"] == {
        "type": "access_key",
        "secret": "aws-illo",
        "scopes": [],
        "redirect_uri": "http://127.0.0.1:8765/callback",
    }
    assert git(ctx.workspace.root, "log", "-1", "--format=%s") == "workspace: Save connector aws"
    async with ctx.pool.connection() as conn:
        assert json.loads(await store.vault(ctx).get(conn, "aws-illo")) == KEYS
    result = await replay(text="disconnect AWS")
    assert result["decision"] == "claimed" and path.exists()
    async with ctx.pool.connection() as conn:
        draft = await one(conn, "SELECT * FROM drafts WHERE kind='connect_remove'")
    await approve(ctx, draft, who="ben")
    assert path.exists()
    await approve(ctx, draft)
    assert not path.exists()
    assert git(ctx.workspace.root, "log", "-1", "--format=%s") == "workspace: Remove connector aws"
    async with ctx.pool.connection() as conn:
        assert await store.vault(ctx).get(conn, "aws-illo") is None
    await assert_private(ctx, caplog)


@pytest.mark.parametrize("command", ["connect AWS", "disconnect AWS"])
async def test_non_owner_gets_owner_name(connect_ctx, replay, monkeypatch, command):
    check = AsyncMock()
    monkeypatch.setattr(aws, "check_key", check)
    result = await replay(text=command, sender="U_ACME_BEN")
    assert result["decision"] == "claimed" and "Ada must" in result["text"]
    assert not check.called
    assert not await open_aws(connect_ctx, user="U_ACME_BEN")
    assert not await open_aws(connect_ctx, channel="C_ACME_TEAM")


@pytest.mark.parametrize("existing", [True, False])
async def test_failed_check_saves_nothing(connect_ctx, monkeypatch, caplog, existing):
    ctx = connect_ctx
    monkeypatch.setattr(aws, "check_key", AsyncMock(side_effect=ValueError(str(KEYS))))
    if existing:
        async with ctx.pool.connection() as conn:
            await store.vault(ctx).set(conn, "aws-illo", "old-encrypted-value")
    assert await finish(ctx, submission(await open_aws(ctx))) == {"response_action": "clear"}
    assert ctx.slack_client.calls[-1]["text"] == chat.AWS_FAILED
    assert not (ctx.workspace.root / "connectors/aws.yaml").exists()
    async with ctx.pool.connection() as conn:
        assert await store.vault(ctx).get(conn, "aws-illo") == (
            "old-encrypted-value" if existing else None
        )
    await assert_private(ctx, caplog)


async def test_form_rejects_wrong_owner_and_bad_region(connect_ctx, monkeypatch):
    ctx = connect_ctx
    check = AsyncMock(return_value={"Arn": ARN})
    monkeypatch.setattr(aws, "check_key", check)
    view = await open_aws(ctx)
    for changes in (
        {"user": "U_ACME_BEN"},
        {"region": "https://example.com"},
        {"secret": ""},
        {"access_key_id": ""},
    ):
        response, work = await modals.prepare(ctx, submission(view, **changes))
        assert response["response_action"] == "errors" and work is None
    async with ctx.pool.connection() as conn:
        await conn.execute("UPDATE people SET role='member' WHERE key='ada'")
    assert (await modals.prepare(ctx, submission(view)))[0]["response_action"] == "errors"
    assert not check.called


async def test_sts_check_has_no_model_tool(monkeypatch):
    fake = AsyncMock(return_value={"Arn": ARN})
    monkeypatch.setattr(aws.asyncio, "to_thread", fake)
    assert await aws.check_key(KEYS, "us-east-1") == {"Arn": ARN}
    fake.assert_awaited_once_with(aws.invoke, KEYS, "sts", "GetCallerIdentity", {}, "us-east-1")
