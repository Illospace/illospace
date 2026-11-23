"""Check the ChatGPT sign-in that pays for this person's DM."""

from dataclasses import replace

from illo.connectors import chatgpt_oauth
from illo.db import one
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
    return {"message": await chatgpt_oauth.signin_status(ctx.conn, ctx.app.workspace, person["id"])}


DESCRIPTIONS = {
    "chatgpt_status": "Check the ChatGPT sign-in that pays for this person's runs. Returns one status sentence that you can pass on. Use it when the person asks about their sign-in.",
}


def register(registry):
    for name, risk, handler in (("chatgpt_status", "read", status),):
        tool = Tool(
            name,
            "chatgpt",
            risk,
            lambda a: "person:<self>",
            False,
            handler,
            description=DESCRIPTIONS[name],
            profiles=frozenset({"person_dm"}),
        )

        def resolve(ctx, args, tool=tool):
            return replace(tool, destination=lambda a: ctx.principal)

        registry.add(replace(tool, resolve=resolve))
