from __future__ import annotations

from pathlib import Path
import re
import ast


DIRECT_SYNC_UOW = re.compile(r"^\s*with\s+UnitOfWork\s*\(")
BANNED_BRIDGES = (
    "run_sync_with_unit_of_work",
    "run_blocking_unit_of_work",
)
RUN_SYNC_REFERENCE = re.compile(r"getattr\([^#\n]+['\"]run_sync['\"]|\.\s*run_sync\s*\(")
CENTRAL_SESSION_BRIDGES = {
    Path("brain/platform/db/repositories/unit_of_work.py"),
    Path("brain/platform/db/session_tasks.py"),
}


def _function_source(path: Path, name: str) -> str:
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text)
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == name:
            lines = text.splitlines()
            return "\n".join(lines[node.lineno - 1:node.end_lineno])
    raise AssertionError(f"{name} not found in {path}")


def test_production_db_references_are_async_shaped():
    root = Path(__file__).resolve().parents[1]
    offenders: list[str] = []
    for path in sorted((root / "brain").rglob("*.py")):
        relative_path = path.relative_to(root)
        text = path.read_text(encoding="utf-8")
        for line_number, line in enumerate(text.splitlines(), start=1):
            if DIRECT_SYNC_UOW.search(line):
                offenders.append(f"{relative_path}:{line_number}: {line.strip()}")
                continue
            if any(name in line for name in BANNED_BRIDGES):
                offenders.append(f"{relative_path}:{line_number}: {line.strip()}")
                continue
            if relative_path not in CENTRAL_SESSION_BRIDGES and RUN_SYNC_REFERENCE.search(line):
                offenders.append(f"{relative_path}:{line_number}: {line.strip()}")

    assert offenders == [], (
        f"Found {len(offenders)} production sync-shaped DB references:\n"
        + "\n".join(offenders[:120])
    )


def test_notification_routes_stay_on_native_async_db_path():
    root = Path(__file__).resolve().parents[1]
    path = root / "brain/app/api/routers/notifications.py"
    text = path.read_text(encoding="utf-8")

    assert "run_db" not in text
    assert "run_session_task" not in text
    assert "run_unit_of_work_task" not in text
    assert "open_unit_of_work" not in text


def test_team_routes_stay_on_native_async_db_path():
    root = Path(__file__).resolve().parents[1]
    path = root / "brain/app/api/routers/team.py"
    text = path.read_text(encoding="utf-8")

    assert "run_db" not in text
    assert "run_session_task" not in text
    assert "run_unit_of_work_task" not in text
    assert "open_unit_of_work" not in text


def test_health_routes_stay_on_native_async_db_path():
    root = Path(__file__).resolve().parents[1]
    main_path = root / "brain/app/api/main.py"
    system_path = root / "brain/app/api/routers/system.py"
    route_sources = [
        _function_source(main_path, "health"),
        _function_source(system_path, "health_ready"),
        _function_source(system_path, "health_deep"),
    ]

    for source in route_sources:
        assert "run_db" not in source
        assert "run_session_task" not in source
        assert "run_unit_of_work_task" not in source
        assert "open_unit_of_work" not in source


def test_system_read_routes_stay_on_native_async_db_path():
    root = Path(__file__).resolve().parents[1]
    system_path = root / "brain/app/api/routers/system.py"
    route_sources = [
        _function_source(system_path, "list_metrics"),
        _function_source(system_path, "list_consolidations"),
        _function_source(system_path, "retrieval_stats"),
        _function_source(system_path, "scheduler_state"),
        _function_source(system_path, "scheduler_health"),
    ]

    for source in route_sources:
        assert "run_db" not in source
        assert "run_session_task" not in source
        assert "run_unit_of_work_task" not in source
        assert "open_unit_of_work" not in source


