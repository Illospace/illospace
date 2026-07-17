from __future__ import annotations

import importlib

import pytest
import sqlalchemy as sa


ACTIVATION_MODULE = "brain.app.cli.activate_uwear_engineering_triage"


def _schema() -> tuple[sa.MetaData, dict[str, sa.Table]]:
    metadata = sa.MetaData()
    tables = {
        "domain_object_types": sa.Table(
            "domain_object_types",
            metadata,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("domain_id", sa.Integer, nullable=False),
            sa.Column("key", sa.Text, nullable=False),
            sa.Column("archived_at", sa.DateTime(timezone=True)),
        ),
        "domain_records": sa.Table(
            "domain_records",
            metadata,
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column("org_id", sa.Text, nullable=False),
            sa.Column("domain_id", sa.Integer, nullable=False),
            sa.Column("object_type_id", sa.Integer, nullable=False),
            sa.Column("title", sa.Text, nullable=False),
            sa.Column("data", sa.JSON, nullable=False),
            sa.Column("search_text", sa.Text, nullable=False, server_default=""),
            sa.Column("version", sa.Integer, nullable=False, server_default="1"),
            sa.Column("archived_at", sa.DateTime(timezone=True)),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        ),
    }
    return metadata, tables


def _seed(
    connection: sa.Connection,
    tables: dict[str, sa.Table],
    *,
    include_core: bool = True,
) -> None:
    connection.execute(
        tables["domain_object_types"].insert(),
        {"id": 75, "domain_id": 37, "key": "doc_page"},
    )
    rows = [
        {
            "id": 1274,
            "org_id": "org-1",
            "domain_id": 1,
            "object_type_id": 1,
            "title": "Unrelated pull request",
            "data": {"slug": "github-uwear-backend-pr-910"},
            "version": 1,
        },
        {
            "id": 1275,
            "org_id": "org-1",
            "domain_id": 40,
            "object_type_id": 40,
            "title": "Unrelated audit plan",
            "data": {"slug": "route-database-audit-pickup-plan"},
            "version": 1,
        },
    ]
    if include_core:
        rows.insert(
            0,
            {
                "id": 1155,
                "org_id": "org-1",
                "domain_id": 37,
                "object_type_id": 75,
                "title": "Uwear Engineering Triage",
                "data": {"slug": "uwear-engineering-triage", "content": "v7"},
                "version": 7,
            },
        )
    connection.execute(tables["domain_records"].insert(), rows)


def _records(connection: sa.Connection, table: sa.Table) -> list[dict[str, object]]:
    return [
        dict(row)
        for row in connection.execute(sa.select(table).order_by(table.c.id)).mappings()
    ]


def test_activation_ignores_legacy_id_collisions_and_is_idempotent():
    activation = importlib.import_module(ACTIVATION_MODULE)
    engine = sa.create_engine("sqlite://")
    metadata, tables = _schema()
    metadata.create_all(engine)

    with engine.begin() as connection:
        _seed(connection, tables)

        first = activation._activate(connection, apply=True)
        after_first = _records(connection, tables["domain_records"])
        second = activation._activate(connection, apply=True)
        after_second = _records(connection, tables["domain_records"])

        assert len(first.created) == 5
        assert first.updated == ("uwear-engineering-triage",)
        assert second.created == ()
        assert second.updated == ()
        assert len(second.unchanged) == 6
        assert after_second == after_first

        targets = {
            row["data"]["slug"]: row
            for row in after_second
            if row["domain_id"] == 37
        }
        assert set(targets) == {
            "uwear-engineering-triage",
            *activation.PLAYBOOK_SLUGS,
        }
        assert all(targets[slug]["id"] > 1275 for slug in activation.PLAYBOOK_SLUGS)
        assert targets["uwear-engineering-triage"]["version"] == 8


def test_activation_fails_loudly_on_cross_domain_slug_collision():
    activation = importlib.import_module(ACTIVATION_MODULE)
    engine = sa.create_engine("sqlite://")
    metadata, tables = _schema()
    metadata.create_all(engine)

    with engine.begin() as connection:
        _seed(connection, tables)
        connection.execute(
            tables["domain_records"]
            .update()
            .where(tables["domain_records"].c.id == 1274)
            .values(data={"slug": "uwear-engineering-triage-chantier-operations"})
        )

        with pytest.raises(
            activation.ActivationError,
            match="chantier-operations.*occupied.*Domain 1",
        ):
            activation._activate(connection, apply=True)


def test_activation_fails_loudly_when_core_target_is_missing():
    activation = importlib.import_module(ACTIVATION_MODULE)
    engine = sa.create_engine("sqlite://")
    metadata, tables = _schema()
    metadata.create_all(engine)

    with engine.begin() as connection:
        _seed(connection, tables, include_core=False)

        with pytest.raises(activation.ActivationError, match="record 1155 is missing"):
            activation._activate(connection, apply=True)


def test_activation_fails_loudly_when_core_id_is_occupied_cross_domain():
    activation = importlib.import_module(ACTIVATION_MODULE)
    engine = sa.create_engine("sqlite://")
    metadata, tables = _schema()
    metadata.create_all(engine)

    with engine.begin() as connection:
        _seed(connection, tables)
        connection.execute(
            tables["domain_records"]
            .update()
            .where(tables["domain_records"].c.id == 1155)
            .values(domain_id=1)
        )

        with pytest.raises(
            activation.ActivationError,
            match="id 1155.*Domain 1",
        ):
            activation._activate(connection, apply=True)
