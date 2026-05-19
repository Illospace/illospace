"""Tool schema definitions for the Illo agent loop.

Contains all tool definitions (BRAIN_TOOLS, EXEC_TOOLS, etc.) and
tier constants. These are pure data — no handlers, no side effects.
"""

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
                "model_tier": {"type": "string", "enum": ["local", "low", "medium", "high"], "default": "medium"},
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
        "description": "Record a new memory (lesson, pattern, fact, or episode) into the brain.",
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "Memory content (min 20 chars)"},
                "type": {"type": "string", "enum": ["lesson", "pattern", "fact", "episode"], "default": "episode"},
                "salience": {"type": "number", "description": "Importance 1-10 (default 5)", "default": 5.0},
            },
            "required": ["content"],
        },
    },
    {
        "name": "vault_inventory",
        "description": (
            "List metadata-only Vault secrets for credential reasoning. Returns key names, "
            "descriptions, categories, and agent_access_level, never secret values. Call this before "
            "brain_vault or vault_secret_prompt when a task needs a credential. Use a returned exact "
            "key with brain_vault; if multiple candidates fit, ask the user; only call "
            "vault_secret_prompt when no suitable existing secret exists."
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
            "Request task-scoped access to a secret (API key, token, etc.) from the encrypted vault. "
            "The secret is only returned when the user has approved a live grant for this exact run and key."
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
                "domain_id": {"type": "integer", "description": "Optional Domain id filter."},
                "object_key": {"type": "string", "description": "Optional Domain object key filter."},
                "include_archived": {"type": "boolean", "default": False},
            },
        },
    },
    {
        "name": "read_workspace_overview",
        "description": (
            "Read a curated overview of the current Illospace workspace before introducing Illo, "
            "answering broad setup questions, or explaining what context is available. Returns team members, "
            "active/recent Cortex thoughts, recent agent runs/messages, Project Context profiles and attachments, "
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
            "'what did Illo do?', or 'what changed recently?'."
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
            "Create, update, delete, list, or manually run workspace Cycles, which are recurring "
            "Illo prompts/check-ins/reports or one-time reminders. This is the action tool. For answering questions about "
            "which Cycles exist or what ran recently, prefer read_cycles first. Actions: 'create', "
            "'list', 'update', 'delete', 'run'. Use action='help' or action='schema' with operation to inspect "
            "arguments before mutating."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["help", "schema", "create", "list", "update", "delete", "run"],
                    "description": "What to do. Use help/schema to inspect cycle operations before mutating.",
                },
                "operation": {
                    "type": "string",
                    "description": "Optional operation name to inspect when action is help or schema.",
                },
                "id": {"type": "integer", "description": "Cycle id (required for update/delete/run)"},
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
                "execution_mode": {
                    "type": "string",
                    "enum": ["reuse_same_idea"],
                    "description": "Cycles always reuse one thought thread",
                },
                "target_idea_id": {
                    "type": "string",
                    "description": "Optional thought id to reuse; the first run creates one if omitted",
                },
                "reopen_archived": {
                    "type": "boolean",
                    "description": "Archived cycle thoughts are reopened automatically",
                },
            },
            "required": ["action"],
        },
    },
]

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

# ── Domain Tools ─────────────────────────────────────────────
# Org-wide custom databases/knowledge structures.

DOMAIN_TOOLS = [
    {
        "name": "manage_domain",
        "description": (
            "Create and maintain org-wide custom Domains: user-created shared team databases with "
            "object types, typed records, relations, and audit events. This is the action/exact-object "
            "tool for creating trackers, adding fields, recording or updating items, linking records, "
            "or deleting/archiving Domains and records. For broad awareness questions about existing "
            "workspace records, prefer read_workspace_records first. Use action='help' or action='schema' with "
            "operation to inspect domain operations before mutating."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "help",
                        "list",
                        "create_domain",
                        "remove_domain",
                        "schema",
                        "add_object",
                        "add_field",
                        "add_relation_type",
                        "query_records",
                        "get_record",
                        "create_record",
                        "update_record",
                        "remove_record",
                        "link_records",
                        "events",
                    ],
                    "description": "The domain operation to run.",
                },
                "operation": {
                    "type": "string",
                    "description": "Optional operation name to inspect when action is help or schema.",
                },
                "domain_id": {"type": "integer", "description": "Domain id for existing-domain actions."},
                "name": {"type": "string", "description": "Domain/object/field/relation display name."},
                "slug": {"type": "string", "description": "Optional domain slug."},
                "description": {"type": "string", "description": "Optional description."},
                "objects": {
                    "type": "array",
                    "description": "Object definitions for create_domain.",
                    "items": {"type": "object"},
                },
                "fields": {
                    "type": "array",
                    "description": "Field definitions for add_object.",
                    "items": {"type": "object"},
                },
                "relations": {
                    "type": "array",
                    "description": "Relation type definitions for create_domain.",
                    "items": {"type": "object"},
                },
                "object_key": {"type": "string", "description": "Object type key, e.g. hook or contact."},
                "field": {
                    "type": "object",
                    "description": "Field definition for add_field.",
                },
                "relation_type": {
                    "type": "object",
                    "description": "Relation type definition for add_relation_type.",
                },
                "search": {"type": "string", "description": "Search text for query_records."},
                "limit": {"type": "integer", "default": 50, "description": "Maximum records/events to return."},
                "include_archived": {"type": "boolean", "default": False},
                "record_id": {"type": "integer", "description": "Record id for get/update/remove/events."},
                "data": {"type": "object", "description": "Record data for create_record."},
                "data_patch": {"type": "object", "description": "Record field patch for update_record."},
                "title": {"type": "string", "description": "Optional record title override."},
                "expected_version": {"type": "integer", "description": "Optimistic concurrency version."},
                "mode": {
                    "type": "string",
                    "enum": ["archive", "delete"],
                    "default": "archive",
                    "description": "Whether remove_domain/remove_record archives or permanently deletes.",
                },
                "relation_key": {"type": "string", "description": "Relation type key for link_records."},
                "source_record_id": {"type": "integer"},
                "target_record_id": {"type": "integer"},
                "properties": {"type": "object", "description": "Optional relation properties."},
            },
            "required": ["action"],
        },
    },
]

