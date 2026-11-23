"""Validate the coordinator's one decision before it can change the board."""

import json
import re
import unicodedata
from typing import Literal

from pydantic import ConfigDict, Field, ValidationError

from illo.asks.cards import escape
from illo.tools.registry import Arguments


class InvalidDecision(ValueError):
    """A fixed error code that can go in the ledger."""


class Option(Arguments):
    label: str = Field(min_length=1)
    detail: str = ""


class Escalate(Arguments):
    person: str = Field(pattern=r"^person:[a-z][a-z0-9_-]*$")
    format: Literal["dm", "card", "link"]
    text: str = ""
    question: str = ""
    options: list[Option] = Field(default_factory=list, max_length=4)
    recommended: int | None = Field(default=None, ge=0, le=3)


class Headstart(Arguments):
    target: str = Field(pattern=r"^(agent|person):[a-z][a-z0-9_-]*$")
    minutes: int = Field(ge=5, le=240)


class Decision(Arguments):
    model_config = ConfigDict(strict=True, extra="forbid")

    action: Literal["wait", "headstart", "escalate", "nothing"]
    item: int | None = None
    recheck_minutes: int | None = Field(default=None, ge=5, le=240)
    headstart: Headstart | None = None
    escalate: Escalate | None = None
    group_key: str | None = None
    group_clear: bool = False


_FENCE = re.compile(r"```json\s+(.*?)\s*```", re.DOTALL)
_SLACK_FORM = re.compile(r"<[^<>]*>")
_URL = re.compile(r"(?:[a-z][a-z0-9+.-]*://|www\.|mailto:)\S+", re.IGNORECASE)
_BROADCAST = re.compile(r"@(?:here|channel|everyone)\b", re.IGNORECASE)


def unescape_slack(value):
    return value.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def clean_slack(value, limit):
    value = unescape_slack(value)
    value = "".join(c for c in value if not unicodedata.category(c).startswith("C"))
    value = value.replace("`", "")
    value = _SLACK_FORM.sub("", value)
    value = _URL.sub("[link removed]", value)
    value = _BROADCAST.sub("", value)
    return escape(" ".join(value.split()))[:limit]


def _escalation(value, snapshot):
    if value is None or value.person not in snapshot["candidates"] or value.format not in snapshot["allowed"]["formats"]:
        raise InvalidDecision("recipient_or_format")
    if snapshot["allowed"]["formats"] == ["link"]:
        if value.text or value.question or value.options or value.recommended is not None:
            raise InvalidDecision("notice_content")
        return value
    value.text = clean_slack(value.text, 400)
    if not value.text:
        raise InvalidDecision("summary_text")
    if value.format != "card":
        if value.question or value.options or value.recommended is not None:
            raise InvalidDecision("card_fields")
        return value
    value.question = clean_slack(value.question, 150)
    for option in value.options:
        option.label = clean_slack(option.label, 40)
        option.detail = clean_slack(option.detail, 80)
    labels = {opt.label.casefold() for opt in value.options}
    if not value.question or not 2 <= len(value.options) <= 4 or "" in labels or len(labels) != len(value.options):
        raise InvalidDecision("card_options")
    if value.recommended is not None and value.recommended >= len(value.options):
        raise InvalidDecision("card_recommendation")
    return value


def parse(text, snapshot):
    text = text.strip()
    if len(text) > 12000:
        raise InvalidDecision("not_json")
    fence = _FENCE.fullmatch(text)
    try:
        value = json.loads(fence[1] if fence else text)
    except (ValueError, RecursionError):
        raise InvalidDecision("not_json") from None
    try:
        decision = Decision.model_validate(value)
    except ValidationError:
        raise InvalidDecision("schema") from None

    if decision.item != snapshot["episode_id"] and (decision.item is not None or decision.action != "nothing"):
        raise InvalidDecision("item")
    if ((decision.escalate is not None) != (decision.action == "escalate")
            or (decision.headstart is not None) != (decision.action == "headstart")
            or (decision.recheck_minutes is not None and decision.action != "wait")):
        raise InvalidDecision("action_fields")
    if decision.headstart:
        allowed = snapshot["allowed"]
        if (not allowed["headstart"] or decision.headstart.target not in snapshot["targets"]
                or decision.headstart.minutes > allowed["max_headstart_minutes"]):
            raise InvalidDecision("headstart")
    if decision.escalate:
        _escalation(decision.escalate, snapshot)
    if decision.group_key is not None:
        key = re.sub(r"[^a-z0-9]+", "-", decision.group_key.lower()).strip("-")
        if decision.group_clear or not 2 <= len(key) <= 48:
            raise InvalidDecision("group_key")
        decision.group_key = key
    return decision
