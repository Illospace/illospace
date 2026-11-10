"""Ask tools use the authenticated person for answers and cancellations."""

from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from illo.asks.service import is_live
from illo.channels.mcp.schemas import check_ask_text
from illo.ledger import ask_workflow as ledger
from illo.ledger import asks, lanes, mcp, people
from illo.tools._format import Compact, ask, body, json_text, rows
from illo.tools.checks import Decision
from illo.tools.registry import Arguments, Tool

READ_PROFILES = frozenset({"person_dm", "team_channel", "tainted", "routine", "agent_request"})
PERSON_PROFILES = frozenset({"person_dm"})
RULE_PROFILES = frozenset({"person_dm", "agent_request"})


class ListArgs(Arguments):
    owner: str | None = None
    limit: int = Field(default=15, ge=1, le=40)


class GetArgs(Arguments):
    id: int = Field(gt=0)


class Option(Arguments):
    id: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_.-]+$")
    label: str = Field(min_length=1, max_length=200)
    detail: str = Field(default="", max_length=1000)


class CreateArgs(Arguments):
    owner: str = Field(min_length=1)
    question: str = Field(min_length=1, max_length=2000)
    kind: Literal["decision", "action", "review", "reminder"] = "decision"
    options: list[Option] = Field(default_factory=list, max_length=25)
    recommended_option: str | None = None
    proof: dict = Field(default_factory=dict)
    lane_id: int | None = Field(default=None, gt=0)
    due_at: str | None = None
    expires_at: str | None = None
    allow_rule: bool = True
    permission: bool = False

    @model_validator(mode="after")
    def visible_text(self):
        check_ask_text(self.model_dump_json())
        return self


class AnswerArgs(GetArgs):
    option: str | None = None
    text: str | None = Field(default=None, min_length=1, max_length=10000)


class RuleAnswerArgs(GetArgs):
    rule_id: int = Field(gt=0)


async def asks_list(ctx, args):
    values = await asks.compact_list(ctx.conn, principal=ctx.principal, **args)
    return rows(values, ask, total=values[0]["total"] if values else 0,
                hint="Pass owner= or limit= to read more asks.")


async def asks_get(ctx, args):
    row = await asks.get(ctx.conn, args["id"], principal=ctx.principal)
    if not row:
        return None
    text = [ask(row), body(row["question"]), "Options: " + json_text(row["options"]),
            "Recommended: " + str(row["recommended_option"] or "none"),
            "Proof: " + body(json_text(row["proof"]))]
    return Compact("\n".join(text), row["visibility"] == "private")


def date(value):
    if value is None:
        return None
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("Use a time with a time zone")
    return result


async def asks_create(ctx, args):
    args = dict(args)
    owner = await people.get(ctx.conn, args.pop("owner"))
    if not owner:
        raise ValueError("The owner is missing or inactive")
    if args["lane_id"]:
        lane = await lanes.get(ctx.conn, args["lane_id"], principal=ctx.principal)
        if not lane or (lane["visibility"] == "private" and lane["owner_person_id"] != owner["id"]):
            raise ValueError("The lane is not visible to the owner")
    args["due_at"], args["expires_at"] = date(args["due_at"]), date(args["expires_at"])
    requester_token_id = None
    if ctx.run["profile"] == "agent_request":
        requester_token_id = await mcp.requester_token_for_run(ctx.conn, ctx.run["id"])
    ask = await asks.create(
        ctx.conn,
        owner_person_id=owner["id"],
        requester_person_id=ctx.run["person_id"],
        requester_token_id=requester_token_id,
        **args,
    )
    return {"ask_id": ask["id"], "state": ask["state"]}


async def answer_preflight(ctx, args):
    ask = await ledger.lock_ask(ctx.conn, args["id"])
    if (
        not ask
        or ask["owner_person_id"] != ctx.run["person_id"]
        or not await is_live(ctx.conn, ask)
        or (args["option"] is None and not args["text"])
        or (
            args["option"] is not None
            and not any(o["id"] == args["option"] for o in ask["options"])
        )
    ):
        return Decision(
            "deny", "Only the owner can answer an open ask with a proposed option or text"
        )
    return Decision("allow", "The ask belongs to this person")


async def asks_answer(ctx, args):
    ask = await ledger.lock_ask(ctx.conn, args["id"])
    if (
        not ask
        or ask["owner_person_id"] != ctx.run["person_id"]
        or not await is_live(ctx.conn, ask)
    ):
        raise ValueError("The ask is no longer open for this owner")
    answered = await asks.answer(
        ctx.conn,
        ask["id"],
        person_id=ctx.run["person_id"],
        option=args["option"],
        text=args["text"],
        via="approved_draft",
    )
    if not answered:
        raise ValueError("The ask cannot accept this answer")
    await ledger.decision(ctx.conn, answered)
    return {"answered": True}


async def asks_answer_with_rule(ctx, args):
    ask = await asks.get(ctx.conn, args["id"], principal=ctx.principal)
    if not ask or not await is_live(ctx.conn, ask):
        return {"answered": False}
    answered = await asks.answer_with_rule(ctx.conn, args["id"], args["rule_id"])
    if answered:
        await ledger.decision(ctx.conn, answered)
    return {"answered": bool(answered), "answer": answered}


async def asks_cancel_mine(ctx, args):
    ask = await ledger.lock_ask(ctx.conn, args["id"])
    if not ask or ctx.run["person_id"] not in {ask["owner_person_id"], ask["requester_person_id"]}:
        return {"cancelled": False}
    return {"cancelled": await asks.cancel(ctx.conn, ask["id"])}


async def asks_digest(ctx, args):
    from illo.tools.lanes import lanes_attention

    return await lanes_attention(ctx, args)


def register(registry):
    # Tainted reads use ctx.principal, which hides all other people's private lanes.
    declarations = [
        ("asks_list", asks_list, ListArgs, "read", READ_PROFILES, lambda a: "ledger"),
        ("asks_get", asks_get, GetArgs, "read", READ_PROFILES, lambda a: "ledger"),
        (
            "asks_create",
            asks_create,
            CreateArgs,
            "write_internal",
            frozenset({"person_dm", "team_channel", "routine", "agent_request"}),
            lambda a: f"person:{a['owner']}",
        ),
        (
            "asks_answer",
            asks_answer,
            AnswerArgs,
            "send_outside",
            PERSON_PROFILES,
            lambda a: "ledger",
        ),
        (
            "asks_answer_with_rule",
            asks_answer_with_rule,
            RuleAnswerArgs,
            "write_internal",
            RULE_PROFILES,
            lambda a: "ledger",
        ),
        (
            "asks_cancel_mine",
            asks_cancel_mine,
            GetArgs,
            "write_internal",
            PERSON_PROFILES,
            lambda a: "ledger",
        ),
        (
            "asks_digest",
            asks_digest,
            Arguments,
            "read",
            frozenset({"person_dm"}),
            lambda a: "ledger",
        ),
    ]
    for name, handler, arguments, risk, profiles, destination in declarations:
        registry.add(
            Tool(
                name,
                "asks",
                risk,
                destination,
                name == "asks_digest",
                handler,
                name.replace("_", " "),
                arguments,
                profiles=profiles - {"tainted"},
                preflight=answer_preflight if name == "asks_answer" else None,
            )
        )