# ── Inbound Coordination Tools ────────────────────────────────
# Illo-facing configuration for external source signals. External systems submit
# to /webhooks or hosted MCP; Illo configures that lane through this tool.

INBOUND_TOOLS = [
    {
        "name": "manage_inbound",
        "description": (
            "Configure and inspect the inbound coordination layer on behalf of the current user: "
            "external source connections, scoped signal tokens, origin policies, Domain Projections, "
            "event logs, and decision receipts. Use this when a user asks Illo to set up or adjust "
            "webhooks, MCP personal-tool signals, Jira/GitHub/Stripe-style sources, routing rules, "
            "or deterministic storage into Domains. External tools should submit signals through "
            "hosted MCP or POST /webhooks; this tool is Illo's chat-based admin/configuration surface. "
            "Use action='help' or action='schema' with operation before mutating unfamiliar configs."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "help",
                        "schema",
                        "list_connections",
                        "get_connection",
                        "create_connection",
                        "update_connection",
                        "mint_token",
                        "list_tokens",
                        "get_token",
                        "revoke_token",
                        "list_policies",
                        "get_policy",
                        "create_policy",
                        "update_policy",
                        "list_projections",
                        "get_projection",
                        "create_projection",
                        "update_projection",
                        "list_events",
                        "get_event",
                        "list_receipts",
                        "dry_run_match",
                        "replay_events",
                        "get_source_card",
                        "refresh_source_card",
                    ],
                    "description": "The inbound coordination operation to run.",
                },
                "operation": {
                    "type": "string",
                    "description": "Optional operation name to inspect when action is help or schema.",
                },
                "connection_id": {"type": "string", "description": "External source connection id."},
                "display_name": {"type": "string", "description": "Human-readable source name."},
                "agent_kind": {
                    "type": "string",
                    "description": "Source kind, e.g. codex, jira, github, stripe, custom.",
                },
                "transport": {
                    "type": "string",
                    "description": "Inbound transport, e.g. webhook, hosted_mcp, bridge_pull.",
                },
                "endpoint_url": {"type": "string", "description": "Optional remote endpoint URL."},
                "remote_agent_id": {"type": "string", "description": "Optional source-side agent or app id."},
                "remote_agent_card": {"type": "object", "description": "Optional source card/metadata."},
                "capabilities": {"type": "object", "description": "Source capability metadata."},
                "metadata": {"type": "object", "description": "Operator notes or structured metadata."},
                "status": {"type": "string", "description": "Connection status override."},
                "include_disabled": {"type": "boolean", "default": False},
                "include_revoked": {
                    "type": "boolean",
                    "default": False,
                    "description": "Include revoked tokens when listing token metadata.",
                },
                "token_id": {"type": "string", "description": "Connection token id."},
                "token_name": {"type": "string", "description": "Display name for a minted token."},
                "token_scopes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional explicit token scopes. Defaults to signal:submit.",
                },
                "expires_at": {"type": "string", "description": "Optional ISO expiry for minted token."},
                "policy_id": {"type": "string", "description": "Inbound source policy id."},
                "name": {"type": "string", "description": "Policy name."},
                "enabled": {"type": "boolean", "description": "Whether a policy or projection is active."},
                "priority": {
                    "type": "integer",
                    "description": "Lower priority wins when several origin policies match.",
                },
                "origin_patterns": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "fnmatch-style patterns, e.g. jira.issue_*, github.*, *.",
                },
                "envelope_kinds": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Inbound envelope kinds this policy accepts. Usually signal.",
                },
                "instructions": {
                    "type": "string",
                    "description": "Natural-language instructions for Illo when this policy needs agent handling.",
                },
                "schema_config": {
                    "type": "object",
                    "description": (
                        "User-friendly schema config. Supported keys include required_paths and "
                        "fields with path/field, type, required, description, and example."
                    ),
                },
                "allowed_actions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Policy-level allowed actions, e.g. domain_projection.upsert.",
                },
                "auto_execute_actions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Actions that may auto-run when confidence thresholds are satisfied.",
                },
                "auto_execute_min_confidence": {
                    "type": "number",
                    "description": "Minimum confidence for auto-execution where applicable.",
                },
                "review_mode": {
                    "type": "string",
                    "description": "Fallback review status, e.g. review_required or quarantined.",
                },
                "projection_id": {"type": "string", "description": "Inbound Domain Projection id."},
                "domain_id": {"type": "integer", "description": "Target Domain id."},
                "object_key": {"type": "string", "description": "Target Domain object key."},
                "external_id_path": {
                    "type": "string",
                    "description": "Path to source external id, e.g. payload.issue.key.",
                },
                "external_id_field": {
                    "type": "string",
                    "description": "Target Domain field that stores the external id.",
                },
                "field_mapping": {
                    "type": "object",
                    "description": "Map target Domain field keys to source paths.",
                },
                "title_path": {"type": "string", "description": "Optional path used as Domain record title."},
                "upsert_mode": {
                    "type": "string",
                    "enum": ["upsert", "create_only", "update_only"],
                    "description": "How the projection writes matching Domain records.",
                },
                "validation_failure_status": {
                    "type": "string",
                    "enum": ["review_required", "quarantined", "failed"],
                    "description": "Event status when projection validation fails.",
                },
                "auto_allow_policy_action": {
                    "type": "boolean",
                    "default": True,
                    "description": "When creating a projection for a policy, add domain_projection.upsert to allowed_actions.",
                },
                "event_id": {"type": "string", "description": "Inbound event id."},
                "origin": {"type": "string", "description": "Source event origin, e.g. jira.issue_created."},
                "kind": {"type": "string", "default": "signal", "description": "Inbound envelope kind."},
                "payload": {"type": "object", "description": "Sample payload for dry-run matching."},
                "include_payload": {
                    "type": "boolean",
                    "default": False,
                    "description": "Include stored raw/normalized payloads in event reads or replay output.",
                },
                "include_receipts": {"type": "boolean", "default": False},
                "source_purpose": {
                    "type": "string",
                    "description": "Optional human/Illo-authored purpose to store on a refreshed source card.",
                },
                "source_notes": {
                    "type": "string",
                    "description": "Optional notes to store on a refreshed source card.",
                },
                "source_tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional tags to store on a refreshed source card.",
                },
                "limit": {"type": "integer", "default": 25, "description": "Maximum rows to return."},
            },
            "required": ["action"],
        },
    },
]

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
            "thread/idea when one is bound. Use action='help' or action='schema' with operation to inspect "
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
                    "enum": [
                        "emerged",
                        "queued",
                        "active",
                        "working",
                        "needs_input",
                        "unread_reply",
                        "blocked",
                        "failed",
                        "resolved",
                        "stale",
                        "paused",
                        "done",
                        "archived",
                    ],
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
    }
]

