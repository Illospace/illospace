"""Exercise maintenance scripts with a real Git remote and a recording Docker fake."""

import fcntl
import json
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
FAKE_DOCKER = r'''#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
state = pathlib.Path(os.environ["FAKE_STATE"])
layout = pathlib.Path(args[args.index("-f") + 1]).read_text() if "-f" in args else ""
with (state / "calls").open("a") as log:
    log.write(json.dumps({"args": args, "revision": os.getenv("ILLO_REVISION"), "layout": layout}) + "\n")
revision = os.getenv("ILLO_REVISION", "local")
if args == ["info"] or args == ["compose", "version"]:
    sys.exit(0)
for operation in ("build", "up", "exec", "run", "stop", "down"):
    if operation in args:
        break
else:
    sys.exit("Unexpected fake Docker call")
if operation == "build" and os.getenv("FAIL_BUILD") == "1":
    sys.exit(1)
if operation == "up" and ("--remove-orphans" in args or "illo" in args):
    (state / "running").write_text(revision)
if operation == "run" and os.getenv("FAIL_MIGRATION") == "1":
    sys.exit(1)
if operation == "exec" and "python" in args:
    if revision == os.getenv("FAIL_HEALTH_REVISION"):
        sys.exit(1)
    sys.exit(0)
if operation == "exec" and "pg_dump" in args:
    if os.getenv("FAIL_DUMP"):
        sys.exit(1)
    print("PGDMP fake custom backup")
if operation == "exec" and "psql" in args:
    print(os.getenv("FAKE_TABLES", "0"))
if operation == "exec" and "pg_restore" in args:
    (state / "restored").write_bytes(sys.stdin.buffer.read())
'''


def git(root, *args):
    return subprocess.check_output(
        ["git", "-C", str(root), *args], text=True, stderr=subprocess.DEVNULL
    ).strip()


@pytest.fixture
def install(tmp_path):
    root = tmp_path / "checkout"
    root.mkdir()
    shutil.copytree(ROOT / "deploy", root / "deploy")
    shutil.copytree(ROOT / "workspace.example", root / "workspace.example")
    (root / "brain").mkdir()
    (root / "brain/Dockerfile").touch()
    home = tmp_path / "home"
    home.mkdir()
    state = tmp_path / "fake"
    state.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(0o755)
    git(root, "init", "--initial-branch=main")
    git(root, "config", "user.name", "Acme Tests")
    git(root, "config", "user.email", "tests@example.com")
    git(root, "add", ".")
    git(root, "commit", "-m", "initial")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ILLO_", "GIT_", "XDG_"))}
    env.update({
        "HOME": str(home), "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "ILLO_ENV_FILE": str(home / ".config/illo/env"),
        "ILLO_WORKSPACE_DIR": str(home / "workspace"),
        "ILLO_STATE_DIR": str(home / "state"),
        "ILLO_BACKUP_DIR": str(home / "backups"),
        "ILLO_HEALTH_ATTEMPTS": "1", "ILLO_HEALTH_INTERVAL": "0", "FAKE_STATE": str(state),
    })
    return root, env, state


def run(install, script, *args, **overrides):
    root, env, _ = install
    return subprocess.run(
        [str(root / "deploy" / script), *map(str, args)],
        cwd=root, env=env | overrides, text=True, capture_output=True, check=False,
    )


def calls(install):
    return [json.loads(line) for line in (install[2] / "calls").read_text().splitlines()]


def init(install):
    result = run(install, "install.sh")
    assert result.returncode == 0, result.stdout + result.stderr
    return result


def remote_commit(install, *, layout_change=False):
    root = install[0]
    remote = root.parent / "remote.git"
    git(root, "clone", "--bare", str(root), str(remote))
    git(root, "remote", "add", "origin", str(remote))
    writer = root.parent / "writer"
    git(root, "clone", str(remote), str(writer))
    git(writer, "config", "user.name", "Acme Tests")
    git(writer, "config", "user.email", "tests@example.com")
    (writer / "change").write_text("a new revision")
    if layout_change:
        compose = writer / "deploy/compose.yaml"
        compose.write_text(
            compose.read_text().replace("brainnet", "newbrainnet")
            .replace("[db]", "[newdb]").replace("[db,", "[newdb,")
            .replace("  db:", "  newdb:")
        )
    git(writer, "add", ".")
    git(writer, "commit", "-m", "new")
    git(writer, "push", "origin", "main")
    return git(writer, "rev-parse", "HEAD")


