"""Exact DM guides for the agent door. The gateway answers before a model run."""

from illo.channels.mcp.oauth import issuer
from illo.channels.mcp.slack_signin import credentials
from illo.connectors.chatgpt_oauth import is_dm

COMMANDS = {
    "connect my claude": "claude", "connect claude": "claude",
    "connect my codex": "codex", "connect codex": "codex",
    "connect my agent": "agent", "connect an agent": "agent", "connect my app": "agent",
}


async def claim(ctx, event, authority):
    if not is_dm(event, authority):
        return None
    command = event.text.strip().lower().rstrip(".!?")
    kind = COMMANDS.get(command)
    if not kind:
        return None
    # A workspace connector keeps its existing command and sign-in path.
    if any(command in {f"connect {c.key}", f"connect my {c.key}"} for c in ctx.workspace.connectors):
        return None
    base = issuer(ctx)
    if not base:
        return "App sign-in is not set up on this Illo. Ask your operator for a token."
    async with ctx.pool.connection() as conn:
        configured = await credentials(ctx, conn)
    address = base + "/mcp"
    if kind == "claude":
        text = (
            "To connect Claude to me:\n"
            "For a Team or Enterprise plan, ask your owner to add the connector first.\n"
            '1. In Claude, open Customize, then Connectors. Click "+ Add", then "Add custom connector".\n'
            f"2. Type the name Illo and this address: `{address}`\n"
            'Choose "Register automatically" under OAuth client. Finish the setup and click Add.\n'
            "3. Click Connect. Then sign in with Slack and click Allow.\n"
            "One connection works on each device where you use Claude. "
            f"For Claude Code in a terminal: `claude mcp add --transport http illo {address}`, "
            "then type `/mcp` and sign in."
        )
    elif kind == "codex":
        text = (
            "To connect Codex to me:\n"
            f"1. In Codex, open the MCP settings and add a server with this address: `{address}`\n"
            "2. Start the sign-in for that server. Then sign in with Slack and click Allow.\n"
            f"In a terminal: `codex mcp add illo --url {address}`, then `codex mcp login illo`.\n"
            "Do this one time on each machine."
        )
    else:
        text = (
            f"To connect an app or an agent to me, add an MCP server with this address: `{address}`\n"
            "Then start its sign-in, sign in with Slack, and click Allow.\n"
            'Say "connect my Claude" or "connect my Codex" for the exact steps.'
        )
    if not configured:
        for phrase in ("sign in with Slack and click Allow", "sign in with Slack, and click Allow"):
            text = text.replace(phrase, "send me the code that the page shows, and click Approve on my card")
    return text