# ── Project Tools ─────────────────────────────────────────────
# Durable reusable folders/context bundles for Cortex threads.

PROJECT_TOOLS = [
    {
        "name": "manage_project",
        "description": (
            "Create, list, update, archive, attach, and maintain Cortex Project Context profiles. "
            "This is the action tool for managing project/folder/context bundles, adding or removing "
            "files/repos/folders/docs, or attaching reusable project context to the current thread. "
            "For awareness questions about what project context exists or what Illo can see, prefer "
            "read_project_contexts first. Thread attachments do not require a project. Use action='help' or "
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
                        "update",
                        "archive",
                        "delete",
                        "add_resource",
                        "update_resource",
                        "remove_resource",
                        "reorder_resources",
                        "attach_to_thread",
                    ],
                    "description": "The project operation to run. delete archives the project profile.",
                },
                "operation": {
                    "type": "string",
                    "description": "Optional operation name to inspect when action is help or schema.",
                },
                "project_id": {"type": "string", "description": "Project profile id for existing-project actions."},
                "profile_id": {"type": "string", "description": "Alias for project_id."},
                "slug": {"type": "string", "description": "Stable project slug for create/update."},
                "name": {"type": "string", "description": "Human-readable project name."},
                "description": {"type": "string", "description": "Optional project description."},
                "project_context": {
                    "type": "object",
                    "description": "Full Project Context payload when creating, replacing, or attaching a custom context.",
                },
                "resources": {
                    "type": "array",
                    "description": "Project resources such as files, folders, repos, docs, or uploaded file manifests.",
                    "items": {"type": "object"},
                },
                "resource": {"type": "object", "description": "Single resource for add_resource or update_resource."},
                "resource_id": {"type": "string", "description": "Resource id/path/uri/name for update/remove."},
                "resource_ids": {
                    "type": "array",
                    "description": "Complete ordered resource id list for reorder_resources.",
                    "items": {"type": "string"},
                },
                "metadata": {"type": "object", "description": "Optional metadata for profile or attachment provenance."},
                "visibility": {
                    "type": "string",
                    "enum": ["private", "public"],
                    "description": "Project visibility. New projects default to private; public projects are visible to the org.",
                },
                "shared_usernames": {
                    "type": "array",
                    "description": "User names to grant access to a private project.",
                    "items": {"type": "string"},
                },
                "default_environment_binding_id": {"type": "integer", "description": "Optional default environment binding id."},
                "environment_binding_id": {"type": "integer", "description": "Optional per-thread attachment environment binding id."},
                "idea_id": {
                    "type": "string",
                    "description": "Thread/idea id for attach_to_thread when no current Cortex thread is bound.",
                },
                "include_inactive": {"type": "boolean", "default": False},
            },
            "required": ["action"],
        },
    },
]

