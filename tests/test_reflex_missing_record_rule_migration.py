from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest
import sqlalchemy as sa


MIGRATION_MODULE = "brain.platform.db.alembic.versions.0068_reflex_missing_record_rule"
ORIGINAL_PROMPT = "Read the GitHub Event Feed Domain id `38`. Ignore routine noise."
RATIONALE = (
    "Record an open issue or PR that has no tracker record, even when a bot "
    "comment or label is its latest webhook action (#920)."
)


def _schema(*, optional_columns: bool = True) -> tuple[sa.MetaData, sa.Table, sa.Table]:
    metadata = sa.MetaData()

    def configuration_columns() -> list[sa.Column]:
        columns = [
            sa.Column("name", sa.Text, nullable=False),
            sa.Column("prompt", sa.Text, nullable=False),
            sa.Column("schedule_expr", sa.Text, nullable=False),
            sa.Column("timezone", sa.Text, nullable=False),
            sa.Column("enabled", sa.Boolean, nullable=False),
        ]
        if optional_columns:
            columns.extend([
                sa.Column("model_override", sa.Text),
                sa.Column("thinking_override", sa.Text),
                sa.Column("execution_policy_key", sa.Text),
                sa.Column("target_idea_id", sa.Text),
            ])
        return columns

    cycles = sa.Table(
        "cycles",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        *configuration_columns(),
        sa.Column("updated_at", sa.DateTime, server_default=sa.func.now()),
    )
    revisions = sa.Table(
        "cycle_revisions",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("cycle_id", sa.Integer, nullable=False),
        sa.Column("revision_number", sa.Integer, nullable=False),
        sa.Column("source_type", sa.Text, nullable=False),
        sa.Column("source_id", sa.Text),
        sa.Column("rationale", sa.Text),
        *configuration_columns(),
        sa.Column("context_policy", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
        sa.UniqueConstraint("cycle_id", "revision_number"),
    )
    return metadata, cycles, revisions


def _cycle_values(
    *,
    cycle_id: int = 8,
    name: str = "GitHub Reflex — event fast lane",
    prompt: str = ORIGINAL_PROMPT,
) -> dict:
    return {
        "id": cycle_id,
        "name": name,
        "prompt": prompt,
        "schedule_expr": "*/15 * * * *",
        "timezone": "America/Toronto",
        "enabled": True,
    }


@pytest.mark.parametrize("name", ["GitHub Reflex — event fast lane", "GitHub Reflex"])
def test_upgrade_appends_rule_and_one_revision_idempotently(name):
    migration = importlib.import_module(MIGRATION_MODULE)
    metadata, cycles, revisions = _schema()
    engine = sa.create_engine("sqlite:///:memory:")
    metadata.create_all(engine)
    cycle = _cycle_values(name=name, prompt=ORIGINAL_PROMPT + " \n\n")
    cycle.update(
        model_override="retired-model-pin",
        thinking_override="low",
        execution_policy_key="existing-policy",
        target_idea_id="existing-target",
    )
    context_policy = {"preserve": "existing context policy"}

    with engine.begin() as connection:
        connection.execute(cycles.insert().values(**cycle))
        connection.execute(revisions.insert().values(
            **{key: value for key, value in cycle.items() if key != "id"},
            cycle_id=8,
            revision_number=4,
            source_type="user",
            source_id="existing-source",
            rationale="Existing revision",
            context_policy=context_policy,
        ))
        original_revision = connection.execute(sa.select(revisions)).mappings().one()

        migration._upgrade(connection)
        migration._upgrade(connection)

        updated = connection.execute(sa.select(cycles)).mappings().one()
        assert updated["prompt"].startswith(ORIGINAL_PROMPT)
        assert updated["prompt"].endswith(migration._RULE)
        assert updated["prompt"].count("Missing-record rule:") == 1
        assert updated["prompt"] == ORIGINAL_PROMPT + "\n\n" + migration._RULE
        assert updated["model_override"] == cycle["model_override"]
        history = connection.execute(
            sa.select(revisions).order_by(revisions.c.revision_number)
        ).mappings().all()
        assert len(history) == 2
        assert history[0] == original_revision
        new_revision = history[1]
        assert new_revision["cycle_id"] == 8
        assert new_revision["revision_number"] == 5
        assert new_revision["source_type"] == "system"
        assert new_revision["source_id"] is None
        assert new_revision["prompt"] == updated["prompt"]
        assert new_revision["rationale"] == RATIONALE
        assert new_revision["model_override"] is None
        assert new_revision["context_policy"] == context_policy
        for field in (
            "name", "schedule_expr", "timezone", "enabled", "thinking_override",
            "execution_policy_key", "target_idea_id",
        ):
            assert new_revision[field] == cycle[field]


@pytest.mark.parametrize(
    ("cycle_id", "name", "prompt"),
    [
        (8, "Another cycle", ORIGINAL_PROMPT),
        (8, "GitHub Reflex — event fast lane", "Read another domain."),
        (9, "GitHub Reflex — event fast lane", ORIGINAL_PROMPT),
        (8, "GitHub Reflex — event fast lane", ORIGINAL_PROMPT + "\nMissing-record rule: present \n"),
    ],
    ids=["wrong-name", "wrong-domain", "wrong-id", "existing-marker"],
)
def test_upgrade_leaves_non_targets_and_existing_marker_unchanged(cycle_id, name, prompt):
    migration = importlib.import_module(MIGRATION_MODULE)
    metadata, cycles, revisions = _schema()
    engine = sa.create_engine("sqlite:///:memory:")
    metadata.create_all(engine)

    with engine.begin() as connection:
        connection.execute(cycles.insert().values(
            **_cycle_values(cycle_id=cycle_id, name=name, prompt=prompt)
        ))
        migration._upgrade(connection)
        migration._upgrade(connection)
        assert connection.execute(sa.select(cycles.c.prompt)).scalar_one() == prompt
        assert connection.execute(sa.select(sa.func.count()).select_from(revisions)).scalar_one() == 0


def test_upgrade_returns_when_cycles_table_is_absent():
    migration = importlib.import_module(MIGRATION_MODULE)
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        migration._upgrade(connection)
        assert sa.inspect(connection).get_table_names() == []


def test_upgrade_records_first_revision_without_optional_columns():
    migration = importlib.import_module(MIGRATION_MODULE)
    metadata, cycles, revisions = _schema(optional_columns=False)
    engine = sa.create_engine("sqlite:///:memory:")
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(cycles.insert().values(**_cycle_values()))
        migration._upgrade(connection)
        migration._upgrade(connection)
        revision = connection.execute(sa.select(revisions)).mappings().one()
        assert revision["revision_number"] == 1
        assert revision["context_policy"] == {}
        assert revision["prompt"] == ORIGINAL_PROMPT + "\n\n" + migration._RULE
        assert revision["rationale"] == RATIONALE


def test_upgrade_updates_prompt_without_revisions_table():
    migration = importlib.import_module(MIGRATION_MODULE)
    _, cycles, _ = _schema()
    engine = sa.create_engine("sqlite:///:memory:")
    cycles.create(engine)
    with engine.begin() as connection:
        connection.execute(cycles.insert().values(**_cycle_values()))
        migration._upgrade(connection)
        migration._upgrade(connection)
        assert connection.execute(sa.select(cycles.c.prompt)).scalar_one() == (
            ORIGINAL_PROMPT + "\n\n" + migration._RULE
        )


def test_rule_preserves_open_state_bot_exception_and_commenter_distinction():
    rule = importlib.import_module(MIGRATION_MODULE)._RULE
    assert "If the item's `state` is `open` and Domain 1 has no record for it" in rule
    assert (
        "Bot exception: when `event` is `issues` or `pull_request`, `author` is the "
        "account that opened the item; if that account is a bot, ignore the item as before."
    ) in rule
    assert (
        "When `event` is `issue_comment`, `author` is only the commenter and says "
        "nothing about who opened the item, so create the record."
    ) in rule


def test_context_admission_fixture_includes_rule_length():
    rule = importlib.import_module(MIGRATION_MODULE)._RULE
    fixture = json.loads(
        Path("tests/fixtures/enabled_cycles_context_admission.json").read_text()
    )
    reflex = next(cycle for cycle in fixture["cycles"] if cycle["cycle_id"] == 8)
    assert reflex["prompt_chars"] == 4138 + 2 + len(rule)
