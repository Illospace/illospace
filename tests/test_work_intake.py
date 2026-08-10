from __future__ import annotations

import ast
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest


def _trigger_payload(**overrides):
    payload = {
        "source": "chat",
        "event_type": "chat.room_message_mention",
        "actor": {"id": "user-1", "org_id": "org-1", "internal": False},
        "org_id": "org-1",
        "target": {"conversation_id": "conv-1"},
        "payload": {
            "message": "Summarize this",
            "metadata": {
                "chat_trigger": {"conversation_id": "conv-1", "message_id": 22},
                "execution_profile": "deep",
                "model": "openai:gpt-5.6-sol",
                "provider": "openai",
                "thinking": "xhigh",
            },
        },
        "idempotency_key": "idem-1",
        "policy": {"priority": 2},
    }
    payload.update(overrides)
    return payload


@pytest.mark.asyncio
async def test_chat_work_intake_builds_agent_run_request_from_normalized_trigger():
    from brain.systems.runs.domain import RunProfile, RunRecipe
    from brain.systems.runs.work_intake import WorkIntakeEvent, build_agent_run_request

    request = await build_agent_run_request(
        object(),
        WorkIntakeEvent.from_trigger_payload(_trigger_payload()),
    )

    assert request.org_id == "org-1"
    assert request.user_id == "user-1"
    assert request.thread_id == "chat:conv-1:22"
    assert request.message == "Summarize this"
    assert request.profile == RunProfile.FAST
    assert request.recipe == RunRecipe.FAST
    assert request.target_ref == {
        "conversation_id": "conv-1",
        "kind": "chat_message",
        "event": "room_message_mention",
        "chat_trigger": {"conversation_id": "conv-1", "message_id": 22},
    }
    assert request.model_policy == {
        "thinking": "xhigh",
        "model": "openai/gpt-5.6-sol",
        "provider": "openai",
    }
    assert request.metadata["source"] == "chat"
    assert request.metadata["producer"] == "trigger"
    assert request.metadata["priority"] == 2
    assert request.metadata["idempotency_key"] == "idem-1"
    assert request.metadata["illo_trigger"]["event_type"] == "chat.room_message_mention"
    assert request.metadata["work_intake"]["source"] == "chat"


@pytest.mark.asyncio
async def test_cortex_work_intake_builds_agent_run_request_from_normalized_trigger(monkeypatch):
    from brain.systems.runs.work_intake import WorkIntakeEvent, build_agent_run_request

    class _Session:
        async def get(self, _model, _idea_id):
            return SimpleNamespace(
                id="idea-1",
                title="Launch",
                org_id="org-1",
                user_id="owner-1",
                agent_details=None,
            )

        async def scalars(self, *_args, **_kwargs):
            return SimpleNamespace(first=lambda: None)

    async def fake_thread_context(*_args, **_kwargs):
        return {"formatted": "Earlier thread context"}

    monkeypatch.setattr(
        "brain.systems.runs.work_intake.async_build_agent_visible_thread_context",
        fake_thread_context,
    )

    trigger = _trigger_payload(
        source="cortex",
        event_type="cortex.thread_reply",
        target={"idea_id": "idea-1"},
        payload={
            "user_id": "user-1",
            "thread_message": "@illo go",
            "run_message": '[Idea: "Launch" | idea-1]\n\n@illo go',
            "metadata": {
                "execution_profile": "fast",
                "human_message": "@illo go",
                "introspection_message": "@illo go",
            },
        },
        policy={"priority": 1, "run_event": "thread_reply"},
        idempotency_key="cortex-idem",
    )

    request = await build_agent_run_request(_Session(), WorkIntakeEvent.from_trigger_payload(trigger))

    assert request.thread_id == "idea-1"
    assert request.user_id == "user-1"
    assert request.message == '[Idea: "Launch" | idea-1]\n\n@illo go'
    assert request.metadata["event"] == "thread_reply"
    assert request.metadata["priority"] == 1
    assert request.metadata["source"] == "cortex"
    assert request.metadata["producer"] == "trigger"
    assert request.metadata["idempotency_key"] == "cortex-idem"
    assert request.metadata["execution_profile"] == "fast"
    assert request.metadata["human_message"] == "@illo go"
    assert request.metadata["introspection_message"] == "@illo go"
    assert request.metadata["thread_context"]["formatted"] == "Earlier thread context"
    assert request.metadata["work_intake"]["source"] == "cortex"


