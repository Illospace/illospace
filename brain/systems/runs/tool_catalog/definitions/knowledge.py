"""Knowledge index tool definitions."""

from __future__ import annotations

from brain.systems.knowledge.search_contract import (
    KNOWLEDGE_SEARCH_DEFAULT_RESULTS,
    KNOWLEDGE_SEARCH_MAX_RESULTS,
)

KNOWLEDGE_TOOLS = [
    {
        "name": "search_knowledge",
        "description": (
            "Search the source-backed company knowledge index with hybrid lexical and semantic recall. "
            "Returns distilled evidence with canonical source_ref provenance; follow that pointer when "
            "the full source is needed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Question, exact token, error string, flag, or topic to find.",
                },
                "sources": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional source filter such as domain_records or github.",
                },
                "kinds": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional row-kind filter such as doc_page, issue, or pr.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": KNOWLEDGE_SEARCH_MAX_RESULTS,
                    "default": KNOWLEDGE_SEARCH_DEFAULT_RESULTS,
                    "description": "Maximum fused results to return.",
                },
            },
            "required": ["query"],
        },
    }
]


__all__ = ["KNOWLEDGE_TOOLS"]