# ── Workspace App Tools ──────────────────────────────────────
# Org-wide generated UI apps that can use app-local state or Domains.

WORKSPACE_APP_TOOLS = [
    {
        "name": "manage_workspace_app",
        "description": (
            "Create, list, update, archive, and persist state for generated workspace apps. "
            "This is the action tool to create or change a persistent programmable UI surface or dashboard "
            "inside Cortex. Use renderer_key='generated-ui-app' and source_kind='json' for common "
            "host-rendered structured UIs, including tables, lists, cards, metrics, charts, forms, details, "
            "board/kanban views, and manifest action buttons. Use renderer_key='sandboxed-html-app' and source_kind='html' "
            "only for bespoke interactions or custom blocks that cannot be represented by structured views. "
            "For list/get discovery, keep include_archived=false unless the user explicitly asks to inspect archived apps; "
            "archived apps are not candidates for new build/create requests. "
            "Use action='restore' only when the user explicitly asks to restore an archived app; for build/create "
            "requests, create a new app or update an active app instead of resurrecting archived drafts. "
            "Recordful apps must use manage_domain first; app-local "
            "state is only for UI preferences, filters, drafts, and ephemeral interface state. "
            "For awareness questions about what apps exist or current app state, prefer read_workspace_apps first. "
            "New generated apps must pass the workspace app contract before they are persisted. Use action='help' "
            "or action='schema' with operation to inspect arguments before mutating."
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
                        "get_state",
                        "update_state",
                    ],
                    "description": "The workspace app operation to run.",
                },
                "operation": {
                    "type": "string",
                    "description": "Optional operation name to inspect when action is help or schema.",
                },
                "app_id": {"type": "string", "description": "Workspace app id for existing-app actions."},
                "key": {"type": "string", "description": "Stable app key; generated from name when omitted."},
                "name": {"type": "string", "description": "Human-readable app name."},
                "description": {"type": "string", "description": "Short app description."},
                "renderer_key": {
                    "type": "string",
                    "default": "generated-ui-app",
                    "description": (
                        "Renderer runtime key. Use generated-ui-app for host-rendered structured UI. "
                        "Use sandboxed-html-app for first-class full-code workspace apps that still follow "
                        "the Illospace design contract."
                    ),
                },
                "source_kind": {
                    "type": "string",
                    "default": "json",
                    "description": "Generated source format. Use json for generated-ui-app; html for sandboxed HTML apps.",
                },
                "source_code": {
                    "type": "string",
                    "description": (
                        "Generated app source. For generated-ui-app, provide a JSON string with "
                        "schema_version=1, title, optional description, optional top-level actions "
                        "referencing manifest.actions, and views. For sandboxed html, "
                        "provide a responsive HTML/CSS/JS body or document only when structured views are insufficient. "
                        "Canonical calls pass "
                        "manifest, visual_spec, and metadata as separate tool args; the app compiler "
                        "also normalizes wrapped generated-app envelopes when needed."
                    ),
                },
                "manifest": {
                    "type": "object",
                    "description": (
                        "Optional contract-bearing runtime manifest. The app compiler supplies safe "
                        "contract_version, app_local UI-state, and design_contract defaults for simple "
                        "generated-ui apps. For Domain-backed apps, provide an explicit data_plan "
                        "and include the strict design contract shape "
                        "{\"design_contract\":{\"kit\":\"constellation-app-kit\","
                        "\"theme_modes\":[\"dark\",\"light\"]}}. Do not use alternate keys like "
                        "system, design_system, uses_app_kit_classes, or supports_color_scheme in "
                        "manifest.design_contract; put descriptive labels in metadata instead. "
                        "Domain records expose a virtual top-level title; bindings may include \"title\" "
                        "for display/card labels even when the object's field definitions do not contain "
                        "a data field named title. "
                        "Use bindings such as {\"data_plan\":{\"mode\":\"domain\","
                        "\"bindings\":{\"todos\":{\"domain_id\":1,\"domain_slug\":\"todo-notes\","
                        "\"object_key\":\"todo_item\",\"fields\":[\"title\",\"notes\",\"completed\"],"
                        "\"operations\":[\"schema\",\"list\",\"query\",\"create\",\"update\",\"archive\","
                        "\"aggregate\",\"bulkUpdate\",\"history\",\"listRelations\",\"createRelation\","
                        "\"archiveRelation\"]}}}} and access them with window.illo.domain('todos'). "
                        "Domain binding operations are exact SDK method names; do not use capability labels "
                        "such as read, write, or crud in manifest.data_plan.bindings.*.operations. "
                        "The app runtime exposes manifest-bound Domain CRUD, aggregate, bulkUpdate, "
                        "history, relation helpers, polling-backed subscribe, app state, and "
                        "window.illo.actions.run(actionKey, payload) for manifest-declared server-side actions. "
                        "Treat Domains as the workspace truth bridge and actions/connectors as external IO. "
                        "For external systems, prefer workflow-level action keys such as tickets.importExternal "
                        "or tickets.syncExternal; declare provider/auth as connector metadata rather than "
                        "hardcoding GitHub/Jira into the primitive. Action declarations must include kind, effects "
                        "(such as external.read, external.write, domain.read, domain.write), connector metadata "
                        "when external IO is involved, and executor {type:'deferred'} or an approved registered "
                        "executor key. Never include raw tokens, API keys, Authorization headers, or passwords in "
                        "source, state, payload examples, or manifest fields. "
                        "In generated JavaScript, use canonical app SDK shapes such as recordId, dataPatch, "
                        "sourceRecordId, and targetRecordId. Relation lists should pass relationKey plus "
                        "sourceRecordId or targetRecordId, not a generic recordId."
                    ),
                },
                "visual_spec": {
                    "type": "object",
                    "description": (
                        "Visual metadata for Cortex placement and host-rendered thumbnail. The app "
                        "compiler supplies a basic structured thumbnail when omitted. Prefer explicit "
                        "thumbnail metadata such as label, value/status, secondary, unit, and progress; "
                        "do not provide thumbnail.source_code/html."
                    ),
                },
                "metadata": {"type": "object", "description": "App metadata for provenance and runtime notes."},
                "initial_state": {"type": "object", "description": "Initial app-local state for create."},
                "state_key": {
                    "type": "string",
                    "default": "default",
                    "description": "Durable state bucket key.",
                },
                "data": {"type": "object", "description": "Replacement state data for update_state."},
                "data_patch": {"type": "object", "description": "Shallow state patch for update_state."},
                "include_archived": {"type": "boolean", "default": False},
                "include_prototypes": {"type": "boolean", "default": False},
                "confirm_include_archived": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Required with action='list' or action='get' and include_archived=true. "
                        "Set true only when the user explicitly asked to inspect archived apps."
                    ),
                },
                "confirm_restore_archived": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Required for action='restore'. Set true only when the user explicitly asked "
                        "to restore or reopen an archived app."
                    ),
                },
            },
            "required": ["action"],
        },
    },
]

