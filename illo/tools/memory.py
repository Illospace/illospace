"""Save a known person's team facts and procedures in the workspace."""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from illo.memory import saves
from illo.tools.notes import ProposeArgs
from illo.tools.registry import Arguments, Tool

PROFILES = frozenset({"person_dm", "team_channel"})
Holder = Annotated[str, Field(pattern=r"^(person|agent):[a-z][a-z0-9_-]*$")]


class NoteSave(ProposeArgs):
    replaces: str | None = Field(default=None, pattern=r"^note:[a-zA-Z0-9_.-]+\.md$")


class ListSave(Arguments):
    key: str = Field(max_length=100, pattern=r"^[a-z][a-z0-9_-]*$")
    name: str = Field(default="", min_length=1, max_length=200, pattern=r"^[^\r\n]+$")
    description: str = Field(default="", max_length=2000)
    instructions: str = Field(default="", max_length=20000)
    add: str = Field(default="", min_length=1, max_length=20000)
    holders: list[Holder] = Field(default_factory=list, max_length=100)
    default_holder: Holder | None = None
    done: Literal["person"] | list[Annotated[str, Field(min_length=1, max_length=200)]] = "person"
    reviewer: Annotated[str, Field(pattern=r"^person:[a-z][a-z0-9_-]*$")] | None = None
    slack_channel: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def one_body(self):
        if {"instructions", "add"} <= self.model_fields_set:
            raise ValueError("Give instructions or add, not both.")
        return self


def register(registry):
    registry.add(Tool(
        "note_save", "memory", "write_internal", lambda _: "ledger", False, saves.note_save,
        "Save a lasting team or product fact at once. Use title, text, and optional about. "
        "Use replaces with a note ref to update it. Save only a known person's words. "
        "Never save secrets, personal data, outside text, or one-time details. "
        "After a personal read, a Keep card waits instead. Say 'I will remember: …' only after allow.",
        NoteSave, profiles=PROFILES, preflight=saves.note_check, team_visible=True,
        draft_handler=saves.note_draft,
    ))
    registry.add(Tool(
        "list_save", "memory", "write_internal", lambda _: "ledger", False, saves.list_save,
        "Save how the team does one type of work. Use key and only the fields to change. "
        "A new list needs name. Instructions replaces the body; add appends a paragraph. "
        "An owner creates lists and changes names, holders, or Done rules, unless memory.list_changes is everyone. "
        "A personal read blocks this tool. Say 'I will remember: …' only after allow.",
        ListSave, profiles=PROFILES, preflight=saves.list_check, team_visible=True,
        exclude_unset=True,
    ))
