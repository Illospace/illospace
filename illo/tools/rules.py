"""Rule tools expose ownership and enforce it for removal."""

from pydantic import Field

from illo.ledger import rules
from illo.tools._format import json_text, line, rows
from illo.tools.registry import Arguments, Tool


class RemoveArgs(Arguments):
    id: int = Field(gt=0)


async def rules_list(ctx, args):
    """Read team rules and the principal's own rules."""
    return rows(await rules.list_rules(ctx.conn, principal=ctx.principal), lambda r: line(
        r["kind"], r["text"], json_text(r["match"]), r.get("answer_option_label"),
        r.get("answer_option_id"), f"id {r['id']}"))


async def rules_remove_mine(ctx, args):
    """Remove only the authenticated person's own rule."""
    return {"removed": await rules.remove(ctx.conn, args["id"], ctx.run["person_id"])}


def register(registry) -> None:
    """Register rule handlers. A team run cannot remove a person's rule."""
    # Tainted reads expose only team rules through the principal visibility filter.
    registry.add(
        Tool(
            "rules_list",
            "rules",
            "read",
            lambda a: "ledger",
            False,
            rules_list,
            "Read standing rules",
            profiles=frozenset(
                {"person_dm", "team_channel", "routine", "agent_request"}
            ),
        )
    )
    registry.add(
        Tool(
            "rules_remove_mine",
            "rules",
            "write_internal",
            lambda a: "ledger",
            False,
            rules_remove_mine,
            "Remove one of your standing rules",
            RemoveArgs,
            profiles=frozenset({"person_dm"}),
        )
    )
