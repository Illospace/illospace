"""Workspace connector contracts. Credentials are vault names, never values."""

from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


GOOGLE_SCOPES = (
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
)


class ConnectorTool(Model):
    name: str = Field(min_length=1)
    risk: Literal["read", "write_internal", "send_outside"]
    description: str = ""
    tainted: bool = False

    @model_validator(mode="after")
    def safe_taint(self):
        if self.tainted and self.risk != "read":
            raise ValueError("Only a read tool can allow a tainted run")
        return self


class Auth(Model):
    type: Literal["bearer", "oauth", "service_account"] = "bearer"
    secret: str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    authorization_url: str | None = None
    token_url: str | None = None
    scopes: list[str] = Field(default_factory=list)
    redirect_uri: str = "http://127.0.0.1:8765/callback"

    @field_validator("authorization_url", "token_url", "redirect_uri")
    @classmethod
    def safe_url(cls, value):
        if value:
            validate_url(value)
        return value


def validate_url(value):
    parts = urlsplit(value)
    if not parts.hostname or parts.username or parts.password or parts.fragment:
        raise ValueError("Use a URL without credentials or a fragment")
    if parts.scheme != "https" and not (
        parts.scheme == "http" and parts.hostname in {"localhost", "127.0.0.1", "::1"}
    ):
        raise ValueError("Use HTTPS or a loopback HTTP URL")
    return value


class Connector(Model):
    key: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    kind: Literal["mcp", "google", "github"]
    scope: Literal["team", "personal"]
    url: str | None = None
    tools: list[ConnectorTool] = Field(default_factory=list)
    auth: Auth | None = None
    scopes: list[str] = Field(default_factory=lambda: list(GOOGLE_SCOPES))
    drive_id: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_-]+$")
    folder_ids: list[str] = Field(default_factory=list)
    team_grant: bool = False
    timeout_s: float = Field(default=20, gt=0, le=120)
    max_bytes: int = Field(default=262144, ge=1024, le=1048576)

    @field_validator("tools", mode="before")
    @classmethod
    def tool_map(cls, value):
        if isinstance(value, dict):
            return [{"name": name, **config} for name, config in value.items()]
        return value

    @field_validator("url")
    @classmethod
    def safe_url(cls, value):
        return validate_url(value) if value else value

    @model_validator(mode="after")
    def valid_connector(self):
        names = [t.name for t in self.tools]
        if len(names) != len(set(names)):
            raise ValueError("Use unique tool names")
        if self.kind == "mcp" and (not self.url or self.scope != "team"):
            raise ValueError("An MCP connector needs a team scope and URL")
        if self.kind == "google":
            if not self.scopes or not set(self.scopes) <= set(GOOGLE_SCOPES):
                raise ValueError("Use only the three Google read-only scopes")
            if self.scope == "personal" and self.auth:
                raise ValueError("A personal Google connector uses its owner's OAuth grant")
            if self.scope == "team" and not (
                self.team_grant
                or (self.auth and self.auth.type == "service_account" and self.auth.secret)
            ):
                raise ValueError("A shared drive needs a service account or an explicit team grant")
            if self.scope == "team" and not (self.drive_id or self.folder_ids):
                raise ValueError("A shared drive needs a drive ID or folder IDs")
        if any(not f or not all(c.isalnum() or c in "_-" for c in f) for f in self.folder_ids):
            raise ValueError("Use valid Google folder IDs")
        return self
