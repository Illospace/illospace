"""Keep the sandbox private and include its image in each update."""

from pathlib import Path

import yaml

from tests.deploy import test_scripts as scripts

ROOT = Path(__file__).resolve().parents[2]
install = scripts.install


def test_container_boundary_and_volume_ownership():
    config = yaml.safe_load((ROOT / "deploy/compose.yaml").read_text())
    sandbox = config["services"]["sandbox"]
    gateway = config["services"]["illo"]
    assert sandbox["networks"] == ["sandboxnet"]
    assert config["networks"]["sandboxnet"]["internal"] is True
    assert "sandboxnet" in gateway["networks"]
    assert all("sandboxnet" not in service.get("networks", [])
               for name, service in config["services"].items() if name not in {"illo", "sandbox"})
    assert not any(key in sandbox for key in ("ports", "env_file", "secrets", "environment"))
    assert sandbox["read_only"] and sandbox["cap_drop"] == ["ALL"]
    assert sandbox["security_opt"] == ["no-new-privileges:true"]
    assert sandbox["volumes"] == ["repos:/repos:ro"]
    assert "repos:/repos:rw" in gateway["volumes"]
    assert sandbox["user"] == "10002:10002"
    assert sandbox["mem_limit"] == "3g" and sandbox["cpus"] == "2"
    assert sandbox["build"]["args"]["ILLO_REVISION"] == "${ILLO_REVISION:-local}"
    assert any("/work:" in value and "noexec" in value and "size=2g" in value for value in sandbox["tmpfs"])
    for dockerfile in ("deploy/Dockerfile", "sandbox/Dockerfile"):
        assert "chown 10001:10001 /repos" in (ROOT / dockerfile).read_text()


def test_updater_builds_sandbox_and_checks_it_from_gateway(install):
    scripts.init(install)
    target = scripts.remote_commit(install)
    result = scripts.run(install, "update.sh")
    assert result.returncode == 0, result.stderr
    calls = [call for call in scripts.calls(install) if call["revision"] == target]
    build = next(call for call in calls if "build" in call["args"])
    assert build["args"][-3:] == ["illo", "brain", "sandbox"]
    assert any("http://sandbox:8600/healthz" in " ".join(call["args"])
               and "illo" in call["args"] for call in calls)
