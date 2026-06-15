"""Add reconstructive memory graph tables.

Revision ID: 0020_reconstructive_memory
Revises: 0019_workspace_tool_user_configs
Create Date: 2026-06-15
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

try:
    from pgvector.sqlalchemy import Vector
except ImportError:  # pragma: no cover - migration fallback for non-pg test envs
    Vector = sa.Text

from brain.kernel.config import MEMORY_SEMANTIC_EMBEDDING_DIM


revision = "0020_reconstructive_memory"
down_revision = "0019_workspace_tool_user_configs"
branch_labels = None
depends_on = None


def _schema() -> str | None:
    return "public" if op.get_bind().dialect.name == "postgresql" else None


def _inspector():
    return sa.inspect(op.get_bind())


def _table_exists(table_name: str) -> bool:
    return table_name in set(_inspector().get_table_names(schema=_schema()))


def _index_exists(table_name: str, index_name: str) -> bool:
    if not _table_exists(table_name):
        return False
    return index_name in {index["name"] for index in _inspector().get_indexes(table_name, schema=_schema())}


def _constraint_exists(table_name: str, constraint_name: str) -> bool:
    if not _table_exists(table_name):
        return False
    constraints = _inspector().get_unique_constraints(table_name, schema=_schema())
    constraints += _inspector().get_foreign_keys(table_name, schema=_schema())
    return constraint_name in {constraint["name"] for constraint in constraints}


def _uuid_type():
    return postgresql.UUID(as_uuid=False) if op.get_bind().dialect.name == "postgresql" else sa.String()


def _json_type():
    return postgresql.JSONB() if op.get_bind().dialect.name == "postgresql" else sa.JSON()


def _json_default(value: str):
    if op.get_bind().dialect.name == "postgresql":
        return sa.text(f"'{value}'::jsonb")
    return sa.text(f"'{value}'")


def _create_index_if_missing(table: str, name: str, columns: list[str]) -> None:
    if _table_exists(table) and not _index_exists(table, name):
        op.create_index(name, table, columns)


def _drop_leftover_table(table: str) -> None:
    if not _table_exists(table):
        return
    if op.get_bind().dialect.name == "postgresql":
        op.execute(sa.text(f'DROP TABLE IF EXISTS "{table}" CASCADE'))
    else:
        op.drop_table(table)


def _drop_leftover_flat_memory_tables() -> None:
    for table in (
        "summary_lineage",
        "memory_summaries",
        "memory_reviews",
        "memory_contradictions",
        "tags",
        "edges",
        "memories",
    ):
        _drop_leftover_table(table)


def upgrade() -> None:
    if not _table_exists("memory_sources"):
        op.create_table(
            "memory_sources",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("org_id", _uuid_type(), sa.ForeignKey("orgs.id", ondelete="SET NULL"), nullable=True),
            sa.Column("user_id", _uuid_type(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("visibility", sa.String(length=20), nullable=False, server_default=sa.text("'private'")),
            sa.Column("source_kind", sa.String(length=60), nullable=False),
            sa.Column("source_ref", sa.Text(), nullable=True),
            sa.Column("source_url", sa.Text(), nullable=True),
            sa.Column("content_digest", sa.String(length=128), nullable=False),
            sa.Column("raw_content", sa.Text(), nullable=True),
            sa.Column("structured_payload", _json_type(), nullable=False, server_default=_json_default("{}")),
            sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
            sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
            sa.Column("authority_principal", sa.Text(), nullable=True),
            sa.Column("sensitivity", sa.String(length=30), nullable=False, server_default=sa.text("'low'")),
            sa.Column("retention_policy", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
            sa.UniqueConstraint(
                "org_id",
                "source_kind",
                "source_ref",
                "content_digest",
                name="uq_memory_sources_identity",
            ),
        )

    if not _table_exists("memory_spans"):
        op.create_table(
            "memory_spans",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("source_id", sa.Integer(), sa.ForeignKey("memory_sources.id", ondelete="CASCADE"), nullable=False),
            sa.Column("span_kind", sa.String(length=40), nullable=False, server_default=sa.text("'text'")),
            sa.Column("locator", _json_type(), nullable=False, server_default=_json_default("{}")),
            sa.Column("text", sa.Text(), nullable=False),
            sa.Column("token_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("content_digest", sa.String(length=128), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        )

    if not _table_exists("memory_nodes"):
        op.create_table(
            "memory_nodes",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("node_kind", sa.String(length=40), nullable=False),
            sa.Column("content_kind", sa.String(length=40), nullable=True),
            sa.Column("canonical_label", sa.Text(), nullable=False),
            sa.Column("text", sa.Text(), nullable=True),
            sa.Column("normalized_key", sa.Text(), nullable=False),
            sa.Column("scope_key", sa.Text(), nullable=False, server_default=sa.text("'default'")),
            sa.Column("org_id", _uuid_type(), sa.ForeignKey("orgs.id", ondelete="SET NULL"), nullable=True),
            sa.Column("user_id", _uuid_type(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("visibility", sa.String(length=20), nullable=False, server_default=sa.text("'private'")),
            sa.Column("sensitivity", sa.String(length=30), nullable=False, server_default=sa.text("'low'")),
            sa.Column("confidence", sa.Double(), nullable=False, server_default=sa.text("0.5")),
            sa.Column("truth_status", sa.String(length=30), nullable=False, server_default=sa.text("'unknown'")),
            sa.Column("freshness_status", sa.String(length=30), nullable=False, server_default=sa.text("'unknown'")),
            sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
            sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
            sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
            sa.UniqueConstraint("org_id", "node_kind", "scope_key", "normalized_key", name="uq_memory_nodes_scope_key"),
        )

    if not _table_exists("memory_node_embeddings"):
        op.create_table(
            "memory_node_embeddings",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("node_id", sa.Integer(), sa.ForeignKey("memory_nodes.id", ondelete="CASCADE"), nullable=False),
            sa.Column("embedding_kind", sa.String(length=40), nullable=False),
            sa.Column("model", sa.String(length=120), nullable=False),
            sa.Column("dimension", sa.Integer(), nullable=False),
            sa.Column("embedding", Vector(MEMORY_SEMANTIC_EMBEDDING_DIM), nullable=True),
            sa.Column("content_digest", sa.String(length=128), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
            sa.UniqueConstraint("node_id", "embedding_kind", "model", "content_digest", name="uq_memory_node_embeddings_digest"),
        )

    if not _table_exists("memory_edges"):
        op.create_table(
            "memory_edges",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("source_node_id", sa.Integer(), sa.ForeignKey("memory_nodes.id", ondelete="CASCADE"), nullable=False),
            sa.Column("target_node_id", sa.Integer(), sa.ForeignKey("memory_nodes.id", ondelete="CASCADE"), nullable=False),
            sa.Column("edge_kind", sa.String(length=60), nullable=False),
            sa.Column("weight", sa.Double(), nullable=False, server_default=sa.text("1.0")),
            sa.Column("confidence", sa.Double(), nullable=False, server_default=sa.text("0.5")),
            sa.Column("directionality", sa.String(length=20), nullable=False, server_default=sa.text("'directed'")),
            sa.Column("org_id", _uuid_type(), sa.ForeignKey("orgs.id", ondelete="SET NULL"), nullable=True),
            sa.Column("visibility", sa.String(length=20), nullable=False, server_default=sa.text("'private'")),
            sa.Column("evidence_span_ids", _json_type(), nullable=False, server_default=_json_default("[]")),
            sa.Column("created_by", sa.String(length=40), nullable=False, server_default=sa.text("'extractor'")),
            sa.Column("last_activated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("activation_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
            sa.UniqueConstraint("source_node_id", "target_node_id", "edge_kind", name="uq_memory_edges_src_tgt_kind"),
        )

    if not _table_exists("memory_assertions"):
        op.create_table(
            "memory_assertions",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("node_id", sa.Integer(), sa.ForeignKey("memory_nodes.id", ondelete="CASCADE"), nullable=False),
            sa.Column("claim_text", sa.Text(), nullable=False),
            sa.Column("subject_node_id", sa.Integer(), sa.ForeignKey("memory_nodes.id", ondelete="SET NULL"), nullable=True),
            sa.Column("predicate", sa.String(length=80), nullable=True),
            sa.Column("object_node_id", sa.Integer(), sa.ForeignKey("memory_nodes.id", ondelete="SET NULL"), nullable=True),
            sa.Column("object_text", sa.Text(), nullable=True),
            sa.Column("polarity", sa.String(length=20), nullable=False, server_default=sa.text("'positive'")),
            sa.Column("confidence", sa.Double(), nullable=False, server_default=sa.text("0.5")),
            sa.Column("truth_status", sa.String(length=30), nullable=False, server_default=sa.text("'unknown'")),
            sa.Column("review_status", sa.String(length=30), nullable=False, server_default=sa.text("'unreviewed'")),
            sa.Column("source_span_ids", _json_type(), nullable=False, server_default=_json_default("[]")),
            sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
            sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
            sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        )

    if not _table_exists("reconstruction_runs"):
        op.create_table(
            "reconstruction_runs",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("run_id", sa.Integer(), sa.ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True),
            sa.Column("thread_id", sa.Text(), nullable=True),
            sa.Column("query_text", sa.Text(), nullable=False),
            sa.Column("query_kind", sa.String(length=50), nullable=False, server_default=sa.text("'fact_lookup'")),
            sa.Column("org_id", _uuid_type(), sa.ForeignKey("orgs.id", ondelete="SET NULL"), nullable=True),
            sa.Column("user_id", _uuid_type(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("visibility_context", _json_type(), nullable=False, server_default=_json_default("{}")),
            sa.Column("budget_tokens", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("budget_steps", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("policy_version", sa.String(length=80), nullable=False, server_default=sa.text("'deterministic-v1'")),
            sa.Column("model", sa.String(length=120), nullable=True),
            sa.Column("status", sa.String(length=30), nullable=False, server_default=sa.text("'running'")),
            sa.Column("final_confidence", sa.Double(), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        )

    if not _table_exists("reconstruction_steps"):
        op.create_table(
            "reconstruction_steps",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("reconstruction_run_id", sa.Integer(), sa.ForeignKey("reconstruction_runs.id", ondelete="CASCADE"), nullable=False),
            sa.Column("step_index", sa.Integer(), nullable=False),
            sa.Column("state_summary", sa.Text(), nullable=True),
            sa.Column("action_kind", sa.String(length=60), nullable=False),
            sa.Column("action_input", _json_type(), nullable=False, server_default=_json_default("{}")),
            sa.Column("action_output", _json_type(), nullable=False, server_default=_json_default("{}")),
            sa.Column("selected_node_ids", _json_type(), nullable=False, server_default=_json_default("[]")),
            sa.Column("rejected_node_ids", _json_type(), nullable=False, server_default=_json_default("[]")),
            sa.Column("reason", sa.Text(), nullable=True),
            sa.Column("cost_tokens", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("latency_ms", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        )

    if not _table_exists("reconstruction_evidence"):
        op.create_table(
            "reconstruction_evidence",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("reconstruction_run_id", sa.Integer(), sa.ForeignKey("reconstruction_runs.id", ondelete="CASCADE"), nullable=False),
            sa.Column("node_id", sa.Integer(), sa.ForeignKey("memory_nodes.id", ondelete="SET NULL"), nullable=True),
            sa.Column("assertion_id", sa.Integer(), sa.ForeignKey("memory_assertions.id", ondelete="SET NULL"), nullable=True),
            sa.Column("source_span_id", sa.Integer(), sa.ForeignKey("memory_spans.id", ondelete="SET NULL"), nullable=True),
            sa.Column("role", sa.String(length=40), nullable=False, server_default=sa.text("'supports_answer'")),
            sa.Column("confidence", sa.Double(), nullable=False, server_default=sa.text("0.5")),
            sa.Column("rank", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        )

    if not _table_exists("reconstruction_feedback"):
        op.create_table(
            "reconstruction_feedback",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("reconstruction_run_id", sa.Integer(), sa.ForeignKey("reconstruction_runs.id", ondelete="CASCADE"), nullable=False),
            sa.Column("signal_kind", sa.String(length=60), nullable=False),
            sa.Column("target_step_id", sa.Integer(), sa.ForeignKey("reconstruction_steps.id", ondelete="SET NULL"), nullable=True),
            sa.Column("target_node_id", sa.Integer(), sa.ForeignKey("memory_nodes.id", ondelete="SET NULL"), nullable=True),
            sa.Column("target_edge_id", sa.Integer(), sa.ForeignKey("memory_edges.id", ondelete="SET NULL"), nullable=True),
            sa.Column("details", _json_type(), nullable=False, server_default=_json_default("{}")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        )

    for table, indexes in {
        "memory_sources": {
            "ix_memory_sources_org_kind_created": ["org_id", "source_kind", "created_at"],
            "ix_memory_sources_source_ref": ["source_ref"],
        },
        "memory_spans": {
            "ix_memory_spans_source": ["source_id"],
            "ix_memory_spans_digest": ["content_digest"],
        },
        "memory_nodes": {
            "ix_memory_nodes_org_kind_key": ["org_id", "node_kind", "normalized_key"],
            "ix_memory_nodes_visibility": ["org_id", "user_id", "visibility"],
            "ix_memory_nodes_truth_freshness": ["truth_status", "freshness_status"],
        },
        "memory_node_embeddings": {
            "ix_memory_node_embeddings_node_kind": ["node_id", "embedding_kind"],
        },
        "memory_edges": {
            "ix_memory_edges_source_kind": ["source_node_id", "edge_kind"],
            "ix_memory_edges_target_kind": ["target_node_id", "edge_kind"],
        },
        "memory_assertions": {
            "ix_memory_assertions_node": ["node_id"],
            "ix_memory_assertions_subject_predicate": ["subject_node_id", "predicate"],
            "ix_memory_assertions_truth": ["truth_status", "review_status"],
        },
        "reconstruction_runs": {
            "ix_reconstruction_runs_agent_run": ["run_id"],
            "ix_reconstruction_runs_thread": ["thread_id"],
            "ix_reconstruction_runs_org_user_created": ["org_id", "user_id", "created_at"],
        },
        "reconstruction_steps": {
            "ix_reconstruction_steps_run_index": ["reconstruction_run_id", "step_index"],
        },
        "reconstruction_evidence": {
            "ix_reconstruction_evidence_run_rank": ["reconstruction_run_id", "rank"],
            "ix_reconstruction_evidence_node": ["node_id"],
        },
        "reconstruction_feedback": {
            "ix_reconstruction_feedback_run": ["reconstruction_run_id"],
            "ix_reconstruction_feedback_signal": ["signal_kind"],
        },
    }.items():
        for name, columns in indexes.items():
            _create_index_if_missing(table, name, columns)

    _drop_leftover_flat_memory_tables()


def downgrade() -> None:
    for table in (
        "reconstruction_feedback",
        "reconstruction_evidence",
        "reconstruction_steps",
        "reconstruction_runs",
        "memory_assertions",
        "memory_edges",
        "memory_node_embeddings",
        "memory_nodes",
        "memory_spans",
        "memory_sources",
    ):
        if not _table_exists(table):
            continue
        for index in list(_inspector().get_indexes(table, schema=_schema())):
            op.drop_index(index["name"], table_name=table)
        for constraint in list(_inspector().get_unique_constraints(table, schema=_schema())):
            if _constraint_exists(table, constraint["name"]):
                op.drop_constraint(constraint["name"], table, type_="unique")
        op.drop_table(table)