# ── Execution Tools ──────────────────────────────────────────
# Filesystem and shell tools — give agents the ability to DO things

EXEC_TOOLS = [
    {
        "name": "exec_command",
        "description": (
            "Execute a shell command and return stdout, stderr, and exit code. "
            "Use for running tests, git operations, builds, and any CLI task. "
            "Commands run in the project workspace directory. "
            "For multi-step shell operations or when you need to iterate/aggregate "
            "across commands, prefer `run_script` which executes a full Python script in one call. "
            "For running tests, prefer `test_runner` which parses output into structured format."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to execute"},
                "workspace": {
                    "type": "string",
                    "description": "Optional workspace root or registered workspace name to target",
                },
                "working_dir": {"type": "string", "description": "Working directory (optional, defaults to workspace)"},
                "timeout": {"type": "integer", "description": "Timeout in seconds (default 60, max 300)", "default": 60},
            },
            "required": ["command"],
        },
    },
    {
        "name": "read_file",
        "description": (
            "Read a file's contents. Supports optional line range for large files. "
            "Returns the file content with line numbers. "
            "For large files or when you only need structure (imports, classes, functions), "
            "prefer `file_summary`. To read/search multiple files at once, prefer `run_script`."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path (absolute or relative to workspace)"},
                "workspace": {
                    "type": "string",
                    "description": "Optional workspace root or registered workspace name to target",
                },
                "start_line": {"type": "integer", "description": "First line to read (1-based, optional)"},
                "end_line": {"type": "integer", "description": "Last line to read (inclusive, optional)"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write content to a file. Creates parent directories if needed. Overwrites existing files.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path (absolute or relative to workspace)"},
                "workspace": {
                    "type": "string",
                    "description": "Optional workspace root or registered workspace name to target",
                },
                "content": {"type": "string", "description": "Full file content to write"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": (
            "Make a surgical edit to a file by replacing an exact string match. "
            "More efficient than write_file for modifications — only sends the diff."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path"},
                "workspace": {
                    "type": "string",
                    "description": "Optional workspace root or registered workspace name to target",
                },
                "old_text": {"type": "string", "description": "Exact text to find and replace (must be unique in file)"},
                "new_text": {"type": "string", "description": "Replacement text"},
            },
            "required": ["path", "old_text", "new_text"],
        },
    },
    {
        "name": "search_files",
        "description": (
            "Search file contents using a regex pattern. Returns matching lines with context. "
            "For semantic/conceptual search (not just text matching), prefer `semantic_search`. "
            "For searching across many files with complex logic, prefer `run_script`."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regex pattern to search for"},
                "workspace": {
                    "type": "string",
                    "description": "Optional workspace root or registered workspace name to target",
                },
                "path": {"type": "string", "description": "Directory or file to search in (default: workspace root)"},
                "glob": {"type": "string", "description": "File glob filter (e.g., '*.py', '**/*.js')"},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "list_files",
        "description": "List files matching a glob pattern. Returns file paths sorted by modification time.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob pattern (e.g., '**/*.py', 'src/**/*.ts')"},
                "workspace": {
                    "type": "string",
                    "description": "Optional workspace root or registered workspace name to target",
                },
                "path": {"type": "string", "description": "Base directory (default: workspace root)"},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "run_script",
        "description": (
            "Write and execute a Python script in one shot. Use this INSTEAD of chaining "
            "multiple exec_command/read_file/search_files calls when you need to iterate, "
            "search, or aggregate across multiple files or resources. The script runs with "
            "the full Python standard library and returns stdout. Print structured results "
            "(JSON or formatted text) to stdout for clean output."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "script": {"type": "string", "description": "Python 3 script body to execute"},
                "description": {"type": "string", "description": "Brief description of what the script does"},
                "workspace": {
                    "type": "string",
                    "description": "Optional workspace root or registered workspace name to target",
                },
                "timeout": {"type": "integer", "description": "Timeout in seconds (default 60, max 300)", "default": 60},
            },
            "required": ["script"],
        },
    },
    {
        "name": "parallel_tool_batch",
        "description": (
            "Execute multiple independent read/search/fetch-style tool calls concurrently in the runtime. "
            "Use this instead of serial tool calls when you need several files, searches, or web fetches "
            "that do not depend on each other. Safe tools only; write/edit/exec side effects are blocked."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "operations": {
                    "type": "array",
                    "description": "List of tool invocations to execute in parallel.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "tool_name": {
                                "type": "string",
                                "description": "Safe tool name to invoke in parallel",
                            },
                            "args": {
                                "type": "object",
                                "description": "Arguments for that tool invocation",
                                "default": {},
                            },
                        },
                        "required": ["tool_name"],
                    },
                },
                "max_parallel": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Optional concurrency cap for this batch",
                },
            },
            "required": ["operations"],
        },
    },
    {
        "name": "web_search",
        "description": (
            "Search the public web using a configured provider with safe defaults and bounded output. "
            "Use this to discover URLs and recent information before escalating to browser automation."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "provider": {
                    "type": "string",
                    "description": "Optional search provider override (e.g. brave, tavily, duckduckgo-lite)",
                },
                "limit": {"type": "integer", "description": "Maximum result count", "default": 5},
            },
            "required": ["query"],
        },
    },
    {
        "name": "web_fetch",
        "description": (
            "Fetch a public URL with SSRF protection and readable-content extraction. "
            "Use this when you already know the URL and do not need a full browser."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Public HTTP/HTTPS URL"},
                "extract_mode": {
                    "type": "string",
                    "enum": ["markdown", "text", "html"],
                    "default": "markdown",
                },
                "max_chars": {"type": "integer", "description": "Max output characters", "default": 12000},
            },
            "required": ["url"],
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
        },
        "required": ["content"],
    },
}

