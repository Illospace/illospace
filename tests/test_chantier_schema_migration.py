from __future__ import annotations

from datetime import datetime, timezone
import importlib

import sqlalchemy as sa


MIGRATION_MODULE = "brain.platform.db.alembic.versions.0026_chantier_object_type"
SUNSET_MIGRATION_MODULE = (
    "brain.platform.db.alembic.versions.0029_chantier_sunset_kind"
)
SUPERSEDED_MIGRATION_MODULE = (
    "brain.platform.db.alembic.versions.0032_chantier_superseded_by"
)
MEMBERS_BLOCKERS_MIGRATION_MODULE = (
    "brain.platform.db.alembic.versions.0036_chantier_members_blockers"
)


def _schema() -> tuple[sa.MetaData, dict[str, sa.Table]]:
    metadata = sa.MetaData()
    tables = {
        "domains": sa.Table(
            "domains",
            metadata,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("slug", sa.Text, nullable=False),
            sa.Column("archived_at", sa.DateTime(timezone=True)),
        ),
        "domain_object_types": sa.Table(
            "domain_object_types",
            metadata,
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column("domain_id", sa.Integer, nullable=False),
            sa.Column("key", sa.Text, nullable=False),
            sa.Column("name", sa.Text, nullable=False),
            sa.Column("description", sa.Text),
            sa.Column("title_field", sa.Text),
            sa.Column("sort_order", sa.Integer, nullable=False, server_default="0"),
            sa.Column("archived_at", sa.DateTime(timezone=True)),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.UniqueConstraint("domain_id", "key"),
        ),
        "domain_field_definitions": sa.Table(
            "domain_field_definitions",
            metadata,
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column("domain_id", sa.Integer, nullable=False),
            sa.Column("object_type_id", sa.Integer, nullable=False),
            sa.Column("key", sa.Text, nullable=False),
            sa.Column("name", sa.Text, nullable=False),
            sa.Column("field_type", sa.Text, nullable=False),
            sa.Column("required", sa.Boolean, nullable=False),
            sa.Column("options", sa.JSON, nullable=False),
            sa.Column("default_value", sa.JSON),
            sa.Column("validation", sa.JSON, nullable=False),
            sa.Column("searchable", sa.Boolean, nullable=False),
            sa.Column("sortable", sa.Boolean, nullable=False),
            sa.Column("archived_at", sa.DateTime(timezone=True)),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.UniqueConstraint("object_type_id", "key"),
        ),
        "domain_records": sa.Table(
            "domain_records",
            metadata,
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column("domain_id", sa.Integer, nullable=False),
            sa.Column("object_type_id", sa.Integer, nullable=False),
            sa.Column("data", sa.JSON, nullable=False),
            sa.Column("version", sa.Integer, nullable=False, server_default="1"),
            sa.Column("archived_at", sa.DateTime(timezone=True)),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        ),
    }
    return metadata, tables


def _seed(connection: sa.Connection, tables: dict[str, sa.Table]) -> None:
    connection.execute(
        tables["domains"].insert(),
        [
            {"id": 1, "slug": "github-ticket-tracker", "archived_at": None},
            {"id": 2, "slug": "other-domain", "archived_at": None},
            {
                "id": 3,
                "slug": "github-ticket-tracker",
                "archived_at": datetime(2025, 1, 1, tzinfo=timezone.utc),
            },
        ],
    )
    connection.execute(
        tables["domain_object_types"].insert(),
        [
            {
                "id": 1,
                "domain_id": 1,
                "key": "ticket",
                "name": "Ticket",
                "description": None,
                "title_field": None,
                "sort_order": 1,
                "archived_at": None,
            },
            {
                "id": 2,
                "domain_id": 1,
                "key": "milestone",
                "name": "Milestone",
                "description": "Dormant legacy object",
                "title_field": None,
                "sort_order": 4,
                "archived_at": None,
            },
            {
                "id": 3,
                "domain_id": 2,
                "key": "ticket",
                "name": "Ticket",
                "description": None,
                "title_field": None,
                "sort_order": 1,
                "archived_at": None,
            },
        ],
    )


