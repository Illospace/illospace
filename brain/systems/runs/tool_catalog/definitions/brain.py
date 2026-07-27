"""Brain memory and workspace-read tool schemas."""

from __future__ import annotations


_WORKSPACE_TIME_WINDOW_VALUES = [
    "all",
    "today",
    "yesterday",
    "last_24h",
    "this_week",
    "last_7d",
    "this_month",
    "last_30d",
    "custom",
]


_WORKSPACE_TIME_WINDOW_SCHEMA = {
    "type": "string",
    "enum": _WORKSPACE_TIME_WINDOW_VALUES,
    "default": "last_7d",
    "description": "Relative time window. Use custom with start_at/end_at.",
}

_WORKSPACE_ALL_TIME_WINDOW_SCHEMA = {
    **_WORKSPACE_TIME_WINDOW_SCHEMA,
    "default": "all",
    "description": "Relative time window. Use all for current/existing workspace state, or custom with start_at/end_at.",
}

WORKSPACE_OVERVIEW_SPARSE_GUIDANCE = (
    "If the overview is empty or sparse, ask the user for a few team, project, "
    "and workflow details so Illo can fill in workspace context and help better."
)


# ── Brain Tools ───────────────────────────────────────────────
# Available to all agents (coordinator + workers)

BRAIN_TOOLS = [
    {
        "name": "brain_recall",
        "description": (
            "Search Illo's long-term semantic memories: durable lessons, facts, patterns, and episodes. "
            "Use this for remembered context. For current workspace truth, team activity, Domains, projects, "
            "apps, Cycles, or thread records, use the read_workspace_* tools instead."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to search for in brain memories"},
                "limit": {"type": "integer", "description": "Max results (default 3)", "default": 3},
            },
            "required": ["query"],
        },
    },
    {
        "name": "memory_reconstruct",
        "description": (
            "Reconstruct source-backed evidence from Illo's new memory graph. Prefer this over brain_recall "
            "for decisions, facts, multi-hop history, stale/conflicted context, and any answer that needs "
            "traceable source evidence."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Question or context need to reconstruct evidence for"},
                "limit": {"type": "integer", "description": "Maximum supporting evidence items", "default": 5},
            },
            "required": ["query"],
        },
    },
    {
        "name": "memory_link",
        "description": "Create a deliberate, reason-backed relationship between two visible memory nodes.",
        "input_schema": {
            "type": "object",
            "properties": {
                "source_node": {"type": "integer", "minimum": 1},
                "target_node": {"type": "integer", "minimum": 1},
                "relationship": {"type": "string", "minLength": 1, "maxLength": 60},
                "reason": {"type": "string", "minLength": 3},
            },
            "required": ["source_node", "target_node", "relationship", "reason"],
        },
    },
    {
        "name": "memory_supersede",
        "description": "Replace a stale visible memory with new content or an existing visible node.",
        "input_schema": {
            "type": "object",
            "properties": {
                "old_node": {"type": "integer", "minimum": 1},
                "new_content": {"type": "string", "minLength": 20},
                "new_node": {"type": "integer", "minimum": 1},
                "reason": {"type": "string", "minLength": 3},
            },
            "required": ["old_node", "reason"],
            "oneOf": [
                {"required": ["new_content"], "not": {"required": ["new_node"]}},
                {"required": ["new_node"], "not": {"required": ["new_content"]}},
            ],
        },
    },
    {
        "name": "memory_archive",
        "description": "Archive obsolete or redundant visible memory nodes with a durable reason.",
        "input_schema": {
            "type": "object",
            "properties": {
                "node_ids": {
                    "type": "array",
                    "items": {"type": "integer", "minimum": 1},
                    "minItems": 1,
                    "maxItems": 100,
                    "uniqueItems": True,
                },
                "reason": {"type": "string", "minLength": 3},
            },
            "required": ["node_ids", "reason"],
        },
    },
    {
        "name": "brain_guardrails",
        "description": "Get guardrails: recent skill failures, high-salience warnings, and pitfalls for a specific skill.",
        "input_schema": {
            "type": "object",
            "properties": {
                "skill": {"type": "string", "description": "Skill name to get specific guardrails for"},
            },
        },
    },
    {
        "name": "brain_skills",
        "description": (
            "Plan a task: recommend skill catalog cards, guardrails, and execution "
            "strategy based on past experience. Use skill_view to load cards, summaries, or full procedures."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "Task description to plan for"},
            },
            "required": ["task"],
        },
    },
    {
        "name": "skill_view",
        "description": (
            "Load one section of an installed skill on demand. Use card for just "
            "name and description, summary for compact context, or procedure for full guidance."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Installed skill name"},
                "section": {
                    "type": "string",
                    "enum": [
                        "card",
                        "summary",
                        "procedure",
                        "pitfalls",
                        "triggers",
                        "guardrails",
                        "graduated_steps",
                        "metadata",
                    ],
                    "description": "Skill section to load",
                    "default": "procedure",
                },
                "max_chars": {
                    "type": "integer",
                    "description": "Maximum text chars to return",
                    "default": 12000,
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "manage_skill",
        "description": (
            "Create, update, archive, bundle, or edit assets for installed Illo skills. "
            "This is the action tool for durable slash-routable skill changes. Use brain_skills "
            "and skill_view first when discovering existing skills. Use action='help' or "
            "action='schema' with operation to inspect arguments before mutating."
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
                        "create_many",
                        "update",
                        "edit",
                        "archive",
                        "delete",
                        "convert_to_bundle",
                        "list_assets",
                        "get_asset",
                        "upsert_asset",
                        "delete_asset",
                    ],
                    "description": "What to do. Use help/schema to inspect skill operations before mutating.",
                },
                "operation": {
                    "type": "string",
                    "description": "Optional operation name to inspect when action is help or schema.",
                },
                "skill_id": {"type": "integer", "description": "Skill id for existing-skill actions."},
                "skill_name": {
                    "type": "string",
                    "description": "Existing skill name for lookup when skill_id is not known.",
                },
                "name": {"type": "string", "description": "Skill name for create, or replacement name for update."},
                "description": {"type": "string", "description": "Skill description."},
                "procedure": {"type": "string", "description": "Skill procedure/instructions."},
                "thinking_tier": {
                    "type": "string",
                    "enum": ["none", "low", "medium", "high", "xhigh"],
                    "default": "medium",
                },
                "triggers": {"type": "array", "items": {"type": "object"}, "description": "Routing triggers."},
                "guardrails": {"type": "array", "items": {"type": "object"}, "description": "Skill guardrails."},
                "pitfalls": {"type": "array", "items": {}, "description": "Known failure modes or cautions."},
                "refinements": {"type": "array", "items": {}, "description": "Improvement notes."},
                "assets": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Initial package assets for create. Each item needs path and content.",
                },
                "skills": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "description": {"type": "string"},
                            "procedure": {"type": "string"},
                            "thinking_tier": {
                                "type": "string",
                                "enum": ["none", "low", "medium", "high", "xhigh"],
                            },
                            "triggers": {"type": "array", "items": {"type": "object"}},
                            "guardrails": {"type": "array", "items": {"type": "object"}},
                            "pitfalls": {"type": "array", "items": {}},
                            "refinements": {"type": "array", "items": {}},
                            "assets": {"type": "array", "items": {"type": "object"}},
                            "create_as_package": {"type": "boolean"},
                            "user_requested": {"type": "boolean"},
                        },
                        "required": ["name", "procedure"],
                    },
                    "description": (
                        "Skill specs for action='create_many'. Per-skill fields mirror create; "
                        "top-level thinking_tier, create_as_package, and user_requested act as defaults."
                    ),
                },
                "create_as_package": {
                    "type": "boolean",
                    "default": False,
                    "description": "Create or convert the skill as a portable local bundle package.",
                },
                "user_requested": {
                    "type": "boolean",
                    "default": True,
                    "description": "Whether the user explicitly requested this durable skill change.",
                },
                "path": {"type": "string", "description": "Asset path for get/upsert/delete asset actions."},
                "content": {"type": "string", "description": "Text content for upsert_asset."},
                "asset_kind": {"type": "string", "description": "Optional asset kind override."},
                "mime_type": {"type": "string", "description": "Optional MIME type override."},
                "loading_budget_tokens": {"type": "integer", "description": "Optional loading budget for the asset."},
                "limit": {"type": "integer", "default": 50, "description": "Maximum skills or assets to return."},
                "max_chars": {"type": "integer", "default": 12000, "description": "Maximum asset content chars to return."},
                "include_archived": {"type": "boolean", "default": False},
            },
            "required": ["action"],
        },
    },
    {
        "name": "skill_asset",
        "description": "Load a specific versioned skill bundle asset by relative path.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Installed skill name"},
                "path": {
                    "type": "string",
                    "description": "Relative bundle asset path, e.g. examples/happy.md",
                },
                "max_chars": {
                    "type": "integer",
                    "description": "Maximum text chars to return",
                    "default": 12000,
                },
            },
            "required": ["name", "path"],
        },
    },
    {
        "name": "brain_encode",
        "description": (
            "Compatibility alias for source-backed reconstructive memory ingestion. "
            "Prefer memory_ingest_source for new calls."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "Memory content (min 20 chars)"},
                "type": {"type": "string", "enum": ["lesson", "pattern", "fact", "episode"], "default": "episode"},
                "salience": {"type": "number", "description": "Ignored compatibility field", "default": 5.0},
            },
            "required": ["content"],
        },
    },
    {
        "name": "memory_ingest_source",
        "description": "Ingest source-backed reconstructive memory and create cue/tag/content graph nodes.",
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "Source content to ingest"},
                "content_kind": {"type": "string", "description": "Derived content kind", "default": "episode"},
                "source_kind": {"type": "string", "description": "Immutable source kind", "default": "agent_run"},
                "source_ref": {"type": "string", "description": "Optional source reference"},
                "source_url": {"type": "string", "description": "Optional source URL"},
                "visibility": {
                    "type": "string",
                    "enum": ["private", "team", "org"],
                    "default": "private",
                },
                "confidence": {"type": "number", "description": "Extraction confidence", "default": 0.5},
            },
            "required": ["content"],
        },
    },
    {
        "name": "vault_inventory",
        "description": (
            "List metadata-only Vault secrets for credential reasoning. Returns key names, "
            "descriptions, categories, and agent_access_level, never secret values. For command/API "
            "work, use a returned exact key with exec_command or run_script secret_env. Call "
            "brain_vault only to check/request access to a reference; call vault_secret_prompt when "
            "no suitable existing secret exists."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "enum": [
                        "general",
                        "api",
                        "aws",
                        "auth",
                        "analytics",
                        "database",
                        "messaging",
                        "monitoring",
                        "payments",
                        "service",
                    ],
                    "description": "Optional Vault category filter.",
                },
                "access_level": {
                    "type": "string",
                    "enum": ["available", "ask", "manual"],
                    "description": "Optional agent access level filter.",
                },
            },
        },
    },
    {
        "name": "brain_vault",
        "description": (
            "Request task-scoped access to a Vault secret reference. Raw secret values are not returned "
            "to agents. For command/API work, mount the Vault key with exec_command or run_script secret_env."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Secret key name"},
                "reason": {
                    "type": "string",
                    "description": "Specific reason this active task needs this exact secret.",
                },
            },
            "required": ["key", "reason"],
        },
    },
    {
        "name": "vault_secret_prompt",
        "description": (
            "Open a guided Vault form in the current Cortex thread for a user-supplied secret. "
            "Call vault_inventory first, then use this only when no suitable existing secret exists "
            "or the user explicitly asked to add a new key. "
            "Use before asking the user to paste an API key in chat, when a task needs a missing credential "
            "or a newly created skill/API integration needs a named key. Do not use this before producing "
            "the main requested deliverable when the credential is only needed for a deferred connector or "
            "future sync; build the app or artifact first, declare the deferred action, and mention setup as "
            "a follow-up. This tool never reads or stores the secret value itself; the user enters the value "
            "into Vault UI."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "key_name": {
                    "type": "string",
                    "description": "Secret key name to prefill, e.g. EXAMPLE_API_KEY",
                },
                "description": {
                    "type": "string",
                    "description": "Optional Vault description to prefill.",
                },
                "category": {
                    "type": "string",
                    "enum": [
                        "general",
                        "api",
                        "aws",
                        "auth",
                        "analytics",
                        "database",
                        "messaging",
                        "monitoring",
                        "payments",
                        "service",
                    ],
                    "description": "Vault category to prefill.",
                    "default": "api",
                },
                "reason": {
                    "type": "string",
                    "description": "Specific reason this active task needs the user to add this secret.",
                },
            },
            "required": ["key_name"],
        },
    },
    {
        "name": "runtime_settings",
        "description": (
            "Inspect the current runtime provider, auth status, and provider model mappings "
            "for the active user/workspace. Use for questions about default provider, "
            "credential precedence, or which provider/model the system will use."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "provider": {
                    "type": "string",
                    "enum": ["anthropic", "openai"],
                    "description": "Optional provider to focus on; defaults to the effective provider.",
                },
            },
        },
    },
    {
        "name": "read_self_context",
        "description": (
            "Read verified identity, source, and runtime self-context for Illo. Use this for "
            "questions about who Illo is, what Illospace is, where this open-source install/source "
            "can be inspected, current git/source facts, or whether code/file inspection tools are "
            "available. This is not the capability index; use read_capabilities for what Illo can do."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "include_paths": {
                    "type": "boolean",
                    "default": True,
                    "description": "Include verified local source-root and documentation path facts.",
                },
                "include_git": {
                    "type": "boolean",
                    "default": True,
                    "description": "Include current git branch, commit, and remote facts when available.",
                },
            },
        },
    },
    {
        "name": "read_capabilities",
        "description": (
            "Read machine-readable capability manifests for Illo's runtime and installed/custom capabilities. "
            "Use this before answering setup, connect, install, integration, connector, plugin, tool, "
            "or 'can you do X?' questions. Capability manifests and tool schemas are the source of truth "
            "for setup modes, status checks, credential stores, and agent actions. Pass the user's natural "
            "language request as query; do not invent capability keys or categories. Auto expands single matches."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The user's natural-language capability/setup question, such as 'help me set up Slack'.",
                },
                "include_setup_guide": {
                    "type": "boolean",
                    "default": False,
                    "description": "When true, include a registered setup guide for a single matched capability.",
                },
                "detail_level": {
                    "type": "string",
                    "enum": ["auto", "summary", "tools", "full"],
                    "default": "auto",
                    "description": "Use summary for compact capability lists, tools for tool names, or full for exact setup/tool metadata. Auto expands only narrow matches.",
                },
            },
        },
    },
    {
        "name": "transcribe_audio_attachment",
        "description": (
            "Transcribe an audio attachment from the current thread or an uploaded file using "
            "the Voice Runtime's selected transcription provider. Use this for Slack voice notes, "
            "recorded messages, or audio files when the spoken content is needed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "attachment_id": {
                    "type": "string",
                    "description": (
                        "Attachment id from the current thread attachment context. "
                        "Optional if there is exactly one audio attachment."
                    ),
                },
                "attachment_url": {
                    "type": "string",
                    "description": "Uploaded attachment URL, such as /static/uploads/voice.webm.",
                },
                "path": {
                    "type": "string",
                    "description": "Backend storage path for an uploaded audio attachment.",
                },
                "language": {
                    "type": "string",
                    "enum": ["auto", "en", "fr"],
                    "default": "auto",
                    "description": "Optional language hint. Use auto for bilingual English/French audio.",
                },
            },
        },
    },
    {
        "name": "read_thread_messages",
        "description": (
            "Read or search raw stored messages from this agent run's persistent LLM session when "
            "the durable handoff summary is too thin or an older exact detail matters. This is not "
            "the user's Cortex workspace thread; use read_team_activity or query_workspace_data for "
            "Cortex idea/thread history. Defaults to the current session; use recent for latest turns, "
            "range for indexes, or search for literal text."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["recent", "range", "search"],
                    "default": "recent",
                    "description": "How to select messages.",
                },
                "start_index": {"type": "integer", "description": "Inclusive start index for range mode."},
                "end_index": {"type": "integer", "description": "Exclusive end index for range mode."},
                "query": {"type": "string", "description": "Literal text query for search mode."},
                "limit": {"type": "integer", "description": "Maximum messages to return (default 20).", "default": 20},
                "max_chars": {
                    "type": "integer",
                    "description": "Maximum characters per returned message (default 8000).",
                    "default": 8000,
                },
            },
        },
    },
    {
        "name": "query_workspace_data",
        "description": (
            "Low-level read-only query over Illospace DB-backed workspace truth: team members, "
            "Cortex ideas/thread messages, agent runs, tool calls, Project Context profiles, "
            "Domains and records, workspace apps/state, and Cycles. Prefer the more specific "
            "read_workspace_* tools for normal answers; use this when you need exact source "
            "selection or a cross-source query. This does not search semantic memories; use brain_recall for that."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sources": {
                    "type": "array",
                    "description": (
                        "DB-backed data sources to inspect. Use ['activity'] for recent teammate/workspace "
                        "activity, ['project_contexts'] for project profiles/attachments, ['records'] for "
                        "Domains and records, ['apps'] for workspace apps/state, or ['all'] for the default broad set."
                    ),
                    "items": {
                        "type": "string",
                        "enum": [
                            "all",
                            "activity",
                            "team",
                            "people",
                            "team_members",
                            "runs",
                            "threads",
                            "ideas",
                            "tool_calls",
                            "projects",
                            "project_contexts",
                            "project_profiles",
                            "project_attachments",
                            "records",
                            "domain",
                            "domains",
                            "domain_records",
                            "domain_events",
                            "apps",
                            "workspace_apps",
                            "app_state",
                            "cycles",
                            "cycle_runs",
                        ],
                    },
                },
                "query": {
                    "type": "string",
                    "description": "Natural-language reason for the lookup; used for provenance and memory recall.",
                },
                "search": {
                    "type": "string",
                    "description": "Optional literal text filter for titles, messages, records, tool names, or app names.",
                },
                "person": {
                    "type": "string",
                    "description": "Optional teammate/user name or email to scope activity to.",
                },
                "time_window": {
                    **_WORKSPACE_TIME_WINDOW_SCHEMA,
                },
                "start_at": {"type": "string", "description": "ISO timestamp for custom lower bound."},
                "end_at": {"type": "string", "description": "ISO timestamp for custom upper bound."},
                "limit": {"type": "integer", "description": "Max records per source (default 20)", "default": 20},
                "idea_id": {"type": "string", "description": "Optional Cortex idea/thread id filter."},
                "thread_url": {"type": "string", "description": "Optional canonical Illo Thread URL or /threads/{id} route filter."},
                "domain_id": {"type": "integer", "description": "Optional Domain id filter."},
                "cycle_id": {"type": "integer", "description": "Optional Cycle id filter."},
                "object_key": {"type": "string", "description": "Optional Domain object key filter."},
                "include_archived": {"type": "boolean", "default": False},
                "cursor": {
                    "type": "string",
                    "description": "Opaque next_page token returned by a previous call.",
                },
            },
        },
    },
    {
        "name": "read_workspace_overview",
        "description": (
            "Read a curated overview of the current Illospace workspace before introducing Illo, "
            "answering broad workspace setup questions, or explaining what context is available. "
            "Returns team members, active/recent Cortex thoughts, recent agent runs/messages, Project Context profiles and attachments, "
            "Domains/records, workspace apps, Cycles, and setup gaps. Use this first for 'what is this workspace?', "
            "'what can you see?', and onboarding setup guidance. "
            f"{WORKSPACE_OVERVIEW_SPARSE_GUIDANCE}"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Reason for the overview lookup."},
                "time_window": {**_WORKSPACE_ALL_TIME_WINDOW_SCHEMA},
                "limit": {"type": "integer", "description": "Max records per source (default 10)", "default": 10},
                "include_archived": {"type": "boolean", "default": False},
            },
        },
    },
    {
        "name": "read_team_activity",
        "description": (
            "Read recent human and Illo activity in this workspace: Cortex thread messages, ideas, agent runs, "
            "tool-call summaries, Domain events, Project Context attachments, workspace app updates, and Cycle runs. "
            "Use before answering questions like 'what happened?', 'what is the team working on?', "
            "'what did Illo do?', 'did someone work on this?', or 'what changed recently?'. "
            "Thread and idea results include thread_url/thread_reference; cite the Thread title plus thread_url "
            "when pointing a teammate to prior work."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Reason for the activity lookup."},
                "search": {
                    "type": "string",
                    "description": "Optional literal text filter for titles, messages, records, tool names, apps, or cycles.",
                },
                "person": {"type": "string", "description": "Optional teammate/user name or email to scope activity to."},
                "time_window": {**_WORKSPACE_TIME_WINDOW_SCHEMA},
                "start_at": {"type": "string", "description": "ISO timestamp for custom lower bound."},
                "end_at": {"type": "string", "description": "ISO timestamp for custom upper bound."},
                "limit": {"type": "integer", "description": "Max records per source (default 20)", "default": 20},
                "idea_id": {"type": "string", "description": "Optional Cortex idea/thread id filter."},
                "thread_url": {"type": "string", "description": "Optional canonical Illo Thread URL or /threads/{id} route filter."},
                "cursor": {
                    "type": "string",
                    "description": "Opaque next_page token returned by a previous call.",
                },
            },
        },
    },
    {
        "name": "read_project_contexts",
        "description": (
            "Read reusable Project Context profiles and thread attachments: project names, resources, repos/files/docs, "
            "validation status, permission scope, and which Cortex thoughts have attached context. Use for questions "
            "about what projects Illo knows, which code/docs are connected, or how to improve project context."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Reason for the project context lookup."},
                "search": {"type": "string", "description": "Optional text filter for profile names, slugs, descriptions, or idea titles."},
                "idea_id": {"type": "string", "description": "Optional Cortex idea/thread id filter for attachments."},
                "time_window": {**_WORKSPACE_ALL_TIME_WINDOW_SCHEMA},
                "start_at": {"type": "string", "description": "ISO timestamp for custom lower bound."},
                "end_at": {"type": "string", "description": "ISO timestamp for custom upper bound."},
                "limit": {"type": "integer", "description": "Max records per source (default 20)", "default": 20},
                "include_inactive": {"type": "boolean", "default": False},
            },
        },
    },
    {
        "name": "read_team_members",
        "description": (
            "Read the workspace roster and, by default, nearby activity for those people. Use for questions about "
            "who is in the workspace, roles, ownership, or what a named teammate appears to be working on. "
            "Use when teammate coordination requires exact member ids, names, roles, or ownership. "
            "This is read-only and should be used before answering teammate-activity questions from memory."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Reason for the team lookup."},
                "search": {"type": "string", "description": "Optional text filter for names, emails, or roles."},
                "person": {"type": "string", "description": "Optional teammate/user name or email to focus on."},
                "time_window": {**_WORKSPACE_ALL_TIME_WINDOW_SCHEMA},
                "limit": {"type": "integer", "description": "Max team/activity records per source (default 20)", "default": 20},
                "include_activity": {
                    "type": "boolean",
                    "description": "Also include recent activity rows for the matched people.",
                    "default": True,
                },
            },
        },
    },
    {
        "name": "read_workspace_records",
        "description": (
            "Read user-created structured workspace data: Domain schemas, typed records, and Domain audit events. "
            "Use this for questions about trackers, leads, bugs, tasks, decisions, research records, or any team "
            "database stored as a Domain. Use manage_domain only when you need to create or change records."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Reason for the records lookup."},
                "search": {"type": "string", "description": "Optional literal text filter for domain names, record titles, or record text."},
                "domain_id": {"type": "integer", "description": "Optional Domain id filter."},
                "object_key": {"type": "string", "description": "Optional Domain object key filter."},
                "time_window": {**_WORKSPACE_ALL_TIME_WINDOW_SCHEMA},
                "start_at": {"type": "string", "description": "ISO timestamp for custom lower bound."},
                "end_at": {"type": "string", "description": "ISO timestamp for custom upper bound."},
                "limit": {"type": "integer", "description": "Max records per source (default 20)", "default": 20},
                "include_archived": {"type": "boolean", "default": False},
                "cursor": {
                    "type": "string",
                    "description": "Opaque next_page token returned by a previous call.",
                },
            },
        },
    },
    {
        "name": "read_cycles",
        "description": (
            "Read workspace Cycles and Cycle runs: recurring prompts, one-time reminders, schedules, enabled state, last/next run, "
            "linked thoughts, and recent run status. Use this for questions about recurring check-ins, reports, "
            "automations, or what scheduled Illo work exists. Use manage_cycle only to create, update, delete, or run one."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Reason for the Cycle lookup."},
                "search": {"type": "string", "description": "Optional text filter for Cycle names, prompts, statuses, or linked ideas."},
                "person": {"type": "string", "description": "Optional owner name or email to scope Cycles to."},
                "time_window": {**_WORKSPACE_ALL_TIME_WINDOW_SCHEMA},
                "start_at": {"type": "string", "description": "ISO timestamp for custom lower bound."},
                "end_at": {"type": "string", "description": "ISO timestamp for custom upper bound."},
                "limit": {"type": "integer", "description": "Max records per source (default 20)", "default": 20},
                "include_deleted": {"type": "boolean", "default": False},
                "cycle_id": {
                    "type": "integer",
                    "description": "Optional Cycle id filter; required for last_completed_run.",
                },
                "last_completed_run": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Return only this Cycle's latest completed-run timestamp using one bounded query."
                    ),
                },
                "cursor": {
                    "type": "string",
                    "description": "Opaque next_page token returned by a previous call.",
                },
            },
        },
    },
    {
        "name": "read_workspace_apps",
        "description": (
            "Read generated workspace apps/dashboards and optional app-local state. Use this for questions about "
            "what apps exist, what dashboards are available, or what UI state an app currently stores. "
            "For build/create requests, leave include_archived=false; archived apps are not candidates for "
            "new app discovery. Only inspect archived apps when the user explicitly asks about archived or "
            "restorable apps, and then set confirm_include_archived=true. "
            "Use manage_workspace_app only when creating, updating, archiving, restoring, or changing app state."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Reason for the workspace app lookup."},
                "search": {"type": "string", "description": "Optional text filter for app names, keys, descriptions, or state keys."},
                "time_window": {**_WORKSPACE_ALL_TIME_WINDOW_SCHEMA},
                "start_at": {"type": "string", "description": "ISO timestamp for custom lower bound."},
                "end_at": {"type": "string", "description": "ISO timestamp for custom upper bound."},
                "limit": {"type": "integer", "description": "Max records per source (default 20)", "default": 20},
                "include_archived": {
                    "type": "boolean",
                    "default": False,
                    "description": "Include archived apps only when the user explicitly asks about archived/restorable apps.",
                },
                "confirm_include_archived": {
                    "type": "boolean",
                    "default": False,
                    "description": "Required with include_archived=true; confirms the user explicitly asked to inspect archived apps.",
                },
                "include_state": {"type": "boolean", "description": "Include app-local state rows.", "default": True},
            },
        },
    },
    {
        "name": "manage_cycle",
        "description": (
            "Create, update, delete, list, manually run, or orient workspace Cycles, which are recurring "
            "Illo prompts/check-ins/reports or one-time reminders. This is the action tool. For answering questions about "
            "which Cycles exist or what ran recently, prefer read_cycles first. Actions: 'create', "
            "'list', 'usage_summary', 'update', 'delete', 'run', 'add_guidance', 'add_output_target', "
            "'remove_output_target'. usage_summary is read-only and reports real token/cost burn from model API calls. "
            "Use action='help' or action='schema' with operation to inspect "
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
                        "create",
                        "list",
                        "usage_summary",
                        "update",
                        "delete",
                        "run",
                        "add_guidance",
                        "add_output_target",
                        "remove_output_target",
                    ],
                    "description": "What to do. Use help/schema to inspect cycle operations before mutating.",
                },
                "operation": {
                    "type": "string",
                    "description": "Optional operation name to inspect when action is help or schema.",
                },
                "id": {"type": "integer", "description": "Cycle id (required for update/delete/run)"},
                "days": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 3650,
                    "description": "For usage_summary, include Cycle runs scheduled within the last N days. Omit when selecting only by run_limit.",
                },
                "run_limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 500,
                    "description": "For usage_summary, cap the window to the most recent N Cycle runs.",
                },
                "name": {"type": "string", "description": "Cycle name"},
                "prompt": {"type": "string", "description": "Prompt Illo should run each cycle"},
                "schedule_expr": {
                    "type": "string",
                    "description": "5-field cron expression like '0 8 * * *', or a one-time expression like 'at:2026-05-08T15:30:00-04:00'",
                },
                "run_at": {
                    "type": "string",
                    "description": "ISO timestamp for a one-time reminder/run. Use instead of schedule_expr when the user asks for a reminder at a specific time.",
                },
                "timezone": {"type": "string", "description": "IANA timezone name"},
                "enabled": {"type": "boolean", "description": "Whether the cycle is active"},
                "model_override": {"type": "string", "description": "Optional model override"},
                "thinking_override": {
                    "type": "string",
                    "enum": ["none", "low", "medium", "high", "xhigh"],
                    "description": "Optional thinking level override",
                },
                "target_idea_id": {
                    "type": "string",
                    "description": "Optional thought id to reuse; the first run creates one if omitted",
                },
                "guidance": {
                    "type": "string",
                    "description": "Durable guidance to add to a Cycle or seed at creation.",
                },
                "rationale": {
                    "type": "string",
                    "description": "Why this Cycle change is useful. Required by convention for autonomous changes.",
                },
                "run_kind": {
                    "type": "string",
                    "enum": ["scheduled_digest", "off_slot_material_alert"],
                    "description": (
                        "Explicit coordinator run kind for action='run'. Use off_slot_material_alert for a concise, "
                        "single-focus alert and scheduled_digest for the full digest contract."
                    ),
                },
                "output_target_type": {
                    "type": "string",
                    "description": "Output target type, such as cycle_ledger, thread, domain, project_file, workspace_app, or chat.",
                },
                "output_target_id": {
                    "type": "string",
                    "description": "Identifier for the output target; for remove_output_target this is the numeric CycleOutputTarget id.",
                },
                "output_target_label": {
                    "type": "string",
                    "description": "Human label for the output target.",
                },
                "output_target_config": {
                    "type": "object",
                    "description": "Optional structured output target configuration.",
                },
            },
            "required": ["action"],
        },
    },
]


__all__ = [
    "BRAIN_TOOLS",
    "WORKSPACE_OVERVIEW_SPARSE_GUIDANCE",
]
