"""Give a person a private link to the task list."""

from illo.tasks.list_page import list_link
from illo.tools.registry import Arguments, Tool


class ListLink(Arguments):
    revoke: bool = False


def register(registry):
    registry.add(
        Tool(
            "list_link",
            "connect",
            "write_internal",
            lambda _: "ledger",
            False,
            list_link,
            description="Get a private link to your task list for 24 hours. A channel request sends it by DM. Set revoke to cancel all your list links.",
            arguments=ListLink,
            profiles=frozenset({"person_dm", "team_channel"}),
        )
    )
