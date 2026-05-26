import importlib.util
from pathlib import Path

from sqlalchemy import CheckConstraint

from brain.platform.db.constraints import check_in_constraint
from brain.platform.db.models.agent import AgentApiCall
from brain.platform.db.models.agent_run import AgentRunRow
from brain.platform.db.models.chat import ChatConversationMember, ChatMessage
from brain.platform.db.models.domain import DomainRecord
from brain.platform.db.models.external_agent import ExternalAgentTaskRow
from brain.platform.db.models.idea import Idea, IdeaStateLog
from brain.platform.db.models.inbound import InboundEventRow
from brain.systems.cortex.status import IDEA_STATUS_VALUES
from brain.systems.external_agents.status import EXTERNAL_AGENT_TASK_STATUS_VALUES
from brain.systems.inbound.status import INBOUND_EVENT_STATUS_VALUES
from brain.platform.status_contracts import AGENT_RUN_DB_STATUS_VALUES


ROOT = Path(__file__).resolve().parents[1]
VERSIONS = ROOT / "brain" / "platform" / "db" / "alembic" / "versions"
MIGRATION = VERSIONS / "0011_db_audit_remediation_indexes.py"
BASELINE = VERSIONS / "0001_public_schema_baseline.py"


def _index_columns(table, name: str) -> list[str]:
    for index in table.indexes:
        if index.name == name:
            return [column.name for column in index.columns]
    raise AssertionError(f"Missing index {name} on {table.name}")


def _index_names(table) -> set[str]:
    return {index.name for index in table.indexes}


def _check_constraint_sql(table, name: str) -> str:
    for constraint in table.constraints:
        if isinstance(constraint, CheckConstraint) and constraint.name == name:
            return str(constraint.sqltext)
    raise AssertionError(f"Missing check constraint {name} on {table.name}")


def test_agent_api_calls_link_to_runs_and_support_run_date_queries():
    run_id = AgentApiCall.__table__.c.run_id
    foreign_key = next(iter(run_id.foreign_keys))

    assert foreign_key.target_fullname == "agent_runs.id"
    assert foreign_key.constraint.name == "fk_agent_api_calls_run_id_agent_runs"
    assert foreign_key.ondelete == "SET NULL"
    assert _index_columns(AgentApiCall.__table__, "ix_agent_api_calls_run_created") == [
        "run_id",
        "created_at",
    ]
    assert _index_columns(AgentApiCall.__table__, "ix_agent_api_calls_created_run") == [
        "created_at",
        "run_id",
    ]


def test_timeline_search_and_chat_index_metadata_matches_audit_targets():
    assert _index_columns(IdeaStateLog.__table__, "ix_idea_state_log_idea_changed") == [
        "idea_id",
        "changed_at",
        "id",
    ]

    domain_index = next(
        index
        for index in DomainRecord.__table__.indexes
        if index.name == "ix_domain_records_search_text_trgm"
    )
    assert domain_index.dialect_options["postgresql"]["using"] == "gin"
    assert domain_index.dialect_options["postgresql"]["ops"] == {
        "search_text": "gin_trgm_ops"
    }
    assert "ix_domain_records_search_text" not in _index_names(DomainRecord.__table__)

    assert "ix_chat_conversation_members_conversation_user" not in _index_names(
        ChatConversationMember.__table__
    )
    assert "ix_chat_messages_conversation_seq" not in _index_names(ChatMessage.__table__)


def test_high_value_status_columns_have_named_check_constraints():
    expected = {
        AgentRunRow.__table__: ("ck_agent_runs_status", AGENT_RUN_DB_STATUS_VALUES),
        Idea.__table__: ("ck_ideas_status", IDEA_STATUS_VALUES),
        InboundEventRow.__table__: (
            "ck_inbound_events_status",
            INBOUND_EVENT_STATUS_VALUES,
        ),
        ExternalAgentTaskRow.__table__: (
            "ck_external_agent_tasks_status",
            EXTERNAL_AGENT_TASK_STATUS_VALUES,
        ),
    }

    for table, (constraint_name, status_values) in expected.items():
        assert _check_constraint_sql(table, constraint_name) == check_in_constraint(
            "status",
            status_values,
        )


def test_remediation_migration_status_checks_use_canonical_contracts():
    spec = importlib.util.spec_from_file_location("db_audit_remediation_0011", MIGRATION)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.STATUS_CHECKS == {
        "agent_runs": (
            "ck_agent_runs_status",
            check_in_constraint("status", AGENT_RUN_DB_STATUS_VALUES),
        ),
        "ideas": (
            "ck_ideas_status",
            check_in_constraint("status", IDEA_STATUS_VALUES),
        ),
        "inbound_events": (
            "ck_inbound_events_status",
            check_in_constraint("status", INBOUND_EVENT_STATUS_VALUES),
        ),
        "external_agent_tasks": (
            "ck_external_agent_tasks_status",
            check_in_constraint("status", EXTERNAL_AGENT_TASK_STATUS_VALUES),
        ),
    }


def test_remediation_migration_carries_existing_database_repairs():
    content = MIGRATION.read_text()

    assert 'revision = "0011_db_audit_remediation_indexes"' in content
    assert 'down_revision = "0010_thread_context_and_discussion"' in content
    assert "CREATE EXTENSION IF NOT EXISTS pg_trgm" in content
    assert "CREATE EXTENSION IF NOT EXISTS pg_stat_statements" in content
    assert "UPDATE agent_api_calls calls" in content
    assert "fk_agent_api_calls_run_id_agent_runs" in content
    assert "ix_agent_api_calls_run_created" in content
    assert "ix_agent_api_calls_created_run" in content
    assert "ix_idea_state_log_idea_changed" in content
    assert "ix_domain_records_search_text_trgm" in content
    assert "gin_trgm_ops" in content
    assert "ix_chat_conversation_members_conversation_user" in content
    assert "ix_chat_messages_conversation_seq" in content
    assert "NOT VALID" in content


def test_public_baseline_enables_extensions_needed_by_current_metadata():
    content = BASELINE.read_text()

    assert "CREATE EXTENSION IF NOT EXISTS vector" in content
    assert "CREATE EXTENSION IF NOT EXISTS pg_trgm" in content
    assert "CREATE EXTENSION IF NOT EXISTS pg_stat_statements" in content
