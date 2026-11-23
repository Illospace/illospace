"""Scout findings are internal records. Only their buttons can request task work."""

from pydantic import Field, field_validator

from illo.deep import findings
from illo.tools._format import Compact, data_text, json_text
from illo.tools.registry import Arguments, Tool
from illo.tools.web import normalize_url


class Link(Arguments):
    title: str = Field(min_length=1, max_length=200)
    url: str = Field(min_length=1, max_length=2000)

    @field_validator("url")
    @classmethod
    def https_url(cls, value):
        return str(normalize_url(value))


class Finding(Arguments):
    key: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    title: str = Field(min_length=1, max_length=200)
    message: str = Field(min_length=1, max_length=600)
    links: list[Link] = Field(min_length=1, max_length=4)
    person: str | None = None

    @field_validator("message")
    @classmethod
    def short_message(cls, value):
        if len(value.split()) > 60:
            raise ValueError("A finding has 60 words at most.")
        return value


async def list_findings(ctx, args):
    rows = await findings.list_findings(ctx)
    private = bool(rows) and findings.scope(ctx.run)[0] == "person"
    return Compact(data_text("scout-findings", json_text(rows)), private=private)


def register(registry):
    registry.add(
        Tool(
            "findings_list",
            "scout",
            "read",
            lambda _: "ledger",
            False,
            list_findings,
            "Read the findings of the last 90 days for this scout's scope: key, title, state, and date.",
            Arguments,
            profiles=frozenset(),
        )
    )
    registry.add(
        Tool(
            "finding_add",
            "scout",
            "write_internal",
            lambda _: "ledger",
            False,
            findings.add,
            "Save one finding. Write `message` as a colleague writes it: what happens and when, where it is in our code, what you could not verify, and the next step. 60 words at most. Give the outside source and the place in the code as links.",
            Finding,
            profiles=frozenset(),
        )
    )
