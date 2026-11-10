"""Validate agent calls before they can change the ledger."""

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Source = Literal["t3", "claude-code", "codex", "github", "manual", "email"]
State = Literal["working", "waiting_person", "blocked", "review", "done", "failed", "unknown"]


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)


class Submit(Arguments):
    message: str = Field(min_length=1)
    desired_outcome: str = ""
    origin: str | None = None
    parts: list[dict[str, Any] | str] = Field(default_factory=list)
    source: dict[str, Any] = Field(default_factory=dict)
    constraints: dict[str, Any] = Field(default_factory=dict)
    correlation: dict[str, Any] = Field(default_factory=dict)
    response: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, min_length=1)
    source_tool: str = "external"
    repo: str | None = None
    branch: str | None = None
    task_title: str | None = None
    files_touched: list[str] = Field(default_factory=list)
    session_id: str | None = None
    run_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class GetResult(Arguments):
    event_id: str | None = None
    submission_id: str | None = None
    result_id: str | None = None
    compact: bool = False
    view: str | None = None
    include_payload: bool = True
    limit: int = 25

    @model_validator(mode="after")
    def require_id(self):
        ids = {v for v in (self.event_id, self.submission_id, self.result_id) if v}
        if len(ids) != 1:
            raise ValueError("Give one event_id, submission_id, or result_id")
        return self


class Read(Arguments):
    capability: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)


class Search(Arguments):
    query: str = Field(min_length=1)
    sources: list[str] = Field(default_factory=list)
    kinds: list[str] = Field(default_factory=list)
    limit: int = 10


class GetKnowledge(Arguments):
    source_ref: str | None = None
    ref: str | None = None
    id: str | int | None = None


class Lane(Arguments):
    source: Source
    external_id: str = Field(min_length=1)
    title: str | None = None
    state: State = "unknown"
    summary: str = ""
    links: list[Any] = Field(default_factory=list)
    repo: str | None = None
    machine: str | None = None
    owner: str | None = None
    event: str | None = None
    end_summary: str | None = None
    visibility: Literal["team", "private"] = "team"


class Collector(Arguments):
    label: str = Field(min_length=1, max_length=200)
    version: str = ""
    threads_seen: int = Field(default=0, ge=0)


class LaneReport(Lane):
    source: Source | None = None
    external_id: str | None = Field(default=None, min_length=1)
    lanes: list[Lane] | None = Field(default=None, max_length=1000)
    collector: Collector | None = None

    @model_validator(mode="after")
    def require_lane(self):
        if self.lanes is None and (not self.source or not self.external_id):
            raise ValueError("Give source and external_id, or lanes")
        if self.lanes is not None and (self.source or self.external_id):
            raise ValueError("Give one lane or a batch")
        return self


class LaneSearch(Arguments):
    query: str = ""
    state: State | Literal["stale"] | None = None
    owner: str | None = None
    limit: int = Field(default=20, ge=1, le=50)


class LaneRef(Arguments):
    source: Source
    external_id: str = Field(min_length=1)


class Option(Arguments):
    id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    detail: str = ""


class AskCreate(Arguments):
    lane: LaneRef | None = None
    kind: Literal["decision", "action", "review", "reminder"] = "decision"
    question: str = Field(min_length=1, max_length=240)
    options: list[Option] = Field(min_length=2, max_length=5)
    recommended: str
    proof: dict[str, Any] = Field(default_factory=dict)
    owner: str | None = None
    due_at: str | None = None
    expires_at: str | None = None
    source_ref: str | None = Field(default=None, min_length=1)

    @field_validator("question")
    @classmethod
    def one_sentence(cls, value):
        if "\n" in value or len(re.findall(r"[.!?](?:\s+|$)", value)) > 1:
            raise ValueError("The question must be one short sentence")
        return value

    @model_validator(mode="after")
    def check_options(self):
        ids = [o.id for o in self.options]
        if len(set(ids)) != len(ids) or self.recommended not in ids:
            raise ValueError("Use unique option IDs and a valid recommended option")
        return self


class AskPull(Arguments):
    scope: Literal["token", "person"] = "token"


class AskAck(Arguments):
    ask_id: int = Field(gt=0)


class AskCancel(Arguments):
    ask_id: int | None = Field(default=None, gt=0)
    source_ref: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def require_reference(self):
        if (self.ask_id is None) == (self.source_ref is None):
            raise ValueError("Give ask_id or source_ref")
        return self


class Signal(Arguments):
    source: str = Field(min_length=1)
    dedupe_key: str = Field(min_length=1)
    severity: str = Field(min_length=1)
    title: str = Field(min_length=1)
    body: str = Field(min_length=1)
    data: dict[str, Any] = Field(default_factory=dict)
