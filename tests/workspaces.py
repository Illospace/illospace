"""Add the advanced task examples to a temporary test workspace."""

import shutil
from pathlib import Path


def task_workspace(root):
    source = Path(__file__).parent / "fixtures/task-lists"
    shutil.copytree(source, root / "lists", dirs_exist_ok=True)
    (root / "lists/tasks.md").unlink(missing_ok=True)
    config = root / "illo.yaml"
    config.write_text(config.read_text().replace("github:\n", "github:\n  task_list: bugs\n"))
    return root
