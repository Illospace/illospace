#!/usr/bin/env python3
"""Experiment tracking — creates experiment memories after successful changes.

Called by nightly_implement.py after a change is applied and PR'd/merged.
"""
import json
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 3))))
from brain.app.cli.memory import add_memory


def create_experiment_memory(
    description: str,
    hypothesis: str,
    what_changed: str,
    success_metric: str,
    data_source: str,
    pr_number: int | str | None = None,
    assess_days: int = 7,
) -> dict:
    """Create an experiment memory to track a change's impact.

    Stores metadata as structured text in the content field since the memories
    table doesn't have a dedicated metadata column.

    Args:
        description: What was changed
        hypothesis: What this change should improve
        what_changed: Files/description of the change
        success_metric: How to measure success
        data_source: Where to find assessment data
        pr_number: PR number for potential revert
        assess_days: Days until assessment (default 7)

    Returns:
        Result from add_memory (dict with id or rejection info)
    """
    assess_by = (date.today() + timedelta(days=assess_days)).isoformat()

    meta = {
        "hypothesis": hypothesis,
        "what_changed": what_changed,
        "success_metric": success_metric,
        "data_source": data_source,
        "assess_by": assess_by,
        "status": "active",
        "extensions": 0,
    }
    if pr_number:
        meta["pr_number"] = int(pr_number) if str(pr_number).isdigit() else pr_number

    content = f"EXPERIMENT: {description}\nEXPERIMENT_META:{json.dumps(meta)}"

    return add_memory(
        content=content,
        memory_type="experiment",
        salience=6.0,
        tags=["experiment", "active"],
        source="nightly-implement",
    )
