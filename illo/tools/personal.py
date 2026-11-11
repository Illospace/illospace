"""Personal Google reads. These tools cannot choose another person's grant."""

from datetime import datetime

from pydantic import Field, model_validator

from illo.connectors.google import google_ready, personal_call, shared_call
from illo.tools._format import external_list, outside_result
from illo.tools.registry import Arguments, Tool


class Page(Arguments):
    limit: int = Field(default=15, ge=1, le=40)
    offset: int = Field(default=0, ge=0)


class Query(Page):
    query: str = Field(min_length=1, max_length=2000)


class MailQuery(Query):
    limit: int = Field(default=10, ge=1, le=40)


class File(Arguments):
    id: str = Field(min_length=1, max_length=256, pattern=r"^[a-zA-Z0-9_-]+$")


class Calendar(Page):
    from_: str = Field(alias="from")
    to: str

    @model_validator(mode="after")
    def valid_range(self):
        start = datetime.fromisoformat(self.from_.replace("Z", "+00:00"))
        end = datetime.fromisoformat(self.to.replace("Z", "+00:00"))
        if not start.tzinfo or not end.tzinfo or end <= start:
            raise ValueError("Use ordered dates with time zones")
        return self


async def personal_available(ctx):
    return await google_ready(ctx.conn, ctx.app, "personal", ctx.run.get("person_id"))


async def shared_available(ctx):
    return await google_ready(ctx.conn, ctx.app, "team", None)


DESCRIPTIONS = {
    "mail_search": "Search this person's own mail. `query` uses Gmail search syntax; returns one line per message with its id.",
    "mail_get": "Read one message of this person's own mail by `id` from `mail_search`.",
    "calendar_list": "List this person's calendar events between `from` and `to`: ISO times with a time zone, for example 2026-11-12T00:00:00+01:00, and `to` after `from`.",
    "drive_search": "Search this person's own Drive files. For team documents, use `drive_shared_search`.",
    "drive_get": "Read one of this person's own Drive files by `id` from `drive_search`.",
    "drive_shared_search": "Search the team's shared Drive, through the team's grant, not a person's own files. Returns one line per file with its id.",
    "drive_shared_get": "Read one file of the team's shared Drive by `id` from `drive_shared_search`.",
}


def register(registry) -> None:
    for name, schema in (
        ("mail_search", MailQuery),
        ("mail_get", File),
        ("calendar_list", Calendar),
        ("drive_search", Query),
        ("drive_get", File),
    ):

        async def handler(ctx, args, name=name):
            if "from_" in args:
                args["from"] = args.pop("from_")
            page = {key: args.pop(key) for key in ("offset", "limit") if key in args}
            if name == "mail_search":
                # Fetch the rows before the page too. Keep the source request bounded.
                args["limit"] = min(page["offset"] + page["limit"], 50)
            value = await personal_call(ctx, name, args)
            return outside_result(external_list(value, **page) if page else value)

        registry.add(
            Tool(
                name,
                "personal",
                "read",
                lambda a: "ledger",
                True,
                handler,
                description=DESCRIPTIONS[name],
                arguments=schema,
                profiles=frozenset({"person_dm"}),
                available=personal_available,
            )
        )
    # Separate tools preserve personal=True on every personal Drive read.
    for name, schema in (("drive_shared_search", Query), ("drive_shared_get", File)):

        async def shared(ctx, args, name=name):
            page = {key: args.pop(key) for key in ("offset", "limit") if key in args}
            value = await shared_call(ctx, name.replace("drive_shared_", "drive_"), args)
            return outside_result(external_list(value, **page) if page else value)

        registry.add(
            Tool(
                name,
                "connectors",
                "read",
                lambda a: "ledger",
                False,
                shared,
                description=DESCRIPTIONS[name],
                arguments=schema,
                profiles=frozenset({"person_dm", "agent_request", "team_channel", "routine"}),
                available=shared_available,
            )
        )
