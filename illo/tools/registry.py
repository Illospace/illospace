"""Discover tool families and execute each call through the authority checks."""

import importlib
import pkgutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from illo.db import Connection
from illo.ledger import audit, drafts
from illo.tools.checks import Decision, check, grant_names, scope


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

    @property
    def json_schema(self) -> dict:
        """Pydantic supplies the same schema used to validate each call."""
        return self.arguments.model_json_schema()

    def declaration(self) -> dict:
        """Return the MCP tool shape without a server handler."""
        return {"name": self.name, "description": self.description, "inputSchema": self.json_schema}


@dataclass
class Registry:
    tools: dict[str, Tool] = field(default_factory=dict)

    def add(self, tool: Tool) -> None:
        """Reject duplicate names and risks outside the three allowed classes."""
        if tool.name in self.tools:
            raise ValueError(f"Duplicate tool: {tool.name}")
        if tool.risk not in {"read", "write_internal", "send_outside"}:
            raise ValueError(f"Tool risk is not allowed: {tool.name}")
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
        names = grant_names(run)
        return [
            tool.declaration()
            for name, tool in sorted(self.tools.items())
            if name in names and scope(run, tool).outcome == "allow"
        ]

    async def call(self, ctx: ToolContext, name: str, args: dict) -> dict:
        """Call under the run row lock. The HTTP layer owns the transaction."""
        tool = self.tools.get(name)
        decision, reason, result = "deny", "The tool is not registered", None
        if tool:
            try:
                validated = tool.arguments.model_validate(args).model_dump()
            except ValidationError:
                reason = "The tool arguments do not match its schema"
            else:
                try:
                    verdict = check(ctx.run, tool, validated)
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
                    except Exception:
                        # Keep secrets out of errors and keep the audit row after a rollback.
                        decision, reason = "deny", "The tool failed"
        await audit.write(
            ctx.conn,
            run_id=ctx.run["id"],
            principal=ctx.run["principal"],
            tool=name,
            args=args,
            decision=decision,
            reason=reason,
        )
        return {"decision": decision, "reason": reason, "result": result}
