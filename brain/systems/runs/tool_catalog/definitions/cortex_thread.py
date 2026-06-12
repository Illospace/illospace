"""Cortex thread and handoff tool schemas."""

from __future__ import annotations

from brain.systems.cortex.status import IDEA_STATUS_VALUES


# ── Cortex Idea Tools ─────────────────────────────────────────
# Durable Cortex thoughts/threads. In the DB/API these are called ideas.

CORTEX_IDEA_TOOLS = [
    {
        "name": "manage_idea",
        "description": (
            "Create, list, get, update, archive, restore, or mark-read Cortex thoughts. "
            "When a created thought should start working immediately, set start_run=true "
            "or use status=queued/working so a starter message and AgentRun are created. "
            "For teammate coordination or handoffs, use action=create with user_id set to the teammate owner. "
            "The first thread_message you provide is authored by Illo; user_id controls ownership/assignment, "
            "not message authorship. "
            "Use this for requests about thoughts, threads, idea threads, or ideas, such as "
            "'archive this thread', 'rename this thought', 'mark this resolved', or "
            "'restore that idea'. This is the action/exact-thread tool. For recent team-wide thread "
            "activity, prefer read_team_activity first. idea_id defaults to the current Cortex "
            "thread/idea when one is bound. For exact-thread actions, pass idea_id, thread_id, "
            "thread_url, or thread_route. Results include thread_url; share it when the user needs "
            "to open or hand off the Thread. Use action='help' or action='schema' with operation to inspect "
            "arguments before mutating."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "help",
                        "schema",
                        "list",
                        "get",
                        "create",
                        "update",
                        "archive",
                        "restore",
                        "set_status",
                        "mark_read",
                    ],
                    "description": "The thought/thread operation to run.",
                },
                "operation": {
                    "type": "string",
                    "description": "Optional operation name to inspect when action is help or schema.",
                },
                "idea_id": {
                    "type": "string",
                    "description": "Cortex idea/thread id. Defaults to the current thread when available.",
                },
                "thread_id": {
                    "type": "string",
                    "description": "Alias for idea_id, for user wording that says thread.",
                },
                "thread_url": {
                    "type": "string",
                    "description": "Canonical Illo Thread URL identifying the target thread.",
                },
                "thread_route": {
                    "type": "string",
                    "description": "Canonical /threads/{id} route identifying the target thread.",
                },
                "url": {
                    "type": "string",
                    "description": "Alias for thread_url when a Thread link is passed as a generic URL.",
                },
                "title": {"type": "string", "description": "Raw idea title for create/update."},
                "thread_message": {
                    "type": "string",
                    "description": "Optional first Illo-authored thread message for a newly created idea. Defaults to description, then title.",
                },
                "start_run": {
                    "type": "boolean",
                    "description": "For create, enqueue Illo on the new idea immediately. Defaults true when status is queued or working.",
                },
                "display_title": {"type": "string", "description": "Readable UI title for update."},
                "description": {"type": "string", "description": "Optional idea description."},
                "status": {
                    "type": "string",
                    "enum": list(IDEA_STATUS_VALUES),
                    "description": (
                        "Next status for create, update, or set_status. Use needs_input only when the "
                        "requested deliverable cannot be produced without user input; missing credentials "
                        "for deferred integrations are a follow-up limitation, not a reason to block an "
                        "otherwise buildable app."
                    ),
                },
                "salience_score": {"type": "number", "description": "Optional salience score."},
                "position_x": {"type": "number", "description": "Optional Cortex canvas x position."},
                "position_y": {"type": "number", "description": "Optional Cortex canvas y position."},
                "position_sticky": {"type": "boolean", "description": "Whether the canvas position is sticky."},
                "orbit_anchor_type": {
                    "type": "string",
                    "enum": ["user", "pin", "none"],
                    "description": "Optional orbit anchor type. Use none to clear the anchor.",
                },
                "orbit_anchor_id": {"type": "string", "description": "Optional orbit anchor target id."},
                "parent_id": {"type": "string", "description": "Optional parent idea id for create."},
                "user_id": {"type": "string", "description": "Optional owner/assignee id for explicit thread handoff. This does not author messages as that user."},
                "origin": {"type": "string", "description": "Origin for create.", "default": "illo_created"},
                "origin_ref": {"type": "string", "description": "Optional origin reference for create."},
                "search": {"type": "string", "description": "Optional title/description filter for list."},
                "include_archived": {"type": "boolean", "default": False},
                "limit": {"type": "integer", "description": "Max ideas for list (default 20).", "default": 20},
            },
            "required": ["action"],
        },
    },
]

# ── Native Chat Tools ─────────────────────────────────────────

