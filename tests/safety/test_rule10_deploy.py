"""Keep the brain container apart from the host and the app's files."""

from pathlib import Path

import yaml


def test_rule10_brain_container_has_no_host_access():
    root = Path(__file__).resolve().parents[2]
    brain = yaml.safe_load((root / "deploy/compose.yaml").read_text())["services"]["brain"]
    assert not brain.get("volumes")
    assert not brain.get("ports")
    assert "env_file" not in brain
    assert "ILLO_DATABASE_URL" not in brain["environment"]
    assert brain["read_only"] is True
    assert brain["cap_drop"] == ["ALL"]
    assert brain["security_opt"] == ["no-new-privileges:true"]
    assert brain["user"] == "10001:10001"
    assert brain["tmpfs"]
    assert brain["pids_limit"]
    assert brain["mem_limit"]
    assert brain["cpus"]
