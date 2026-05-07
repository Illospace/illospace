from __future__ import annotations


def test_slash_skill_names_treat_questions_as_commands_and_paths_as_paths():
    from brain.systems.runs.skill_commands import parse_slash_skill_names

    assert parse_slash_skill_names("what does /debug do?") == ["debug"]
    assert parse_slash_skill_names("can you explain /manage-domains?") == ["manage-domains"]
    assert parse_slash_skill_names("open /api/foo and then use /debug") == ["debug"]
    assert parse_slash_skill_names("/a/path") == []
    assert parse_slash_skill_names("use /debug and /debug again") == ["debug"]


def test_slash_skill_metadata_marks_user_interest_without_forcing_load():
    from brain.systems.runs.skill_commands import annotate_metadata_with_slash_skill_commands

    metadata = annotate_metadata_with_slash_skill_commands(
        {"run_profile": "fast"},
        "what does /debug do?",
    )

    assert metadata["run_profile"] == "fast"
    assert metadata["slash_skill_names"] == ["debug"]
    assert metadata["slash_skill_commands"] == [
        {
            "name": "debug",
            "token": "/debug",
            "kind": "skill_command",
            "source": "user_message",
        }
    ]


def test_run_context_prompts_agent_about_slash_skill_command():
    from brain.systems.runs.context import RunContextLoader

    context = RunContextLoader().load(
        thread_id="idea-1",
        message="what does /debug do?",
        metadata={},
    )

    prompt = context.prompt_context()
    assert "Slash skill command(s): /debug" in prompt
    assert "user is interested in those skills" in prompt
    assert "prefer loading the skill card or summary before the full procedure" in prompt