def test_install_twice_keeps_existing_files_and_does_not_print_secrets(install):
    first = init(install)
    _, env, _ = install
    path = Path(env["ILLO_ENV_FILE"])
    original = path.read_bytes()
    workspace = Path(env["ILLO_WORKSPACE_DIR"])
    (workspace / "notes/custom.md").write_text("Keep this note.")
    before = {p: p.read_bytes() for p in workspace.rglob("*") if p.is_file()}
    second = run(install, "install.sh")
    assert second.returncode == 0, second.stderr
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.read_bytes() == original
    assert before == {p: p.read_bytes() for p in workspace.rglob("*") if p.is_file()}
    for line in original.decode().splitlines():
        if line.startswith(("POSTGRES_PASSWORD=", "ILLO_VAULT_KEY=", "ILLO_BRAIN_SECRET=")):
            value = line.split("=", 1)[1].strip("'")
            assert value and value not in first.stdout + first.stderr + second.stdout + second.stderr
    assert sum("build" in c["args"] for c in calls(install)) == 1


def test_update_deploys_new_commit_and_records_revision(install):
    init(install)
    target = remote_commit(install)
    result = run(install, "update.sh")
    assert result.returncode == 0, result.stderr
    state = Path(install[1]["ILLO_STATE_DIR"])
    assert (state / "last-good").read_text().strip() == target
    assert (install[2] / "running").read_text() == target
    assert f"updated {target} ok" in (state / "update.log").read_text()
    assert git(install[0], "rev-parse", "HEAD") == target
    assert not any("down" in c["args"] for c in calls(install))


@pytest.mark.parametrize("failure", ["health", "build", "migration"])
def test_update_failure_returns_to_old_images(install, failure):
    init(install)
    old = git(install[0], "rev-parse", "HEAD")
    target = remote_commit(install)
    overrides = {
        "health": {"FAIL_HEALTH_REVISION": target},
        "build": {"FAIL_BUILD": "1"},
        "migration": {"FAIL_MIGRATION": "1"},
    }[failure]
    result = run(install, "update.sh", **overrides)
    assert result.returncode == 1
    state = Path(install[1]["ILLO_STATE_DIR"])
    assert (state / "last-good").read_text().strip() == old
    assert (install[2] / "running").read_text() == old
    log = (state / "update.log").read_text()
    assert f"failed {target}: {failure}" in log
    assert f"rollback {old} ok" in log
    # The checkout may advance on failure. A retry must still deploy the target.
    assert run(install, "update.sh").returncode == 0
    assert (state / "last-good").read_text().strip() == target


def test_update_lock_stops_second_process(install):
    init(install)
    state = Path(install[1]["ILLO_STATE_DIR"])
    with (state / "deploy.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = run(install, "update.sh")
    assert result.returncode == 1
    assert "Another install" in result.stderr


def test_watch_releases_lock_so_backups_can_run(install):
    init(install)
    target = remote_commit(install)
    root, env, _ = install
    process = subprocess.Popen(
        [str(root / "deploy/update.sh"), "--watch", "10"], cwd=root, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )
    state = Path(env["ILLO_STATE_DIR"])
    acquired = False
    try:
        for _ in range(100):
            if (state / "last-good").read_text().strip() == target:
                with (state / "deploy.lock").open("w") as lock:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        acquired = True
                        break
                    except BlockingIOError:
                        pass
            time.sleep(0.05)
        assert acquired, "The update watcher blocks backups while it waits"
        assert run(install, "backup.sh").returncode == 0
    finally:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)


def test_update_uses_configured_branch(install):
    init(install)
    target = remote_commit(install)
    root = install[0]
    git(root, "--git-dir", str(root.parent / "remote.git"), "branch", "next", target)
    git(root, "switch", "-c", "next")
    path = Path(install[1]["ILLO_ENV_FILE"])
    path.write_text(path.read_text().replace("ILLO_DEPLOY_BRANCH=main", "ILLO_DEPLOY_BRANCH=next"))
    result = run(install, "update.sh")
    assert result.returncode == 0, result.stderr
    assert (install[2] / "running").read_text() == target


