"""Agent identity and product-contract prompt support."""

from brain.systems.personality.agent_contract import (
    DEFAULT_AGENT_CONTRACT_MD,
    agent_contract_prompt_section,
)
from brain.systems.personality.soul import (
    DEFAULT_SOUL_MD,
    manage_agent_soul,
    read_agent_soul,
    soul_prompt_section,
)

__all__ = [
    "DEFAULT_AGENT_CONTRACT_MD",
    "DEFAULT_SOUL_MD",
    "agent_contract_prompt_section",
    "manage_agent_soul",
    "read_agent_soul",
    "soul_prompt_section",
]
