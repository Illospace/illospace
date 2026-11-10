"""Manage only the ChatGPT sign-in of the person of this DM."""

from dataclasses import replace

from illo.connectors import chatgpt_oauth
from illo.db import one
from illo.ledger import chatgpt as grants
from illo.tools.registry import Tool


async def owner(ctx):
    person = await one(
        ctx.conn, "SELECT id,key FROM people WHERE id=%s AND active", (ctx.run["person_id"],)
    )
    if (
        ctx.run["profile"] != "person_dm"
        or ctx.run["tainted"]
        or not person
        or ctx.principal != "person:" + person["key"]
    ):
        raise ValueError("The tool needs the person's own DM")
    return person


async def status(ctx, args):
    person = await owner(ctx)
    return {"message": chatgpt_oauth.status_text((await grants.status(ctx.conn, person["id"]))[0])}


def register(registry):
    for name, risk, handler in (("chatgpt_status", "read", status),):
        tool = Tool(
            name,
            "chatgpt",
            risk,
            lambda a: "person:<self>",
            False,
            handler,
            description="Check your own ChatGPT sign-in in this DM.",
            profiles=frozenset({"person_dm"}),
        )

        def resolve(ctx, args, tool=tool):
            return replace(tool, destination=lambda a: ctx.principal)

        registry.add(replace(tool, resolve=resolve))
