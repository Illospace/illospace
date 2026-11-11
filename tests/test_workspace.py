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
    assert len(workspace.routines) == 1
    assert "digest_snapshot" in workspace.digest_instructions
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
    path = example / "routines/bad.md"
    path.write_text("---\nname: bad\nschedule: not cron\nprincipal: team\ndestination: none\n---\nRead the lanes.")
    with pytest.raises(WorkspaceError, match="schedule"):
        load_workspace(example)


@pytest.mark.parametrize("front", ["", "---\nname: daily-digest\nschedule: ignored\n---\n"])
def test_digest_instructions_accept_optional_front_matter(example, front):
    (example / "routines/digest.md").write_text(front + "Write for {name}.")
    loaded = load_workspace(example)
    assert loaded.digest_instructions == "Write for {name}."
    assert all(r.path != "routines/digest.md" for r in loaded.routines)


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


@pytest.mark.parametrize(("field", "length"), [("description", 301), ("name", 101)])
def test_skill_metadata_size_names_file(example, field, length):
    path = next((example / "skills").glob("*/SKILL.md"))
    front = {"name": "large", "description": "A procedure", field: "x" * length}
    path.write_text("---\n" + yaml.safe_dump(front) + "---\nSteps.")
    with pytest.raises(WorkspaceError, match=field) as error:
        load_workspace(example)
    assert str(path) in str(error.value)


def test_rule_total_prompt_size_names_file(example):
    path = example / "rules/team.md"
    path.write_text("---\nname: team\n---\n" + "x" * 60_000)
    with pytest.raises(WorkspaceError, match="rules/team.md.*60,000"):
        load_workspace(example)


def test_skill_list_total_prompt_size_names_skill(example):
    for index in range(200):
        path = example / "skills" / f"large-{index:03d}" / "SKILL.md"
        path.parent.mkdir()
        path.write_text(
            f"---\nname: large-{index:03d}\ndescription: " + "x" * 300 + "\n---\nSteps."
        )
    with pytest.raises(WorkspaceError, match=r"skills/large-.*SKILL.md.*skill large-.*60,000"):
        load_workspace(example)


async def test_oversized_workspace_reload_keeps_active_workspace(app, example):
    ctx = app.state.ctx
    previous = ctx.workspace
    ctx.config.workspace_dir = example
    path = example / "rules/team.md"
    path.write_text("---\nname: team\n---\n" + "x" * 60_000)
    with pytest.raises(WorkspaceError, match="rules/team.md.*60,000"):
        await ctx.reload()
    assert ctx.workspace is previous


def test_fixed_prompt_size_names_config_file(example):
    path = example / "illo.yaml"
    data = yaml.safe_load(path.read_text())
    data["team_name"] = "x" * 60_000
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(WorkspaceError, match="illo.yaml.*60,000"):
        load_workspace(example)


def test_combined_rule_size_names_last_file(example):
    for name in ["large-a", "large-b"]:
        path = example / "rules" / f"{name}.md"
        path.write_text(f"---\nname: {name}\n---\n" + "x" * 30_000)
    with pytest.raises(WorkspaceError, match="rules/large-b.md.*60,000"):
        load_workspace(example)
