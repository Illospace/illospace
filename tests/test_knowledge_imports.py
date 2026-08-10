"""Import-order regression tests for the knowledge package."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest


@pytest.mark.parametrize(
    "module_name",
    [
        "brain.systems.knowledge.service",
        "brain.systems.knowledge.connectors.github",
    ],
)
def test_knowledge_entry_point_imports_in_clean_interpreter(module_name: str):
    result = subprocess.run(
        [sys.executable, "-c", f"import {module_name}"],
        capture_output=True,
        env={**os.environ, "SECRET_KEY": "test-secret"},
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