@pytest.mark.asyncio
async def test_api_key_openai_metadata_model_is_coerced_with_structured_log(caplog):
    from brain.systems.runs.work_intake import WorkIntakeEvent, build_agent_run_request

    trigger = _trigger_payload()
    trigger["payload"]["metadata"]["model"] = "openai/gpt-4.1"

    with caplog.at_level("WARNING", logger="work_intake"):
        request = await build_agent_run_request(
            object(),
            WorkIntakeEvent.from_trigger_payload(trigger),
        )

    assert request.model_policy["model"] == "openai/gpt-5.6-sol"
    record = next(
        record for record in caplog.records if record.event == "api_key_model_coerced"
    )
    assert record.routing_source == "chat"
    assert record.requested_value == "openai/gpt-4.1"
    assert record.coerced_value == "openai/gpt-5.6-sol"


@pytest.mark.asyncio
async def test_bare_api_key_model_is_normalized_away_with_structured_log(caplog):
    from brain.systems.runs.work_intake import WorkIntakeEvent, build_agent_run_request

    trigger = _trigger_payload()
    trigger["payload"]["metadata"]["model"] = "gpt-4.1"

    with caplog.at_level("WARNING", logger="work_intake"):
        request = await build_agent_run_request(
            object(),
            WorkIntakeEvent.from_trigger_payload(trigger),
        )

    assert request.model_policy["model"] == "openai/gpt-5.5"
    record = next(
        record for record in caplog.records if record.event == "api_key_model_coerced"
    )
    assert record.requested_value == "gpt-4.1"
    assert record.coerced_value == "openai/gpt-5.5"


@pytest.mark.asyncio
async def test_admitted_model_policy_stores_the_normalized_id():
    from brain.systems.runs.work_intake import WorkIntakeEvent, build_agent_run_request

    trigger = _trigger_payload()
    trigger["payload"]["metadata"]["model"] = " OPENAI/GPT-5.6-SOL "

    request = await build_agent_run_request(
        object(),
        WorkIntakeEvent.from_trigger_payload(trigger),
    )

    assert request.model_policy["model"] == "openai/gpt-5.6-sol"


@pytest.mark.asyncio
async def test_anthropic_metadata_model_passes_admission_guard_untouched():
    from brain.systems.runs.work_intake import WorkIntakeEvent, build_agent_run_request

    trigger = _trigger_payload()
    trigger["payload"]["metadata"]["model"] = "anthropic/claude-sonnet-5"
    trigger["payload"]["metadata"]["provider"] = "anthropic"

    request = await build_agent_run_request(
        object(),
        WorkIntakeEvent.from_trigger_payload(trigger),
    )

    assert request.model_policy["model"] == "anthropic/claude-sonnet-5"
    assert request.metadata["work_intake"]["actor"] == {"id": "user-1", "org_id": "org-1", "internal": False}


@pytest.mark.asyncio
async def test_cortex_work_intake_promotes_actor_org_to_run_identity(monkeypatch):
    from brain.systems.runs.work_intake import WorkIntakeEvent, build_agent_run_request

    class _Session:
        async def get(self, _model, _idea_id):
            return SimpleNamespace(
                id="idea-1",
                title="Launch",
                org_id=None,
                user_id="owner-1",
                agent_details=None,
            )

        async def scalars(self, *_args, **_kwargs):
            return SimpleNamespace(first=lambda: None)

    async def fake_thread_context(*_args, **_kwargs):
        return {}

    monkeypatch.setattr(
        "brain.systems.runs.work_intake.async_build_agent_visible_thread_context",
        fake_thread_context,
    )

    trigger = _trigger_payload(
        source="cortex",
        event_type="cortex.thread_reply",
        org_id="",
        target={"idea_id": "idea-1"},
        payload={"user_id": "user-1", "message": "Build a workspace app", "metadata": {}},
        policy={"run_event": "thread_reply"},
    )

    request = await build_agent_run_request(_Session(), WorkIntakeEvent.from_trigger_payload(trigger))

    assert request.org_id == "org-1"
    assert request.metadata["org_id"] == "org-1"
    assert request.metadata["work_intake"]["org_id"] == "org-1"


