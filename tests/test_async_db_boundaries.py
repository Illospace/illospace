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
REQUIRED_ALEMBIC_BRIDGES = {
    Path("brain/platform/db/alembic/env.py"),
}
SYNC_DB_API_NAMES = {
    "open_unit_of_work",
    "run_unit_of_work_task",
    "run_session_task",
}
SYNC_IMPORTS_BY_MODULE = {
    "brain.platform.db.repositories.unit_of_work": {
        "open_unit_of_work",
        "run_unit_of_work_task",
    },
    "brain.platform.db.session_tasks": {
        "run_session_task",
    },
    "sqlalchemy.orm": {
        "Session",
    },
    "brain.systems.runs.store": {
        "AgentRunStore",
    },
}
API_FACING_SYNC_DB_BOUNDARY_ROOTS = (
    Path("brain/app/api/main.py"),
    Path("brain/app/api/auth.py"),
    Path("brain/app/api/deps.py"),
    Path("brain/app/api/authorization.py"),
    Path("brain/app/api/routers"),
    Path("brain/app/api/services"),
    Path("brain/app/triggers"),
    Path("brain/systems/runtime_settings/auth.py"),
    Path("brain/systems/runtime_settings/models.py"),
    Path("brain/systems/runtime_settings/router.py"),
    Path("brain/systems/runtime_settings/self_update.py"),
    Path("brain/systems/runtime_settings/service.py"),
)


def _function_source(path: Path, name: str) -> str:
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text)
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == name:
            lines = text.splitlines()
            return "\n".join(lines[node.lineno - 1:node.end_lineno])
    raise AssertionError(f"{name} not found in {path}")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _iter_python_files(root: Path, relative_paths: tuple[Path, ...]) -> list[Path]:
    files: list[Path] = []
    for relative_path in relative_paths:
        path = root / relative_path
        if path.is_file():
            files.append(path)
        elif path.is_dir():
            files.extend(sorted(path.rglob("*.py")))
    return sorted(set(files))


def _sync_db_api_offenders(root: Path, paths: list[Path]) -> list[str]:
    offenders: list[str] = []
    for path in paths:
        relative_path = path.relative_to(root)
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        lines = text.splitlines()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                banned_names = SYNC_IMPORTS_BY_MODULE.get(module, set())
                imported = sorted(alias.name for alias in node.names if alias.name in banned_names)
                if imported:
                    line = lines[node.lineno - 1].strip()
                    offenders.append(
                        f"{relative_path}:{node.lineno}: imports sync DB API(s) {', '.join(imported)}: {line}"
                    )
            elif isinstance(node, ast.Call):
                func = node.func
                name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
                if name in SYNC_DB_API_NAMES or name == "AgentRunStore":
                    line = lines[node.lineno - 1].strip()
                    offenders.append(
                        f"{relative_path}:{node.lineno}: calls sync DB API {name}: {line}"
                    )
            elif isinstance(node, ast.With):
                for item in node.items:
                    context_expr = item.context_expr
                    if not isinstance(context_expr, ast.Call):
                        continue
                    func = context_expr.func
                    name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
                    if name in {"UnitOfWork", "uow_factory"}:
                        line = lines[node.lineno - 1].strip()
                        offenders.append(
                            f"{relative_path}:{node.lineno}: opens sync DB context {name}: {line}"
                        )
    return offenders


def _route_decorator_name(decorator: ast.AST) -> str | None:
    target = decorator.func if isinstance(decorator, ast.Call) else decorator
    if not isinstance(target, ast.Attribute):
        return None
    if target.attr not in {"get", "post", "put", "patch", "delete", "websocket"}:
        return None
    if isinstance(target.value, ast.Name) and target.value.id in {"router", "app"}:
        return target.attr
    return None


def test_production_db_references_are_async_shaped():
    root = _repo_root()
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
            if (
                relative_path not in CENTRAL_SESSION_BRIDGES
                and relative_path not in REQUIRED_ALEMBIC_BRIDGES
                and RUN_SYNC_REFERENCE.search(line)
            ):
                offenders.append(f"{relative_path}:{line_number}: {line.strip()}")

    assert offenders == [], (
        f"Found {len(offenders)} production sync-shaped DB references:\n"
        + "\n".join(offenders[:120])
    )


def test_api_facing_boundaries_do_not_import_or_call_sync_db_apis():
    root = _repo_root()
    paths = _iter_python_files(root, API_FACING_SYNC_DB_BOUNDARY_ROOTS)
    offenders = _sync_db_api_offenders(root, paths)

    assert offenders == [], (
        f"Found {len(offenders)} API-facing sync DB boundary references:\n"
        + "\n".join(offenders[:120])
    )


def test_api_route_handlers_are_async_functions():
    root = _repo_root()
    paths = _iter_python_files(root, (Path("brain/app/api"),))
    offenders: list[str] = []
    for path in paths:
        relative_path = path.relative_to(root)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if not any(_route_decorator_name(decorator) for decorator in node.decorator_list):
                continue
            if isinstance(node, ast.FunctionDef):
                offenders.append(f"{relative_path}:{node.lineno}: route handler {node.name} is sync")

    assert offenders == [], (
        f"Found {len(offenders)} sync API route handlers:\n"
        + "\n".join(offenders[:120])
    )


def test_runtime_settings_api_entrypoints_do_not_import_or_call_sync_db_apis():
    root = _repo_root()
    paths = _iter_python_files(
        root,
        (
            Path("brain/systems/runtime_settings/router.py"),
            Path("brain/systems/runtime_settings/service.py"),
        ),
    )
    offenders = _sync_db_api_offenders(root, paths)

    assert offenders == [], (
        f"Found {len(offenders)} runtime settings API sync DB boundary references:\n"
        + "\n".join(offenders)
    )


def test_trigger_and_cortex_admission_paths_do_not_use_sync_agent_run_store():
    root = _repo_root()
    paths = _iter_python_files(
        root,
        (
            Path("brain/app/triggers/router.py"),
            Path("brain/app/api/routers/cortex"),
            Path("brain/app/api/routers/cortex_intel.py"),
        ),
    )
    offenders = [
        offender
        for offender in _sync_db_api_offenders(root, paths)
        if "AgentRunStore" in offender
    ]

    assert offenders == [], (
        f"Found {len(offenders)} sync AgentRunStore references in admission paths:\n"
        + "\n".join(offenders)
    )


def test_cortex_intel_routes_use_async_service_entrypoints():
    root = _repo_root()
    path = root / "brain/app/api/routers/cortex_intel.py"
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text)
    sync_calls = {
        "detect_connections",
        "similarity_matrix",
        "compute_gravity",
        "run_emergence",
        "run_optimization",
    }
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
        if name in sync_calls:
            offenders.append(f"{path.relative_to(root)}:{node.lineno}: calls {name}")

    assert offenders == []


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
