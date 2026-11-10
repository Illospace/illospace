"""Discover tool families and execute each call through the authority checks."""

import importlib
import logging
import pkgutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from illo.db import Connection, one
from illo.ledger import audit, drafts
from illo.tools._format import Compact, json_text
from illo.tools.checks import PROFILE_NAMES, Decision, check, scope

MAX_RESULT_CHARS = 12_000

log = logging.getLogger(__name__)


class Refusal(ValueError):
    """A tool refuses a call. The model may read the reason, so the text must be fixed."""


RUN_LIMITS = {
    "asks_create": 3,
    "slack_send_dm": 3,
    "github_create_issue": 3,
    "github_comment_issue": 5,
    "work_start": 1,
}


class Arguments(BaseModel):
    """Reject fields that a tool does not declare, including principal overrides."""

    model_config = ConfigDict(extra="forbid", strict=True)


@dataclass
class ToolContext:
    """Handlers receive server authority. Arguments never supply that authority."""

    conn: Connection
    run: dict
    app: Any

    @property
    def principal(self) -> str:
        """Tainted runs cannot read their sender's private ledger data."""
        if self.run["principal"] == "outsider":
            return "outsider"
        return "team" if self.run["tainted"] else self.run["principal"]


@dataclass(frozen=True)
class Tool:
    name: str
    family: str
    risk: Literal["read", "write_internal", "send_outside"]
    destination: Callable[[dict], str]
    personal: bool
    handler: Callable[[ToolContext, dict], Awaitable[Any]]
    description: str = ""
    arguments: type[BaseModel] = Arguments
    # The profiles that may call this tool: person_dm, team_channel, tainted, routine,
    # agent_request, outsider. None means: use the default data in checks.py.
    profiles: frozenset[str] | None = None
    # Resolve API facts before a write. A preflight can only narrow the checks.
    preflight: Callable[[ToolContext, dict], Awaitable[Decision]] | None = None

    # A ledger write can expose text to the whole team, as a note does.
    team_visible: bool = False
    # Resolve a configured remote tool before any authority check or draft.
    resolve: Callable[[ToolContext, dict], "Tool"] | None = None
    on_error: Callable[[ToolContext, dict], Awaitable[None]] | None = None

    @property
    def json_schema(self) -> dict:
        """Pydantic supplies the same schema used to validate each call."""
        return self.arguments.model_json_schema()

    def declaration(self) -> dict:
        """Return the MCP tool shape without a server handler."""
        return {"name": self.name, "description": self.description, "inputSchema": self.json_schema}


def _has_private_data(value) -> bool:
    """A private ledger read has the same destination limit as a personal connector."""
    if isinstance(value, Compact):
        return value.private
    if isinstance(value, dict):
        return value.get("visibility") == "private" or any(
            _has_private_data(v) for v in value.values()
        )
    if isinstance(value, (list, tuple)):
        return any(_has_private_data(v) for v in value)
    return False


