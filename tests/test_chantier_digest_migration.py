from __future__ import annotations

import importlib

import sqlalchemy as sa


MIGRATION_MODULE = "brain.platform.db.alembic.versions.0027_chantier_digest_v2"
REPAIR_MODULE = (
    "brain.platform.db.alembic.versions.0028_deactivate_pinned_chantier_digest"
)


def _schema() -> tuple[sa.MetaData, dict[str, sa.Table]]:
    metadata = sa.MetaData()
    tables = {
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
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
            ),
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
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
            ),
            sa.UniqueConstraint("cycle_id", "revision_number"),
        ),
        "domain_records": sa.Table(
            "domain_records",
            metadata,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("domain_id", sa.Integer, nullable=False),
            sa.Column("version", sa.Integer, nullable=False),
            sa.Column("data", sa.JSON, nullable=False),
        ),
    }
    return metadata, tables


def _seed(connection: sa.Connection, tables: dict[str, sa.Table]) -> None:
    mission = "Run the existing owner-primary coordinator mission."
    cycles = (
        (
            2,
            "Uwear Ticket Coordinator Check-ins",
            "0 8,13 * * *",
            "America/Toronto",
        ),
        (3, "Uwear Ticket Coordinator Check-ins", "0 9 * * *", "America/Toronto"),
        (8, "GitHub Reflex", "*/5 * * * *", "UTC"),
    )
    connection.execute(
        tables["cycles"].insert(),
        [
            {
                "id": cycle_id,
                "name": name,
                "prompt": mission,
                "schedule_expr": schedule,
                "timezone": timezone,
                "enabled": True,
            }
            for cycle_id, name, schedule, timezone in cycles
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
            for cycle_id, name, schedule, timezone in cycles
        ],
    )
    connection.execute(
        tables["domain_records"].insert(),
        {
            "id": 1155,
            "domain_id": 37,
            "version": 8,
            "data": {
                "slug": "uwear-engineering-triage",
                "content": "The verified chantier operating model.",
            },
        },
    )


def test_migration_seeds_only_coordinator_with_chantier_digest_v2():
    migration = importlib.import_module(MIGRATION_MODULE)
    engine = sa.create_engine("sqlite://")
    metadata, tables = _schema()
    metadata.create_all(engine)

    with engine.begin() as connection:
        _seed(connection, tables)
        migration._upgrade(connection)
        migration._upgrade(connection)

        prompts = dict(
            connection.execute(
                sa.select(tables["cycles"].c.id, tables["cycles"].c.prompt)
            ).all()
        )
        coordinator = prompts[2]
        assert coordinator.startswith("Chantier-primary digest contract v2:")
        assert coordinator.count("Chantier-primary digest contract v2:") == 1
        for expected in (
            "exact active-chantier count",
            "one goal-progress line",
            "roll quiet chantiers into one line",
            "Loose items",
            "Per-person recap footer naming Reda, Axel, and JB",
            "exact-assignee/GitHub-issue/authored-PR/builder-candidate empty checks",
            "rebalancing recommendation",
            "a chantier may not depart silently",
            "state change, member gain/loss, or blocker hit/clear",
            "only propose, never auto-create",
            "untouched 3+ days",
            "outcome summary in goal language, not PR counts",
        ):
            assert expected in coordinator
        assert prompts[3] == "Run the existing owner-primary coordinator mission."
        assert prompts[8] == "Run the existing owner-primary coordinator mission."

        revisions = connection.execute(
            sa.select(tables["cycle_revisions"]).order_by(
                tables["cycle_revisions"].c.cycle_id,
                tables["cycle_revisions"].c.revision_number,
            )
        ).mappings().all()
        assert [(row["cycle_id"], row["revision_number"]) for row in revisions] == [
            (2, 1),
            (2, 2),
            (3, 1),
            (8, 1),
        ]
        latest = next(
            row
            for row in revisions
            if row["cycle_id"] == 2 and row["revision_number"] == 2
        )
        assert latest["prompt"] == coordinator
        assert latest["source_type"] == "system"
        assert latest["context_policy"] == {"workspace_id": "workspace-1"}


def test_v2_contract_stays_visible_at_front_of_near_cap_mission():
    migration = importlib.import_module(MIGRATION_MODULE)

    prompt = migration._mission_prompt("z" * 12_000)

    assert prompt.startswith("Chantier-primary digest contract v2:")
    assert prompt.endswith("z" * 12_000)


def test_migration_replay_with_cycle_2_and_doc_1155_v7_does_not_raise(caplog):
    migration = importlib.import_module(MIGRATION_MODULE)
    repair = importlib.import_module(REPAIR_MODULE)
    engine = sa.create_engine("sqlite://")
    metadata, tables = _schema()
    metadata.create_all(engine)

    with engine.begin() as connection:
        _seed(connection, tables)
        connection.execute(
            tables["domain_records"]
            .update()
            .where(tables["domain_records"].c.id == 1155)
            .values(version=7, data={"slug": "uwear-engineering-triage", "content": "v7"})
        )

        migration._upgrade(connection)
        repair._upgrade(connection)
        repair._upgrade(connection)

        prompt = connection.execute(
            sa.select(tables["cycles"].c.prompt).where(tables["cycles"].c.id == 2)
        ).scalar_one()
        revisions = connection.execute(
            sa.select(tables["cycle_revisions"])
            .where(tables["cycle_revisions"].c.cycle_id == 2)
            .order_by(tables["cycle_revisions"].c.revision_number)
        ).mappings().all()
        assert prompt == "Run the existing owner-primary coordinator mission."
        assert "record 1274" in revisions[-2]["prompt"]
        assert revisions[-1]["prompt"] == prompt
        assert [row["revision_number"] for row in revisions] == [1, 2, 3]
        assert "deploy remains usable" in caplog.text


def test_repair_migration_no_ops_loudly_when_unactivated_cycle_has_no_pinned_contract(
    caplog,
):
    repair = importlib.import_module(REPAIR_MODULE)
    engine = sa.create_engine("sqlite://")
    metadata, tables = _schema()
    metadata.create_all(engine)

    with engine.begin() as connection:
        _seed(connection, tables)
        connection.execute(
            tables["domain_records"]
            .update()
            .where(tables["domain_records"].c.id == 1155)
            .values(version=7, data={"slug": "uwear-engineering-triage", "content": "v7"})
        )

        repair._upgrade(connection)

        assert "slug-based activation remains pending" in caplog.text