def test_system_router_stays_on_native_async_db_path():
    root = Path(__file__).resolve().parents[1]
    path = root / "brain/app/api/routers/system.py"
    text = path.read_text(encoding="utf-8")

    assert "run_db" not in text
    assert "run_session_task" not in text
    assert "run_unit_of_work_task" not in text
    assert "open_unit_of_work" not in text
    assert "from sqlalchemy.orm import Session" not in text


def test_cortex_run_event_status_uses_native_async_db_path():
    root = Path(__file__).resolve().parents[1]
    path = root / "brain/app/api/routers/cortex/_run.py"
    source = _function_source(path, "run_events_status")

    assert "run_db" not in source
    assert "run_session_task" not in source
    assert "run_unit_of_work_task" not in source
    assert "open_unit_of_work" not in source


def test_cortex_run_router_stays_on_native_async_db_path():
    root = Path(__file__).resolve().parents[1]
    path = root / "brain/app/api/routers/cortex/_run.py"
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text)
    route_names = {
        "ops_active_runs",
        "ops_recent_runs",
        "run_tools",
        "run_events_status",
        "run_history",
        "run_debug",
        "download_run_trace_export",
        "download_thread_trace_export",
        "approve_run",
        "deny_run",
        "cancel_run",
        "steer_run",
        "run_graph",
        "run_skill_feedback",
    }
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }

    assert "run_db" not in text
    assert "run_session_task" not in text
    assert "run_unit_of_work_task" not in text
    assert "open_unit_of_work" not in text
    assert "from sqlalchemy.orm import Session" not in text
    assert "from brain.systems.runs.store import AgentRunStore" not in text
    for name in route_names:
        assert isinstance(functions[name], ast.AsyncFunctionDef), name


def test_cortex_browser_routes_use_native_async_db_path():
    root = Path(__file__).resolve().parents[1]
    path = root / "brain/app/api/routers/cortex/_browser.py"
    route_sources = [
        _function_source(path, "create_browser_session"),
        _function_source(path, "get_browser_session"),
    ]

    for source in route_sources:
        assert "run_db" not in source
        assert "run_session_task" not in source
        assert "run_unit_of_work_task" not in source
        assert "open_unit_of_work" not in source


def test_workspace_pins_router_stays_on_native_async_db_path():
    root = Path(__file__).resolve().parents[1]
    path = root / "brain/app/api/routers/workspace_pins.py"
    text = path.read_text(encoding="utf-8")

    assert "run_db" not in text
    assert "run_session_task" not in text
    assert "run_unit_of_work_task" not in text
    assert "open_unit_of_work" not in text


def test_vault_router_stays_on_native_async_db_path():
    root = Path(__file__).resolve().parents[1]
    path = root / "brain/app/api/routers/vault.py"
    text = path.read_text(encoding="utf-8")

    assert "run_db" not in text
    assert "run_session_task" not in text
    assert "run_unit_of_work_task" not in text
    assert "open_unit_of_work" not in text
    assert "from sqlalchemy.orm import Session" not in text


def test_costs_router_stays_on_native_async_db_path():
    root = Path(__file__).resolve().parents[1]
    path = root / "brain/app/api/routers/costs.py"
    text = path.read_text(encoding="utf-8")

    assert "run_db" not in text
    assert "run_session_task" not in text
    assert "run_unit_of_work_task" not in text
    assert "open_unit_of_work" not in text
    assert "from sqlalchemy.orm import Session" not in text


def test_brain_router_stays_on_native_async_db_path():
    root = Path(__file__).resolve().parents[1]
    path = root / "brain/app/api/routers/brain.py"
    text = path.read_text(encoding="utf-8")

    assert "run_db" not in text
    assert "run_session_task" not in text
    assert "run_unit_of_work_task" not in text
    assert "open_unit_of_work" not in text
    assert "from sqlalchemy.orm import Session" not in text


def test_cycles_router_stays_on_native_async_db_path():
    root = Path(__file__).resolve().parents[1]
    path = root / "brain/app/api/routers/cycles.py"
    text = path.read_text(encoding="utf-8")

    assert "run_db" not in text
    assert "run_session_task" not in text
    assert "run_unit_of_work_task" not in text
    assert "open_unit_of_work" not in text
    assert "from sqlalchemy.orm import Session" not in text


