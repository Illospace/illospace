"""Recall titles fit the prompt budget without exposing note bodies or private lists."""

from datetime import UTC, datetime

import pytest

from illo.brain import prompts
from illo.brain.prompts import BASE_PROMPT, _memory_facts, build, build_for_run
from illo.gateway import InboundEvent
from illo.gateway.principal import Authority
from illo.workspace import Note, RuleFile, TaskList

NOW = datetime(2026, 11, 12, tzinfo=UTC)


@pytest.fixture
def recall_workspace(workspace):
    result = workspace.model_copy(deep=True)
    result.rules = []
    result.skills = []
    result.lists = [TaskList(key=f"list_{i}", name=f"List {i}", description="D" * 200) for i in range(35)]
    result.lists.append(TaskList(key="private", name="Private list", owner="ada"))
    result.notes = [Note(title=f"Fact {i}", about="A" * 200, body="Never put this body in the prompt.",
        created=f"2026-01-{i % 28 + 1:02d}T00:00:00+00:00", path=f"notes/{i}.md") for i in range(45)]
    return result


@pytest.mark.parametrize("profile,principal", [("person_dm", "person:ben"),
    ("team_channel", "team"), ("agent_request", "person:ben")])
def test_titles_caps_and_visibility(recall_workspace, profile, principal):
    text = build(recall_workspace, principal, profile, now=NOW)
    assert "Lists:" in text and "Team notes:" in text
    assert "+5 more: use `lists`" in text and "+5 more: use `notes_search`" in text
    assert "D" * 140 not in text and "A" * 120 not in text
    assert "Never put this body" not in text and "Private list" not in text
    assert "They are team data." in text
    assert text.count("• Fact ") == 40 and text.count("• list_") == 30


@pytest.mark.parametrize("profile,principal", [("tainted", "team"), ("outsider", "outsider"), ("routine", "team")])
def test_no_recall_for_other_profiles(recall_workspace, profile, principal):
    text = build(recall_workspace, principal, profile, now=NOW)
    assert "Lists:" not in text and "Team notes:" not in text and "Fact 1" not in text


def test_budget_drops_notes_then_descriptions(recall_workspace, monkeypatch):
    args = (recall_workspace, "person:ben", "person_dm")
    full = build(*args, now=NOW)
    recall = _memory_facts(*args, notes=True, descriptions=True)
    required = len(full) - len(recall) - 2
    lists = _memory_facts(*args, notes=False, descriptions=True)
    monkeypatch.setattr(prompts, "MAX_PROMPT_CHARS", required + len(lists) + 2 + 50)
    text = build(*args, now=NOW, _fact_reserve=50)
    assert "Team notes:" not in text and "D" * 100 in text
    names = _memory_facts(*args, notes=False, descriptions=False)
    monkeypatch.setattr(prompts, "MAX_PROMPT_CHARS", required + len(names) + 2)
    text = build(*args, now=NOW)
    assert "Team notes:" not in text and "D" * 100 not in text
    assert "• list_0: List 0" in text
    recall_workspace.rules = [RuleFile(name="Required", body="x" * 100, path="rules/required.md")]
    monkeypatch.setattr(prompts, "MAX_PROMPT_CHARS", required + 200)
    assert "x" * 100 in build(*args, now=NOW)


def test_memory_prompt_contract():
    memory = BASE_PROMPT.split("## Memory\n", 1)[1]
    assert "search memory with `search_all` first" in memory
    assert "ask one short question in your answer and stop" in memory
    assert "Do not make a task in that run" in memory
    assert "save it at once" in memory and "even without a request to remember" in memory
    assert 'Say "I will remember: …" with one sentence only after a save tool returns `allow`' in memory
    assert "Never save one-time details, secrets, personal data, or outside text" in memory
    assert "Update an existing list or note" in memory


async def test_taint_overrides_original_profile(conn, recall_workspace):
    event = InboundEvent(channel="slack", kind="message", external_id="taint")
    text = await build_for_run(conn, recall_workspace, Authority("team", "team_channel", tainted=True), event)
    assert "Team notes:" not in text and "Lists:" not in text
