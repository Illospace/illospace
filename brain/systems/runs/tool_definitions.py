"""Tool schema definitions for the Illo agent loop.

Contains all tool definitions (BRAIN_TOOLS, EXEC_TOOLS, etc.) and
tier constants. These are pure data — no handlers, no side effects.
"""

from __future__ import annotations


# ── Brain Tools ───────────────────────────────────────────────
# Available to all agents (coordinator + workers)

BRAIN_TOOLS = [
    {
        "name": "brain_recall",
        "description": "Search brain memories semantically. Returns the most relevant memories for the query.",
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
                "emotion": {"type": "string", "description": "Emotion label (default neutral)", "default": "neutral"},
            },
            "required": ["content"],
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
            "Use before asking the user to paste an API key in chat, when a task needs a missing credential "
            "or a newly created skill/API integration needs a named key. This tool never reads or stores the "
            "secret value itself; the user enters the value into Vault UI."
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
            "Read or search raw stored messages from the current persistent agent thread when "
            "the durable handoff summary is too thin or an older exact detail matters. Defaults "
            "to the current session; use recent for latest turns, range for indexes, or search "
            "for literal text."
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
            "Query DB-backed workspace truth outside memories: runs, thread messages, "
            "ideas, tool calls, Domains, workspace apps, app state, and optional memory recall. "
            "Use for temporal or teammate activity questions before answering from memory alone; "
            "read activity_items first for the newest cross-source signals."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sources": {
                    "type": "array",
                    "description": (
                        "Data sources to inspect. Use ['activity'] for teammate/workspace "
                        "activity or ['all'] for the default broad set."
                    ),
                    "items": {
                        "type": "string",
                        "enum": [
                            "all",
                            "activity",
                            "runs",
                            "threads",
                            "ideas",
                            "tool_calls",
                            "domains",
                            "domain_records",
                            "domain_events",
                            "workspace_apps",
                            "app_state",
                            "memories",
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
                    "type": "string",
                    "enum": [
                        "today",
                        "yesterday",
                        "last_24h",
                        "this_week",
                        "last_7d",
                        "this_month",
                        "last_30d",
                        "custom",
                    ],
                    "default": "last_7d",
                    "description": "Relative time window. Use custom with start_at/end_at.",
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
        "name": "manage_cycle",
        "description": (
            "Create, list, update, delete, or run workspace Cycles. Use when the user asks "
            "Illo to set up or manage recurring work. Actions: 'create', 'list', "
            "'update', 'delete', 'run'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["create", "list", "update", "delete", "run"],
                    "description": "What to do: create/list/update/delete/run",
                },
                "id": {"type": "integer", "description": "Cycle id (required for update/delete/run)"},
                "name": {"type": "string", "description": "Cycle name"},
                "prompt": {"type": "string", "description": "Prompt Illo should run each cycle"},
                "schedule_expr": {
                    "type": "string",
                    "description": "5-field cron expression like '0 8 * * *'",
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

# ── Domain Tools ─────────────────────────────────────────────
# Org-wide custom databases/knowledge structures.

DOMAIN_TOOLS = [
    {
        "name": "manage_domain",
        "description": (
            "Create and maintain org-wide custom Domains: shared team databases with "
            "object types, typed records, relations, and audit events. Use for requests "
            "like creating a tracker, adding fields, recording items, querying records, "
            "linking records, or deleting/archiving domains and domain records."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
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

# ── Cortex Idea Tools ─────────────────────────────────────────
# Durable Cortex thoughts/threads. In the DB/API these are called ideas.

CORTEX_IDEA_TOOLS = [
    {
        "name": "manage_idea",
        "description": (
            "Create, list, get, update, archive, restore, or mark-read Cortex thoughts. "
            "When a created thought should start working immediately, set start_run=true "
            "or use status=queued/working so a starter message and AgentRun are created. "
            "Use this for requests about thoughts, threads, idea threads, or ideas, such as "
            "'archive this thread', 'rename this thought', 'mark this resolved', or "
            "'restore that idea'. idea_id defaults to the current Cortex thread/idea when one is bound."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
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
                    "description": "Optional first thread message for a newly created idea. Defaults to description, then title.",
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
                    "description": "Next status for create, update, or set_status.",
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
                "user_id": {"type": "string", "description": "Optional owner id for explicit thread handoff."},
                "origin": {"type": "string", "description": "Origin for create.", "default": "user_created"},
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
            "Use when the user asks to manage a project/folder/context bundle, add or remove files/repos/folders, "
            "or attach reusable project context to the current thread. Thread attachments do not require a project."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
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
            "Use after designing a small UI surface or dashboard that should remain available "
            "inside Cortex. Prefer renderer_key='generated-ui-app' and source_kind='json' with "
            "a structured generated UI spec. Use renderer_key='sandboxed-html-app' only as a "
            "custom HTML escape-hatch runtime. Recordful apps must use manage_domain first; app-local "
            "state is only for UI preferences, filters, drafts, and ephemeral interface state. "
            "New generated apps must pass the workspace app contract before they are persisted."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
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
                "app_id": {"type": "string", "description": "Workspace app id for existing-app actions."},
                "key": {"type": "string", "description": "Stable app key; generated from name when omitted."},
                "name": {"type": "string", "description": "Human-readable app name."},
                "description": {"type": "string", "description": "Short app description."},
                "renderer_key": {
                    "type": "string",
                    "default": "generated-ui-app",
                    "description": (
                        "Renderer runtime key. Use generated-ui-app for host-rendered structured UI. "
                        "Use sandboxed-html-app only for custom HTML escape hatches."
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
                        "schema_version=1, title, optional description, and views. For sandboxed html, "
                        "provide a responsive HTML/CSS/JS body or document. Canonical calls pass "
                        "manifest, visual_spec, and metadata as separate tool args; the app compiler "
                        "also normalizes wrapped generated-app envelopes when needed."
                    ),
                },
                "manifest": {
                    "type": "object",
                    "description": (
                        "Optional contract-bearing runtime manifest. The app compiler supplies safe "
                        "contract_version, app_local UI-state, and design_contract defaults for simple "
                        "generated-ui apps. Provide an explicit data_plan for Domain-backed apps, "
                        "including bindings such as {\"data_plan\":{\"mode\":\"domain\","
                        "\"bindings\":{\"todos\":{\"domain_id\":1,\"domain_slug\":\"todo-notes\","
                        "\"object_key\":\"todo_item\",\"fields\":[\"title\",\"notes\",\"completed\"],"
                        "\"operations\":[\"schema\",\"list\",\"create\",\"update\",\"archive\"]}}}} "
                        "and access them with window.illo.domain('todos')."
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
                "anchor_user_id": {
                    "type": "string",
                    "description": "Optional user id whose astre should anchor the app object.",
                },
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
    "description": "Capture the current browser viewport and optionally persist it into the thought.",
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
    "description": "Save a PNG screenshot of the current page into the thought workspace uploads area.",
    "input_schema": {
        "type": "object",
        "properties": {
            "full_page": {"type": "boolean", "default": True},
        },
    },
}

BROWSER_PRINT_PDF_TOOL = {
    "name": "browser_print_pdf",
    "description": "Export the current page as a PDF into the thought workspace uploads area.",
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
    + CORTEX_IDEA_TOOLS
    + CHAT_TOOLS
    + PROJECT_TOOLS
    + WORKSPACE_APP_TOOLS
    + EXEC_TOOLS
    + SESSION_TOOLS
    + [
        CORTEX_VISUAL_REPLY_TOOL,
        BROWSER_SESSION_OPEN_TOOL,
        BROWSER_NAVIGATE_TOOL,
        BROWSER_CLICK_TOOL,
        BROWSER_TYPE_TOOL,
        BROWSER_KEY_TOOL,
        BROWSER_BACK_TOOL,
        BROWSER_FORWARD_TOOL,
        BROWSER_NEW_TAB_TOOL,
        BROWSER_SWITCH_TAB_TOOL,
        BROWSER_CLOSE_TAB_TOOL,
        BROWSER_LIST_TABS_TOOL,
        BROWSER_WAIT_TOOL,
        BROWSER_EXTRACT_TOOL,
        BROWSER_DISCOVER_TOOL,
        BROWSER_UPLOAD_ATTACHMENT_TOOL,
        BROWSER_SNAPSHOT_TOOL,
        BROWSER_SAVE_SCREENSHOT_TOOL,
        BROWSER_PRINT_PDF_TOOL,
        BROWSER_CLOSE_TOOL,
    ]
)

# Coordinator tools = normal workspace/product capabilities plus reply and
# introspection tools. Deep planning/workers are runtime state transitions.
COORDINATOR_TOOLS = (
    BRAIN_TOOLS
    + DOMAIN_TOOLS
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
        BROWSER_SESSION_OPEN_TOOL,
        BROWSER_NAVIGATE_TOOL,
        BROWSER_CLICK_TOOL,
        BROWSER_TYPE_TOOL,
        BROWSER_KEY_TOOL,
        BROWSER_BACK_TOOL,
        BROWSER_FORWARD_TOOL,
        BROWSER_NEW_TAB_TOOL,
        BROWSER_SWITCH_TAB_TOOL,
        BROWSER_CLOSE_TAB_TOOL,
        BROWSER_LIST_TABS_TOOL,
        BROWSER_WAIT_TOOL,
        BROWSER_EXTRACT_TOOL,
        BROWSER_DISCOVER_TOOL,
        BROWSER_UPLOAD_ATTACHMENT_TOOL,
        BROWSER_SNAPSHOT_TOOL,
        BROWSER_SAVE_SCREENSHOT_TOOL,
        BROWSER_PRINT_PDF_TOOL,
        BROWSER_CLOSE_TOOL,
    ]
)

# ── Gate Constants ────────────────────────────────────────────

# Brain gate: these tool names satisfy the "brain context accessed" requirement
_BRAIN_TOOL_NAMES = frozenset({
    "brain_recall", "brain_guardrails", "brain_skills", "skill_view", "skill_asset",
    "brain_encode", "runtime_settings", "query_workspace_data",
})

# Brain gate: these tools require brain context before first use
# Destructive tools need guardrails; cortex_reply needs recall so the agent
# checks its memory before asking users questions it should already know.
_GATED_TOOL_NAMES = frozenset({
    "write_file", "edit_file", "exec_command", "run_script", "cortex_reply", "cortex_visual_reply",
})
