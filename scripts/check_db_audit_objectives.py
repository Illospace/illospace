#!/usr/bin/env python3
"""Fail when DB audit remediation objectives are left unchecked."""

from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OBJECTIVES = ROOT / "docs" / "db-audit-remediation-objectives.md"
UNCHECKED_RE = re.compile(r"^\s*-\s+\[(?!x\])", re.IGNORECASE)


def unchecked_objectives(path: Path = OBJECTIVES) -> list[tuple[int, str]]:
    """Return unchecked markdown task-list items."""
    return [
        (line_no, line.rstrip())
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if UNCHECKED_RE.match(line)
    ]


def main() -> int:
    unchecked = unchecked_objectives()
    if not unchecked:
        print(f"All DB audit remediation objectives are checked in {OBJECTIVES.relative_to(ROOT)}.")
        return 0

    print(f"Unchecked DB audit remediation objectives in {OBJECTIVES.relative_to(ROOT)}:", file=sys.stderr)
    for line_no, line in unchecked:
        print(f"{line_no}: {line}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
