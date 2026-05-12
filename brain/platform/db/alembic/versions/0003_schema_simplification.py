"""Consolidated legacy schema simplification.

Revision ID: 0003_schema_simplification
Revises: 0002_idea_timestamps_timestamptz
Create Date: 2026-05-12
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Computed,
    Date,
    DateTime,
    Double,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    inspect,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from pgvector.sqlalchemy import Vector


revision = "0003_schema_simplification"
down_revision = "0002_idea_timestamps_timestamptz"
branch_labels = None
depends_on = None


DROP_TABLES = (
    "cron_jobs",
    "run_log",
    "tasks",
    "learning_examples",
    "practice_runs",
    "policy_update_candidates",
    "trajectory_eval_cases",
    "learning_signals",
    "policy_promotions",
    "run_genomes",
    "agency_budget_events",
    "agency_approvals",
    "agency_decisions",
    "agency_budgets",
    "agency_candidates",
    "habit_executions",
    "habit_versions",
    "run_habits",
    "execution_outcomes",
    "critic_reviews",
    "delegation_quality",
    "operating_params",
    "reflections",
    "prompt_template_outcomes",
    "prompt_templates",
    "brain_prompts",
    "emotional_snapshots",
)


def _table_exists(table_name: str) -> bool:
    return table_name in set(inspect(op.get_bind()).get_table_names(schema="public"))


def _column_exists(table_name: str, column_name: str) -> bool:
    if not _table_exists(table_name):
        return False
    return column_name in {
        column["name"]
        for column in inspect(op.get_bind()).get_columns(table_name, schema="public")
    }


def _table_count(table_name: str) -> int:
    value = op.get_bind().execute(text(f'SELECT count(*) FROM "{table_name}"')).scalar_one()
    return int(value or 0)


def _drop_column_if_exists(table_name: str, column_name: str) -> None:
    if _column_exists(table_name, column_name):
        op.drop_column(table_name, column_name)


def _drop_run_habits_active_version_fk() -> None:
    if not _table_exists("run_habits"):
        return
    inspector = inspect(op.get_bind())
    for fk in inspector.get_foreign_keys("run_habits", schema="public"):
        if fk.get("constrained_columns") == ["active_version_id"] and fk.get("name"):
            op.drop_constraint(fk["name"], "run_habits", type_="foreignkey")


def upgrade() -> None:
    non_empty = {
        table: _table_count(table)
        for table in DROP_TABLES
        if _table_exists(table) and _table_count(table) > 0
    }
    if non_empty:
        details = ", ".join(f"{table}={count}" for table, count in sorted(non_empty.items()))
        raise RuntimeError(
            "Refusing to simplify schema because legacy tables contain data. "
            f"Export or migrate these rows before retrying: {details}"
        )

    _drop_run_habits_active_version_fk()

    for table in DROP_TABLES:
        if _table_exists(table):
            op.drop_table(table)

    for column_name in (
        "emotional_embedding",
        "emotion_valence",
        "emotion_arousal",
        "emotion_label",
    ):
        _drop_column_if_exists("memories", column_name)

    for column_name in (
        "avg_valence",
        "avg_arousal",
        "valence_trend",
        "frustration_count",
        "joy_count",
        "emotional_snapshots_count",
    ):
        _drop_column_if_exists("daily_metrics", column_name)

    _drop_column_if_exists("skill_executions", "operator_emotion")


def downgrade() -> None:
    _downgrade_0013()
    _downgrade_0012()
    _downgrade_0011()
    _downgrade_0010()
    _downgrade_0009()
    _downgrade_0008()
    _downgrade_0007()
    _downgrade_0006()
    _downgrade_0005()
    _downgrade_0004()
    _downgrade_0003()

def _downgrade_0003() -> None:
    # The old runtime no longer uses this table. Recreate only the legacy shape
    # so a downgrade can satisfy older code during rollback.
    from sqlalchemy import Boolean, Column, DateTime, Integer, Text
    from sqlalchemy.dialects.postgresql import ARRAY

    op.create_table(
        "cron_jobs",
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("name", Text, nullable=False, unique=True),
        Column("description", Text, nullable=True),
        Column("schedule", Text, nullable=False),
        Column("script_path", Text, nullable=False),
        Column("command", Text, nullable=True),
        Column("phases", ARRAY(Text), nullable=True),
        Column("enabled", Boolean, server_default=text("TRUE")),
        Column("created_by", Text, server_default=text("'user'")),
        Column("created_at", DateTime, server_default=text("now()")),
    )


def _downgrade_0004() -> None:
    # Recreate the legacy shape only for rollback compatibility. The runtime now
    # stores CLI runs in agent_runs and replay payloads in agent_run_artifacts.
    from sqlalchemy import Column, DateTime, Integer, String, Text
    from sqlalchemy.dialects.postgresql import ARRAY, JSONB

    op.create_table(
        "run_log",
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("task_summary", Text, nullable=False),
        Column("task_type", String(50), nullable=False),
        Column("template_used", String(50), nullable=True),
        Column("skill_name", String(100), nullable=True),
        Column("memories_injected", ARRAY(Integer), server_default=text("'{}'")),
        Column("guardrails_injected", ARRAY(Text), server_default=text("'{}'")),
        Column("similar_past_ids", ARRAY(Integer), server_default=text("'{}'")),
        Column("session_key", String(200), nullable=True),
        Column("model", String(50), nullable=False, server_default=text("'medium'")),
        Column("thinking_level", String(20), server_default=text("'low'")),
        Column("runed_at", DateTime, nullable=False, server_default=text("NOW()")),
        Column("completed_at", DateTime, nullable=True),
        Column("outcome", String(20), nullable=True),
        Column("outcome_notes", Text, nullable=True),
        Column(
            "duration_s",
            Integer,
            Computed("EXTRACT(EPOCH FROM (completed_at - runed_at))::INTEGER"),
            nullable=True,
        ),
        Column("prompt_hash", String(64), nullable=True),
        Column("payload_json", JSONB, nullable=True),
    )


def _downgrade_0005() -> None:
    # Recreate the legacy shape only for rollback compatibility. The runtime now
    # uses agent_runs.input_message as the task history.
    from sqlalchemy import Column, DateTime, Double, Integer, String, Text
    from sqlalchemy.dialects.postgresql import ARRAY, JSONB

    try:
        from pgvector.sqlalchemy import Vector
    except ImportError:  # pragma: no cover
        Vector = Text

    op.create_table(
        "tasks",
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("description", Text, nullable=False),
        Column("task_type", String(30), nullable=True),
        Column("complexity_estimate", Integer, nullable=True),
        Column("strategy_chosen", String(30), nullable=True),
        Column("plan", JSONB, nullable=True),
        Column("similar_past_tasks", ARRAY(Integer), server_default=text("'{}'")),
        Column("memory_ids_recalled", ARRAY(Integer), server_default=text("'{}'")),
        Column("guardrails", ARRAY(Text), server_default=text("'{}'")),
        Column("skills_used", ARRAY(Integer), server_default=text("'{}'")),
        Column("started_at", DateTime, server_default=text("NOW()"), nullable=True),
        Column("completed_at", DateTime, nullable=True),
        Column("duration_sec", Double, nullable=True),
        Column("outcome", String(20), nullable=True),
        Column("outcome_details", Text, nullable=True),
        Column("operator_satisfaction", Double, nullable=True),
        Column("feedback_notes", Text, nullable=True),
        Column("embedding", Vector(2000), nullable=True),
        Column("created_at", DateTime, nullable=False, server_default=text("now()")),
    )


def _downgrade_0006() -> None:
    op.create_table(
        "run_genomes",
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("run_id", Integer, ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False),
        Column("user_id", UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        Column("org_id", UUID(as_uuid=False), ForeignKey("orgs.id", ondelete="SET NULL"), nullable=True),
        Column("visibility", String(20), nullable=False, server_default=text("'private'")),
        Column("genome_hash", String(64), nullable=False),
        Column("task_family", String(80), nullable=False),
        Column("target_family", String(80), nullable=False),
        Column("context_profile", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        Column("strategy_name", Text, nullable=True),
        Column("skill_name", Text, nullable=True),
        Column("model_mix", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        Column("tool_mix", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        Column("retrieval_profile", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        Column("verifier_outcome", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        Column("contract_type", String(40), nullable=False),
        Column("token_cost_bucket", String(20), nullable=False),
        Column("latency_bucket", String(20), nullable=False),
        Column("success", Boolean, nullable=False, server_default=text("FALSE")),
        Column("rework_required", Boolean, nullable=False, server_default=text("FALSE")),
        Column("satisfaction_proxy", Float, nullable=False),
        Column("evidence_status", String(30), nullable=False, server_default=text("'unverified'")),
        Column("learning_outcome", String(20), nullable=False, server_default=text("'neutral'")),
        Column("evidence_gate", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        Column("created_at", DateTime, server_default=text("now()")),
        UniqueConstraint("run_id", name="uq_run_genomes_run_id"),
    )
    op.create_index("ix_run_genomes_org_created", "run_genomes", ["org_id", "created_at"])
    op.create_index("ix_run_genomes_evidence_status", "run_genomes", ["evidence_status"])
    op.create_index("ix_run_genomes_genome_hash", "run_genomes", ["genome_hash"])

    op.create_table(
        "policy_promotions",
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("promotion_type", String(50), nullable=False),
        Column("source_kind", String(50), nullable=False),
        Column("user_id", UUID(as_uuid=False), ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        Column("org_id", UUID(as_uuid=False), ForeignKey("orgs.id", ondelete="SET NULL"), nullable=True),
        Column("visibility", String(20), nullable=False, server_default=text("'private'")),
        Column("source_refs", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        Column("policy_payload", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        Column("status", String(20), nullable=False, server_default=text("'recommended'")),
        Column("evidence", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        Column("version", Integer, nullable=False, server_default=text("1")),
        Column("shadow_metrics", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        Column("activated_at", DateTime, nullable=True),
        Column("rolled_back_at", DateTime, nullable=True),
        Column("demoted_at", DateTime, nullable=True),
        Column("demotion_reason", Text, nullable=True),
        Column("explicit_global_promotion", Boolean, nullable=False, server_default=text("FALSE")),
        Column("reviewer_id", UUID(as_uuid=False), ForeignKey("users.id"), nullable=True),
        Column("created_at", DateTime, server_default=text("now()")),
    )
    op.create_index("ix_policy_promotions_type_status_version", "policy_promotions", ["promotion_type", "status", "version"])
    op.create_index("ix_policy_promotions_source_kind", "policy_promotions", ["source_kind"])
    op.create_index("ix_policy_promotions_org_type_status", "policy_promotions", ["org_id", "promotion_type", "status"])

    op.create_table(
        "learning_examples",
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("run_id", Integer, ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True),
        Column("genome_id", Integer, ForeignKey("run_genomes.id", ondelete="SET NULL"), nullable=True),
        Column("user_id", UUID(as_uuid=False), ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        Column("org_id", UUID(as_uuid=False), ForeignKey("orgs.id", ondelete="SET NULL"), nullable=True),
        Column("visibility", String(20), nullable=False, server_default=text("'private'")),
        Column("example_type", String(30), nullable=False),
        Column("evidence_status", String(30), nullable=False),
        Column("task_family", String(80), nullable=False),
        Column("target_family", String(80), nullable=False),
        Column("skill_name", Text, nullable=True),
        Column("lesson", Text, nullable=False),
        Column("evidence", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        Column("signals", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        Column("created_at", DateTime, server_default=text("now()")),
    )
    op.create_index("ix_learning_examples_org_type_created", "learning_examples", ["org_id", "example_type", "created_at"])
    op.create_index("ix_learning_examples_run_id", "learning_examples", ["run_id"])
    op.create_index("ix_learning_examples_skill", "learning_examples", ["skill_name"])

    op.create_table(
        "practice_runs",
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("origin_skill_name", Text, nullable=False),
        Column("user_id", UUID(as_uuid=False), ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        Column("org_id", UUID(as_uuid=False), ForeignKey("orgs.id", ondelete="SET NULL"), nullable=True),
        Column("visibility", String(20), nullable=False, server_default=text("'private'")),
        Column("origin_policy_promotion_id", Integer, ForeignKey("policy_promotions.id", ondelete="SET NULL"), nullable=True),
        Column("synthesized_task", Text, nullable=False),
        Column("isolation_mode", String(30), nullable=False),
        Column("workspace_template", Text, nullable=False),
        Column("cost_budget", Float, nullable=False),
        Column("run_status", String(20), nullable=False, server_default=text("'queued'")),
        Column("run_id", Integer, ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True),
        Column("outcome", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        Column("score", Float, nullable=True),
        Column("touched_production", Boolean, nullable=False, server_default=text("FALSE")),
        Column("created_at", DateTime, server_default=text("now()")),
    )

    op.create_table(
        "learning_signals",
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("signal_digest", String(64), nullable=False, unique=True),
        Column("signal_type", String(60), nullable=False),
        Column("status", String(30), nullable=False, server_default=text("'recorded'")),
        Column("review_status", String(30), nullable=False, server_default=text("'unreviewed'")),
        Column("source_run_id", Integer, ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True),
        Column("trace_id", Text, nullable=True),
        Column("trajectory_digest", String(64), nullable=True),
        Column("context_pack_digest", String(64), nullable=True),
        Column("skill_effective_digest", String(128), nullable=True),
        Column("user_id", UUID(as_uuid=False), ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        Column("org_id", UUID(as_uuid=False), ForeignKey("orgs.id", ondelete="SET NULL"), nullable=True),
        Column("visibility", String(20), nullable=False, server_default=text("'private'")),
        Column("outcome_label", String(30), nullable=True),
        Column("label_confidence", Float, nullable=True),
        Column("payload", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        Column("evidence", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        Column("applied_at", DateTime(timezone=True), nullable=True),
        Column("rolled_back_at", DateTime(timezone=True), nullable=True),
        Column("created_at", DateTime, server_default=text("now()")),
    )
    op.create_index("ix_learning_signals_org_type_created", "learning_signals", ["org_id", "signal_type", "created_at"])
    op.create_index("ix_learning_signals_run", "learning_signals", ["source_run_id"])
    op.create_index("ix_learning_signals_skill_digest", "learning_signals", ["skill_effective_digest"])
    op.create_index("ix_learning_signals_status_review", "learning_signals", ["status", "review_status"])

    op.create_table(
        "trajectory_eval_cases",
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("eval_digest", String(64), nullable=False, unique=True),
        Column("schema_version", Integer, nullable=False, server_default=text("1")),
        Column("redaction_mode", String(30), nullable=False, server_default=text("'eval'")),
        Column("status", String(30), nullable=False, server_default=text("'active'")),
        Column("source_run_id", Integer, ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True),
        Column("trace_id", Text, nullable=True),
        Column("trajectory_digest", String(64), nullable=True),
        Column("context_pack_digest", String(64), nullable=True),
        Column("skill_effective_digest", String(128), nullable=True),
        Column("user_id", UUID(as_uuid=False), ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        Column("org_id", UUID(as_uuid=False), ForeignKey("orgs.id", ondelete="SET NULL"), nullable=True),
        Column("visibility", String(20), nullable=False, server_default=text("'private'")),
        Column("payload", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        Column("quality", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        Column("created_at", DateTime, server_default=text("now()")),
    )
    op.create_index("ix_trajectory_eval_cases_org_status_created", "trajectory_eval_cases", ["org_id", "status", "created_at"])
    op.create_index("ix_trajectory_eval_cases_run", "trajectory_eval_cases", ["source_run_id"])
    op.create_index("ix_trajectory_eval_cases_skill_digest", "trajectory_eval_cases", ["skill_effective_digest"])

    op.create_table(
        "policy_update_candidates",
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("candidate_digest", String(64), nullable=False, unique=True),
        Column("candidate_type", String(60), nullable=False),
        Column("status", String(30), nullable=False, server_default=text("'proposed'")),
        Column("review_status", String(30), nullable=False, server_default=text("'unreviewed'")),
        Column("user_id", UUID(as_uuid=False), ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        Column("org_id", UUID(as_uuid=False), ForeignKey("orgs.id", ondelete="SET NULL"), nullable=True),
        Column("visibility", String(20), nullable=False, server_default=text("'private'")),
        Column("source_signal_ids", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
        Column("policy_payload", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        Column("evaluation_payload", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        Column("applied_at", DateTime(timezone=True), nullable=True),
        Column("rolled_back_at", DateTime(timezone=True), nullable=True),
        Column("created_at", DateTime, server_default=text("now()")),
    )
    op.create_index("ix_policy_update_candidates_org_type_status", "policy_update_candidates", ["org_id", "candidate_type", "status"])
    op.create_index("ix_policy_update_candidates_review", "policy_update_candidates", ["review_status"])


def _downgrade_0007() -> None:
    op.create_table(
        "agency_candidates",
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("candidate_key", String(255), nullable=False),
        Column("drive_type", String(40), nullable=False),
        Column("source_type", String(80), nullable=False),
        Column("source_refs", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
        Column("org_id", UUID(as_uuid=False), ForeignKey("orgs.id", ondelete="CASCADE"), nullable=True),
        Column("user_id", UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=True),
        Column("target_binding_id", String(120), nullable=True),
        Column("proposal_kind", String(80), nullable=False),
        Column("proposed_run_payload", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        Column("risk_class", String(20), nullable=False, server_default=text("'low'")),
        Column("reversibility_class", String(20), nullable=False, server_default=text("'read_only'")),
        Column("expected_value", Float, nullable=False, server_default=text("0.0")),
        Column("novelty_score", Float, nullable=False, server_default=text("0.0")),
        Column("urgency_score", Float, nullable=False, server_default=text("0.0")),
        Column("estimated_cost", Float, nullable=False, server_default=text("0.0")),
        Column("estimated_tokens", Integer, nullable=False, server_default=text("0")),
        Column("status", String(24), nullable=False, server_default=text("'proposed'")),
        Column("suppression_until", DateTime(timezone=True), nullable=True),
        Column("expires_at", DateTime(timezone=True), nullable=True),
        Column("created_at", DateTime, server_default=text("now()")),
        Column("updated_at", DateTime, server_default=text("now()")),
        UniqueConstraint("candidate_key", name="uq_agency_candidates_candidate_key"),
        CheckConstraint(
            "status IN ('proposed', 'approved', 'rejected', 'expired', 'suppressed', 'auto_executed')",
            name="ck_agency_candidates_status",
        ),
    )
    op.create_index("ix_agency_candidates_org_id", "agency_candidates", ["org_id"])
    op.create_index("ix_agency_candidates_user_id", "agency_candidates", ["user_id"])
    op.create_index("ix_agency_candidates_target_binding_id", "agency_candidates", ["target_binding_id"])

    op.create_table(
        "agency_budgets",
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("scope_type", String(20), nullable=False),
        Column("scope_id", String(120), nullable=False),
        Column("drive_type", String(40), nullable=True),
        Column("window_start", DateTime(timezone=True), nullable=False),
        Column("window_end", DateTime(timezone=True), nullable=False),
        Column("max_candidates", Integer, nullable=False, server_default=text("0")),
        Column("max_auto_exec", Integer, nullable=False, server_default=text("0")),
        Column("max_estimated_cost", Float, nullable=False, server_default=text("0.0")),
        Column("max_estimated_tokens", Integer, nullable=False, server_default=text("0")),
        Column("require_review_above_risk", String(20), nullable=False, server_default=text("'medium'")),
        Column("auto_execute_enabled", Boolean, nullable=False, server_default=text("FALSE")),
        Column("cooldown_hours", Integer, nullable=False, server_default=text("24")),
        Column("consumed_candidates", Integer, nullable=False, server_default=text("0")),
        Column("consumed_auto_exec", Integer, nullable=False, server_default=text("0")),
        Column("consumed_cost", Float, nullable=False, server_default=text("0.0")),
        Column("consumed_tokens", Integer, nullable=False, server_default=text("0")),
        Column("active", Boolean, nullable=False, server_default=text("TRUE")),
        UniqueConstraint(
            "scope_type", "scope_id", "drive_type", "window_start",
            name="uq_agency_budgets_scope_window",
        ),
    )
    op.create_index("ix_agency_budgets_scope_id", "agency_budgets", ["scope_id"])

    op.create_table(
        "agency_decisions",
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("candidate_id", Integer, ForeignKey("agency_candidates.id", ondelete="CASCADE"), nullable=False),
        Column("decision", String(32), nullable=False),
        Column("actor_type", String(32), nullable=False, server_default=text("'system'")),
        Column("actor_id", String(120), nullable=True),
        Column("reason_code", String(80), nullable=False),
        Column("reason_text", Text, nullable=False),
        Column("policy_snapshot", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        Column("budget_snapshot", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        Column("scheduler_run_id", Integer, ForeignKey("scheduler_runs.id", ondelete="SET NULL"), nullable=True),
        Column("run_id", Integer, ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True),
        Column("created_at", DateTime, server_default=text("now()")),
    )
    op.create_index("ix_agency_decisions_candidate_id", "agency_decisions", ["candidate_id"])
    op.create_index("ix_agency_decisions_scheduler_run_id", "agency_decisions", ["scheduler_run_id"])
    op.create_index("ix_agency_decisions_run_id", "agency_decisions", ["run_id"])

    op.create_table(
        "agency_approvals",
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("candidate_id", Integer, ForeignKey("agency_candidates.id", ondelete="CASCADE"), nullable=False),
        Column("actor_id", String(120), nullable=False),
        Column("actor_role", String(40), nullable=False),
        Column("approval_kind", String(32), nullable=False, server_default=text("'manual'")),
        Column("reason", Text, nullable=False),
        Column("expires_at", DateTime(timezone=True), nullable=True),
        Column("active", Boolean, nullable=False, server_default=text("TRUE")),
        Column("created_at", DateTime, server_default=text("now()")),
    )
    op.create_index("ix_agency_approvals_candidate_id", "agency_approvals", ["candidate_id"])

    op.create_table(
        "agency_budget_events",
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("budget_id", Integer, ForeignKey("agency_budgets.id", ondelete="SET NULL"), nullable=True),
        Column("candidate_id", Integer, ForeignKey("agency_candidates.id", ondelete="SET NULL"), nullable=True),
        Column("decision_id", Integer, ForeignKey("agency_decisions.id", ondelete="SET NULL"), nullable=True),
        Column("event_type", String(40), nullable=False),
        Column("scope_type", String(20), nullable=False),
        Column("scope_id", String(120), nullable=False),
        Column("drive_type", String(40), nullable=True),
        Column("actor_type", String(32), nullable=False, server_default=text("'system'")),
        Column("actor_id", String(120), nullable=True),
        Column("reason_code", String(80), nullable=True),
        Column("delta_candidates", Integer, nullable=False, server_default=text("0")),
        Column("delta_auto_exec", Integer, nullable=False, server_default=text("0")),
        Column("delta_cost", Float, nullable=False, server_default=text("0.0")),
        Column("delta_tokens", Integer, nullable=False, server_default=text("0")),
        Column("before_snapshot", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        Column("after_snapshot", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        Column("metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        Column("created_at", DateTime, server_default=text("now()")),
    )
    op.create_index("ix_agency_budget_events_budget_id", "agency_budget_events", ["budget_id"])
    op.create_index("ix_agency_budget_events_candidate_id", "agency_budget_events", ["candidate_id"])
    op.create_index("ix_agency_budget_events_decision_id", "agency_budget_events", ["decision_id"])
    op.create_index("ix_agency_budget_events_scope_id", "agency_budget_events", ["scope_id"])


def _downgrade_0008() -> None:
    op.create_table(
        "run_habits",
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("task_family", Text, nullable=False),
        Column("signature_hash", Text, nullable=False),
        Column("status", Text, server_default="draft"),
        Column("source_skill", Text, nullable=True),
        Column("active_version_id", Integer, nullable=True),
        Column("eligibility_metrics", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        Column("verifier_profile", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        Column("created_at", DateTime, server_default=text("now()")),
        UniqueConstraint(
            "task_family",
            "signature_hash",
            name="uq_run_habits_task_family_signature_hash",
        ),
    )
    op.create_index("ix_run_habits_task_family", "run_habits", ["task_family"])
    op.create_index("ix_run_habits_signature_hash", "run_habits", ["signature_hash"])
    op.create_index("ix_run_habits_status", "run_habits", ["status"])
    op.create_index("ix_run_habits_active_version_id", "run_habits", ["active_version_id"])

    op.create_table(
        "habit_versions",
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("habit_id", Integer, ForeignKey("run_habits.id", ondelete="CASCADE"), nullable=False),
        Column("version", Integer, nullable=False),
        Column("matcher", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        Column("preconditions", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        Column("step_graph", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
        Column("expected_artifacts", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        Column("fallback_policy", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        Column("source_run_ids", ARRAY(Integer), nullable=False, server_default=text("'{}'")),
        Column("shadow_stats", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        Column("created_at", DateTime, server_default=text("now()")),
        UniqueConstraint(
            "habit_id",
            "version",
            name="uq_habit_versions_habit_version",
        ),
    )
    op.create_index("ix_habit_versions_habit_id", "habit_versions", ["habit_id"])
    op.create_index("ix_habit_versions_version", "habit_versions", ["version"])

    op.create_foreign_key(
        "run_habits_active_version_id_fkey",
        "run_habits",
        "habit_versions",
        ["active_version_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_table(
        "habit_executions",
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("run_id", Integer, ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False),
        Column("habit_id", Integer, ForeignKey("run_habits.id", ondelete="CASCADE"), nullable=False),
        Column("habit_version_id", Integer, ForeignKey("habit_versions.id", ondelete="CASCADE"), nullable=False),
        Column("match_confidence", Float, nullable=False, server_default=text("0")),
        Column("guard_result", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        Column("status", Text, nullable=False),
        Column("fallback_reason", Text, nullable=True),
        Column("executed_steps", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
        Column("verifier_result", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        Column("duration_ms", Integer, nullable=True),
        Column("tokens", Integer, nullable=True),
        Column("cost", Numeric(10, 6), nullable=True),
        Column("created_at", DateTime, server_default=text("now()")),
        UniqueConstraint(
            "run_id",
            "habit_version_id",
            name="uq_habit_executions_run_version",
        ),
    )
    op.create_index("ix_habit_executions_run_id", "habit_executions", ["run_id"])
    op.create_index("ix_habit_executions_habit_id", "habit_executions", ["habit_id"])
    op.create_index("ix_habit_executions_status", "habit_executions", ["status"])


def _downgrade_0009() -> None:
    op.create_table(
        "delegation_quality",
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("session_key", Text, nullable=False),
        Column("original_ask", Text, nullable=False),
        Column("task_delegated", Text, nullable=False),
        Column("sub_agent_output", Text, nullable=True),
        Column("quality_score", Float, server_default=text("0.0")),
        Column("rounds_needed", Integer, server_default=text("1")),
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    )
    op.create_index("idx_delegation_quality_created", "delegation_quality", ["created_at"])
    op.create_index("idx_delegation_quality_session", "delegation_quality", ["session_key"])

    op.create_table(
        "critic_reviews",
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("execution_id", Integer, ForeignKey("skill_executions.id"), nullable=False),
        Column("critic_skill_id", Integer, ForeignKey("skills.id"), nullable=True),
        Column("findings", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
        Column("scores", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        Column("verdict", String(20), nullable=False, server_default=text("'approve'")),
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    )
    op.create_index("idx_critic_reviews_execution", "critic_reviews", ["execution_id"])

    op.create_table(
        "execution_outcomes",
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("execution_id", Integer, ForeignKey("skill_executions.id"), nullable=False),
        Column("critic_review_id", Integer, ForeignKey("critic_reviews.id"), nullable=True),
        Column("outcome", String(20), nullable=False),
        Column("outcome_source", String(50), nullable=False, server_default=text("'user_feedback'")),
        Column("notes", Text, nullable=True),
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    )
    op.create_index("idx_execution_outcomes_execution", "execution_outcomes", ["execution_id"])
    op.create_index("idx_execution_outcomes_critic", "execution_outcomes", ["critic_review_id"])


def _downgrade_0010() -> None:
    op.create_table(
        "operating_params",
        Column("key", Text, primary_key=True),
        Column("value", Float, nullable=False),
        Column("description", Text, nullable=True),
        Column("last_modified", DateTime, nullable=False, server_default=text("now()")),
        Column("modified_by", Text, server_default=text("'init'")),
    )

    op.create_table(
        "reflections",
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("reflection_date", Date, nullable=False),
        Column("phase", Text, nullable=False),
        Column("summary", Text, nullable=False),
        Column("findings", JSONB, nullable=True),
        Column("actions_taken", JSONB, nullable=True),
        Column("created_at", DateTime, server_default=text("now()")),
    )


def _downgrade_0011() -> None:
    op.create_table(
        "prompt_templates",
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("name", Text, nullable=False),
        Column("template_text", Text, nullable=False),
        Column("version", Integer, nullable=False, server_default=text("1")),
        Column("avg_quality_score", Float, server_default=text("0.0")),
        Column("use_count", Integer, server_default=text("0")),
        Column("last_used", DateTime(timezone=True), nullable=True),
        Column("created_at", DateTime(timezone=True), server_default=text("now()")),
        Column("updated_at", DateTime(timezone=True), server_default=text("now()")),
        UniqueConstraint("name", "version", name="uq_prompt_templates_name_version"),
    )
    op.create_index("idx_prompt_templates_name", "prompt_templates", ["name"])

    op.create_table(
        "prompt_template_outcomes",
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("template_name", Text, nullable=False),
        Column("template_version", Integer, nullable=False),
        Column("quality_score", Float, nullable=False),
        Column("created_at", DateTime(timezone=True), server_default=text("now()")),
    )
    op.create_index(
        "idx_prompt_template_outcomes_name",
        "prompt_template_outcomes",
        ["template_name", "template_version"],
    )


def _downgrade_0012() -> None:
    op.create_table(
        "brain_prompts",
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("type", String(32), nullable=False),
        Column("content", Text, nullable=False),
        Column("context_json", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        Column("dismissed_until", DateTime(timezone=True), nullable=True),
        Column("resolved_at", DateTime(timezone=True), nullable=True),
        Column("created_at", DateTime(timezone=True), server_default=text("now()")),
    )


def _downgrade_0013() -> None:
    if _table_exists("memories"):
        if not _column_exists("memories", "emotional_embedding"):
            op.add_column("memories", Column("emotional_embedding", Vector(32), nullable=True))
        if not _column_exists("memories", "emotion_valence"):
            op.add_column(
                "memories",
                Column("emotion_valence", Double, server_default="0.0", nullable=True),
            )
        if not _column_exists("memories", "emotion_arousal"):
            op.add_column(
                "memories",
                Column("emotion_arousal", Double, server_default="0.0", nullable=True),
            )
        if not _column_exists("memories", "emotion_label"):
            op.add_column("memories", Column("emotion_label", String(30), nullable=True))

    if _table_exists("daily_metrics"):
        if not _column_exists("daily_metrics", "avg_valence"):
            op.add_column("daily_metrics", Column("avg_valence", Double, nullable=True))
        if not _column_exists("daily_metrics", "avg_arousal"):
            op.add_column("daily_metrics", Column("avg_arousal", Double, nullable=True))
        if not _column_exists("daily_metrics", "valence_trend"):
            op.add_column("daily_metrics", Column("valence_trend", Double, nullable=True))
        if not _column_exists("daily_metrics", "frustration_count"):
            op.add_column(
                "daily_metrics",
                Column("frustration_count", Integer, server_default=text("0"), nullable=True),
            )
        if not _column_exists("daily_metrics", "joy_count"):
            op.add_column(
                "daily_metrics",
                Column("joy_count", Integer, server_default=text("0"), nullable=True),
            )

    if _table_exists("skill_executions") and not _column_exists("skill_executions", "operator_emotion"):
        op.add_column("skill_executions", Column("operator_emotion", String(20), nullable=True))

    if not _table_exists("emotional_snapshots"):
        op.create_table(
            "emotional_snapshots",
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("session_date", Date, nullable=False),
            Column("timestamp", DateTime(timezone=True), server_default=text("now()")),
            Column("valence", Double, nullable=False),
            Column("arousal", Double, nullable=False),
            Column("label", String(30), nullable=True),
            Column("trigger_summary", Text, nullable=True),
            Column("topic_tags", ARRAY(Text), server_default=text("'{}'::text[]"), nullable=True),
            Column("attributed_to", String(36), nullable=True),
        )
