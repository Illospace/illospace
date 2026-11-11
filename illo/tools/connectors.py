"""Resolve each configured MCP tool before scope, destination, and risk checks."""

from dataclasses import replace
from typing import Any

from pydantic import Field

from illo.ledger import sensors
from illo.tools._format import data_text, json_text, line, outside_result, rows
from illo.tools.registry import Arguments, Tool

PROFILES = frozenset({"person_dm", "team_channel", "agent_request", "routine", "tainted"})


class Call(Arguments):
    connector: str = Field(min_length=1, max_length=100)
    tool: str = Field(min_length=1, max_length=200)
    arguments: dict[str, Any] = Field(default_factory=dict)


def find(app, args: dict) -> tuple:
    config = next(
        (
            c
            for c in app.workspace.connectors
            if c.key == args["connector"] and c.scope == "team" and c.kind == "mcp"
        ),
        None,
    )
    remote = next((t for t in config.tools if t.name == args["tool"]), None) if config else None
    if not remote:
        raise ValueError("The connector file does not list this tool")
    return config, remote


async def list_connectors(ctx, args: dict) -> list[dict]:
    results = []
    for config in ctx.app.workspace.connectors:
        if config.kind != "mcp" or config.scope != "team":
            continue
        visible = [
            tool
            for tool in config.tools
            if not ctx.run["tainted"] or (tool.tainted and tool.risk == "read")
        ]
        # Do not contact a connector with no tools that this run may use.
        schemas = {}
        if visible:
            try:
                schemas = await ctx.app.mcp_client.catalog(ctx.app, ctx.conn, config)
                await sensors.ok(ctx.conn, f"connector:{config.key}")
            except Exception:
                await sensors.error(
                    ctx.conn, f"connector:{config.key}", "The connector catalog failed"
                )
        results.append(
            {
                "connector": config.key,
                "tools": [
                    {
                        **tool.model_dump(),
                        "inputSchema": schemas.get(tool.name),
                        "schema_trusted": False,
                    }
                    for tool in visible
                ],
            }
        )
    values = [{"connector": c["connector"], **t} for c in results for t in c["tools"]]
    return data_text(
        "connector-catalog",
        rows(
            values,
            lambda t: line(t["connector"], t["name"], t["risk"], json_text(t["inputSchema"])),
        ).text,
    )


async def call_connector(ctx, args: dict) -> dict:
    if ctx.run["personal_read"]:
        raise ValueError("This run read personal data, so it cannot call a connector.")
    config, remote = find(ctx.app, args)
    # Check again at the handler boundary, even when trusted code calls it directly.
    if ctx.run["principal"] == "outsider" or (
        ctx.run["tainted"] and not (remote.tainted and remote.risk == "read")
    ):
        raise ValueError("This connector tool does not allow a tainted run")
    return outside_result(await ctx.app.mcp_client.call(ctx.app, ctx.conn, config, args["tool"], args["arguments"]))


def resolve(ctx, args: dict) -> Tool:
    config, remote = find(ctx.app, args)
    # Tainted access is opt-in per read tool. Hostile text never gets a write tool.
    profiles = PROFILES if remote.tainted and remote.risk == "read" else PROFILES - {"tainted"}
    return replace(
        ctx.app.registry.tools["connectors_call"],
        risk=remote.risk,
        destination=lambda a: f"outside:{config.key}",
        profiles=profiles,
        resolve=None,
    )


async def call_failed(ctx, args: dict) -> None:
    # The registry calls this after the handler savepoint rolls back.
    # Reuse its connection so a pool with one connection cannot deadlock.
    await sensors.error(ctx.conn, f"connector:{args['connector']}", "The connector call failed")


async def connector_available(ctx):
    """Show connector tools only when the run has a configured service to call."""
    return any(
        config.kind == "mcp" and config.scope == "team"
        and (not ctx.run["tainted"] or any(t.tainted and t.risk == "read" for t in config.tools))
        for config in ctx.app.workspace.connectors
    )


def register(registry) -> None:
    # Listing exposes only configured names and risks. It does not read remote data.
    registry.add(
        Tool(
            "connectors_list",
            "connectors",
            "read",
            lambda a: "ledger",
            False,
            list_connectors,
            description="List the team's connected outside services and the tools that this run may call, with their input schemas. Call it before `connectors_call` for the exact names.",
            profiles=PROFILES,
            available=connector_available,
        )
    )
    registry.add(
        Tool(
            "connectors_call",
            "connectors",
            "read",
            lambda a: "ledger",
            False,
            call_connector,
            description="Call one tool of a connected outside service, with `connector`, `tool`, and `arguments` as `connectors_list` shows them. The arguments leave the team, so put no personal data in them; the call is refused after a personal read. A remote tool that sends outside becomes a draft card.",
            arguments=Call,
            profiles=PROFILES,
            available=connector_available,
            resolve=resolve,
            on_error=call_failed,
        )
    )
