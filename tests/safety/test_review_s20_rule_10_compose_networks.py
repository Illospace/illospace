"""Rule 10: the brain never shares the database network."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_rule_10_brain_is_separate_from_postgres():
    compose = yaml.safe_load((ROOT / "deploy/compose.yaml").read_text())
    services = compose["services"]
    assert set(compose["networks"]) == {"db", "brainnet", "default"}
    assert set(services["postgres"]["networks"]) == {"db"}
    assert set(services["illo"]["networks"]) == {"db", "brainnet", "default"}
    assert set(services["brain"]["networks"]) == {"brainnet", "default"}
    assert compose["networks"]["db"]["internal"] is True
    assert compose["networks"]["brainnet"]["internal"] is True
    assert not set(services["brain"]["networks"]) & set(services["postgres"]["networks"])
    assert not services["postgres"].get("ports")
