"""Verify the SQLAlchemy metadata exposes the current product tables."""

from brain.platform.db.base import Base
from brain.platform.db.models import *  # noqa


EXPECTED_TABLES = {
    "action_manifests",
    "agency_approvals",
    "agency_budget_events",
    "agency_budgets",
    "agency_candidates",
    "agency_decisions",
    "agent_api_calls",
    "agent_run_artifacts",
    "agent_run_events",
    "agent_runs",
    "agent_sessions",
    "api_key_shares",
    "brain_prompts",
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
    "critic_reviews",
    "cron_jobs",
    "cycle_runs",
    "cycles",
    "daily_metrics",
    "delegation_quality",
    "domain_events",
    "domain_field_definitions",
    "domain_object_types",
    "domain_records",
    "domain_relation_types",
    "domain_relations",
    "domains",
    "edges",
    "emotional_snapshots",
    "environment_bindings",
    "environment_commands",
    "environment_services",
    "error_pipeline_runs",
    "execution_outcomes",
    "habit_executions",
    "habit_versions",
    "idea_connections",
    "idea_project_attachments",
    "idea_state_log",
    "idea_threads",
    "ideas",
    "learning_examples",
    "learning_signals",
    "memories",
    "memory_contradictions",
    "memory_health_log",
    "memory_reviews",
    "memory_summaries",
    "narrative_sessions",
    "notification_events",
    "operating_params",
    "org_api_keys",
    "org_provider_model_mappings",
    "orgs",
    "policy_promotions",
    "policy_update_candidates",
    "practice_runs",
    "project_narratives",
    "project_profiles",
    "prompt_template_outcomes",
    "prompt_templates",
    "provider_health_snapshots",
    "reflections",
    "resource_leases",
    "retrieval_decisions",
    "retrieval_item_feedback",
    "retrieval_log",
    "retrieval_pool_stats",
    "routing_decisions",
    "routing_experiments",
    "run_genomes",
    "run_habits",
    "run_log",
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
    "tasks",
    "trajectory_eval_cases",
    "user_api_keys",
    "user_mentions",
    "users",
    "vault_access_log",
    "vault_agent_grants",
    "vault_config",
    "vault_missing_requests",
    "vault_project_bindings",
    "vault_sessions",
    "vault_shares",
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
