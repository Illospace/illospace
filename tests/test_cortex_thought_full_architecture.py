from __future__ import annotations

import ast
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]

THOUGHT_MODULE = "brain/systems/cortex/thought_lifecycle.py"

THREAD_AND_STATUS_PRODUCERS = {
    "brain/app/api/routers/cortex/_idea_ops.py",
    "brain/app/api/routers/cortex/_ideas.py",
    "brain/app/api/routers/cortex/_misc.py",
    "brain/systems/cycles/service.py",
    "brain/systems/external_agents/service.py",
    "brain/systems/runs/cortex/runner.py",
    "brain/systems/runs/tool_catalog/handlers/ideas.py",
}

EXPECTED_THOUGHT_API = {
    "ThreadMessageCommand",
    "ThoughtStatusCommand",
    "TerminalRunSettlementCommand",
    "post_thread_message",
    "transition_thought_status",
    "mirror_run_final_answer",
    "settle_terminal_run",
}

REMOVED_THOUGHT_RUN_ADMISSION_API = {
    "ThoughtRunAdmissionCommand",
    "admit_thought_run",
}


class _Session:
    def __init__(self) -> None:
        self.added: list[object] = []
        self.flushed = False

    def add(self, obj: object) -> None:
        if getattr(obj, "id", None) is None and obj.__class__.__name__ == "IdeaThread":
            obj.id = 71
        if getattr(obj, "created_at", None) is None:
            obj.created_at = datetime(2026, 5, 15, 14, 30, tzinfo=timezone.utc)
        self.added.append(obj)

    async def flush(self) -> None:
        self.flushed = True


def _idea(**overrides):
    defaults = {
        "id": "idea-1",
        "title": "Launch thread",
        "status": "working",
        "org_id": "org-1",
        "user_id": "owner-1",
        "agent_details": None,
        "updated_at": datetime(2026, 5, 15, 14, 0, tzinfo=timezone.utc),
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _tree(path: str) -> ast.AST:
    return ast.parse((ROOT / path).read_text(encoding="utf-8"))


def _function_names(tree: ast.AST) -> set[str]:
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _base_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return _base_name(node.value)
    if isinstance(node, ast.Subscript):
        return _base_name(node.value)
    return None


def _iter_call_violations(path: str) -> list[str]:
    tree = _tree(path)
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        if name in {"IdeaThread", "IdeaStateLog"}:
            violations.append(f"{path}:{node.lineno} constructs {name}")
        if name == "a_add_message":
            violations.append(f"{path}:{node.lineno} writes thread via IdeaThreadRepository")
    return violations


def _iter_status_assignment_violations(path: str) -> list[str]:
    tree = _tree(path)
    violations: list[str] = []
    for node in ast.walk(tree):
        targets: list[ast.AST] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        elif isinstance(node, ast.AugAssign):
            targets = [node.target]
        for target in targets:
            if not isinstance(target, ast.Attribute) or target.attr != "status":
                continue
            if _base_name(target.value) in {"idea", "parent"}:
                violations.append(f"{path}:{target.lineno} assigns thought status directly")
    return violations


def _called_names(path: str, function_name: str) -> set[str]:
    tree = _tree(path)
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name
    )
    return {
        name
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        for name in [_call_name(node)]
        if name
    }


def test_thought_module_exposes_complete_lifecycle_api():
    tree = _tree(THOUGHT_MODULE)
    function_names = _function_names(tree)
    missing = EXPECTED_THOUGHT_API - function_names

    assert missing == set()
    assert REMOVED_THOUGHT_RUN_ADMISSION_API.isdisjoint(function_names)


