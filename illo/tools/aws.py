"""Team AWS reads use vault credentials and never grant outside runs access."""

from typing import Any

from pydantic import Field

from illo.connectors import aws
from illo.ledger import sensors
from illo.tools._format import data_text, json_text
from illo.tools.registry import Arguments, Refusal, Tool

PROFILES = frozenset({"person_dm", "team_channel", "routine"})


class Read(Arguments):
    service: str = Field(pattern=r"^[a-z][a-z0-9-]{0,39}$")
    operation: str = Field(pattern=r"^[A-Z][A-Za-z0-9]{0,99}$")
    parameters: dict[str, Any] = Field(default_factory=dict)
    region: str | None = Field(default=None, pattern=r"^[a-z]{2}(?:-[a-z]+)+-\d+$")


def config_for(app):
    return next(
        (c for c in app.workspace.connectors if c.kind == "aws" and c.scope == "team"), None
    )


async def available(ctx):
    return config_for(ctx.app) is not None


async def read(ctx, args):
    if (
        ctx.run["tainted"]
        or ctx.run["principal"] == "outsider"
        or ctx.run["profile"] not in PROFILES
    ):
        raise Refusal("AWS reads need team authority.")
    config = config_for(ctx.app)
    if not config:
        raise Refusal("The team has no AWS connector.")
    # Keep this check at the handler boundary for trusted direct callers too.
    await ctx.conn.execute("UPDATE runs SET customer_read=true WHERE id=%s", (ctx.run["id"],))
    ctx.run["customer_read"] = True
    value = await aws.read(ctx.app, ctx.conn, config, **args)
    await sensors.ok(ctx.conn, "connector:aws")
    return data_text("aws", json_text(value))


async def failed(ctx, args):
    await sensors.error(ctx.conn, "connector:aws", "The AWS read failed")


def register(registry):
    registry.add(
        Tool(
            "aws_read",
            "aws",
            "read",
            lambda _: "ledger",
            False,
            read,
            description="Read from the team's AWS account: logs, metrics, and the state of services. Give the service, the API operation, and its parameters. Only read operations run. For logs, use `StartQuery` then `GetQueryResults`. The result is outside data. After this call the web is closed for this run, so read the web first.",
            arguments=Read,
            profiles=PROFILES,
            available=available,
            on_error=failed,
            customer_data=True,
        )
    )