CHAT_TOOLS = [
    {
        "name": "post_chat_message",
        "description": (
            "Post an Illo-authored message back to the native team room. "
            "Use this when a run was triggered from team chat and you need to "
            "answer in the originating room or thread. It cannot post to DMs."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "body": {
                    "type": "string",
                    "description": "Concise markdown message to post as Illo.",
                },
                "conversation_id": {
                    "type": "string",
                    "description": "Team room conversation id. Defaults to the triggering chat conversation.",
                },
                "thread_root_message_id": {
                    "type": "integer",
                    "description": "Optional room-thread root message id. Defaults to the triggering response target.",
                },
            },
            "required": ["body"],
        },
    },
    {
        "name": "publish_thread_asset",
        "description": (
            "Publish a generated local artifact, such as an SVG, PNG, PDF, or text file, "
            "as a previewable Thread asset under /static/uploads. Use this when a user asks "
            "to see or download an artifact that exists only as a local file path. The tool "
            "also accepts an existing /static/uploads/... URL returned by this tool and will "
            "return the same attachment object idempotently. It returns markdown and an "
            "attachment object. To show the asset in a Thread, write the returned markdown "
            "or /static/uploads/... route in post_thread_discussion_reply or "
            "post_ai_timeline_message; those tools persist visible attachments from valid "
            "upload routes automatically."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": (
                        "Absolute local path to a generated artifact under an allowed artifact root, "
                        "or an existing /static/uploads/... URL for an already-published asset."
                    ),
                },
                "thread_id": {
                    "type": "string",
                    "description": "Optional Thread id. Defaults to the triggering/current Thread.",
                },
                "title": {
                    "type": "string",
                    "description": "Optional human-readable label or alt text for the asset.",
                },
            },
            "required": ["file_path"],
        },
    },
    {
        "name": "post_thread_discussion_reply",
        "description": (
            "Post an Illo-authored reply into the current Thread Discussion. "
            "Use this only when the run was summoned from Thread Discussion. "
            "This does not post to the AI Timeline, and non-Discussion-origin "
            "runs must use post_ai_timeline_message for visible Thread output. "
            "Discussion is a team comment surface, not the default response channel."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "body": {
                    "type": "string",
                    "description": "Concise markdown message to post in Thread Discussion as Illo.",
                },
                "thread_id": {
                    "type": "string",
                    "description": "Optional Thread id. Defaults to the triggering/current Thread.",
                },
                "reply_to_comment_id": {
                    "type": "integer",
                    "description": "Optional Discussion comment id being acknowledged. Defaults to the triggering comment.",
                },
                "attachments": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": (
                        "Optional visible attachments. Valid /static/uploads/... links in body are "
                        "also promoted to visible attachments automatically."
                    ),
                },
            },
            "required": ["body"],
        },
    },
    {
        "name": "post_slack_reply",
        "description": (
            "Post an Illo-authored reply into Slack. Use this when a run was "
            "triggered by a Slack mention or DM and the visible answer belongs "
            "back in Slack. Defaults to the originating Slack channel, existing "
            "thread, or DM; top-level mentions and DMs are not threaded."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "body": {
                    "type": "string",
                    "description": "Concise Slack markdown message to post as Illo. When image_data is provided, this becomes the image's initial comment.",
                },
                "channel_id": {
                    "type": "string",
                    "description": "Optional Slack channel or DM id. Defaults to the triggering surface.",
                },
                "thread_ts": {
                    "type": "string",
                    "description": "Optional Slack thread timestamp. Defaults to the triggering response target; omit for top-level channel replies and DMs.",
                },
                "visibility": {
                    "type": "string",
                    "enum": ["public", "ephemeral"],
                    "description": "Whether to post publicly or ephemerally. Defaults to public.",
                    "default": "public",
                },
                "user_id": {
                    "type": "string",
                    "description": "Slack user id required for ephemeral replies outside a Slack-triggered run.",
                },
                "image_data": {
                    "type": "string",
                    "description": (
                        "Optional base64 data:image URL to upload and share as a Slack image file, "
                        "for generated graphs or charts. Prefer data:image/png;base64 for reliable Slack previews."
                    ),
                },
                "image_filename": {
                    "type": "string",
                    "description": "Optional filename for image_data, e.g. graph.png.",
                },
                "image_title": {
                    "type": "string",
                    "description": "Optional Slack file title for image_data.",
                },
                "image_alt": {
                    "type": "string",
                    "description": "Optional alt text for image_data, used by Slack for screen readers.",
                },
            },
        },
    },
    {
        "name": "post_ai_timeline_message",
        "description": (
            "Post an Illo-authored message into the linked Thread AI Timeline. "
            "Use this only when the user explicitly asks you to carry something into "
            "the AI Timeline, or when the work product naturally belongs there. This "
            "does not reply in Discussion. Discussion and AI Timeline are separate "
            "conversation surfaces linked by Thread context, so acknowledge in "
            "Discussion separately when the user summoned you there."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "body": {
                    "type": "string",
                    "description": "Concise markdown message to post in the Thread AI Timeline as Illo.",
                },
                "thread_id": {
                    "type": "string",
                    "description": "Optional Thread id. Defaults to the linked/current Thread.",
                },
                "attachments": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": (
                        "Optional visible attachments. Valid /static/uploads/... links in body are "
                        "also promoted to visible attachments automatically."
                    ),
                },
            },
            "required": ["body"],
        },
    },
    {
        "name": "read_thread_discussion",
        "description": (
            "Read the Discussion comments attached to the current Thread. "
            "Use this only when team comments may contain relevant context, or when the user asks "
            "about the Discussion. Discussion is not automatically included in every run prompt."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "thread_id": {
                    "type": "string",
                    "description": "Optional Thread id. Defaults to the current Thread for this run.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum comments to return, from newest back then ordered chronologically.",
                    "default": 50,
                },
            },
        },
    },
    {
        "name": "read_slack_conversation",
        "description": (
            "Read bounded Slack context for the current Slack-triggered run. "
            "Use this intentionally when the triggering message is not enough. "
            "Slack channel history is not automatically included in every prompt."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "enum": ["triggering_message", "thread", "recent_channel"],
                    "description": "Which Slack context to read. Defaults to the triggering thread.",
                    "default": "thread",
                },
                "channel_id": {
                    "type": "string",
                    "description": "Optional Slack channel or DM id. Defaults to the triggering surface.",
                },
                "thread_ts": {
                    "type": "string",
                    "description": "Optional Slack thread timestamp. Defaults to the triggering thread.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum Slack messages to return.",
                    "default": 50,
                },
            },
        },
    },
    {
        "name": "manage_slack",
        "description": (
            "Inspect Slack connection health, list Slack conversations visible to Illo's bot or observed from "
            "Slack-origin mentions, "
            "and manage Slack-to-Illospace identity mappings. Use action='status' to check "
            "whether Slack is connected, action='list_channels' to see channels/DMs the "
            "configured bot token can enumerate plus Slack surfaces Illo has already seen, "
            "and identity mapping actions to link Slack users "
            "to Illospace users."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "status",
                        "list_channels",
                        "list_mappings",
                        "link_identity",
                        "unlink_identity",
                    ],
                    "description": "Slack management action.",
                },
                "connection_id": {
                    "type": "string",
                    "description": "Slack source connection id. Optional when only one Slack connection exists.",
                },
                "slack_user_id": {
                    "type": "string",
                    "description": "Slack user id, required for link_identity and unlink_identity.",
                },
                "user_id": {
                    "type": "string",
                    "description": "Illospace user id, required for link_identity.",
                },
                "channel_types": {
                    "oneOf": [
                        {"type": "string"},
                        {"type": "array", "items": {"type": "string"}},
                    ],
                    "description": "Conversation types for list_channels: public_channel, private_channel, mpim, im. Defaults to all supported types.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum Slack conversations to return for list_channels, up to Slack's page limit.",
                    "default": 200,
                },
                "cursor": {
                    "type": "string",
                    "description": "Slack pagination cursor for list_channels.",
                },
                "include_archived": {
                    "type": "boolean",
                    "description": "Include archived Slack conversations in list_channels results.",
                    "default": False,
                },
            },
            "required": ["action"],
        },
    }
]

