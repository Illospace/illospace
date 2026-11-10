"""Skill tools give the brain the steps of one team skill on demand.

The prompt lists only the name and the description of each skill.
This keeps each prompt small. The body loads when a request needs it.
"""

from pydantic import Field

from illo.tools.registry import Arguments, Tool


class GetArgs(Arguments):
    name: str = Field(min_length=1, max_length=200)


async def skills_get(ctx, args):
    """Return the body of one skill from the loaded workspace."""
    skill = next((s for s in ctx.app.workspace.skills if s.name == args["name"]), None)
    if skill is None:
        return {"found": False, "names": [s.name for s in ctx.app.workspace.skills]}
    return {
        "found": True,
        "name": skill.name,
        "description": skill.description,
        "steps": skill.body,
    }


def register(registry) -> None:
    """Register the skill reader.

    The team writes the skill files, and the tool only reads them. So the tool is
    safe in a tainted run: hostile text can only make it read team procedures.
    An outsider run does not get it, because a skill can name people and owners.
    """
    registry.add(
        Tool(
            "skills_get",
            "skills",
            "read",
            lambda a: "ledger",
            False,
            skills_get,
            "Read the steps of one team skill by its name",
            GetArgs,
            profiles=frozenset(
                {"person_dm", "team_channel", "tainted", "routine", "agent_request"}
            ),
        )
    )
