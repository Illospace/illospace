"""Knowledge index tool definitions."""

from __future__ import annotations


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
                    "maximum": 50,
                    "default": 10,
                    "description": "Maximum fused results to return.",
                },
            },
            "required": ["query"],
        },
    }
]


__all__ = ["KNOWLEDGE_TOOLS"]
