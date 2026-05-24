"""Agent personality prompt support."""

from brain.systems.personality.agent_profile import (
    DEFAULT_AGENT_PROFILE_MD,
    agent_profile_prompt_section,
)
from brain.systems.personality.soul import (
    DEFAULT_SOUL_MD,
    manage_agent_soul,
    read_agent_soul,
    soul_prompt_section,
)

__all__ = [
    "DEFAULT_AGENT_PROFILE_MD",
    "DEFAULT_SOUL_MD",
    "agent_profile_prompt_section",
    "manage_agent_soul",
    "read_agent_soul",
    "soul_prompt_section",
]