@pytest.mark.asyncio
async def test_terminal_run_settlement_mirrors_final_answer_once_and_transitions_status():
    from brain.systems.cortex.thought_lifecycle import (
        TerminalRunSettlementCommand,
        settle_terminal_run,
    )

    session = _Session()
    idea = _idea(status="working")
    published: list[tuple[str, dict]] = []

    result = await settle_terminal_run(
        session,
        idea=idea,
        command=TerminalRunSettlementCommand(
            run_id=17,
            run_status="completed",
            final_answer="I shipped the fix.",
            artifact_id=901,
        ),
        publish=lambda event_type, payload: published.append((event_type, payload)),
    )

    assert idea.status == "unread_reply"
    assert result.status_change == {
        "idea_id": "idea-1",
        "old_status": "working",
        "new_status": "unread_reply",
        "org_id": "org-1",
        "run_id": 17,
    }
    assert result.thread_message_payload == {
        "id": 71,
        "idea_id": "idea-1",
        "role": "illo",
        "content": "I shipped the fix.",
        "attachments": [],
        "metadata": {
            "run_id": 17,
            "artifact_id": 901,
            "source": "agent_run_final_answer",
        },
        "user_id": None,
        "message_type": "agent_response",
        "created_at": "2026-05-15T14:30:00+00:00",
    }
    assert [
        (log.from_state, log.to_state, log.trigger)
        for log in session.added
        if log.__class__.__name__ == "IdeaStateLog"
    ] == [("working", "unread_reply", "agent_run_completed")]
    assert published == [
        (
            "thread_message",
            {"idea_id": "idea-1", "message": result.thread_message_payload},
        ),
        (
            "status_change",
            {"idea_id": "idea-1", "new_status": "unread_reply", "run_id": 17},
        ),
    ]


@pytest.mark.asyncio
async def test_terminal_run_settlement_promotes_static_upload_links_to_attachments(monkeypatch, tmp_path):
    from brain.systems.cortex.thought_lifecycle import (
        TerminalRunSettlementCommand,
        settle_terminal_run,
    )
    from brain.systems.cortex import thread_assets

    upload_file = tmp_path / "thread-assets" / "idea-1" / "architecture.png"
    upload_file.parent.mkdir(parents=True)
    upload_file.write_bytes(b"png")
    monkeypatch.setattr(thread_assets, "UPLOAD_DIR", tmp_path)

    session = _Session()
    idea = _idea(status="working")
    body = (
        "Here is the diagram:\n\n"
        "![Architecture](/static/uploads/thread-assets/idea-1/architecture.png)"
    )

    result = await settle_terminal_run(
        session,
        idea=idea,
        command=TerminalRunSettlementCommand(
            run_id=18,
            run_status="completed",
            final_answer=body,
            artifact_id=902,
        ),
    )

    assert result.thread_message_payload is not None
    assert result.thread_message_payload["attachments"] == [
        {
            "url": "/static/uploads/thread-assets/idea-1/architecture.png",
            "download_url": "/static/uploads/thread-assets/idea-1/architecture.png",
            "filename": "architecture.png",
            "label": "architecture.png",
            "kind": "image",
            "content_type": "image/png",
            "mime_type": "image/png",
            "size": 3,
        }
    ]


@pytest.mark.asyncio
async def test_thought_status_transition_is_the_only_status_log_writer():
    from brain.systems.cortex.thought_lifecycle import (
        ThoughtStatusCommand,
        transition_thought_status,
    )

    session = _Session()
    idea = _idea(status="unread_reply")
    published: list[tuple[str, dict]] = []

    result = await transition_thought_status(
        session,
        idea=idea,
        command=ThoughtStatusCommand(
            to_status="needs_input",
            trigger="user_read",
            actor={"user_id": "owner-1", "org_id": "org-1"},
        ),
        publish=lambda event_type, payload: published.append((event_type, payload)),
    )

    assert idea.status == "needs_input"
    assert result.status_change == {
        "idea_id": "idea-1",
        "old_status": "unread_reply",
        "new_status": "needs_input",
        "org_id": "org-1",
    }
    assert [
        (log.from_state, log.to_state, log.trigger)
        for log in session.added
        if log.__class__.__name__ == "IdeaStateLog"
    ] == [("unread_reply", "needs_input", "user_read")]
    assert published == [
        (
            "status_change",
            {"idea_id": "idea-1", "new_status": "needs_input"},
        )
    ]


def test_all_thread_writes_and_status_transitions_delegate_to_thought_module():
    violations: list[str] = []
    for path in THREAD_AND_STATUS_PRODUCERS:
        violations.extend(_iter_call_violations(path))
        violations.extend(_iter_status_assignment_violations(path))

    assert violations == []


def test_runner_final_answer_and_terminal_settlement_delegate_to_thought_module():
    runner_path = "brain/systems/runs/cortex/runner.py"
    function_names = _function_names(_tree(runner_path))

    assert "_post_run_final_answer_to_thread_once_async" not in function_names
    settle_calls = _called_names(runner_path, "_settle_idea_for_terminal_root_run_async")
    assert "settle_terminal_run" in settle_calls
    assert "IdeaThread" not in settle_calls
    assert "IdeaStateLog" not in settle_calls
