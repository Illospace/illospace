"""Guard against reintroducing runtime calls to deprecated raw DB helpers."""
from __future__ import annotations

import re
from pathlib import Path


RAW_HELPER_CALL = re.compile(r"\b(?:get_cursor|get_conn)\(")
ALLOWED_DEFINITIONS = ("def get_cursor(", "def get_conn(")


def test_brain_runtime_does_not_call_raw_db_helpers():
    root = Path(__file__).resolve().parents[1]
    offenders: list[str] = []

    for path in sorted((root / "brain").rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        for line_no, line in enumerate(path.read_text().splitlines(), start=1):
            if not RAW_HELPER_CALL.search(line):
                continue
            stripped = line.strip()
            if rel == "brain/platform/db/__init__.py" and stripped.startswith(ALLOWED_DEFINITIONS):
                continue
            offenders.append(f"{rel}:{line_no}: {stripped}")

    assert not offenders, (
        "Deprecated raw DB helpers should not be called from brain runtime code. "
        "Use UnitOfWork/repositories instead:\n" + "\n".join(offenders)
    )
