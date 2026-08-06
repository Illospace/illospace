"""Shared retrieval scope contract for indexed knowledge items."""

from enum import StrEnum


KNOWLEDGE_SCOPE_EXTRA_KEY = "knowledge_scope"


class KnowledgeScope(StrEnum):
    """Who may retrieve an indexed knowledge item."""

    ORGANIZATION = "organization"
    GLOBAL = "global"


__all__ = ["KNOWLEDGE_SCOPE_EXTRA_KEY", "KnowledgeScope"]
