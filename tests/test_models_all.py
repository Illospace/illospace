"""Verify the SQLAlchemy metadata exposes the current product tables."""

from brain.platform.db.base import Base
from brain.platform.db.models import *  # noqa


EXPECTED_TABLES = {
    "action_manifests",
    "agent_api_calls",
    "agent_run_artifacts",
    "agent_run_events",
    "agent_runs",
    "agent_sessions",
    "browser_pool_entries",
    "browser_sessions",
    "chat_conversation_members",
    "chat_conversation_reads",
    "chat_conversations",
    "chat_message_mentions",
    "chat_messages",
    "chat_notifications",
    "consolidation_runs",
    "cortex_events",
    "cycle_guidance",
    "cycle_output_targets",
    "cycle_revisions",
    "cycle_run_evaluations",
    "cycle_runs",
    "cycles",
    "daily_metrics",
    "domain_events",
    "domain_field_definitions",
    "domain_object_types",
    "domain_records",
    "domain_relation_types",
    "domain_relations",
    "domains",
    "edges",
    "environment_bindings",
    "environment_commands",
    "environment_services",
    "error_pipeline_runs",
    "external_agent_connection_tokens",
    "external_agent_connections",
    "external_agent_task_artifacts",
    "external_agent_task_events",
    "external_agent_tasks",
    "inbound_decision_receipts",
    "inbound_domain_projection_keys",
    "inbound_domain_projections",
    "inbound_events",
    "inbound_source_policies",
    "idea_connections",
    "idea_project_attachments",
    "idea_state_log",
    "idea_threads",
    "ideas",
    "memories",
    "memory_contradictions",
    "memory_health_log",
    "memory_reviews",
    "memory_summaries",
    "narrative_sessions",
    "notification_events",
    "object_references",
    "org_api_keys",
    "org_provider_model_mappings",
    "orgs",
    "project_narratives",
    "project_profile_access",
    "project_profiles",
    "provider_health_snapshots",
    "resource_leases",
    "retrieval_decisions",
    "retrieval_item_feedback",
    "retrieval_log",
    "retrieval_pool_stats",
    "routing_decisions",
    "routing_experiments",
    "run_target_bindings",
    "scheduler_jobs",
    "scheduler_leases",
    "scheduler_run_steps",
    "scheduler_runs",
    "secrets",
    "session_scratchpad",
    "skill_assets",
    "skill_bundle_versions",
    "skill_bundles",
    "skill_dependencies",
    "skill_executions",
    "skill_heuristics",
    "skill_installations",
    "skill_overlays",
    "skill_run_evidence",
    "skill_versions",
    "skills",
    "summary_lineage",
    "tags",
    "target_registry",
    "thread_context_submissions",
    "thread_discussion_comments",
    "user_codex_connections",
    "user_mentions",
    "users",
    "vault_access_log",
    "vault_agent_grants",
    "vault_config",
    "vault_missing_requests",
    "vault_project_bindings",
    "vault_sessions",
    "visual_blocks",
    "workspace_app_states",
    "workspace_app_versions",
    "workspace_apps",
    "workspace_pins",
    "workspace_pool_entries",
}


def _modeled_tables() -> set[str]:
    return {
        table
        for table in Base.metadata.tables
        if not table.startswith("_test_")
    }


def test_all_tables_modeled():
    missing = EXPECTED_TABLES - _modeled_tables()
    assert not missing, f"Missing models for tables: {missing}"


def test_no_extra_tables():
    extra = _modeled_tables() - EXPECTED_TABLES
    assert not extra, f"Unexpected tables registered: {extra}"


def test_table_count():
    assert len(_modeled_tables()) == len(EXPECTED_TABLES)
