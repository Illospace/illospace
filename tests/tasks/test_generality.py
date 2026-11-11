"""Task rules must accept a new team's tools through registration alone."""

import re
from pathlib import Path

CORE = ["illo/ledger/tasks.py", "illo/tools/tasks.py", "illo/channels/mcp/tasks.py",
        "illo/channels/slack/tasks.py", "illo/tasks/checks.py", "illo/tasks/access.py", "illo/tasks/views.py", "illo/tasks/lists.py"]


def test_task_core_has_no_outside_tool_names():
    root = Path(__file__).parents[2]
    forbidden = re.compile(r"github|pull request|repo|issue|t3|rollbar", re.I)
    for file in CORE:
        assert not forbidden.search((root / file).read_text()), file
