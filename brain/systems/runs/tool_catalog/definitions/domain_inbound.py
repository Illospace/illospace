"""Domain and inbound coordination tool schemas."""

from __future__ import annotations

from brain.systems.inbound.status import (
    STATUS_FAILED,
    STATUS_QUARANTINED,
    STATUS_REVIEW_REQUIRED,
)


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
                    "description": (
                        "Field definitions for add_object. For format='compact' query_records, data field "
                        "keys to include per record (e.g. ['status','assignee','priority','external_id']). "
                        "When omitted in compact mode, all short scalar data fields are included. Long "
                        "values are trimmed."
                    ),
                    "items": {
                        "oneOf": [
                            {"type": "object"},
                            {"type": "string"},
                        ]
                    },
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
                "format": {
                    "type": "string",
                    "enum": ["full", "compact"],
                    "default": "full",
                    "description": (
                        "Record serialization for query_records. 'compact' returns "
                        "id/object_key/title/version/updated_at plus selected short data fields - use it "
                        "for listings and sweeps so results stay complete within output budgets."
                    ),
                },
                "order": {
                    "type": "string",
                    "enum": ["updated_desc", "updated_asc"],
                    "default": "updated_desc",
                    "description": (
                        "Sort for query_records. 'updated_asc' returns stalest records first (use for "
                        "staleness sweeps)."
                    ),
                },
                "limit": {"type": "integer", "default": 50, "description": "Maximum records/events to return."},
                "cursor": {
                    "type": "string",
                    "description": "Opaque next_page token returned by query_records or events.",
                },
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
    {
        "name": "merge_chantier",
        "description": (
            "Retire a duplicate Domain-1 chantier into its canonical chantier. The operation "
            "merges typed refs into the canonical record, then sets the duplicate to state=paused "
            "with superseded_by=<canonical slug>. It is idempotent for the same already-completed "
            "merge and returns the post-write active-chantier count and digest record ids. Read both "
            "records first and supply their current versions; this does not mutate GitHub or production "
            "unless invoked in that workspace."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "duplicate_record_id": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Duplicate chantier record to pause and mark superseded.",
                },
                "canonical_record_id": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Canonical active chantier record that survives the merge.",
                },
                "expected_duplicate_version": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Current duplicate version from the pre-write read.",
                },
                "expected_canonical_version": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Current canonical version from the pre-write read.",
                },
                "reason": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Auditable explanation for selecting the canonical chantier.",
                },
            },
            "required": [
                "duplicate_record_id",
                "canonical_record_id",
                "expected_duplicate_version",
                "expected_canonical_version",
                "reason",
            ],
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
            "hosted MCP or POST /webhooks; this tool is Illo's chat-based configuration surface. "
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
                        "list_attention_events",
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
                "metadata": {"type": "object", "description": "Setup notes or structured metadata."},
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
                    "enum": [STATUS_REVIEW_REQUIRED, STATUS_QUARANTINED, STATUS_FAILED],
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


__all__ = [
    "DOMAIN_TOOLS",
    "INBOUND_TOOLS",
]
