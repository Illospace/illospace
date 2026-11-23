"""Resolve each configured MCP tool before scope, destination, and risk checks."""

from dataclasses import replace
from typing import Any

from pydantic import Field

from illo.connectors import store
from illo.connectors.mcp_client import SignInEnded, record_ended
from illo.db import one
from illo.ledger import sensors
from illo.tools._format import data_text, json_text, line, outside_result, rows
from illo.tools.checks import scope
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
            if c.key == args["connector"] and c.kind == "mcp"
        ),
        None,
    )
    remote = next((t for t in config.tools if t.name == args["tool"]), None) if config else None
    if not remote:
        raise ValueError("The connector file does not list this tool")
    return config, remote


async def own_person(ctx):
    # Use the same owner boundary as the personal Google handler.
    person = await one(
        ctx.conn, "SELECT id FROM people WHERE 'person:' || key=%s AND active", (ctx.run["principal"],)
    )
    if (
        ctx.run["tainted"] or ctx.run["profile"] != "person_dm"
        or not person or person["id"] != ctx.run.get("person_id")
    ):
        raise ValueError("A personal tool needs its owner's run")
    return person["id"]


async def list_connectors(ctx, args: dict) -> list[dict]:
    results = []
    for config in ctx.app.workspace.connectors:
        if config.kind == "aws" and config.scope == "team" and not ctx.run["tainted"] and ctx.run["principal"] != "outsider":
            results.append({"connector": config.key, "tools": [], "region": config.region, "services": config.services})
            continue
        if config.kind != "mcp":
            continue
        person_id = None
        if config.scope == "personal":
            try:
                person_id = await own_person(ctx)
            except ValueError:
                continue
        visible = [
            tool
            for tool in config.tools
            if (not ctx.run["tainted"] or (tool.tainted and tool.risk == "read"))
            and (not ctx.run.get("deep_kind") or tool.risk == "read")
        ]
        # Do not contact a connector with no tools that this run may use.
        schemas, hint = {}, ""
        signed_in = True
        if config.scope == "personal":
            try:
                await store.load(ctx.conn, ctx.app, config.key, person_id)
            except ValueError:
                signed_in = False
                hint = f'Not signed in. The person writes "connect my {config.key}" in a DM to Illo.'
        may_contact = (
            not ctx.run["personal_read"] or config.scope == "personal"
        ) and not (ctx.run.get("customer_read") and not config.customer_data)
        if visible and signed_in and may_contact:
            # A tool list is not a data read. If it closed the web and the team connectors,
            # each DM run that lists its tools would lose them before it does any work.
            try:
                options = {"person_id": person_id} if config.scope == "personal" else {}
                schemas = await ctx.app.mcp_client.catalog(ctx.app, ctx.conn, config, **options)
                await sensors.ok(ctx.conn, f"connector:{config.key}")
            except SignInEnded:
                await sensors.error(ctx.conn, f"connector:{config.key}", "The connector sign-in ended")
            except Exception:
                await sensors.error(
                    ctx.conn, f"connector:{config.key}", "The connector catalog failed"
                )
        results.append(
            {
                "connector": config.key,
                "hint": hint,
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
    aws = "\n".join(
        line(c["connector"], c["region"], ", ".join(c["services"]))
        for c in results if "services" in c
    )
    hints = "\n".join(line(c["connector"], c["hint"]) for c in results if c.get("hint"))
    return data_text(
        "connector-catalog",
        rows(
            values,
            lambda t: line(t["connector"], t["name"], t["risk"], json_text(t["inputSchema"])),
        ).text + ("\n" + aws if aws else "") + ("\n" + hints if hints else ""),
    )


async def call_connector(ctx, args: dict, *, approved=False) -> dict:
    config, remote = find(ctx.app, args)
    person_id = None
    if config.scope == "personal":
        person_id = await own_person(ctx)
        if scope(ctx.run, resolve(ctx, args)).outcome != "allow":
            raise ValueError("A personal tool needs its owner's run")
        if remote.risk != "read" and not approved:
            raise ValueError("Each personal connector write needs your approval")
    elif ctx.run["personal_read"]:
        raise ValueError("This run read personal data, so it cannot call a connector.")
    if ctx.run.get("customer_read") and not config.customer_data and not approved:
        raise ValueError("Customer data needs approval for this connector")
    if ctx.run.get("deep_kind") and remote.risk != "read":
        raise ValueError("A deep run only reads")
    # Check again at the handler boundary, even when trusted code calls it directly.
    if ctx.run["principal"] == "outsider" or (
        ctx.run["tainted"] and not (remote.tainted and remote.risk == "read")
    ):
        raise ValueError("This connector tool does not allow a tainted run")
    if config.scope == "personal":
        await ctx.conn.execute("UPDATE runs SET personal_read=true WHERE id=%s", (ctx.run["id"],))
        ctx.run["personal_read"] = True
    if config.customer_data:
        await ctx.conn.execute("UPDATE runs SET customer_read=true WHERE id=%s", (ctx.run["id"],))
        ctx.run["customer_read"] = True
    try:
        options = {"person_id": person_id} if config.scope == "personal" else {}
        return outside_result(await ctx.app.mcp_client.call(ctx.app, ctx.conn, config, args["tool"], args["arguments"], **options))
    except SignInEnded as exc:
        ctx.ended_mcp = exc
        raise


def resolve(ctx, args: dict) -> Tool:
    config, remote = find(ctx.app, args)
    # Tainted access is opt-in per read tool. Hostile text never gets a write tool.
    profiles = PROFILES if remote.tainted and remote.risk == "read" else PROFILES - {"tainted"}
    return replace(
        ctx.app.registry.tools["connectors_call"],
        risk=remote.risk,
        destination=lambda a: f"outside:{config.key}",
        profiles=frozenset({"person_dm"}) if config.scope == "personal" else profiles,
        personal=config.scope == "personal",
        resolve=None,
        customer_data=bool(config.customer_data),
    )


async def call_failed(ctx, args: dict) -> None:
    # The registry calls this after the handler savepoint rolls back.
    # Reuse its connection so a pool with one connection cannot deadlock.
    failure = getattr(ctx, "ended_mcp", None)
    if failure:
        config, _ = find(ctx.app, args)
        await record_ended(ctx.app, ctx.conn, config, failure)
        from illo.connectors.mcp_oauth import notify_ended

        try:
            async with ctx.conn.transaction():
                await notify_ended(ctx.app, ctx.conn, config.key)
        except Exception:
            # The timer retries a notice after a temporary provider failure.
            pass
        del ctx.ended_mcp
    else:
        await sensors.error(ctx.conn, f"connector:{args['connector']}", "The connector call failed")


async def connector_available(ctx):
    """Show connector tools only when the run has a configured service to call."""
    for config in ctx.app.workspace.connectors:
        if config.kind not in {"mcp", "aws"}:
            continue
        if config.scope == "personal":
            try:
                await own_person(ctx)
            except ValueError:
                continue
            return True
        if not ctx.run["tainted"] or any(t.tainted and t.risk == "read" for t in config.tools):
            return True
    return False


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
            description="List the connected outside services and the tools that this run may call, with their input schemas. Call it before `connectors_call` for the exact names. A connector can be personal; the person writes `connect my <name>` in a DM to sign in.",
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
            description="Call one tool of a connected outside service, with `connector`, `tool`, and `arguments` as `connectors_list` shows them. A personal connector uses only this person's sign-in; write `connect my <name>` in a DM to sign in. After a personal read, team connector calls are refused. Each personal write and each outside send needs an approval card.",
            arguments=Call,
            profiles=PROFILES,
            available=connector_available,
            resolve=resolve,
            on_error=call_failed,
        )
    )