def test_memory_router_stays_on_native_async_db_path():
    root = Path(__file__).resolve().parents[1]
    path = root / "brain/app/api/routers/memory.py"
    text = path.read_text(encoding="utf-8")

    assert "run_db" not in text
    assert "run_session_task" not in text
    assert "run_unit_of_work_task" not in text
    assert "open_unit_of_work" not in text
    assert "from sqlalchemy.orm import Session" not in text


def test_workspace_apps_router_stays_on_native_async_db_path():
    root = Path(__file__).resolve().parents[1]
    path = root / "brain/app/api/routers/workspace_apps.py"
    text = path.read_text(encoding="utf-8")

    assert "run_db" not in text
    assert "run_session_task" not in text
    assert "run_unit_of_work_task" not in text
    assert "open_unit_of_work" not in text
    assert "from sqlalchemy.orm import Session" not in text


def test_domains_router_stays_on_native_async_db_path():
    root = Path(__file__).resolve().parents[1]
    path = root / "brain/app/api/routers/domains.py"
    text = path.read_text(encoding="utf-8")

    assert "run_db" not in text
    assert "run_session_task" not in text
    assert "run_unit_of_work_task" not in text
    assert "open_unit_of_work" not in text
    assert "from sqlalchemy.orm import Session" not in text


def test_skills_router_stays_on_native_async_db_path():
    root = Path(__file__).resolve().parents[1]
    path = root / "brain/app/api/routers/skills.py"
    text = path.read_text(encoding="utf-8")

    assert "run_db" not in text
    assert "run_session_task" not in text
    assert "run_unit_of_work_task" not in text
    assert "open_unit_of_work" not in text
    assert "from sqlalchemy.orm import Session" not in text


def test_chat_api_and_realtime_paths_stay_on_native_async_db_path():
    root = Path(__file__).resolve().parents[1]
    paths = [
        root / "brain/app/api/routers/chat.py",
        root / "brain/app/api/routers/ws.py",
        root / "brain/systems/runs/tool_catalog/handlers/chat.py",
    ]

    for path in paths:
        text = path.read_text(encoding="utf-8")
        assert "run_db" not in text
        assert "run_session_task" not in text
        assert "run_unit_of_work_task" not in text
        assert "open_unit_of_work" not in text
        assert "from sqlalchemy.orm import Session" not in text


def test_chat_service_has_single_async_db_implementation():
    root = Path(__file__).resolve().parents[1]
    path = root / "brain/app/api/services/chat.py"
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text)
    service = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "ChatService"
    )
    db_methods = {
        "bootstrap",
        "list_conversations",
        "create_or_fetch_dm",
        "get_conversation_messages",
        "post_conversation_message",
        "get_message_thread",
        "search_room_messages",
        "post_thread_reply",
        "post_agent_message",
        "mark_conversation_read",
        "list_notifications",
        "mark_notification_read",
        "mark_all_notifications_read",
        "build_unread_summary_for_user",
        "build_unread_summaries_for_users",
        "ensure_org_room",
        "get_conversation_or_404",
        "get_root_message_or_404",
    }
    methods = {
        node.name: node
        for node in service.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }

    assert "class AsyncChatService" not in text
    assert "from sqlalchemy.orm import Session" not in text
    assert "run_db" not in text
    assert "run_session_task" not in text
    assert "run_unit_of_work_task" not in text
    assert "open_unit_of_work" not in text
    for name in db_methods:
        assert isinstance(methods[name], ast.AsyncFunctionDef), name


def test_cortex_project_context_routes_use_native_async_db_path():
    root = Path(__file__).resolve().parents[1]
    path = root / "brain/app/api/routers/cortex/_project_context.py"
    text = path.read_text(encoding="utf-8")

    assert "run_db" not in text
    assert "run_session_task" not in text
    assert "run_unit_of_work_task" not in text
    assert "open_unit_of_work" not in text
    assert "from sqlalchemy.orm import Session" not in text


