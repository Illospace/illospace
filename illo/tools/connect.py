"""Let a person start a fixed connection flow without a key in the model."""

from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator

from illo.connectors import chat, google
from illo.connectors.config import validate_url
from illo.db import one
from illo.tools._format import data_text
from illo.tools._person import run_person_id
from illo.tools.registry import Arguments, Refusal, Tool

PERSON = frozenset({"person_dm"})
STATUS = PERSON | {"team_channel", "agent_request", "routine"}


class Start(Arguments):
    kind: str = Field(min_length=1, max_length=100)
    scope: Literal["personal", "team"]


class Key(Arguments):
    key: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,79}$")


class MCP(Key):
    url: str = Field(max_length=2000)
    auth: Literal["none", "bearer"]
    description: str = Field(default="", max_length=500)

    @field_validator("url")
    @classmethod
    def safe_address(cls, value):
        validate_url(value)
        parts = urlsplit(value)
        if parts.scheme != "https" or parts.query or "?" in value:
            raise ValueError("Use an HTTPS address without a query. Enter a key only in the form.")
        return value


class Permission(Arguments):
    name: str = Field(min_length=1, max_length=200)
    risk: Literal["read", "write"]


class Allow(Key):
    tools: list[Permission] = Field(min_length=1, max_length=100)


async def status(tc, args):
    person_id = await run_person_id(tc)
    configs = list(tc.app.workspace.connectors)
    if not any(c.kind == "google" and c.scope == "personal" for c in configs):
        from illo.connectors.config import Connector

        configs.append(Connector(key="google", kind="google", scope="personal"))
    result = []
    for config in configs:
        connected = False
        state = "ready"
        if config.kind == "google" and config.scope == "personal":
            if not await chat.client_ready(tc.app, tc.conn):
                state = "needs the owner"
            else:
                connected = bool(
                    person_id and await google.google_ready(tc.conn, tc.app, "personal", person_id)
                )
                state = "ready" if connected else "not connected"
            description = "Illo can read your mail, calendar, and files."
            phrase = "connect my Google"
        else:
            connected = True
            if config.auth and config.auth.secret:
                try:
                    connected = bool(
                        await chat.store.vault(tc.app).get(tc.conn, config.auth.secret)
                    )
                except ValueError:
                    connected = False
            if config.auth and config.auth.type == "oauth":
                connected = bool(
                    await one(
                        tc.conn,
                        "SELECT id FROM connector_grants WHERE connector_key=%s AND person_id IS NULL AND revoked_at IS NULL",
                        (config.key,),
                    )
                )
            state = "ready" if connected else "needs the owner"
            description = config.description or "Illo can use the tools that the owner allows."
            phrase = f"connect {config.key}"
        sensor = await one(
            tc.conn,
            "SELECT last_ok_at,last_error_at FROM sensors WHERE key=%s",
            ("connector:" + config.key,),
        )
        if (
            sensor
            and sensor["last_error_at"]
            and (not sensor["last_ok_at"] or sensor["last_error_at"] > sensor["last_ok_at"])
        ):
            state = "failing"
        result.append(
            {
                "name": config.key,
                "kind": config.kind,
                "scope": config.scope,
                "state": state,
                "connected": "yes" if connected else "no",
                "allows": data_text("connector-description", description),
                "connect": phrase,
                "disconnect": "disconnect my Google"
                if config.kind == "google" and config.scope == "personal"
                else "ask the owner to remove " + config.key,
                "tools_found": data_text("connector-tools", str(config.discovered_tools))
                if config.discovered_tools
                else "",
            }
        )
    return result


async def start(tc, args):
    return await chat.start(tc.app, tc.conn, tc.run.get("person_id"), args["kind"], args["scope"])


def register(registry):
    registry.add(
        Tool(
            "connect_status",
            "connect",
            "read",
            lambda _: "ledger",
            False,
            status,
            description="Show the team connections and this person's sign-in state. Show no keys or another person's sign-in details.",
            profiles=STATUS,
        )
    )
    registry.add(
        Tool(
            "connect_start",
            "connect",
            "write_internal",
            lambda _: "ledger",
            False,
            start,
            description="Start a connection for this person. Send fixed steps or a sign-in link in their DM. Never ask for a key in chat.",
            arguments=Start,
            profiles=PERSON,
        )
    )
    for name, schema, description in (
        (
            "connect_propose_mcp",
            MCP,
            "Propose a team MCP server for the owner's approval card. Enter its HTTPS address without a key. A private form accepts the key.",
        ),
        (
            "connect_allow_tools",
            Allow,
            "Propose tool permissions for the owner's approval card. Use only names from the server's tool list. Each write needs approval.",
        ),
        (
            "connect_remove",
            Key,
            "Propose removal of a team MCP connector and its saved key. Only the owner can approve the card in their DM.",
        ),
    ):

        async def handler(tc, args, name=name):
            try:
                return await chat.propose(tc, args, name)
            except ValueError as exc:
                # Only fixed local reasons can reach the model.
                raise Refusal(str(exc)) from None

        registry.add(
            Tool(
                name,
                "connect",
                "write_internal",
                lambda _: "ledger",
                False,
                handler,
                description=description,
                arguments=schema,
                profiles=PERSON,
            )
        )