CORTEX_VISUAL_REPLY_TOOL = {
    "name": "cortex_visual_reply",
    "description": (
        "Render compact static visual content in the Cortex workspace. Use for diffs, "
        "charts, diagrams, markdown summaries, and screenshots. For interactive or "
        "recordful generated UI, create or update a workspace app instead."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "content_type": {
                "type": "string",
                "enum": ["diff", "chart", "diagram", "markdown", "screenshot"],
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
                    "For diagram: SVG or Mermaid syntax. For markdown: markdown string. "
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

BROWSER_SESSION_OPEN_TOOL = {
    "name": "browser_session_open",
    "description": (
        "Create or reuse a live server-side browser session for the current Cortex thought. "
        "Use this when a task requires real browsing, JavaScript execution, login flows, "
        "or a live browser viewport in the thought."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Optional initial URL to open."},
            "viewport_width": {"type": "integer", "default": 1280},
            "viewport_height": {"type": "integer", "default": 800},
            "storage_mode": {
                "type": "string",
                "enum": ["ephemeral", "idea"],
                "default": "ephemeral",
                "description": "Whether login/session state persists for the current thought.",
            },
            "allow_downloads": {
                "type": "boolean",
                "default": False,
                "description": "Allow file downloads into the thought workspace uploads area.",
            },
            "allow_file_uploads": {
                "type": "boolean",
                "default": True,
                "description": "Allow uploading existing Cortex attachments into file inputs.",
            },
        },
    },
}

BROWSER_NAVIGATE_TOOL = {
    "name": "browser_navigate",
    "description": "Navigate the active thought browser session to a URL.",
    "input_schema": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL to open"},
        },
        "required": ["url"],
    },
}

BROWSER_CLICK_TOOL = {
    "name": "browser_click",
    "description": "Click in the active browser session by selector or viewport coordinates.",
    "input_schema": {
        "type": "object",
        "properties": {
            "selector": {"type": "string", "description": "Optional CSS selector target"},
            "x": {"type": "number", "description": "Viewport X coordinate"},
            "y": {"type": "number", "description": "Viewport Y coordinate"},
        },
    },
}

BROWSER_TYPE_TOOL = {
    "name": "browser_type",
    "description": "Type text into the active browser session.",
    "input_schema": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Text to type"},
            "selector": {"type": "string", "description": "Optional CSS selector to focus first"},
            "press_enter": {"type": "boolean", "default": False},
        },
        "required": ["text"],
    },
}