def test_backup_retention_and_failed_dump(install):
    init(install)
    for _ in range(3):
        result = run(install, "backup.sh", ILLO_BACKUP_KEEP="2")
        assert result.returncode == 0, result.stderr
    backups = list(Path(install[1]["ILLO_BACKUP_DIR"]).glob("*.dump"))
    assert len(backups) == 2
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in backups)
    result = run(install, "backup.sh", FAIL_DUMP="1")
    assert result.returncode != 0
    assert set(backups) == set(Path(install[1]["ILLO_BACKUP_DIR"]).glob("*.dump"))
    assert not list(Path(install[1]["ILLO_BACKUP_DIR"]).glob("*.partial"))


def test_restore_needs_yes_and_a_fresh_database(install):
    init(install)
    backup = install[0].parent / "example.dump"
    backup.write_bytes(b"PGDMP example")
    assert run(install, "restore.sh", backup).returncode == 1
    assert run(install, "restore.sh", backup, "--yes", FAKE_TABLES="1").returncode == 1
    assert not (install[2] / "restored").exists()
    result = run(install, "restore.sh", backup, "--yes")
    assert result.returncode == 0, result.stderr
    assert (install[2] / "restored").read_bytes() == backup.read_bytes()


def test_credentials_and_backups_cannot_live_in_checkout(install):
    result = run(install, "install.sh", ILLO_ENV_FILE=str(install[0] / ".env"))
    assert result.returncode == 1
    assert "outside the checkout" in result.stderr
    init(install)
    result = run(install, "backup.sh", ILLO_BACKUP_DIR=str(install[0] / "backups"))
    assert result.returncode == 1


def test_scripts_parse_and_compose_keeps_postgres_private():
    for script in (ROOT / "deploy").glob("*.sh"):
        subprocess.run(["bash", "-n", str(script)], check=True)
    compose = yaml.safe_load((ROOT / "deploy/compose.yaml").read_text())
    postgres = compose["services"]["postgres"]
    assert "ports" not in postgres
    assert postgres["networks"] == ["db"]
    assert compose["networks"]["db"]["internal"] is True


@pytest.mark.parametrize("fail_health", [False, True])
def test_layout_change_applies_project_and_rolls_back_saved_layout(install, fail_health):
    init(install)
    root, env, _ = install
    state = Path(env["ILLO_STATE_DIR"])
    old = git(root, "rev-parse", "HEAD")
    old_layout = (root / "deploy/compose.yaml").read_text()
    # An older install has no snapshot yet.
    shutil.rmtree(state / "revisions")
    target = remote_commit(install, layout_change=True)
    result = run(install, "update.sh", **({"FAIL_HEALTH_REVISION": target} if fail_health else {}))
    assert result.returncode == int(fail_health), result.stderr
    update_calls = calls(install)
    database = next(c for c in update_calls if c["revision"] == target and "--wait" in c["args"])
    assert database["args"][-1] == "postgres"
    migration = next(c for c in update_calls if c["revision"] == target and "migrate" in c["args"])
    assert update_calls.index(database) < update_calls.index(migration)
    project = [c for c in update_calls if "--remove-orphans" in c["args"]]
    assert project[0]["revision"] == target
    assert "newbrainnet" in project[0]["layout"]
    assert "--no-deps" not in project[0]["args"]
    assert project[0]["args"][-1] == "--remove-orphans"
    if fail_health:
        rollback = project[-1]
        assert rollback["revision"] == old
        assert rollback["layout"] == old_layout
        saved = state / "revisions" / old / "deploy/compose.yaml"
        assert rollback["args"][rollback["args"].index("-f") + 1] == str(saved)
        assert f"rollback {old} ok" in (state / "update.log").read_text()
    else:
        assert (state / "revisions" / target / "deploy/compose.yaml").read_text() == project[0]["layout"]
    assert (state / "revisions" / old / "deploy/compose.yaml").read_text() == old_layout
    assert not any("down" in c["args"] or "volume" in c["args"] for c in update_calls)