@pytest.mark.asyncio
async def test_cortex_agent_run_request_uses_shared_work_intake_policy(monkeypatch):
    from brain.systems.runs.domain import RunProfile, RunRecipe
    from brain.systems.runs.work_intake import WorkIntakeEvent, build_agent_run_request

    class _Session:
        async def get(self, _model, _idea_id):
            return SimpleNamespace(
                id="idea-1",
                title="Launch",
                org_id="org-1",
                user_id="owner-1",
                agent_details=None,
            )

    async def fake_thread_context(*_args, **_kwargs):
        return {"items": [{"role": "user", "content": "Earlier"}]}

    monkeypatch.setattr(
        "brain.systems.runs.work_intake.async_build_agent_visible_thread_context",
        fake_thread_context,
    )

    request = await build_agent_run_request(
        _Session(),
        WorkIntakeEvent(
            source="cortex",
            event_type="cortex.thread_reply",
            org_id="org-1",
            actor={"id": "user-1", "org_id": "org-1", "internal": False},
            target={"kind": "cortex_idea", "idea_id": "idea-1"},
            payload={
                "message": "@illo continue",
                "metadata": {
                    "execution_profile": "deep",
                    "model_provider": "anthropic",
                    "model_name": "anthropic/claude-sonnet-4-5",
                    "effort_level": "high",
                },
            },
            policy={"priority": 3, "producer": "trigger", "idempotency_key": "idem-2"},
        ),
    )

    assert request.profile == RunProfile.FAST
    assert request.recipe == RunRecipe.FAST
    assert request.user_id == "user-1"
    assert request.thread_id == "idea-1"
    assert request.target_ref["kind"] == "cortex_idea"
    assert request.target_ref["title"] == "Launch"
    assert request.model_policy == {
        "thinking": "high",
        "model": "anthropic/claude-sonnet-4-5",
        "provider": "anthropic",
    }
    assert request.metadata["thread_context"]["items"][0]["content"] == "Earlier"
    assert request.metadata["priority"] == 3


@pytest.mark.asyncio
async def test_cortex_work_intake_stamps_project_identity_from_latest_attachment(monkeypatch):
    from brain.systems.runs.work_intake import WorkIntakeEvent, build_agent_run_request

    class _Session:
        async def get(self, model, key):
            name = getattr(model, "__name__", "")
            if name == "Idea":
                return SimpleNamespace(
                    id="idea-1",
                    title="Launch",
                    org_id="org-1",
                    user_id="owner-1",
                    agent_details=None,
                )
            if name == "ProjectProfile":
                assert key == "profile-1"
                return SimpleNamespace(
                    id="profile-1",
                    slug="strategy-room",
                    name="Strategy Room",
                    description=None,
                    active=True,
                    project_context={"resources": []},
                )
            return None

        async def scalars(self, *_args, **_kwargs):
            attachment = SimpleNamespace(project_profile_id="profile-1", snapshot={"resources": []})
            return SimpleNamespace(first=lambda: attachment)

    async def fake_thread_context(*_args, **_kwargs):
        return {}

    monkeypatch.setattr(
        "brain.systems.runs.work_intake.async_build_agent_visible_thread_context",
        fake_thread_context,
    )

    request = await build_agent_run_request(
        _Session(),
        WorkIntakeEvent(
            source="cortex",
            event_type="cortex.thread_reply",
            org_id="org-1",
            actor={"id": "user-1", "org_id": "org-1", "internal": False},
            target={"kind": "cortex_idea", "idea_id": "idea-1"},
            payload={"message": "@illo continue", "metadata": {}},
        ),
    )

    assert request.workspace_ref["project_key"] == "profile-1"
    assert request.workspace_ref["project_id"] == "profile-1"
    assert request.workspace_ref["slug"] == "strategy-room"
    assert request.target_ref["project_context_snapshot"]["project_key"] == "profile-1"