BROWSER_KEY_TOOL = {
    "name": "browser_key",
    "description": "Press a keyboard key in the active browser session.",
    "input_schema": {
        "type": "object",
        "properties": {
            "key": {"type": "string", "description": "Keyboard key, e.g. Enter or Escape"},
        },
        "required": ["key"],
    },
}

BROWSER_BACK_TOOL = {
    "name": "browser_back",
    "description": "Navigate backward in the active browser session history.",
    "input_schema": {"type": "object", "properties": {}},
}

BROWSER_FORWARD_TOOL = {
    "name": "browser_forward",
    "description": "Navigate forward in the active browser session history.",
    "input_schema": {"type": "object", "properties": {}},
}

BROWSER_NEW_TAB_TOOL = {
    "name": "browser_new_tab",
    "description": "Open a new tab in the active browser session, optionally navigating it immediately.",
    "input_schema": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Optional initial URL"},
        },
    },
}

BROWSER_SWITCH_TAB_TOOL = {
    "name": "browser_switch_tab",
    "description": "Switch to a tab by index in the active browser session.",
    "input_schema": {
        "type": "object",
        "properties": {
            "index": {"type": "integer", "description": "Tab index"},
        },
        "required": ["index"],
    },
}

BROWSER_CLOSE_TAB_TOOL = {
    "name": "browser_close_tab",
    "description": "Close a tab by index, or the current tab if omitted.",
    "input_schema": {
        "type": "object",
        "properties": {
            "index": {"type": "integer", "description": "Optional tab index"},
        },
    },
}

BROWSER_LIST_TABS_TOOL = {
    "name": "browser_list_tabs",
    "description": "List tabs in the active browser session.",
    "input_schema": {"type": "object", "properties": {}},
}

BROWSER_WAIT_TOOL = {
    "name": "browser_wait",
    "description": "Wait for the active browser session to reach a page state or selector.",
    "input_schema": {
        "type": "object",
        "properties": {
            "selector": {"type": "string", "description": "Optional CSS selector to wait for"},
            "wait_until": {
                "type": "string",
                "enum": ["load", "domcontentloaded", "networkidle"],
                "default": "load",
            },
            "timeout_ms": {"type": "integer", "default": 10000},
        },
    },
}

BROWSER_EXTRACT_TOOL = {
    "name": "browser_extract",
    "description": "Extract text or HTML from the active browser session, optionally scoped to a selector.",
    "input_schema": {
        "type": "object",
        "properties": {
            "selector": {"type": "string", "description": "Optional CSS selector target"},
            "mode": {
                "type": "string",
                "enum": ["text", "html", "markdown"],
                "default": "text",
            },
            "max_chars": {"type": "integer", "default": 6000},
        },
    },
}

BROWSER_DISCOVER_TOOL = {
    "name": "browser_discover",
    "description": "List likely interactive elements on the page with suggested selectors and bounds.",
    "input_schema": {
        "type": "object",
        "properties": {
            "selector": {
                "type": "string",
                "description": "Optional selector used to scope discovery",
                "default": "a,button,input,textarea,select,[role='button']",
            },
            "max_results": {"type": "integer", "default": 40},
        },
    },
}

BROWSER_UPLOAD_ATTACHMENT_TOOL = {
    "name": "browser_upload_attachment",
    "description": (
        "Upload an existing Cortex attachment into a file input inside the active browser session. "
        "attachment_url must be a Cortex /static/uploads/... URL."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "selector": {"type": "string", "description": "CSS selector for the file input"},
            "attachment_url": {"type": "string", "description": "Cortex attachment URL under /static/uploads/"},
        },
        "required": ["selector", "attachment_url"],
    },
}

BROWSER_SNAPSHOT_TOOL = {
    "name": "browser_snapshot",
    "description": "Capture the current browser viewport, return it as a model-visible screenshot, and optionally persist it into the thought.",
    "input_schema": {
        "type": "object",
        "properties": {
            "persist": {"type": "boolean", "default": False},
            "title": {"type": "string", "description": "Optional snapshot title"},
        },
    },
}

BROWSER_SAVE_SCREENSHOT_TOOL = {
    "name": "browser_save_screenshot",
    "description": "Save a PNG screenshot of the current page into the thought workspace uploads area. Use the returned download_url when giving the user a link.",
    "input_schema": {
        "type": "object",
        "properties": {
            "full_page": {"type": "boolean", "default": True},
        },
    },
}

BROWSER_PRINT_PDF_TOOL = {
    "name": "browser_print_pdf",
    "description": "Export the current page as a PDF into the thought workspace uploads area. Use the returned download_url when giving the user a link.",
    "input_schema": {
        "type": "object",
        "properties": {
            "landscape": {"type": "boolean", "default": False},
        },
    },
}

BROWSER_CLOSE_TOOL = {
    "name": "browser_close",
    "description": "Close the active thought browser session.",
    "input_schema": {
        "type": "object",
        "properties": {},
    },
}