@dataclass
class Registry:
    tools: dict[str, Tool] = field(default_factory=dict)

    def add(self, tool: Tool) -> None:
        """Reject duplicate names and risks outside the three allowed classes."""
        if tool.name in self.tools:
            raise ValueError(f"Duplicate tool: {tool.name}")
        if tool.risk not in {"read", "write_internal", "send_outside"}:
            raise ValueError(f"Tool risk is not allowed: {tool.name}")
        if tool.profiles is not None and not set(tool.profiles) <= PROFILE_NAMES:
            raise ValueError(f"Tool names an unknown profile: {tool.name}")
        if (
            tool.personal
            and tool.profiles is not None
            and tool.profiles
            & {
                "tainted",
                "outsider",
                "team_channel",
                "routine",
            }
        ):
            raise ValueError(f"A personal tool cannot be in this profile: {tool.name}")
        self.tools[tool.name] = tool

    def discover(self) -> None:
        """Each family exports register(registry). No central family list is needed."""
        import illo.tools

        for info in sorted(pkgutil.iter_modules(illo.tools.__path__), key=lambda i: i.name):
            if info.name in {"registry", "checks"} or info.name.startswith("_"):
                continue
            module = importlib.import_module(f"illo.tools.{info.name}")
            module.register(self)

    def list_tools(self, run: dict) -> list[dict]:
        """Only show tools that this authenticated run can call."""
        return [
            tool.declaration()
            for _, tool in sorted(self.tools.items())
            if scope(run, tool).outcome == "allow"
        ]

    async def call(self, ctx: ToolContext, name: str, args: dict) -> dict:
        """Call under the run row lock. The HTTP layer owns the transaction."""
        tool = self.tools.get(name)
        decision, reason, result = "deny", "The tool is not registered", None
        if tool and name in RUN_LIMITS:
            calls = await one(
                ctx.conn,
                "SELECT count(*) AS n FROM audit WHERE run_id=%s AND tool=%s",
                (ctx.run["id"], name),
            )
            if calls["n"] >= RUN_LIMITS[name]:
                tool = None
                reason = f"The run has reached the {name} call limit of {RUN_LIMITS[name]}"
        if tool:
            try:
                validated = tool.arguments.model_validate(args).model_dump()
            except ValidationError:
                reason = "The tool arguments do not match its schema"
            else:
                try:
                    if tool.resolve:
                        tool = tool.resolve(ctx, validated)
                    verdict = check(ctx.run, tool, validated)
                    if verdict.outcome != "deny" and tool.preflight:
                        extra = await tool.preflight(ctx, validated)
                        if extra.outcome == "deny" or verdict.outcome == "allow":
                            verdict = extra
                except Exception:
                    verdict = Decision("deny", "The tool declaration is invalid")
                decision, reason = verdict.outcome, verdict.reason
                if decision == "draft":
                    if ctx.run["person_id"] is None:
                        decision, reason = "deny", "A draft needs a person to approve it"
                    else:
                        row = await drafts.create(
                            ctx.conn,
                            run_id=ctx.run["id"],
                            approver_person_id=ctx.run["person_id"],
                            kind=name,
                            destination=tool.destination(validated),
                            content={"tool": name, "arguments": validated},
                        )
                        result = {"draft_id": row["id"], "state": "pending"}
                elif decision == "allow":
                    if tool.personal:
                        await ctx.conn.execute(
                            "UPDATE runs SET personal_read=true WHERE id=%s", (ctx.run["id"],)
                        )
                        ctx.run["personal_read"] = True
                    try:
                        async with ctx.conn.transaction():
                            result = await tool.handler(ctx, validated)
                            if tool.risk == "read" and _has_private_data(result):
                                await ctx.conn.execute(
                                    "UPDATE runs SET personal_read=true WHERE id=%s",
                                    (ctx.run["id"],),
                                )
                                ctx.run["personal_read"] = True
                    except Exception as exc:
                        # Keep secrets out of errors and keep the audit row after a rollback.
                        decision, reason = "deny", "The tool failed"
                        if isinstance(exc, Refusal):
                            # A fixed text from the tool. The model can correct the call.
                            reason = f"The tool refused the call: {exc}"
                        log.warning(
                            "Tool %s failed in run %s", name, ctx.run["id"], exc_info=True
                        )
                        if tool.on_error:
                            await tool.on_error(ctx, validated)
        await audit.write(
            ctx.conn,
            run_id=ctx.run["id"],
            principal=ctx.run["principal"],
            tool=name,
            args=args,
            decision=decision,
            reason=reason,
        )
        if isinstance(result, Compact):
            result = result.text
        # Apply the cap after privacy checks, including rows beyond the cut.
        text = result if isinstance(result, str) else json_text(result)
        envelope = {"decision": decision, "reason": reason, "result": result}
        if len(json_text(envelope)) > MAX_RESULT_CHARS:
            note = f"\nResult cut at {MAX_RESULT_CHARS} characters. Ask for less, or for one item."
            # Count JSON escapes too. The model receives this encoded content.
            low, high = 0, min(len(text), MAX_RESULT_CHARS)
            while low < high:
                middle = (low + high + 1) // 2
                candidate = {**envelope, "result": text[:middle] + note}
                if len(json_text(candidate)) <= MAX_RESULT_CHARS:
                    low = middle
                else:
                    high = middle - 1
            result = text[:low] + note
        return {"decision": decision, "reason": reason, "result": result}
