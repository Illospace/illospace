"""Personal Google reads and sweep memory. These tools cannot choose another person's grant."""

from datetime import UTC, datetime, timedelta

from pydantic import Field, model_validator

from illo.connectors.google import google_ready, personal_call, shared_call
from illo.db import one
from illo.ledger import kv
from illo.tools._format import data_text, external_list, line, outside_result, rows
from illo.tools.registry import Arguments, Tool

SWEEP_RETENTION = timedelta(days=30)
SWEEP_MAX_ITEMS = 200


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


class SweepListArgs(Arguments):
    limit: int = Field(default=50, ge=1, le=200)


class SweepItem(Arguments):
    key: str = Field(min_length=1, max_length=120, pattern=r"^\S+(?: \S+)*$")
    summary: str = Field(default="", max_length=160)


class SweepMarkArgs(Arguments):
    items: list[SweepItem] = Field(min_length=1, max_length=20)


async def personal_available(ctx):
    return await google_ready(ctx.conn, ctx.app, "personal", ctx.run.get("person_id"))


async def shared_available(ctx):
    return await google_ready(ctx.conn, ctx.app, "team", None)


async def _owner_person_id(ctx) -> int:
    run = ctx.run
    person = await one(
        ctx.conn, "SELECT id FROM people WHERE 'person:' || key=%s AND active", (run["principal"],)
    )
    if (
        run["tainted"]
        or run["profile"] != "person_dm"
        or not person
        or person["id"] != run["person_id"]
    ):
        raise ValueError("A personal tool needs its owner's run")
    return person["id"]


def _sweep_key(person_id: int) -> str:
    return f"personal-sweep:{person_id}"


def _prune_sweep(entries: list[dict], now: datetime) -> list[dict]:
    cutoff = now - SWEEP_RETENTION
    fresh = []
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("key") or not entry.get("seen_at"):
            continue
        try:
            at = datetime.fromisoformat(str(entry["seen_at"]).replace("Z", "+00:00"))
        except ValueError:
            continue
        if at >= cutoff:
            fresh.append(entry)
    return fresh[-SWEEP_MAX_ITEMS:]


async def sweep_seen_list(ctx, args):
    person_id = await _owner_person_id(ctx)
    now = datetime.now(UTC)
    stored = await kv.get(ctx.conn, _sweep_key(person_id), [])
    fresh = _prune_sweep(stored if isinstance(stored, list) else [], now)
    await ctx.conn.execute("UPDATE runs SET personal_read=true WHERE id=%s", (ctx.run["id"],))
    ctx.run["personal_read"] = True
    shown = list(reversed(fresh))[: args["limit"]]
    if not shown:
        return "No personal sweep items recorded in the last 30 days."
    return data_text(
        "personal-sweep",
        rows(
            shown,
            lambda item: line(item["key"], item.get("seen_at", "")[:10], item.get("summary", "")),
        ).text,
    )


async def sweep_mark_seen(ctx, args):
    person_id = await _owner_person_id(ctx)
    now = datetime.now(UTC)
    key = _sweep_key(person_id)
    stored = await kv.get(ctx.conn, key, [])
    fresh = _prune_sweep(stored if isinstance(stored, list) else [], now)
    by_key = {entry["key"]: entry for entry in fresh}
    stamp = now.isoformat()
    for item in args["items"]:
        by_key[item["key"].strip()] = {
            "key": item["key"].strip(),
            "summary": item.get("summary", "").strip()[:160],
            "seen_at": stamp,
        }
    updated = list(by_key.values())[-SWEEP_MAX_ITEMS:]
    await kv.set(ctx.conn, key, updated)
    return {"saved": len(args["items"]), "total": len(updated)}


DESCRIPTIONS = {
    "mail_search": "Search this person's own mail. `query` uses Gmail search syntax; returns one line per message with its id.",
    "mail_get": "Read one message of this person's own mail by `id` from `mail_search`.",
    "calendar_list": "List this person's calendar events between `from` and `to`: ISO times with a time zone, for example 2026-11-12T00:00:00+01:00, and `to` after `from`.",
    "drive_search": "Search this person's own Drive files. For team documents, use `drive_shared_search`.",
    "drive_get": "Read one of this person's own Drive files by `id` from `drive_search`.",
    "drive_shared_search": "Search the team's shared Drive, through the team's grant, not a person's own files. Returns one line per file with its id.",
    "drive_shared_get": "Read one file of the team's shared Drive by `id` from `drive_shared_search`.",
    "sweep_seen_list": "List item keys already surfaced in this person's proactive personal sweep over the last 30 days, so you do not repeat them.",
    "sweep_mark_seen": "Record item keys (such as `gmail:<id>` or `cal:<id>`) surfaced in this person's proactive personal sweep so future sweeps do not repeat them.",
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
    registry.add(
        Tool(
            "sweep_seen_list",
            "personal",
            "read",
            lambda a: "ledger",
            True,
            sweep_seen_list,
            description=DESCRIPTIONS["sweep_seen_list"],
            arguments=SweepListArgs,
            profiles=frozenset({"person_dm"}),
            available=personal_available,
        )
    )
    registry.add(
        Tool(
            "sweep_mark_seen",
            "personal",
            "write_internal",
            lambda a: "ledger",
            True,
            sweep_mark_seen,
            description=DESCRIPTIONS["sweep_mark_seen"],
            arguments=SweepMarkArgs,
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
