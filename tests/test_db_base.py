"""Base model and mixin behavior."""
from sqlalchemy import DateTime, String, inspect
from sqlalchemy.orm import Mapped, mapped_column

import brain.platform.db.models  # noqa: F401
from brain.platform.db.base import Base, CreatedAtMixin, TimestampMixin, OrgScopedMixin, ArchivableMixin
from brain.platform.db.models.agent_run import AgentRunArtifactRow, AgentRunEventRow, AgentRunRow
from brain.platform.db.models.idea import Idea, IdeaStateLog


class _FakeModel(TimestampMixin, ArchivableMixin, Base):
    __tablename__ = "_test_fake"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50))


class _FakeCreatedOnly(CreatedAtMixin, Base):
    __tablename__ = "_test_created_only"
    id: Mapped[int] = mapped_column(primary_key=True)


def test_timestamp_mixin_has_both_columns():
    cols = {c.name: c for c in inspect(_FakeModel).columns}
    assert "created_at" in cols
    assert "updated_at" in cols
    assert getattr(cols["created_at"].type, "timezone", False)
    assert getattr(cols["updated_at"].type, "timezone", False)

def test_created_at_mixin_has_only_created():
    cols = {c.name: c for c in inspect(_FakeCreatedOnly).columns}
    assert "created_at" in cols
    assert "updated_at" not in cols
    assert getattr(cols["created_at"].type, "timezone", False)


def test_all_datetime_columns_are_timezone_aware():
    naive_columns = [
        f"{table.name}.{column.name}"
        for table in Base.metadata.tables.values()
        for column in table.columns
        if isinstance(column.type, DateTime)
        and not getattr(column.type, "timezone", False)
    ]
    assert naive_columns == []


def test_archivable_mixin():
    cols = {c.name for c in inspect(_FakeModel).columns}
    assert "archived" in cols

def test_repr():
    obj = _FakeModel(id=42, name="test")
    assert repr(obj) == "<_FakeModel 42>"

def test_repr_none_id():
    obj = _FakeModel(name="test")
    assert repr(obj) == "<_FakeModel None>"


def test_idea_status_lifecycle_timestamps_are_timezone_aware():
    columns = {
        column.name: column.type
        for column in inspect(Idea).columns
        if column.name in {"created_at", "updated_at", "archived_at", "encoded_at", "read_at"}
    }

    assert set(columns) == {
        "created_at",
        "updated_at",
        "archived_at",
        "encoded_at",
        "read_at",
    }
    assert all(
        getattr(column_type, "timezone", False)
        for column_type in columns.values()
    )
    assert getattr(inspect(IdeaStateLog).columns.changed_at.type, "timezone", False)


def test_agent_run_lifecycle_timestamps_are_timezone_aware():
    columns = {
        column.name: column.type
        for column in inspect(AgentRunRow).columns
        if column.name in {
            "created_at",
            "updated_at",
            "started_at",
            "paused_at",
            "completed_at",
            "failed_at",
            "canceled_at",
        }
    }

    assert set(columns) == {
        "created_at",
        "updated_at",
        "started_at",
        "paused_at",
        "completed_at",
        "failed_at",
        "canceled_at",
    }
    assert all(
        getattr(column_type, "timezone", False)
        for column_type in columns.values()
    )
    assert getattr(inspect(AgentRunEventRow).columns.created_at.type, "timezone", False)
    assert getattr(inspect(AgentRunArtifactRow).columns.created_at.type, "timezone", False)