@pytest.mark.asyncio
async def test_discussion_origin_cortex_work_intake_records_surface_and_trigger_comment(monkeypatch):
    from brain.systems.runs.work_intake import WorkIntakeEvent, build_agent_run_request

    class _Session:
        async def get(self, _model, _idea_id):
            return SimpleNamespace(
                id="idea-1",
                title="Launch",
                org_id="org-1",
                user_id="owner-1",
                agent_details=None,
            )

        async def scalars(self, *_args, **_kwargs):
            return SimpleNamespace(first=lambda: None)

    async def fake_thread_context(*_args, **_kwargs):
        return {}

    monkeypatch.setattr(
        "brain.systems.runs.work_intake.async_build_agent_visible_thread_context",
        fake_thread_context,
    )

    discussion_trigger = {
        "surface": "thread_discussion",
        "thread_id": "idea-1",
        "comment_id": 7,
        "body": "@illo this is what we decided, carry on",
        "author_user_id": "user-1",
        "response_target": {
            "surface": "thread_discussion",
            "thread_id": "idea-1",
            "reply_to_comment_id": 7,
        },
    }

    request = await build_agent_run_request(
        _Session(),
        WorkIntakeEvent(
            source="cortex",
            event_type="cortex.thread_discussion_mention",
            org_id="org-1",
            actor={"id": "user-1", "org_id": "org-1", "internal": False},
            target={
                "kind": "thread_discussion",
                "idea_id": "idea-1",
                "parent_thread_id": "idea-1",
                "discussion_comment_id": 7,
                "surface": "thread_discussion",
            },
            payload={
                "thread_message": "@illo this is what we decided, carry on",
                "message": "[Idea: \"Launch\" | idea-1]\n\n@illo this is what we decided, carry on",
                "metadata": {
                    "originating_surface": "thread_discussion",
                    "triggering_surface": "thread_discussion",
                    "discussion_trigger": discussion_trigger,
                    "required_response_tool": "post_thread_discussion_reply",
                    "final_answer_target_surface": "thread_discussion",
                    "human_message": "@illo this is what we decided, carry on",
                    "introspection_message": "@illo this is what we decided, carry on",
                },
            },
            policy={"priority": 0, "producer": "trigger", "run_event": "thread_discussion_mention"},
        ),
    )

    assert request.thread_id == "thread-discussion:idea-1"
    assert request.target_ref["kind"] == "thread_discussion"
    assert request.target_ref["idea_id"] == "idea-1"
    assert request.target_ref["parent_thread_id"] == "idea-1"
    assert request.target_ref["originating_surface"] == "thread_discussion"
    assert request.target_ref["triggering_surface"] == "thread_discussion"
    assert request.target_ref["discussion_trigger"] == discussion_trigger
    assert request.target_ref["related_surfaces"] == {
        "ai_timeline": {
            "kind": "ai_timeline",
            "thread_id": "idea-1",
        }
    }
    assert request.metadata["discussion_trigger"] == discussion_trigger
    assert request.metadata["required_response_tool"] == "post_thread_discussion_reply"
    assert request.metadata["final_answer_target_surface"] == "thread_discussion"
    assert request.metadata["human_message"] == "@illo this is what we decided, carry on"
    assert request.metadata["introspection_message"] == "@illo this is what we decided, carry on"


def test_legacy_routing_logic_is_removed_from_adapters():
    files = {
        "brain/app/triggers/router.py": {
            "_metadata_choice",
            "_model_policy_from_metadata",
            "AgentRunRequest",
            "RunRecipe",
        },
    }

    for filename, forbidden_names in files.items():
        tree = ast.parse(open(filename, encoding="utf-8").read())
        defined_or_imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                defined_or_imported.add(node.name)
            elif isinstance(node, ast.ImportFrom):
                defined_or_imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.Import):
                defined_or_imported.update(alias.name for alias in node.names)

        assert not (defined_or_imported & forbidden_names), filename


def test_invalid_high_priority_metadata_warns_before_valid_fallback(caplog):
    from brain.systems.runs.work_intake import model_policy_from_metadata

    with caplog.at_level("WARNING", logger="work_intake"):
        policy = model_policy_from_metadata(
            {
                "thinking_tier": "turbo",
                "effort": "high",
            }
        )

    assert policy["thinking"] == "high"
    assert (
        "Ignoring invalid metadata value for thinking_tier; "
        "falling through to lower-priority keys"
    ) in caplog.messages


def test_model_policy_metadata_accepts_explicit_ollama_route():
    from brain.systems.runs.work_intake import model_policy_from_metadata

    assert model_policy_from_metadata(
        {
            "model": "ollama/qwen3.6-27b",
            "provider": "ollama",
            "effort": "none",
        }
    ) == {
        "model": "ollama/qwen3.6-27b",
        "provider": "ollama",
        "thinking": "none",
    }


def test_deep_profile_metadata_is_coerced_to_fast_with_structured_source_log(caplog):
    from brain.systems.runs.domain import RunProfile
    from brain.systems.runs.work_intake import profile_from_metadata

    with caplog.at_level("WARNING", logger="work_intake"):
        profile = profile_from_metadata(
            {
                "execution_profile": "deep",
                "source": "stale-browser",
            }
        )

    assert profile is RunProfile.FAST
    record = next(record for record in caplog.records if record.event == "deep_run_coerced")
    assert record.routing_source == "stale-browser"
    assert record.routing_field == "profile"
    assert record.requested_value == "deep"
    assert record.coerced_value == "fast"
    assert "source=stale-browser" in record.getMessage()


