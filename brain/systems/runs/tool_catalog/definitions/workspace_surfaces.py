"""Project context and workspace app tool schemas."""

from __future__ import annotations


# ── Project Tools ─────────────────────────────────────────────
# Durable reusable folders/context bundles for Cortex threads.

PROJECT_TOOLS = [
    {
        "name": "manage_project",
        "description": (
            "Create, list, update, archive, attach, and maintain Cortex Project Context profiles. "
            "This is the action tool for managing project/folder/context bundles, adding or removing "
            "files/repos/folders/docs, or attaching reusable project context to the current thread. "
            "Use draft_status to inspect the current run's materialized Project draft workspace and "
            "plan_publish to preview draft-to-source publish operations without mutating Project roots. "
            "Use refresh_draft_from_root to explicitly apply latest root changes into untouched draft files. "
            "Use publish_draft to publish local Project draft changes back to root; conflicts return guidance for the agent to reconcile root and draft before retrying. "
            "Use root_versions, preview_root_version, and restore_root_version to inspect, preview, or roll back local Project root history. "
            "Use search_files to find files in visible Projects without loading everything, and mount_reference to expose selected files or folders from another Project as read-only reference mounts. "
            "Projects are context boundaries, not permission boundaries; mounted references can then be inspected with normal read_file/list_files/search_files tools. "
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
                        "draft_status",
                        "plan_publish",
                        "refresh_draft_from_root",
                        "publish_draft",
                        "root_versions",
                        "preview_root_version",
                        "restore_root_version",
                        "search_files",
                        "mount_reference",
                    ],
                    "description": (
                        "The project operation to run. delete archives the project profile; "
                        "draft_status and plan_publish are read-only draft inspection actions; "
                        "refresh_draft_from_root mutates only the thread draft workspace by copying latest root files into untouched draft paths; "
                        "publish_draft mutates supported Project roots after conflict checkpoints are resolved; "
                        "preview_root_version is read-only; restore_root_version mutates a local Project root back to a captured version; "
                        "search_files searches visible Projects without loading full files; mount_reference exposes selected Project files/folders as read-only reference mounts."
                    ),
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
                    "description": "Complete ordered resource id list for reorder_resources, or selected resource ids for publish_draft.",
                    "items": {"type": "string"},
                },
                "publish_paths": {
                    "type": "array",
                    "description": "Optional relative, mounted, draft, or target paths to publish for publish_draft.",
                    "items": {"type": "string"},
                },
                "path": {
                    "type": "string",
                    "description": "Optional single path filter for publish_draft.",
                },
                "query": {
                    "type": "string",
                    "description": "Search text for action='search_files' across visible Projects; returns matching paths without loading every file.",
                },
                "limit": {
                    "type": "integer",
                    "default": 50,
                    "description": "Maximum search results to return for action='search_files', or maximum glob-selected mounts for action='mount_reference'.",
                },
                "paths": {
                    "type": "array",
                    "description": "Optional file or folder paths to constrain action='search_files', or selected paths to expose with action='mount_reference'.",
                    "items": {"type": "string"},
                },
                "glob": {
                    "type": "string",
                    "description": "Optional glob filter for action='search_files' or action='mount_reference' path selection, e.g. '**/*.md'.",
                },
                "mount_path": {
                    "type": "string",
                    "description": "Workspace mount path for action='mount_reference'; exposes selected files/folders from another Project as a read-only reference mount.",
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
                "version_id": {
                    "type": "string",
                    "description": "Captured Project root version id for preview_root_version or restore_root_version.",
                },
                "branch_name": {
                    "type": "string",
                    "description": "Optional branch name for repo-backed publish_draft operations.",
                },
                "commit_message": {
                    "type": "string",
                    "description": "Commit message for repo-backed publish_draft operations.",
                },
                "check_upstream": {
                    "type": "boolean",
                    "default": True,
                    "description": "When true, repo-backed publish_draft checks origin/base_branch before committing and blocks overlapping upstream changes.",
                },
                "base_branch": {
                    "type": "string",
                    "description": "Base branch for repo-backed upstream freshness checks, defaulting to main.",
                },
                "push": {
                    "type": "boolean",
                    "default": False,
                    "description": "When true, push the repo publish branch after committing.",
                },
                "create_pr": {
                    "type": "boolean",
                    "default": False,
                    "description": "When true, attempt to create a pull request for the pushed repo publish branch.",
                },
                "pr_title": {"type": "string", "description": "Optional pull request title for repo publishing."},
                "pr_body": {"type": "string", "description": "Optional pull request body for repo publishing."},
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
            "Create, list, update, archive, persist state, and inspect collaborative events for generated workspace apps. "
            "This is the action tool to create or change a persistent programmable UI surface or dashboard "
            "inside Cortex. Default new on-demand software to renderer_key='app-capsule' and source_kind='html': "
            "a full-code single-document HTML/CSS/JS app with a capability manifest. Use generated-ui-app/json "
            "only for legacy structured app edits or when the user explicitly asks for that renderer. Use "
            "sandboxed-html-app/html only for existing legacy sandboxed HTML apps. "
            "For list/get discovery, keep include_archived=false unless the user explicitly asks to inspect archived apps; "
            "archived apps are not candidates for new build/create requests. "
            "Use action='restore' only when the user explicitly asks to restore an archived app; for build/create "
            "requests, create a new app or update an active app instead of resurrecting archived drafts. "
            "Durable generated or user data should be exposed through manifest data capabilities, usually "
            "Domain bindings that Illo creates or attaches behind the scenes. App-local state is only for "
            "UI preferences, filters, drafts, and ephemeral interface state. "
            "For collaborative thread artifacts, use manifest.collaboration plus window.illo.collab in the app, "
            "then use get_collaboration/list_events/append_event to inspect or write structured team interaction events. "
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
                        "get_collaboration",
                        "list_events",
                        "append_event",
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
                    "default": "app-capsule",
                    "description": (
                        "Renderer runtime key. Use app-capsule for new full-code workspace apps. "
                        "Use generated-ui-app only for legacy structured UI edits, and sandboxed-html-app "
                        "only for existing legacy sandboxed HTML apps."
                    ),
                },
                "source_kind": {
                    "type": "string",
                    "default": "html",
                    "description": "Generated source format. Use html for app-capsule apps; json only for legacy generated-ui-app specs.",
                },
                "source_code": {
                    "type": "string",
                    "description": (
                        "Generated app source. For app-capsule, provide a responsive single-document HTML/CSS/JS app "
                        "that uses the runtime bridge: window.illo.data(alias), window.illo.state.get/set/update, "
                        "window.illo.collab.get/state/events/event/subscribe for team interaction artifacts, "
                        "and window.illo.actions.run(actionKey, payload). For generated-ui-app legacy edits, provide "
                        "a JSON string with schema_version=1, title, optional description, optional top-level actions "
                        "referencing manifest.actions, and views. "
                        "Canonical calls pass "
                        "manifest, visual_spec, and metadata as separate tool args; the app compiler "
                        "also normalizes wrapped generated-app envelopes when needed."
                    ),
                },
                "manifest": {
                    "type": "object",
                    "description": (
                        "Optional contract-bearing runtime manifest. The app compiler supplies safe "
                        "contract_version, capability data_plan, and design_contract defaults for new app-capsule apps. "
                        "For data-backed capsules, provide explicit data_plan.mode='capability' bindings. "
                        "and include the strict design contract shape "
                        "{\"design_contract\":{\"kit\":\"constellation-app-kit\","
                        "\"theme_modes\":[\"dark\",\"light\"]}}. Do not use alternate keys like "
                        "system, design_system, uses_app_kit_classes, or supports_color_scheme in "
                        "manifest.design_contract; put descriptive labels in metadata instead. "
                        "Domain records expose a virtual top-level title; bindings may include \"title\" "
                        "for display/card labels even when the object's field definitions do not contain "
                        "a data field named title. "
                        "Use bindings such as {\"data_plan\":{\"mode\":\"capability\","
                        "\"bindings\":{\"todos\":{\"domain_id\":1,\"domain_slug\":\"todo-notes\","
                        "\"kind\":\"domain\",\"object_key\":\"todo_item\",\"fields\":[\"title\",\"notes\",\"completed\"],"
                        "\"operations\":[\"schema\",\"list\",\"query\",\"create\",\"update\",\"archive\","
                        "\"aggregate\"]}}}} and access them with window.illo.data('todos'). "
                        "Use system bindings such as {\"kind\":\"system\",\"source\":\"threads\","
                        "\"operations\":[\"schema\",\"list\",\"query\",\"get\",\"aggregate\"]} for scoped workspace reads. "
                        "Domain binding operations are exact SDK method names; do not use capability labels "
                        "such as read, write, or crud in manifest.data_plan.bindings.*.operations. "
                        "The app-capsule runtime exposes manifest-bound data CRUD/read operations, polling-backed "
                        "subscribe, app-local state, collaborative artifact events through window.illo.collab, "
                        "and window.illo.actions.run(actionKey, payload) for "
                        "manifest-declared server-side actions. "
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
                "payload": {"type": "object", "description": "Collaborative event payload for append_event."},
                "state_patch": {
                    "type": "object",
                    "description": "Optional shallow collaborative state patch for append_event; omit to use the manifest-declared reducer.",
                },
                "event_type": {
                    "type": "string",
                    "description": "Collaborative event type such as vote.cast, note.add, status.change, or ask_illo.",
                },
                "idempotency_key": {
                    "type": "string",
                    "description": "Optional stable key to prevent duplicate event appends.",
                },
                "expected_state_version": {
                    "type": "integer",
                    "description": "Optional optimistic concurrency guard for append_event.",
                },
                "after_event_id": {
                    "type": "integer",
                    "description": "Return collaborative events after this id for get_collaboration/list_events.",
                },
                "limit": {
                    "type": "integer",
                    "default": 50,
                    "description": "Maximum collaborative events to return for get_collaboration/list_events.",
                },
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


__all__ = [
    "PROJECT_TOOLS",
    "WORKSPACE_APP_TOOLS",
]