def test_migration_provisions_new_chantier_contract_idempotently_without_reusing_milestone():
    migration = importlib.import_module(MIGRATION_MODULE)
    engine = sa.create_engine("sqlite://")
    metadata, tables = _schema()
    metadata.create_all(engine)

    with engine.begin() as connection:
        _seed(connection, tables)
        migration._upgrade(connection)
        migration._upgrade(connection)

        objects = connection.execute(
            sa.select(tables["domain_object_types"]).order_by(
                tables["domain_object_types"].c.domain_id,
                tables["domain_object_types"].c.id,
            )
        ).mappings().all()
        chantier_objects = [row for row in objects if row["key"] == "chantier"]
        assert len(chantier_objects) == 1
        chantier = chantier_objects[0]
        assert chantier["domain_id"] == 1
        assert chantier["name"] == "Chantier"
        assert chantier["title_field"] == "title"
        assert "chantier-record-contract-v1" in chantier["description"]
        assert chantier["sort_order"] == 5

        milestone = next(row for row in objects if row["key"] == "milestone")
        assert milestone["id"] == 2
        assert milestone["description"] == "Dormant legacy object"

        fields = connection.execute(
            sa.select(tables["domain_field_definitions"])
            .where(
                tables["domain_field_definitions"].c.object_type_id == chantier["id"]
            )
            .order_by(tables["domain_field_definitions"].c.id)
        ).mappings().all()
        assert [field["key"] for field in fields] == [
            "slug",
            "title",
            "goal",
            "kind",
            "state",
            "owner",
            "refs",
            "parent_issue",
            "next_step",
            "progress_note",
            "created_at",
            "updated_at",
        ]
        by_key = {field["key"]: field for field in fields}
        assert {field["key"] for field in fields if field["required"]} == {
            "slug",
            "title",
            "goal",
            "kind",
            "state",
            "refs",
            "next_step",
        }
        assert by_key["slug"]["required"] is True
        assert by_key["slug"]["validation"]["immutable"] is True
        assert by_key["kind"]["options"] == ["feature", "incident", "quality", "gtm"]
        assert by_key["state"]["options"] == [
            "exploring",
            "building",
            "shipping",
            "verifying",
            "done",
            "paused",
        ]
        assert by_key["owner"]["name"] == "Next-action owner"
        assert by_key["owner"]["required"] is False
        assert by_key["refs"]["validation"]["type"] == "array"
        assert by_key["refs"]["validation"]["items"]["properties"]["source"]["enum"] == [
            "github",
            "doc",
            "slack",
            "posthog",
            "url",
        ]
        assert by_key["parent_issue"]["required"] is False
        assert by_key["created_at"]["field_type"] == "datetime"
        assert by_key["updated_at"]["field_type"] == "datetime"


def test_migration_reconciles_drifted_object_and_field_definitions():
    migration = importlib.import_module(MIGRATION_MODULE)
    engine = sa.create_engine("sqlite://")
    metadata, tables = _schema()
    metadata.create_all(engine)

    with engine.begin() as connection:
        _seed(connection, tables)
        migration._upgrade(connection)
        chantier = connection.execute(
            sa.select(tables["domain_object_types"]).where(
                tables["domain_object_types"].c.domain_id == 1,
                tables["domain_object_types"].c.key == "chantier",
            )
        ).mappings().one()
        state = connection.execute(
            sa.select(tables["domain_field_definitions"]).where(
                tables["domain_field_definitions"].c.object_type_id == chantier["id"],
                tables["domain_field_definitions"].c.key == "state",
            )
        ).mappings().one()
        connection.execute(
            tables["domain_object_types"]
            .update()
            .where(tables["domain_object_types"].c.id == chantier["id"])
            .values(name="Project", description="old schema", archived_at=sa.func.now())
        )
        connection.execute(
            tables["domain_field_definitions"]
            .update()
            .where(tables["domain_field_definitions"].c.id == state["id"])
            .values(options=["todo", "done"], required=False, archived_at=sa.func.now())
        )

        migration._upgrade(connection)

        repaired_object = connection.execute(
            sa.select(tables["domain_object_types"]).where(
                tables["domain_object_types"].c.id == chantier["id"]
            )
        ).mappings().one()
        repaired_state = connection.execute(
            sa.select(tables["domain_field_definitions"]).where(
                tables["domain_field_definitions"].c.id == state["id"]
            )
        ).mappings().one()
        assert repaired_object["name"] == "Chantier"
        assert "chantier-record-contract-v1" in repaired_object["description"]
        assert repaired_object["archived_at"] is None
        assert repaired_state["required"] is True
        assert repaired_state["options"] == [
            "exploring",
            "building",
            "shipping",
            "verifying",
            "done",
            "paused",
        ]
        assert repaired_state["archived_at"] is None


def test_migration_is_a_noop_when_domain_schema_tables_are_absent():
    migration = importlib.import_module(MIGRATION_MODULE)
    engine = sa.create_engine("sqlite://")

    with engine.begin() as connection:
        migration._upgrade(connection)


