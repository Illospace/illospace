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


def test_cortex_run_event_status_uses_native_async_db_path():
    root = Path(__file__).resolve().parents[1]
    path = root / "brain/app/api/routers/cortex/_run.py"
    source = _function_source(path, "run_events_status")

    assert "run_db" not in source
    assert "run_session_task" not in source
    assert "run_unit_of_work_task" not in source
    assert "open_unit_of_work" not in source


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


def test_onboarding_router_stays_on_native_async_db_path():
    root = Path(__file__).resolve().parents[1]
    path = root / "brain/app/api/routers/onboarding.py"
    text = path.read_text(encoding="utf-8")

    assert "run_db" not in text
    assert "run_session_task" not in text
    assert "run_unit_of_work_task" not in text
    assert "open_unit_of_work" not in text
    assert "from sqlalchemy.orm import Session" not in text