def test_deep_recipe_metadata_is_coerced_independently_with_structured_source_log(caplog):
    from brain.systems.runs.domain import RunProfile, RunRecipe
    from brain.systems.runs.work_intake import recipe_for_profile

    with caplog.at_level("WARNING", logger="work_intake"):
        recipe = recipe_for_profile(
            RunProfile.FAST,
            {
                "recipe": "deep",
                "work_intake": {"source": "notify"},
            },
        )

    assert recipe is RunRecipe.FAST
    record = next(record for record in caplog.records if record.event == "deep_run_coerced")
    assert record.routing_source == "notify"
    assert record.routing_field == "recipe"
    assert record.requested_value == "deep"
    assert record.coerced_value == "fast"
    assert "source=notify" in record.getMessage()


def test_scout_recipe_metadata_uses_retired_recipe_coercion_log(caplog):
    from brain.systems.runs.domain import RunProfile, RunRecipe
    from brain.systems.runs.work_intake import recipe_for_profile

    with caplog.at_level("WARNING", logger="work_intake"):
        recipe = recipe_for_profile(
            RunProfile.FAST,
            {
                "recipe": "scout",
                "work_intake": {"source": "stale-client"},
            },
        )

    assert recipe is RunRecipe.FAST
    record = next(record for record in caplog.records if record.event == "deep_run_coerced")
    assert record.routing_source == "stale-client"
    assert record.routing_field == "recipe"
    assert record.requested_value == "scout"
    assert record.coerced_value == "fast"
    assert "retired scout run recipe" in record.getMessage()


def test_cortex_thread_binding_compatibility_shell_is_removed():
    assert not Path("brain/systems/runs/cortex/thread_binding.py").exists()


@pytest.mark.asyncio
async def test_cycle_api_key_payload_model_policy_is_coerced(monkeypatch):
    from brain.systems.runs.work_intake import WorkIntakeEvent, build_agent_run_request

    class _Session:
        async def get(self, _model, _idea_id):
            return SimpleNamespace(
                id="idea-1",
                title="Cycle thread",
                org_id="org-1",
                user_id="owner-1",
                agent_details=None,
            )

        async def scalars(self, *_args, **_kwargs):
            return SimpleNamespace(first=lambda: None)

    async def fake_thread_context(*_args, **_kwargs):
        return {}

    monkeypatch.setattr(
        "brain.systems.runs.work_intake.async_build_agent_visible_thread_context",
        fake_thread_context,
    )

    deadline_at = datetime(2026, 7, 30, 19, 0, tzinfo=timezone.utc)
    trigger = _trigger_payload(
        source="cycle",
        event_type="cycle.due_run",
        target={"idea_id": "idea-1"},
        payload={
            "message": "Run the mission",
            "metadata": {"source": "cycle", "cycle_run_id": 12},
            "model_policy": {"model": "openai/gpt-4.1", "thinking": "low"},
            "deadline_at": deadline_at,
        },
        policy={"priority": 1, "run_event": "thread_reply"},
        idempotency_key="cycle_run:12",
    )

    request = await build_agent_run_request(_Session(), WorkIntakeEvent.from_trigger_payload(trigger))

    assert request.model_policy == {"model": "openai/gpt-5.6-sol", "thinking": "low"}
    assert request.deadline_at == deadline_at


@pytest.mark.asyncio
async def test_empty_payload_model_policy_falls_back_to_metadata_parse(monkeypatch):
    from brain.systems.runs.work_intake import WorkIntakeEvent, build_agent_run_request

    class _Session:
        async def get(self, _model, _idea_id):
            return SimpleNamespace(
                id="idea-1",
                title="Cycle thread",
                org_id="org-1",
                user_id="owner-1",
                agent_details=None,
            )

        async def scalars(self, *_args, **_kwargs):
            return SimpleNamespace(first=lambda: None)

    async def fake_thread_context(*_args, **_kwargs):
        return {}

    monkeypatch.setattr(
        "brain.systems.runs.work_intake.async_build_agent_visible_thread_context",
        fake_thread_context,
    )

    trigger = _trigger_payload(
        source="cycle",
        event_type="cycle.due_run",
        target={"idea_id": "idea-1"},
        payload={
            "message": "Run the mission",
            "metadata": {"source": "cycle", "thinking_tier": "medium"},
            "model_policy": {},
        },
        policy={"priority": 1, "run_event": "thread_reply"},
        idempotency_key="cycle_run:13",
    )

    request = await build_agent_run_request(_Session(), WorkIntakeEvent.from_trigger_payload(trigger))

    assert request.model_policy == {"thinking": "medium"}
