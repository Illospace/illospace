"""Ask tools use the authenticated person for answers and cancellations."""

from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from illo.asks import cards, proof
from illo.asks.service import is_live, sync_card
from illo.board import doors
from illo.channels.mcp.schemas import check_ask_text
from illo.db import all_rows, one
from illo.ledger import ask_workflow as ledger
from illo.ledger import asks, board, items, lanes, mcp, people
from illo.tasks import pages
from illo.tasks.pages import outside
from illo.tasks.pages.schema import Block
from illo.tools._comms_guard import contains_secret, has_hidden_format
from illo.tools._format import Compact, ask, body, data_text, json_text, rows
from illo.tools.checks import Decision
from illo.tools.registry import Arguments, Refusal, Tool

READ_PROFILES = frozenset({"person_dm", "team_channel", "tainted", "routine", "agent_request", "board_coordinator"})
PERSON_PROFILES = frozenset({"person_dm"})
RULE_PROFILES = frozenset({"person_dm", "agent_request"})


class ListArgs(Arguments):
    owner: str | None = None
    requester: str | None = None
    state: Literal["open", "answered", "all"] = "open"
    limit: int = Field(default=15, ge=1, le=40)


class GetArgs(Arguments):
    id: int = Field(gt=0)


class Option(Arguments):
    id: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_.-]+$")
    label: str = Field(min_length=1, max_length=200)
    detail: str = Field(default="", max_length=1000)


class CreateArgs(Arguments):
    owner: str | None = Field(default=None, min_length=1)
    owners: list[str] | None = Field(default=None, min_length=1, max_length=20)
    question: str = Field(min_length=1, max_length=2000)
    kind: Literal["decision", "action", "review", "reminder"] = "decision"
    options: list[Option] = Field(default_factory=list, max_length=25)
    recommended_option: str | None = None
    proof: dict = Field(default_factory=dict)
    blocks: list[Block] = Field(default_factory=list, max_length=12)
    share_saved: bool = False
    lane_id: int | None = Field(default=None, gt=0)
    due_at: str | None = None
    expires_at: str | None = None
    allow_rule: bool = True
    permission: bool = False

    @model_validator(mode="after")
    def visible_text(self):
        if bool(self.owner) == bool(self.owners):
            raise ValueError("Give either owner or owners")
        proof.check(self.proof)
        check_ask_text(self.model_dump_json())
        return self


class AnswerArgs(GetArgs):
    option: str | None = None
    text: str | None = Field(default=None, min_length=1, max_length=10000)


class RuleAnswerArgs(GetArgs):
    rule_id: int = Field(gt=0)


class ReassignArgs(GetArgs):
    owner: str = Field(min_length=1)


async def asks_list(ctx, args):
    values = await asks.compact_list(ctx.conn, principal=ctx.principal, **args)
    result = rows(
        values,
        ask,
        total=values[0]["total"] if values else 0,
        hint="Pass owner=, requester=, state= ('open', 'answered', 'all'), or limit= to read more asks.",
    )
    return Compact(data_text("asks", result.text), result.private)


async def asks_get(ctx, args):
    row = await asks.get(ctx.conn, args["id"], principal=ctx.principal)
    if not row:
        return None
    text = [
        ask(row),
        body(row["question"]),
        "Options: " + json_text(row["options"]),
        "Recommended: " + str(row["recommended_option"] or "none"),
        "Proof: " + body(json_text(row["proof"])),
    ]
    return Compact(data_text("asks", "\n".join(text)), row["visibility"] == "private")


def date(value):
    if value is None:
        return None
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("Use a time with a time zone")
    return result


async def _ensure_saved_visible(conn, proof_dict, *, principal, requester_person_id, targets, share_saved):
    saved = await proof.saved_items(conn, proof_dict, principal)
    for ref, item in saved.items():
        if not item:
            raise ValueError(f"You cannot see {ref}.")
        missing_for_target = False
        for target in targets:
            if not await items.get(conn, ref, principal="person:" + target["key"]):
                missing_for_target = True
                break
        if missing_for_target:
            if (
                share_saved
                and requester_person_id is not None
                and item["kind"] == "saved"
                and item["owner_person_id"] == requester_person_id
            ):
                combined = f"{item['title']}\n{item['body']}"
                if contains_secret(combined) or has_hidden_format(combined):
                    raise Refusal(
                        "This saved item cannot be shared automatically because it contains "
                        "sensitive credentials or hidden control characters."
                    )
                published = await items.publish(conn, ref, owner_person_id=requester_person_id)
                if not published:
                    raise ValueError(f"The person who must answer cannot see {ref}.")
            else:
                raise ValueError(f"The person who must answer cannot see {ref}.")
    return saved


