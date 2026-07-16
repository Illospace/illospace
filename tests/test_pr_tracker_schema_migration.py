from __future__ import annotations

import importlib

import sqlalchemy as sa


MIGRATION_MODULE = "brain.platform.db.alembic.versions.0025_pr_tracker_owner_fields"


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
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("domain_id", sa.Integer, nullable=False),
            sa.Column("key", sa.Text, nullable=False),
            sa.Column("archived_at", sa.DateTime(timezone=True)),
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
        "cycles": sa.Table(
            "cycles",
            metadata,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("name", sa.Text, nullable=False),
            sa.Column("prompt", sa.Text, nullable=False),
            sa.Column("schedule_expr", sa.Text, nullable=False),
            sa.Column("timezone", sa.Text, nullable=False),
            sa.Column("enabled", sa.Boolean, nullable=False),
            sa.Column("model_override", sa.Text),
            sa.Column("thinking_override", sa.Text),
            sa.Column("target_idea_id", sa.Text),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        ),
        "cycle_revisions": sa.Table(
            "cycle_revisions",
            metadata,
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column("cycle_id", sa.Integer, nullable=False),
            sa.Column("revision_number", sa.Integer, nullable=False),
            sa.Column("source_type", sa.Text, nullable=False),
            sa.Column("source_id", sa.Text),
            sa.Column("rationale", sa.Text),
            sa.Column("name", sa.Text, nullable=False),
            sa.Column("prompt", sa.Text, nullable=False),
            sa.Column("schedule_expr", sa.Text, nullable=False),
            sa.Column("timezone", sa.Text, nullable=False),
            sa.Column("enabled", sa.Boolean, nullable=False),
            sa.Column("model_override", sa.Text),
            sa.Column("thinking_override", sa.Text),
            sa.Column("target_idea_id", sa.Text),
            sa.Column("context_policy", sa.JSON, nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.UniqueConstraint("cycle_id", "revision_number"),
        ),
    }
    return metadata, tables


def _seed(connection: sa.Connection, tables: dict[str, sa.Table]) -> None:
    connection.execute(
        tables["domains"].insert(),
        [
            {"id": 1, "slug": "github-ticket-tracker"},
            {"id": 2, "slug": "other-domain"},
        ],
    )
    connection.execute(
        tables["domain_object_types"].insert(),
        [
            {"id": 2, "domain_id": 1, "key": "ticket"},
            {"id": 3, "domain_id": 1, "key": "pull_request"},
            {"id": 4, "domain_id": 2, "key": "ticket"},
            {"id": 5, "domain_id": 2, "key": "pull_request"},
        ],
    )
    connection.execute(
        tables["domain_field_definitions"].insert(),
        [
            {
                "id": 8,
                "domain_id": 1,
                "object_type_id": 2,
                "key": "assignee",
                "name": "Next-action owner",
                "field_type": "text",
                "required": False,
                "options": [],
                "default_value": None,
                "validation": {"max_length": 120},
                "searchable": True,
                "sortable": True,
            },
            {
                "id": 18,
                "domain_id": 1,
                "object_type_id": 2,
                "key": "progress_note",
                "name": "Progress note",
                "field_type": "long_text",
                "required": False,
                "options": [],
                "default_value": None,
                "validation": {"max_length": 2000},
                "searchable": True,
                "sortable": False,
            },
        ],
    )
    mission = (
        "Create or refresh the ticket / pull_request record with lifecycle state, "
        "owner by next action, and a one-line next action."
    )
    connection.execute(
        tables["cycles"].insert(),
        [
            {
                "id": 2,
                "name": "Uwear Ticket Coordinator Check-ins",
                "prompt": mission,
                "schedule_expr": "0 8,13 * * *",
                "timezone": "America/Toronto",
                "enabled": True,
            },
            {
                "id": 8,
                "name": "GitHub Reflex",
                "prompt": mission,
                "schedule_expr": "*/5 * * * *",
                "timezone": "UTC",
                "enabled": True,
            },
            {
                "id": 9,
                "name": "Another Cycle",
                "prompt": mission,
                "schedule_expr": "0 0 * * *",
                "timezone": "UTC",
                "enabled": True,
            },
        ],
    )
    connection.execute(
        tables["cycle_revisions"].insert(),
        [
            {
                "cycle_id": cycle_id,
                "revision_number": 1,
                "source_type": "user",
                "name": name,
                "prompt": mission,
                "schedule_expr": schedule,
                "timezone": timezone,
                "enabled": True,
                "context_policy": {"workspace_id": "workspace-1"},
            }
            for cycle_id, name, schedule, timezone in (
                (
                    2,
                    "Uwear Ticket Coordinator Check-ins",
                    "0 8,13 * * *",
                    "America/Toronto",
                ),
                (8, "GitHub Reflex", "*/5 * * * *", "UTC"),
                (9, "Another Cycle", "0 0 * * *", "UTC"),
            )
        ],
    )


def test_migration_mirrors_pr_fields_and_reconciles_both_cycle_missions():
    migration = importlib.import_module(MIGRATION_MODULE)
    engine = sa.create_engine("sqlite://")
    metadata, tables = _schema()
    metadata.create_all(engine)

    with engine.begin() as connection:
        _seed(connection, tables)
        migration._upgrade(connection)
        migration._upgrade(connection)

        source_fields = connection.execute(
            sa.select(tables["domain_field_definitions"])
            .where(tables["domain_field_definitions"].c.object_type_id == 2)
            .order_by(tables["domain_field_definitions"].c.id)
        ).mappings().all()
        pr_fields = connection.execute(
            sa.select(tables["domain_field_definitions"])
            .where(tables["domain_field_definitions"].c.object_type_id == 3)
            .order_by(tables["domain_field_definitions"].c.id)
        ).mappings().all()
        mirrored_keys = (
            "key",
            "name",
            "field_type",
            "required",
            "options",
            "default_value",
            "validation",
            "searchable",
            "sortable",
        )
        assert len(pr_fields) == 2
        assert [tuple(row[key] for key in mirrored_keys) for row in pr_fields] == [
            tuple(row[key] for key in mirrored_keys) for row in source_fields
        ]

        prompts = dict(
            connection.execute(
                sa.select(tables["cycles"].c.id, tables["cycles"].c.prompt)
            ).all()
        )
        for cycle_id in (2, 8):
            assert prompts[cycle_id].count("Pull request write contract:") == 1
            assert '`state: "draft"`' in prompts[cycle_id]
            assert '`review_status: "pending"`' in prompts[cycle_id]
            assert "never write `draft` to `review_status`" in prompts[cycle_id]
            assert "`assignee`" in prompts[cycle_id]
            assert "`progress_note`" in prompts[cycle_id]
        assert "Pull request write contract:" not in prompts[9]

        revision_rows = connection.execute(
            sa.select(tables["cycle_revisions"]).order_by(
                tables["cycle_revisions"].c.cycle_id,
                tables["cycle_revisions"].c.revision_number,
            )
        ).mappings().all()
        assert [(row["cycle_id"], row["revision_number"]) for row in revision_rows] == [
            (2, 1),
            (2, 2),
            (8, 1),
            (8, 2),
            (9, 1),
        ]
        latest = {
            row["cycle_id"]: row
            for row in revision_rows
            if row["revision_number"] == 2
        }
        assert set(latest) == {2, 8}
        assert all(
            "Pull request write contract:" in row["prompt"]
            for row in latest.values()
        )
        assert all(
            row["context_policy"] == {"workspace_id": "workspace-1"}
            for row in latest.values()
        )
