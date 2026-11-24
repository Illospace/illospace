"""The board workers have their own advisory lock keys."""

import re
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_board_lock_keys_have_one_owner():
    owners = defaultdict(list)
    for path in (ROOT / "illo").rglob("*.py"):
        for key in re.findall(r"pg_(?:try_)?advisory(?:_xact)?_lock\(\s*(1\d+)\s*\)", path.read_text()):
            owners[int(key)].append(path.relative_to(ROOT).as_posix())
    assert owners[12000002] == ["illo/channels/slack/tasks.py"]
    assert owners[12000003] == ["illo/tasks/release.py"]
    assert owners[12000010] == ["illo/board/coordinator.py"]
    assert owners[12000011] == ["illo/board/escalate.py"]
    assert owners[12000012] == ["illo/board/policy.py"]
    assert owners[12000013] == ["illo/board/wake.py"]