async def asks_create(ctx, args):
    args = dict(args)
    owner_key = args.pop("owner", None)
    owner_keys = args.pop("owners", None)
    share_saved = args.pop("share_saved", False)
    blocks = args.get("blocks") or []
    if blocks and not pages.enabled(ctx.app):
        raise Refusal(
            "This team has no pages. Put the text in proof.text and the addresses in proof.links."
        )
    multi = owner_keys is not None
    raw_keys = owner_keys if multi else [owner_key]
    targets = []
    seen_ids = set()
    for key in raw_keys:
        person = await people.get(ctx.conn, key)
        if not person:
            raise ValueError("The owner is missing or inactive")
        if person["id"] not in seen_ids:
            seen_ids.add(person["id"])
            targets.append(person)
    lane = None
    if args["lane_id"]:
        lane = await lanes.get(ctx.conn, args["lane_id"], principal=ctx.principal)
        for person in targets:
            if not lane or (lane["visibility"] == "private" and lane["owner_person_id"] != person["id"]):
                raise ValueError("The lane is not visible to the owner")
    await _ensure_saved_visible(
        ctx.conn,
        args["proof"],
        principal=ctx.principal,
        requester_person_id=ctx.run["person_id"],
        targets=targets,
        share_saved=share_saved,
    )
    files = await outside.prepare(ctx.app, blocks, store_refs=True) if blocks else []
    args["due_at"], args["expires_at"] = date(args["due_at"]), date(args["expires_at"])
    requester_token_id = None
    token = None
    if ctx.run["profile"] == "agent_request":
        requester_token_id = await mcp.requester_token_for_run(ctx.conn, ctx.run["id"])
        if requester_token_id:
            token = await one(ctx.conn, "SELECT * FROM tokens WHERE id=%s", (requester_token_id,))
    created = []
    has_pages = pages.enabled(ctx.app)
    base = pages.base_url(ctx.app) if has_pages else None
    for person in targets:
        row = await asks.create(
            ctx.conn,
            owner_person_id=person["id"],
            requester_person_id=ctx.run["person_id"],
            requester_token_id=requester_token_id,
            **args,
        )
        if files:
            await outside.replace(ctx.conn, files, ask_id=row["id"])
        if ctx.run["profile"] == "agent_request" and doors.coordinator_active(ctx.app.workspace):
            await doors.attach_ask_to_board(
                ctx.conn, ctx.app.workspace, row, token=token, lane=lane, owner=person, via="asks_create"
            )
        entry = {"ask_id": row["id"], "state": row["state"], "owner": person["key"]}
        if has_pages:
            entry["page_url"] = f"{base}/web/a/{row['id']}"
        created.append(entry)
    if multi:
        return {"asks": created, "count": len(created)}
    first = created[0]
    result = {"ask_id": first["ask_id"], "state": first["state"]}
    if "page_url" in first:
        result["page_url"] = first["page_url"]
    return result