def test_sunset_migration_extends_contract_and_rekinds_target_record_idempotently():
    contract_migration = importlib.import_module(MIGRATION_MODULE)
    sunset_migration = importlib.import_module(SUNSET_MIGRATION_MODULE)
    engine = sa.create_engine("sqlite://")
    metadata, tables = _schema()
    metadata.create_all(engine)

    with engine.begin() as connection:
        _seed(connection, tables)
        contract_migration._upgrade(connection)
        chantier = connection.execute(
            sa.select(tables["domain_object_types"]).where(
                tables["domain_object_types"].c.domain_id == 1,
                tables["domain_object_types"].c.key == "chantier",
            )
        ).mappings().one()
        connection.execute(
            tables["domain_records"].insert(),
            [
                {
                    "id": 10,
                    "domain_id": 1,
                    "object_type_id": chantier["id"],
                    "data": {"slug": "shopify-app-sunset", "kind": "quality"},
                    "version": 4,
                },
                {
                    "id": 11,
                    "domain_id": 1,
                    "object_type_id": chantier["id"],
                    "data": {"slug": "another-quality-chantier", "kind": "quality"},
                    "version": 2,
                },
            ],
        )

        sunset_migration._upgrade(connection)
        sunset_migration._upgrade(connection)

        kind = connection.execute(
            sa.select(tables["domain_field_definitions"]).where(
                tables["domain_field_definitions"].c.object_type_id == chantier["id"],
                tables["domain_field_definitions"].c.key == "kind",
            )
        ).mappings().one()
        records = connection.execute(
            sa.select(tables["domain_records"]).order_by(tables["domain_records"].c.id)
        ).mappings().all()

        assert kind["options"] == ["feature", "incident", "quality", "gtm", "sunset"]
        assert records[0]["data"]["kind"] == "sunset"
        assert records[0]["version"] == 5
        assert records[1]["data"]["kind"] == "quality"
        assert records[1]["version"] == 2


def test_superseded_migration_adds_repeatable_retirement_field_without_touching_records():
    contract_migration = importlib.import_module(MIGRATION_MODULE)
    superseded_migration = importlib.import_module(SUPERSEDED_MIGRATION_MODULE)
    engine = sa.create_engine("sqlite://")
    metadata, tables = _schema()
    metadata.create_all(engine)

    with engine.begin() as connection:
        _seed(connection, tables)
        contract_migration._upgrade(connection)
        chantier = connection.execute(
            sa.select(tables["domain_object_types"]).where(
                tables["domain_object_types"].c.domain_id == 1,
                tables["domain_object_types"].c.key == "chantier",
            )
        ).mappings().one()
        connection.execute(
            tables["domain_records"].insert(),
            {
                "id": 2096,
                "domain_id": 1,
                "object_type_id": chantier["id"],
                "data": {"slug": "duplicate", "state": "exploring"},
                "version": 1,
            },
        )

        superseded_migration._upgrade(connection)
        superseded_migration._upgrade(connection)

        fields = connection.execute(
            sa.select(tables["domain_field_definitions"]).where(
                tables["domain_field_definitions"].c.object_type_id == chantier["id"],
                tables["domain_field_definitions"].c.key == "superseded_by",
            )
        ).mappings().all()
        record = connection.execute(
            sa.select(tables["domain_records"]).where(
                tables["domain_records"].c.id == 2096
            )
        ).mappings().one()

        assert len(fields) == 1
        assert fields[0]["required"] is False
        assert fields[0]["validation"] == {
            "max_length": 80,
            "pattern": r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
        }
        assert record["data"] == {"slug": "duplicate", "state": "exploring"}
        assert record["version"] == 1


def test_members_blockers_migration_adds_list_fields_to_domain_one_object_type_75():
    contract_migration = importlib.import_module(MIGRATION_MODULE)
    members_blockers_migration = importlib.import_module(
        MEMBERS_BLOCKERS_MIGRATION_MODULE
    )
    engine = sa.create_engine("sqlite://")
    metadata, tables = _schema()
    metadata.create_all(engine)

    with engine.begin() as connection:
        _seed(connection, tables)
        connection.execute(
            tables["domain_object_types"].insert(),
            {
                "id": 75,
                "domain_id": 1,
                "key": "chantier",
                "name": "Chantier",
                "description": None,
                "title_field": "title",
                "sort_order": 5,
                "archived_at": None,
            },
        )
        contract_migration._upgrade(connection)

        members_blockers_migration._upgrade(connection)
        members_blockers_migration._upgrade(connection)

        fields = connection.execute(
            sa.select(tables["domain_field_definitions"])
            .where(
                tables["domain_field_definitions"].c.object_type_id == 75,
                tables["domain_field_definitions"].c.key.in_(
                    ["blockers", "member_refs"]
                ),
            )
            .order_by(tables["domain_field_definitions"].c.key)
        ).mappings().all()

        assert [field["key"] for field in fields] == ["blockers", "member_refs"]
        for field in fields:
            assert field["field_type"] == "json"
            assert field["required"] is False
            assert field["validation"] == {
                "type": "array",
                "items": {"type": "string", "min_length": 1},
            }