BROWSER_TOOL = {
    "name": "browser",
    "description": (
        "Namespace tool for controlling or inspecting the live browser session attached to the current Cortex thought. "
        "Set action='help' to see sub-actions and their required arguments. Use action='open' before navigation or interaction. "
        "Actions that change browser state return a model-visible screenshot; use action='observe' to refresh that screenshot explicitly, "
        "and action='discover' or action='extract' when DOM/text detail is needed. "
        "For tasks that may download a file, open the session with allow_downloads=true."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "help",
                    "open",
                    "navigate",
                    "click",
                    "type",
                    "key",
                    "back",
                    "forward",
                    "new_tab",
                    "switch_tab",
                    "close_tab",
                    "list_tabs",
                    "wait",
                    "observe",
                    "extract",
                    "discover",
                    "upload_attachment",
                    "snapshot",
                    "save_screenshot",
                    "print_pdf",
                    "close",
                ],
                "description": "Browser sub-action to run.",
            },
            "operation": {
                "type": "string",
                "description": "Optional sub-action name to inspect when action is help.",
            },
            "url": {"type": "string", "description": "URL for open, navigate, or new_tab."},
            "viewport_width": {"type": "integer", "default": 1280},
            "viewport_height": {"type": "integer", "default": 800},
            "storage_mode": {
                "type": "string",
                "enum": ["ephemeral", "idea"],
                "default": "ephemeral",
                "description": "Whether login/session state persists for the current thought.",
            },
            "allow_downloads": {"type": "boolean", "default": False},
            "allow_file_uploads": {"type": "boolean", "default": True},
            "selector": {"type": "string", "description": "CSS selector for click/type/wait/extract/discover/upload."},
            "x": {"type": "number", "description": "Viewport X coordinate for click."},
            "y": {"type": "number", "description": "Viewport Y coordinate for click."},
            "text": {"type": "string", "description": "Text to type."},
            "press_enter": {"type": "boolean", "default": False},
            "key": {"type": "string", "description": "Keyboard key, e.g. Enter or Escape."},
            "index": {"type": "integer", "description": "Tab index for switch_tab or close_tab."},
            "wait_until": {
                "type": "string",
                "enum": ["load", "domcontentloaded", "networkidle"],
                "default": "load",
            },
            "timeout_ms": {"type": "integer", "default": 10000},
            "mode": {
                "type": "string",
                "enum": ["text", "html", "markdown"],
                "default": "text",
                "description": "Extraction mode for extract.",
            },
            "max_chars": {"type": "integer", "default": 6000},
            "max_results": {"type": "integer", "default": 40},
            "attachment_url": {"type": "string", "description": "Cortex /static/uploads/... attachment URL for upload_attachment."},
            "persist": {"type": "boolean", "default": False},
            "title": {"type": "string", "description": "Optional snapshot title."},
            "full_page": {"type": "boolean", "default": True},
            "landscape": {"type": "boolean", "default": False},
        },
        "required": ["action"],
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

# ── Composite Tool Lists ─────────────────────────────────────

# Worker tools = normal workspace/product capabilities. Harness orchestration
# is AgentRun-owned, not model-visible.
WORKER_TOOLS = (
    BRAIN_TOOLS
    + DOMAIN_TOOLS
    + INBOUND_TOOLS
    + CORTEX_IDEA_TOOLS
    + CHAT_TOOLS
    + PROJECT_TOOLS
    + WORKSPACE_APP_TOOLS
    + EXEC_TOOLS
    + SESSION_TOOLS
    + [
        CORTEX_VISUAL_REPLY_TOOL,
        BROWSER_TOOL,
    ]
)

# Coordinator tools = normal workspace/product capabilities plus reply and
# introspection tools. Deep planning/workers are runtime state transitions.
COORDINATOR_TOOLS = (
    BRAIN_TOOLS
    + SOUL_TOOLS
    + DOMAIN_TOOLS
    + INBOUND_TOOLS
    + CORTEX_IDEA_TOOLS
    + CHAT_TOOLS
    + PROJECT_TOOLS
    + WORKSPACE_APP_TOOLS
    + EXEC_TOOLS
    + SESSION_TOOLS
    + LIFECYCLE_TOOLS
    + [
        CORTEX_REPLY_TOOL,
        CORTEX_VISUAL_REPLY_TOOL,
        MY_ACTIVITY_TOOL,
        BROWSER_TOOL,
    ]
)

# ── Gate Constants ────────────────────────────────────────────

# Brain gate: these tool names satisfy the "brain context accessed" requirement
_BRAIN_TOOL_NAMES = frozenset({
    "brain_recall", "brain_guardrails", "brain_skills", "skill_view", "skill_asset",
    "brain_encode", "runtime_settings", "query_workspace_data", "read_workspace_overview",
    "read_team_activity", "read_project_contexts", "read_team_members", "read_workspace_records",
    "read_cycles", "read_workspace_apps",
})

# Brain gate: these tools require brain context before first use
# Destructive tools need guardrails; cortex_reply needs recall so the agent
# checks its memory before asking users questions it should already know.
_GATED_TOOL_NAMES = frozenset({
    "write_file", "edit_file", "exec_command", "run_script", "cortex_reply", "cortex_visual_reply",
})
