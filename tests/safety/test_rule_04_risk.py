"""Rule 4: outside messages are drafts. Destructive tools are absent."""

from illo.tools.registry import Tool


async def test_outside_send_does_not_run_handler(app, rpc, rpc_token):
    _, token = rpc_token
    sent = []

    async def send(ctx, args):
        sent.append(args)

    app.state.ctx.registry.add(
        Tool(
            "email_draft",
            "email",
            "send_outside",
            lambda a: "outside:recipient@example.net",
            False,
            send,
        )
    )
    result = (await rpc(token, "tools/call", {"name": "email_draft"})).json()["result"][
        "structuredContent"
    ]
    assert result["decision"] == "draft" and result["result"]["draft_id"]
    assert not sent
    names = {t["name"] for t in (await rpc(token, "tools/list")).json()["result"]["tools"]}
    assert not any("delete" in name or "release" in name or "shell" in name for name in names)
