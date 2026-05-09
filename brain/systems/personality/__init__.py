"""Agent personality prompt support."""

from brain.systems.personality.soul import (
    DEFAULT_SOUL_MD,
    manage_agent_soul,
    read_agent_soul,
    soul_prompt_section,
)

__all__ = [
    "DEFAULT_SOUL_MD",
    "manage_agent_soul",
    "read_agent_soul",
    "soul_prompt_section",
]
