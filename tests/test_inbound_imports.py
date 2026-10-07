"""Inbound entry points must work without an earlier API import."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest


@pytest.mark.parametrize("module_name", [
    "brain.systems.inbound.service",
    "brain.systems.inbound.admin",
    "brain.systems.inbound.results",
    "brain.systems.slack.connector",
])
def test_inbound_entry_point_imports_in_clean_interpreter(module_name: str):
    result = subprocess.run(
        [sys.executable, "-c", f"import {module_name}"],
        capture_output=True,
        env={**os.environ, "SECRET_KEY": "test-secret"},
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
