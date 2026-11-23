"""Each entry module loads in a new process, whatever the import order."""

import subprocess
import sys

import pytest


@pytest.mark.parametrize(
    "module",
    [
        "illo.connectors.chatgpt_smoke",
        "illo.brain.run_manager",
        "illo.gateway.replies",
        "illo.gateway",
        "illo.cli",
        "illo.app",
    ],
)
def test_module_loads_first_in_a_new_process(module):
    code = f"import {module}; from illo.gateway import Gateway; assert Gateway.__name__ == 'Gateway'"
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr[-500:]
