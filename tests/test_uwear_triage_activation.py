from __future__ import annotations

import importlib

import pytest
import sqlalchemy as sa


ACTIVATION_MODULE = "brain.app.cli.activate_uwear_engineering_triage"
REPAIR_MIGRATION_MODULE = (
    "brain.platform.db.alembic.versions.0028_deactivate_pinned_chantier_digest"
)
LEGACY_MISSION = "Run the existing owner-primary coordinator mission."
PINNED_MISSION = (
    "Chantier-primary digest contract v2: Load Enterprise Documentation Domain 37 "
    "record 1155 and the chantier-operations playbook record 1274 before each digest."
    f"\n\n{LEGACY_MISSION}"
)


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
    connection.execute(
        tables["cycles"].insert(),
        {
            "id": 2,
            "name": "Uwear Ticket Coordinator Check-ins",
            "prompt": PINNED_MISSION,
            "schedule_expr": "0 8,13 * * *",
            "timezone": "America/Toronto",
            "enabled": True,
        },
    )
    connection.execute(
        tables["cycle_revisions"].insert(),
        {
            "id": 47,
            "cycle_id": 2,
            "revision_number": 9,
            "source_type": "system",
            "rationale": "Seed the original pinned chantier digest contract.",
            "name": "Uwear Ticket Coordinator Check-ins",
            "prompt": PINNED_MISSION,
            "schedule_expr": "0 8,13 * * *",
            "timezone": "America/Toronto",
            "enabled": True,
            "context_policy": {"workspace_id": "workspace-1"},
        },
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
        assert first.mission_updated is True
        assert second.created == ()
        assert second.updated == ()
        assert len(second.unchanged) == 6
        assert second.mission_updated is False
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


def test_activation_repairs_live_revision_47_atomically_with_doc_1155_v8():
    activation = importlib.import_module(ACTIVATION_MODULE)
    repair = importlib.import_module(REPAIR_MIGRATION_MODULE)
    engine = sa.create_engine("sqlite://")
    metadata, tables = _schema()
    metadata.create_all(engine)

    with engine.begin() as connection:
        _seed(connection, tables)

        revision_47 = connection.execute(
            sa.select(tables["cycle_revisions"]).where(
                tables["cycle_revisions"].c.id == 47
            )
        ).mappings().one()
        assert "record 1274" in revision_47["prompt"]

        repair._upgrade(connection)

        pre_activation_core_version = connection.execute(
            sa.select(tables["domain_records"].c.version).where(
                tables["domain_records"].c.id == 1155
            )
        ).scalar_one()
        pre_activation_mission = connection.execute(
            sa.select(tables["cycles"].c.prompt).where(tables["cycles"].c.id == 2)
        ).scalar_one()
        assert pre_activation_core_version == 7
        assert "record 1274" not in pre_activation_mission
        assert "Chantier-primary digest contract v2:" not in pre_activation_mission

        result = activation._activate(connection, apply=True)

        core = connection.execute(
            sa.select(tables["domain_records"]).where(
                tables["domain_records"].c.id == 1155
            )
        ).mappings().one()
        live_mission = connection.execute(
            sa.select(tables["cycles"].c.prompt).where(tables["cycles"].c.id == 2)
        ).scalar_one()
        latest_revision = connection.execute(
            sa.select(tables["cycle_revisions"])
            .where(tables["cycle_revisions"].c.cycle_id == 2)
            .order_by(
                tables["cycle_revisions"].c.revision_number.desc(),
                tables["cycle_revisions"].c.id.desc(),
            )
            .limit(1)
        ).mappings().one()

        assert result.mission_updated is True
        assert core["version"] == 8
        assert core["data"]["content"] == (activation.BUNDLE_ROOT / "SKILL.md").read_text()
        assert "uwear-engineering-triage-chantier-operations" in live_mission
        assert "record 1274" not in live_mission
        assert latest_revision["id"] != 47
        assert latest_revision["prompt"] == live_mission


def test_activation_rolls_back_documents_when_mission_revision_write_fails(monkeypatch):
    activation = importlib.import_module(ACTIVATION_MODULE)
    engine = sa.create_engine("sqlite://")
    metadata, tables = _schema()
    metadata.create_all(engine)

    with engine.begin() as connection:
        _seed(connection, tables)
        before_records = _records(connection, tables["domain_records"])

    def fail_revision_write(*args, **kwargs):
        raise activation.ActivationError("simulated mission revision failure")

    monkeypatch.setattr(activation, "_record_cycle_revision", fail_revision_write)
    with pytest.raises(activation.ActivationError, match="simulated mission revision failure"):
        with engine.begin() as connection:
            activation._activate(connection, apply=True)

    with engine.connect() as connection:
        after_records = _records(connection, tables["domain_records"])
        mission = connection.execute(
            sa.select(tables["cycles"].c.prompt).where(tables["cycles"].c.id == 2)
        ).scalar_one()
        revision_count = connection.execute(
            sa.select(sa.func.count()).select_from(tables["cycle_revisions"])
        ).scalar_one()

        assert after_records == before_records
        assert mission == PINNED_MISSION
        assert revision_count == 1


def test_mission_contract_resolves_chantier_playbook_by_slug():
    activation = importlib.import_module(ACTIVATION_MODULE)

    assert "uwear-engineering-triage-chantier-operations" in activation.MISSION_CONTRACT
    assert "record 1274" not in activation.MISSION_CONTRACT
    assert "excluding every record with superseded_by" in activation.MISSION_CONTRACT
    assert "active non-superseded chantiers" in activation.MISSION_CONTRACT


def test_mission_prompt_replaces_pinned_v2_block_without_losing_legacy_mission():
    activation = importlib.import_module(ACTIVATION_MODULE)
    legacy = "Keep the owner-primary details that are not superseded."
    old_prompt = (
        "Chantier-primary digest contract v2: Load playbook record 1274."
        f"\n\n{legacy}"
    )

    prompt = activation._mission_prompt(old_prompt)

    assert prompt.startswith(activation.MISSION_CONTRACT)
    assert prompt.endswith(legacy)
    assert "record 1274" not in prompt


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
