"""Import-safe GitHub integration contracts shared across layers."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class GitHubConnectorError(Exception):
    status_code: int
    message: str

    def __post_init__(self) -> None:
        super().__init__(self.message)


__all__ = ["GitHubConnectorError"]