# ── Launch Handoff Tools ─────────────────────────────────────
# Surface-agnostic handoffs that bridge Illo coordination into local coding agents.

LAUNCH_HANDOFF_TOOLS = [
    {
        "name": "create_launch_handoff",
        "description": (
            "Prepare a durable launch handoff link for opening a task in a teammate's local coding agent. "
            "Use this when a user asks to open, launch, send, or hand off coding work to Codex. "
            "The returned HTTPS launch_url is surface-agnostic and can be posted in Slack, chat, or Thread Discussion."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Short task title for the handoff card and Codex starter prompt.",
                },
                "instructions": {
                    "type": "string",
                    "description": "What the coding agent should do after fetching full context from Illo.",
                },
                "summary": {
                    "type": "string",
                    "description": "Optional compact preview summary for Slack/web cards.",
                },
                "target_tool": {
                    "type": "string",
                    "enum": ["codex"],
                    "description": "Local agent surface to launch. Currently codex.",
                    "default": "codex",
                },
                "repo_origin_url": {
                    "type": "string",
                    "description": "Git remote/origin URL so any teammate's Codex can match their local project.",
                },
                "branch_hint": {
                    "type": "string",
                    "description": "Optional branch or worktree hint for the person picking up the task.",
                },
                "source_surface": {
                    "type": "string",
                    "description": "Where this request came from, such as slack, webapp, thread, or chat.",
                    "default": "illo_run",
                },
                "source_ref": {
                    "type": "object",
                    "description": "Surface-specific provenance, for example Slack channel/thread/permalink or Thread ids.",
                    "default": {},
                },
                "context_parts": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Ordered context parts Codex can fetch later through Illo MCP.",
                    "default": [],
                },
                "acceptance_criteria": {
                    "type": "array",
                    "items": {},
                    "description": "Success criteria for the coding task.",
                    "default": [],
                },
                "idempotency_key": {
                    "type": "string",
                    "description": "Optional stable key to avoid duplicate handoffs for one source event.",
                },
                "metadata": {
                    "type": "object",
                    "description": "Optional machine-readable metadata.",
                    "default": {},
                },
            },
            "required": ["title", "instructions"],
        },
    },
]


__all__ = [
    "CHAT_TOOLS",
    "CORTEX_IDEA_TOOLS",
    "LAUNCH_HANDOFF_TOOLS",
]
