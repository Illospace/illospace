"""Chief-of-Staff action tools for board triage runs."""

from typing import Literal

from pydantic import Field

from illo.board import coordinator
from illo.tools.registry import Arguments, Tool, ToolContext

PROFILES = frozenset({"board_coordinator"})


class CardOption(Arguments):
    label: str = Field(min_length=1, max_length=60)
    detail: str = Field(default="", max_length=120)


class GroupArgs(Arguments):
    group_key: str | None = Field(default=None, max_length=64)
    group_clear: bool = False


class AnswerArgs(GroupArgs):
    answer: str = Field(min_length=1, max_length=4000)
    sources: list[str] = Field(min_length=1, max_length=10)


class RouteArgs(GroupArgs):
    target: str = Field(pattern=r"^(agent|person):[a-z][a-z0-9_-]*$")
    minutes: int = Field(default=30, ge=5, le=240)
    note: str = Field(default="", max_length=300)


class AskPersonArgs(GroupArgs):
    person: str = Field(pattern=r"^person:[a-z][a-z0-9_-]*$")
    format: Literal["dm", "card", "link"]
    summary: str = Field(default="", max_length=1800)
    question: str = Field(default="", max_length=300)
    options: list[CardOption] = Field(default_factory=list, max_length=4)
    recommended: int | None = Field(default=None, ge=0, le=3)


class WaitArgs(GroupArgs):
    recheck_minutes: int = Field(default=30, ge=5, le=240)
    reason: str = Field(default="", max_length=300)


async def _answer(ctx: ToolContext, args: dict) -> dict:
    return await coordinator.apply_tool_action(ctx.conn, ctx.app, ctx.run, "answer", args)


async def _route(ctx: ToolContext, args: dict) -> dict:
    return await coordinator.apply_tool_action(ctx.conn, ctx.app, ctx.run, "route", args)


async def _ask_person(ctx: ToolContext, args: dict) -> dict:
    return await coordinator.apply_tool_action(ctx.conn, ctx.app, ctx.run, "ask_person", args)


async def _wait(ctx: ToolContext, args: dict) -> dict:
    return await coordinator.apply_tool_action(ctx.conn, ctx.app, ctx.run, "wait", args)


def register(registry) -> None:
    specs = [
        (
            "need_answer",
            _answer,
            AnswerArgs,
            "Answer a factual board question directly on the thread after verifying the answer with read tools. "
            "Cite every source you checked in `sources`. Never use this for a decision_needed item or turn_cap stall.",
        ),
        (
            "need_route",
            _route,
            RouteArgs,
            "Route a factual question to another online or wakeable agent (`target` from packet candidates) "
            "and wait `minutes` before escalating to a person. Never use for decision_needed or turn_cap.",
        ),
        (
            "need_ask_person",
            _ask_person,
            AskPersonArgs,
            "Queue one Slack message or decision card for a candidate person. Write `summary` in clear, plain human prose "
            "explaining what is happening, why it matters, and what you recommend (no raw agent jargon). "
            "For decision_needed items, use format='card': the agent's exact question and option buttons are preserved automatically.",
        ),
        (
            "need_wait",
            _wait,
            WaitArgs,
            "Defer triage and recheck this board item after `recheck_minutes` when active work is still settling "
            "and no person needs to be interrupted yet.",
        ),
    ]
    for name, handler, arguments, description in specs:
        registry.add(
            Tool(
                name,
                "board_triage",
                "write_internal",
                lambda _: "ledger",
                False,
                handler,
                description=description,
                arguments=arguments,
                profiles=PROFILES,
            )
        )
