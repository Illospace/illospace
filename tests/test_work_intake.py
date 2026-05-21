from __future__ import annotations

import ast
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
                "model": "openai:gpt-5.4",
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
    assert request.profile == RunProfile.DEEP
    assert request.recipe == RunRecipe.DEEP
    assert request.target_ref == {
        "conversation_id": "conv-1",
        "kind": "chat_message",
        "event": "room_message_mention",
        "chat_trigger": {"conversation_id": "conv-1", "message_id": 22},
    }
    assert request.model_policy == {
        "tier": "high",
        "thinking": "xhigh",
        "model": "openai/gpt-5.4",
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
            "run_message": '[Idea: "Launch" | idea-1]\n\n@illo go',
            "metadata": {"execution_profile": "fast"},
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
    assert request.metadata["thread_context"]["formatted"] == "Earlier thread context"
    assert request.metadata["work_intake"]["source"] == "cortex"
    assert request.metadata["work_intake"]["actor"] == {"id": "user-1", "org_id": "org-1", "internal": False}


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

    assert request.profile == RunProfile.DEEP
    assert request.recipe == RunRecipe.DEEP
    assert request.user_id == "user-1"
    assert request.thread_id == "idea-1"
    assert request.target_ref["kind"] == "cortex_idea"
    assert request.target_ref["title"] == "Launch"
    assert request.model_policy == {
        "tier": "high",
        "thinking": "high",
        "model": "anthropic/claude-sonnet-4-5",
        "provider": "anthropic",
    }
    assert request.metadata["thread_context"]["items"][0]["content"] == "Earlier"
    assert request.metadata["priority"] == 3


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
            event_type="cortex.thread_reply",
            org_id="org-1",
            actor={"id": "user-1", "org_id": "org-1", "internal": False},
            target={"kind": "cortex_idea", "idea_id": "idea-1"},
            payload={
                "message": "[Idea: \"Launch\" | idea-1]\n\n@illo this is what we decided, carry on",
                "metadata": {
                    "originating_surface": "thread_discussion",
                    "triggering_surface": "thread_discussion",
                    "discussion_trigger": discussion_trigger,
                    "required_response_tool": "post_thread_discussion_reply",
                    "final_answer_target_surface": "originating_surface",
                },
            },
            policy={"priority": 0, "producer": "trigger", "run_event": "thread_reply"},
        ),
    )

    assert request.thread_id == "idea-1"
    assert request.target_ref["originating_surface"] == "thread_discussion"
    assert request.target_ref["triggering_surface"] == "thread_discussion"
    assert request.target_ref["discussion_trigger"] == discussion_trigger
    assert request.metadata["discussion_trigger"] == discussion_trigger
    assert request.metadata["required_response_tool"] == "post_thread_discussion_reply"
    assert request.metadata["final_answer_target_surface"] == "originating_surface"


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


def test_cortex_thread_binding_compatibility_shell_is_removed():
    assert not Path("brain/systems/runs/cortex/thread_binding.py").exists()