def test_cortex_ideas_router_stays_on_native_async_db_path():
    root = Path(__file__).resolve().parents[1]
    path = root / "brain/app/api/routers/cortex/_ideas.py"
    text = path.read_text(encoding="utf-8")

    assert "run_db" not in text
    assert "run_session_task" not in text
    assert "run_unit_of_work_task" not in text
    assert "open_unit_of_work" not in text
    assert "from sqlalchemy.orm import Session" not in text


def test_cortex_bootstrap_route_stays_on_native_async_db_path():
    root = Path(__file__).resolve().parents[1]
    path = root / "brain/app/api/routers/cortex/_bootstrap.py"
    text = path.read_text(encoding="utf-8")

    assert "run_db" not in text
    assert "run_session_task" not in text
    assert "run_unit_of_work_task" not in text
    assert "open_unit_of_work" not in text
    assert "from sqlalchemy.orm import Session" not in text


def test_cortex_auth_keys_router_stays_on_native_async_db_path():
    root = Path(__file__).resolve().parents[1]
    path = root / "brain/app/api/routers/cortex/_auth_keys.py"
    text = path.read_text(encoding="utf-8")

    assert "run_db" not in text
    assert "run_session_task" not in text
    assert "run_unit_of_work_task" not in text
    assert "open_unit_of_work" not in text
    assert "from sqlalchemy.orm import Session" not in text


def test_cortex_idea_ops_router_stays_on_native_async_db_path():
    root = Path(__file__).resolve().parents[1]
    path = root / "brain/app/api/routers/cortex/_idea_ops.py"
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text)
    async_functions = {
        node.name
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef)
    }

    assert "run_db" not in text
    assert "run_session_task" not in text
    assert "run_unit_of_work_task" not in text
    assert "open_unit_of_work" not in text
    assert "from sqlalchemy.orm import Session" not in text
    assert "from brain.systems.runs.store import AgentRunStore" not in text
    assert "unified_stream_payload" in async_functions


def test_cortex_misc_router_stays_on_native_async_db_path():
    root = Path(__file__).resolve().parents[1]
    path = root / "brain/app/api/routers/cortex/_misc.py"
    text = path.read_text(encoding="utf-8")

    assert "run_db" not in text
    assert "run_session_task" not in text
    assert "run_unit_of_work_task" not in text
    assert "open_unit_of_work" not in text
    assert "from sqlalchemy.orm import Session" not in text


def test_cortex_helpers_do_not_open_sync_db_boundaries():
    root = Path(__file__).resolve().parents[1]
    path = root / "brain/app/api/routers/cortex/_helpers.py"
    text = path.read_text(encoding="utf-8")

    assert "run_db" not in text
    assert "run_session_task" not in text
    assert "run_unit_of_work_task" not in text
    assert "open_unit_of_work" not in text


def test_onboarding_router_stays_on_native_async_db_path():
    root = Path(__file__).resolve().parents[1]
    path = root / "brain/app/api/routers/onboarding.py"
    text = path.read_text(encoding="utf-8")

    assert "run_db" not in text
    assert "run_session_task" not in text
    assert "run_unit_of_work_task" not in text
    assert "open_unit_of_work" not in text
    assert "from sqlalchemy.orm import Session" not in text


def test_trigger_router_exposes_only_async_db_admission():
    root = Path(__file__).resolve().parents[1]
    path = root / "brain/app/triggers/router.py"
    text = path.read_text(encoding="utf-8")

    assert "def route_trigger" not in text
    assert "from brain.systems.runs.store import AgentRunStore" not in text
    assert "admit_run" not in text.replace("async_admit_run", "")
    assert "open_unit_of_work" not in text
    assert "from sqlalchemy.orm import Session" not in text
