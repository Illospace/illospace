"""Read boot settings without requiring a channel connection."""

import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator


class Config(BaseModel):
    """Settings use ILLO_ plus the uppercase field name."""

    model_config = ConfigDict(extra="forbid")
    database_url: str = "postgresql://illo:illo@127.0.0.1:5432/illo"
    workspace_dir: Path = Path("workspace.example")
    vault_key: SecretStr | None = None
    public_url: str = "http://127.0.0.1:8000"
    brain_url: str = "http://127.0.0.1:8001"
    brain_runtime: str | None = None
    brain_model: str | None = None
    slack_bot_token: SecretStr | None = None
    slack_app_token: SecretStr | None = None
    github_token: SecretStr | None = None
    github_app_id: str | None = None
    github_app_private_key_file: Path | None = None
    github_app_installation_id: str | None = None
    email_host: str | None = None
    email_user: str | None = None
    email_password: SecretStr | None = None
    revision: str = "unknown"
    migrate_on_start: bool = True
    reload_token: SecretStr | None = None
    workspace_check_seconds: float = Field(default=30, gt=0)
    pool_min_size: int = Field(default=1, ge=1)
    pool_max_size: int = Field(default=10, ge=1)

    @model_validator(mode="after")
    def validate_pool(self):
        if self.pool_max_size < self.pool_min_size:
            raise ValueError("pool_max_size must be at least pool_min_size")
        return self

    @classmethod
    def from_env(cls) -> "Config":
        """Ignore unrelated environment variables. Blank optional fields are absent."""
        return cls.model_validate(
            {
                name: os.environ[f"ILLO_{name.upper()}"]
                for name in cls.model_fields
                if os.environ.get(f"ILLO_{name.upper()}", "") != ""
            }
        )