async def answer_preflight(ctx, args):
    ask = await ledger.lock_ask(ctx.conn, args["id"])
    if await asks.is_board_native(ctx.conn, ask):
        return Decision("deny", "Answer this board question with its buttons or on the thread page.")
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
    ask = await board.lock_for_ask(ctx.conn, args["id"])
    if (
        not ask
        or await asks.is_board_native(ctx.conn, ask)
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
    await board.record_decision(ctx.conn, answered, workspace=ctx.app.workspace)
    await ledger.decision(ctx.conn, answered)
    return {"answered": True}


async def asks_answer_with_rule(ctx, args):
    ask = await asks.get(ctx.conn, args["id"], principal=ctx.principal)
    if await asks.is_board_native(ctx.conn, ask):
        raise ValueError("Answer this board question with its buttons or on the thread page.")
    if not ask or not await is_live(ctx.conn, ask):
        return {"answered": False}
    locked = await board.lock_for_ask(ctx.conn, args["id"])
    if not locked or not await is_live(ctx.conn, locked):
        return {"answered": False}
    answered = await asks.answer_with_rule(ctx.conn, args["id"], args["rule_id"])
    if answered:
        await board.record_decision(ctx.conn, answered, workspace=ctx.app.workspace)
        await ledger.decision(ctx.conn, answered)
    return {"answered": bool(answered), "answer": answered}


async def _resolve_active_person(conn, value: str) -> dict | None:
    raw = value.strip()
    if raw.startswith("person:"):
        raw = raw[7:]
    person = await people.get(conn, raw)
    if person and person.get("active"):
        return person
    lower = raw.lower()
    if not lower:
        return None
    members = await all_rows(conn, "SELECT * FROM people WHERE active")
    matches = [
        m
        for m in members
        if lower
        in {
            m["key"].lower(),
            m["name"].lower(),
            (m.get("github_login") or "").lower(),
        }
    ]
    return matches[0] if len(matches) == 1 else None


async def asks_reassign(ctx, args):
    ask = await board.lock_for_ask(ctx.conn, args["id"])
    if not ask or not await is_live(ctx.conn, ask):
        raise ValueError("The ask is no longer open.")
    if ctx.run["person_id"] not in {ask["owner_person_id"], ask["requester_person_id"]}:
        raise Refusal("Only the current owner or requester can reassign this ask.")
    caller = await one(ctx.conn, "SELECT * FROM people WHERE id=%s AND active", (ctx.run["person_id"],))
    if not caller:
        raise Refusal("Caller is inactive.")
    new_owner = await _resolve_active_person(ctx.conn, args["owner"])
    if not new_owner:
        raise ValueError("The new owner is missing or inactive.")
    if new_owner["id"] == ask["owner_person_id"]:
        return {
            "reassigned": False,
            "ask_id": ask["id"],
            "owner": new_owner["key"],
            "reason": "already_owner",
        }
    if ask["lane_id"]:
        lane = await lanes.get(ctx.conn, ask["lane_id"], principal=ctx.principal)
        if not lane or (lane["visibility"] == "private" and lane["owner_person_id"] != new_owner["id"]):
            raise ValueError("The lane is not visible to the new owner.")
    await _ensure_saved_visible(
        ctx.conn,
        ask.get("proof") or {},
        principal=ctx.principal,
        requester_person_id=ctx.run["person_id"],
        targets=[new_owner],
        share_saved=False,
    )
    old_owner_id = ask["owner_person_id"]
    old_channel, old_ts = ask.get("slack_channel"), ask.get("slack_ts")
    is_board = await asks.is_board_ask(ctx.conn, ask)
    board_info = None
    async with ctx.conn.transaction():
        if is_board:
            board_info = await doors.reassign_board_ask(
                ctx.conn,
                ctx.app.workspace,
                ask,
                old_owner_id=old_owner_id,
                new_owner=new_owner,
                caller=caller,
                slack_connected=bool(ctx.app.slack_client),
            )
        updated = await asks.reassign(ctx.conn, ask["id"], new_owner_id=new_owner["id"])
        if not updated:
            raise ValueError("The ask is no longer open.")
    if ctx.app.slack_client and old_channel and old_ts:
        notice = f"Reassigned to {new_owner['name']}."
        try:
            await ctx.app.slack_client.chat_update(
                channel=old_channel,
                ts=old_ts,
                text=notice,
                blocks=[cards.section(f"Reassigned to *{cards.escape(new_owner['name'])}*.")],
            )
        except Exception:
            pass
    if not is_board and ctx.app.slack_client:
        try:
            await sync_card(ctx.app, ctx.conn, updated)
        except Exception:
            pass
    result = {
        "reassigned": True,
        "ask_id": updated["id"],
        "owner": new_owner["key"],
    }
    if board_info:
        result["thread_id"] = board_info["thread_id"]
    return result


async def asks_cancel_mine(ctx, args):
    ask = await board.lock_for_ask(ctx.conn, args["id"])
    if not ask or ctx.run["person_id"] not in {ask["owner_person_id"], ask["requester_person_id"]}:
        return {"cancelled": False}
    cancelled = await asks.cancel(ctx.conn, ask["id"])
    if cancelled:
        await doors.close_board_for_ask(ctx.conn, ask["id"])
    return {"cancelled": cancelled}


async def asks_digest(ctx, args):
    from illo.tools.digest import digest_snapshot_read

    return await digest_snapshot_read(ctx, args)


DESCRIPTIONS = {
    "asks_list": "List asks that this run can see (`state`: `open` by default, `answered`, or `all`). Optional `owner` or `requester` (person keys) filter the list. One line per ask with its id, kind, question, owner, status/answer, due time, and link.",
    "asks_get": "Read one ask by `id`: the question, its options, the recommended option, and the proof that the asker gave.",
    "asks_create": "Ask one teammate (`owner`) or several teammates (`owners`, up to 20) for a decision, an action, a review, or a reminder; each gets their own card. Then tell the requester whom you asked and what. Pass person keys from `people_list`; put context or `saved:<id>` refs in `proof` (set `share_saved: true` to publish your own private saved item for recipients), full reading material in `blocks`, and `options` when the answer is a choice. At most 10 calls per run.",
    "asks_answer": "Answer an open ask of this person with an option id in `option`, or with `text`. Always a draft card: the answer goes out only when the person taps Send. An ask of another person is refused.",
    "asks_answer_with_rule": "Answer an open ask with a standing rule of its owner: the ask `id` and the `rule_id` from `rules_list`. Code answers only when the rule matches one of the ask's options; else the result is `answered: false`. Never use it to decide yourself.",
    "asks_reassign": "Reassign an open ask (including board decisions) from its current owner to another teammate (`id`: ask id, `owner`: person key or name). Preserves the board thread link so the new owner's answer wakes the waiting agent.",
    "asks_cancel_mine": "Cancel an open ask that this person owns or asked, when they say it is no longer needed. To hand an ask over to another teammate instead, use `asks_reassign`. Returns `cancelled: true` or `false`.",
    "asks_digest": "Older name of `digest_snapshot`, with the same result. Use `digest_snapshot`.",
}


def _create_destination(args: dict) -> str:
    if args.get("owner"):
        return f"person:{args['owner']}"
    owners = args.get("owners") or []
    if len(owners) == 1:
        return f"person:{owners[0]}"
    return "channel:team"


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
            _create_destination,
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
            "asks_reassign",
            asks_reassign,
            ReassignArgs,
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
                DESCRIPTIONS[name],
                arguments,
                profiles=profiles - {"tainted"},
                preflight=answer_preflight if name == "asks_answer" else None,
            )
        )
