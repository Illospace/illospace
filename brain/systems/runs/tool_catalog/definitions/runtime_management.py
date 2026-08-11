"""Runtime management tool schemas."""

from __future__ import annotations

from brain.systems.storage_policy import storage_policy_field_schema


HOST_CAPACITY_TOOLS = [
    {
        "name": "read_host_capacity",
        "description": (
            "Read live host disk capacity, the latest recorded workspace consumers, the "
            "active storage-policy thresholds, and recent scheduled measurements as a trend. "
            "Use this to answer how much disk is available and what is using it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 168,
                    "default": 24,
                    "description": "Maximum recent hourly measurements to return.",
                },
                "refresh_inventory": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Walk the workspace now instead of using the latest recorded inventory. "
                        "Keep false unless the caller explicitly needs a fresh inventory."
                    ),
                },
            },
        },
    },
]


WORKSPACE_RECLAMATION_TOOLS = [
    {
        "name": "manage_workspace_reclamation",
        "description": (
            "Inventory retained headless-worker workspaces with their sizes, or reclaim "
            "workspaces that are older than the active storage-policy retention window. "
            "Reclaim never touches a non-terminal run, refuses unsafe paths and incomplete "
            "inventories, re-confirms each candidate, and always reports bytes freed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["inventory", "reclaim"],
                    "default": "inventory",
                    "description": (
                        "Use inventory for a read-only measurement. Use reclaim to delete "
                        "only workspaces that the active retention policy marks reclaimable."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 200,
                    "default": 100,
                    "description": "Maximum workspace inventory records to return.",
                },
                "max_reclaims": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "default": 100,
                    "description": "Maximum eligible workspaces to reclaim in this call.",
                },
            },
            "required": ["action"],
        },
    },
]


DEPLOYMENT_TOOLS = [
    {
        "name": "manage_deployment",
        "description": (
            "Check or start the Illospace self-update flow for the running server. "
            "Use this only when an authenticated workspace user explicitly asks Illo "
            "to update, deploy, redeploy, or pull latest main for this Illospace instance. "
            "The update flow syncs origin/main, rebuilds the Compose app images, runs "
            "database migrations, and restarts runtime services through the updater sidecar "
            "when available."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["status", "start_update"],
                    "default": "status",
                    "description": "Use status to inspect availability/progress, or start_update to queue a deployment.",
                },
                "build_no_cache": {
                    "type": "boolean",
                    "default": False,
                    "description": "For start_update, rebuild app images without Docker cache when cache staleness is suspected.",
                },
                "worker_drain_timeout_seconds": {
                    "type": "integer",
                    "description": "For start_update, optional positive timeout for active worker drain before leaving old worker to finish.",
                },
            },
            "required": ["action"],
        },
    },
    {
        "name": "manage_runtime_services",
        "description": (
            "List, inspect, or restart known Illospace runtime services through the host controller. "
            "Use list before choosing targets. Use restart when an authenticated workspace user asks "
            "Illo to restart one, many, or all services in this Illospace installation."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "status", "restart"],
                    "default": "list",
                    "description": "Use list/status to inspect service management, or restart to queue a service restart.",
                },
                "services": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "For restart, one or more service ids returned by list, or all.",
                },
            },
            "required": ["action"],
        },
    },
]

RUNTIME_PREFERENCE_TOOLS = [
    {
        "name": "manage_runtime_preferences",
        "description": (
            "Inspect or persist an explicitly supported durable workspace preference. Use "
            "action='set', setting='display_timezone', and an IANA timezone (ET/Eastern are "
            "accepted as America/New_York) only when a human explicitly asks for a durable "
            "timezone presentation preference. A successful write returns a durable write "
            "receipt and the exact confirmation to give the human. Unsupported settings return "
            "an honest no-write response; never promise to remember a preference without "
            "status='saved'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["get", "set"],
                    "default": "get",
                    "description": "Inspect supported preferences or persist one known setting.",
                },
                "setting": {
                    "type": "string",
                    "description": (
                        "Concrete setting key. The only writable presentation setting is "
                        "display_timezone."
                    ),
                },
                "value": {
                    "type": "string",
                    "description": "Value for action=set; an IANA timezone or ET/Eastern alias.",
                },
            },
        },
    },
    {
        "name": "manage_storage_policy",
        "description": (
            "Read or revise the installation-wide storage policy without a deploy. "
            "Use get for the active retention windows, capacity thresholds, and automatic "
            "reclamation permission; history for the audit trail; update with a rationale "
            "to append a new active revision; and revert with a prior policy_id and rationale."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["get", "update", "history", "revert"],
                    "default": "get",
                },
                "policy_id": {
                    "type": "integer",
                    "description": "Prior policy revision id required for revert.",
                },
                **storage_policy_field_schema(),
                "rationale": {
                    "type": "string",
                    "description": "Required reason for update and revert actions.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 200,
                    "default": 50,
                    "description": "Maximum revisions returned by history.",
                },
            },
        },
    },
]

WORKSPACE_TOOL_TOOLS = [
    {
        "name": "manage_workspace_tools",
        "description": (
            "Inspect, install, and health-check opt-in workspace tool bundles for this team. "
            "Use this when a skill needs external tooling that should persist for the workspace "
            "without baking that tool into every Illospace install. Use catalog/list/status/check "
            "before install unless the user explicitly asked to install a known bundle."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "help",
                        "schema",
                        "catalog",
                        "list",
                        "status",
                        "install",
                        "check",
                        "get_config",
                        "set_config",
                    ],
                    "default": "list",
                    "description": (
                        "Use catalog to see installable bundles, list/status to inspect current workspace state, "
                        "install to queue an approved bundle install, check to refresh persisted health, "
                        "and get_config/set_config for the originating user's non-secret tool preferences "
                        "and credential references."
                    ),
                },
                "operation": {
                    "type": "string",
                    "description": "Optional operation name to inspect when action is help or schema.",
                },
                "bundle_id": {
                    "type": "string",
                    "description": (
                        "Tool bundle id, e.g. aws-diagrams. Required for install, check, get_config, "
                        "and set_config; optional for status."
                    ),
                },
                "preferences": {
                    "type": "object",
                    "description": "Non-secret per-user preferences for set_config, such as default profile or flags.",
                },
                "credential_refs": {
                    "type": "object",
                    "description": (
                        "Per-user credential references for set_config. Store references such as provider "
                        "connection ids or Vault key names, never raw secret values."
                    ),
                },
            },
            "required": ["action"],
        },
    },
]


__all__ = [
    "DEPLOYMENT_TOOLS",
    "HOST_CAPACITY_TOOLS",
    "RUNTIME_PREFERENCE_TOOLS",
    "WORKSPACE_TOOL_TOOLS",
]
