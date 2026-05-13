from __future__ import annotations

from pathlib import Path
import re


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
