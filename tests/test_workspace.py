"""Workspace errors must identify the file and field."""

import shutil

import pytest
import yaml

from illo.ledger import people
from illo.workspace import WorkspaceError, load_workspace
from tests.conftest import ROOT


@pytest.fixture
def example(tmp_path):
    path = tmp_path / "workspace"
    shutil.copytree(ROOT / "workspace.example", path)
    return path


def test_complete_example(workspace):
    assert len(workspace.people) == 3
    assert len(workspace.rules) == 2
    assert len(workspace.skills) == 1
    assert len(workspace.routines) == 2
    assert len(workspace.connectors) == 1
    assert len(workspace.notes) == 1
    assert workspace.config.slack.channels[1].mode == "coordinator"


@pytest.mark.parametrize(
    ("file", "field", "value"),
    [
        ("illo.yaml", "timezone", "Invalid/Zone"),
        ("illo.yaml", "default_owner", "missing"),
        ("illo.yaml", "extra_field", True),
    ],
)
def test_rejects_bad_team_field(example, file, field, value):
    path = example / file
    data = yaml.safe_load(path.read_text())
    data[field] = value
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(WorkspaceError, match=field) as exc:
        load_workspace(example)
    assert file in str(exc.value)


def test_bad_front_matter_names_file(example):
    (example / "rules/team.md").write_text("No front matter")
    with pytest.raises(WorkspaceError, match="rules/team.md: front matter"):
        load_workspace(example)


def test_bad_routine_schedule(example):
    path = example / "routines/digest.md"
    path.write_text(path.read_text().replace("0 9 * * 1-5", "not cron"))
    with pytest.raises(WorkspaceError, match="schedule"):
        load_workspace(example)


def test_duplicate_identity(example):
    path = example / "people.yaml"
    path.write_text(path.read_text().replace("U_ACME_BEN", "U_ACME_ADA"))
    with pytest.raises(WorkspaceError, match="slack_id: duplicate"):
        load_workspace(example)


def test_missing_rule(example):
    (example / "rules/team.md").unlink()
    with pytest.raises(WorkspaceError, match="rule_files"):
        load_workspace(example)


async def test_sync_insert_update_deactivate(conn, example):
    path = example / "people.yaml"
    data = yaml.safe_load(path.read_text())
    data["people"][0]["name"] = "Ada Example"
    data["people"] = data["people"][:2]
    path.write_text(yaml.safe_dump(data))
    await load_workspace(example).sync_people(conn)
    assert (await people.get(conn, "ada"))["name"] == "Ada Example"
    assert await people.get(conn, "chloe") is None
    row = await (await conn.execute("SELECT active FROM people WHERE key='chloe'")).fetchone()
    assert row["active"] is False
