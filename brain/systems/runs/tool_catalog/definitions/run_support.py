"""Run support, reply, and lifecycle tool schemas."""

from __future__ import annotations

from brain.systems.runs.direct_loop.reply_coordination import (
    REPLY_COORDINATION_INPUT_SCHEMA,
)


# ── Soul Tools ────────────────────────────────────────────────
# Controlled access to private operator/personality context.

SOUL_TOOLS = [
    {
        "name": "manage_soul",
        "description": (
            "Read or update Illo's private SOUL.md file: default voice, personality, identity, "
            "tone, and collaboration posture. Use only when the user explicitly asks to view, "
            "change, refine, reset, or otherwise customize Illo's soul/personality/voice. "
            "Read first before replacing so edits preserve the parts the user did not ask to "
            "change. Project rules and technical conventions belong elsewhere."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["read", "replace", "reset"],
                    "description": (
                        "read returns the current effective soul; replace writes content; "
                        "reset restores the built-in default."
                    ),
                },
                "content": {
                    "type": "string",
                    "description": "Full replacement SOUL.md content. Required for replace.",
                },
                "reason": {
                    "type": "string",
                    "description": "Short reason for the personality change, preferably reflecting the user's request.",
                },
            },
            "required": ["action"],
        },
    },
]

# Cortex reply tool — lets coordinators stage final messages for run settlement
CORTEX_REPLY_TOOL = {
    "name": "cortex_reply",
    "description": (
        "Stage the final Cortex reply for run settlement. During runs, this does "
        "not publish immediately; the lifecycle verifies the staged answer and posts it once. "
        "Do NOT call this again to rephrase or improve a previous staged reply unless you have "
        "done genuinely new work (wrote code, ran commands, spawned workers) that changes the result."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": (
                    "Readable Markdown message to stage for the idea thread. "
                    "Write polished prose for the user: short paragraphs, compact bullets when useful, "
                    "no raw evidence dumps, no one-metric-per-line formatting, and no punctuation on its own line."
                ),
            },
            "coordination": REPLY_COORDINATION_INPUT_SCHEMA,
        },
        "required": ["content"],
    },
}

CORTEX_VISUAL_REPLY_TOOL = {
    "name": "cortex_visual_reply",
    "description": (
        "Render compact static visual content in the Cortex workspace. Use for diffs, "
        "charts, diagrams, images, markdown summaries, and screenshots. For interactive or "
        "recordful generated UI, create or update a workspace app instead."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "content_type": {
                "type": "string",
                "enum": ["diff", "chart", "diagram", "image", "markdown", "screenshot"],
                "description": "Type of visual content",
            },
            "title": {
                "type": "string",
                "description": "Title shown above the visual block",
            },
            "content": {
                "type": "string",
                "description": (
                    "The visual content. For diff: unified diff string. "
                    "For chart: JSON {type: 'bar'|'line'|'pie'|'scatter', data: [{label, value}], title?, xlabel?, ylabel?}. "
                    "For diagram: SVG or Mermaid syntax. For image: HTTP URL, data:image URL, or inert SVG markup. For markdown: markdown string. "
                    "For screenshot: an image URL or data:image URL."
                ),
            },
            "display": {
                "type": "string",
                "enum": ["inline", "canvas"],
                "description": "inline = in conversation stream. canvas = dedicated visual area (triggers split mode).",
            },
        },
        "required": ["content_type", "title", "content"],
    },
}

# Self-introspection tool — lets agents see their own activity
MY_ACTIVITY_TOOL = {
    "name": "my_activity",
    "description": (
        "See your own activity in this run: tool calls made, replies staged, "
        "recorded execution artifacts such as branches/commits/pushes/PRs, tokens spent, "
        "and time elapsed. Use this to assess whether you are making forward progress, "
        "what you actually changed in the current run, or whether you are repeating yourself. "
        "Call this before posting another cortex_reply to check if you already said something similar."
    ),
    "input_schema": {
        "type": "object",
        "properties": {},
    },
}

# ── Session Scratchpad Tools ──────────────────────────────────
# Shared state between AgentRun workers within a single run

SESSION_TOOLS = [
    {
        "name": "session_write",
        "description": (
            "Write a structured entry to the session scratchpad. Use this to share "
            "discoveries, decisions, or context with other workers in this AgentRun. "
            "Sections: 'findings', 'decisions', 'open_questions', 'resources', 'handoffs'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "section": {
                    "type": "string",
                    "enum": ["findings", "decisions", "open_questions", "resources", "handoffs"],
                    "description": "Which section to write to",
                },
                "value": {"type": "string", "description": "The content to write"},
                "key": {
                    "type": "string",
                    "description": "Optional named key for direct lookup later",
                },
            },
            "required": ["section", "value"],
        },
    },
    {
        "name": "session_read",
        "description": (
            "Read entries from the session scratchpad. Returns entries from this "
            "AgentRun, optionally filtered by section and/or key."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "section": {
                    "type": "string",
                    "description": "Filter by section (optional)",
                },
                "key": {
                    "type": "string",
                    "description": "Filter by key (optional)",
                },
            },
        },
    },
    {
        "name": "session_append",
        "description": (
            "Shorthand for session_write without a key. Appends an entry to a section."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "section": {
                    "type": "string",
                    "enum": ["findings", "decisions", "open_questions", "resources", "handoffs"],
                    "description": "Which section to append to",
                },
                "value": {"type": "string", "description": "The content to append"},
            },
            "required": ["section", "value"],
        },
    },
    {
        "name": "session_list",
        "description": (
            "List all sections (or entries in a section) from the session scratchpad."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "section": {
                    "type": "string",
                    "description": "If provided, list entries in this section. Otherwise list all section names.",
                },
            },
        },
    },
]

# ── Memory Lifecycle Tools ───────────────────────────────────
# Coordinator-only tools for promoting session knowledge to long-term brain

LIFECYCLE_TOOLS = [
    {
        "name": "session_promote",
        "description": (
            "Gather all scratchpad entries for this AgentRun, formatted for review. "
            "Returns entries organized by section. Review each entry and brain_encode "
            "any that reveal patterns or lessons worth keeping long-term. "
            "Session-specific noise should be skipped (it auto-expires after 24h)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "session_close",
        "description": (
            "Mark the session scratchpad as closed. Call this when the AgentRun completes "
            "(success or failure). Entries are kept for 24h for debugging, then auto-expire."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
]


__all__ = [
    "CORTEX_REPLY_TOOL",
    "CORTEX_VISUAL_REPLY_TOOL",
    "LIFECYCLE_TOOLS",
    "MY_ACTIVITY_TOOL",
    "SESSION_TOOLS",
    "SOUL_TOOLS",
]
