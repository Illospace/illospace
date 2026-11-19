"""A page contains data from a fixed set of block kinds."""

from datetime import datetime
from typing import Annotated, Literal
from unicodedata import category

import httpx
from pydantic import Field, field_validator

from illo.tools.registry import Arguments
from illo.tools.web import normalize_url


class ImageRef(Arguments):
    repo: str = Field(min_length=1, max_length=200)
    ref: str = Field(min_length=1, max_length=200)
    path: str = Field(min_length=1, max_length=1000)

    @field_validator("path")
    @classmethod
    def safe_path(cls, value):
        if value.startswith(("/", "-")) or ".." in value or any(category(c) == "Cc" for c in value):
            raise ValueError("Use a relative file path without control characters or '..'")
        return value


class ImageUrl(Arguments):
    url: str = Field(max_length=2000)

    @field_validator("url", mode="before")
    @classmethod
    def address(cls, value):
        if isinstance(value, str):
            try:
                if any(category(c) == "Cc" for c in value):
                    raise ValueError
                normalize_url(value)
            except (ValueError, httpx.InvalidURL) as exc:
                # Do not repeat an address that can contain credentials.
                raise ValueError("The address is not a public HTTPS address.") from exc
        return value


class Text(Arguments):
    kind: Literal["text"]
    title: str = Field(default="", max_length=80)
    text: str = Field(max_length=6000)


class Preview(Arguments):
    kind: Literal["preview"]
    title: str = Field(default="", max_length=80)
    name: str = Field(max_length=80)
    label: str = Field(default="", max_length=80)
    text: str = Field(max_length=6000)
    image: ImageRef | ImageUrl | None = None


class Image(Arguments):
    kind: Literal["image"]
    title: str = Field(default="", max_length=80)
    image: ImageRef | ImageUrl
    caption: str = Field(default="", max_length=200)
    alt: str = Field(default="", max_length=200)


class Check(Arguments):
    claim: str = Field(max_length=200)
    state: Literal["yes", "no", "open"]
    source_url: str | None = Field(default=None, max_length=2000, pattern=r"^https?://[^\s<>]+$")
    checked_at: str | None = None

    @field_validator("checked_at")
    @classmethod
    def timestamp(cls, value):
        if value is not None:
            at = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if not at.tzinfo:
                raise ValueError("Use a time with a time zone")
        return value


class Checks(Arguments):
    kind: Literal["checks"]
    title: str = Field(default="", max_length=80)
    items: list[Check] = Field(min_length=1, max_length=12)


class Bar(Arguments):
    label: str = Field(max_length=40)
    value: float = Field(ge=0, allow_inf_nan=False)
    unit: str = Field(default="", max_length=12)


class Bars(Arguments):
    kind: Literal["bars"]
    title: str = Field(default="", max_length=80)
    rows: list[Bar] = Field(min_length=1, max_length=12)


Block = Annotated[Text | Preview | Image | Checks | Bars, Field(discriminator="kind")]


class Read(Arguments):
    task: int = Field(ge=1)


class Write(Read):
    blocks: list[Block] = Field(max_length=12)
