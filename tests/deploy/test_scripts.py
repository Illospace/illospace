"""Exercise maintenance scripts with a real Git remote and a recording Docker fake."""

import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
FAKE_DOCKER = r"""#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
state = pathlib.Path(os.environ["FAKE_STATE"])
layout = pathlib.Path(args[args.index("-f") + 1]).read_text() if "-f" in args else ""
with (state / "calls").open("a") as log:
    log.write(json.dumps({"args": args, "revision": os.getenv("ILLO_REVISION"), "layout": layout}) + "\n")
revision = os.getenv("ILLO_REVISION", "local")
if args == ["info"] or args == ["compose", "version"]:
    sys.exit(0)
for operation in ("config", "build", "up", "exec", "run", "stop", "down"):
    if operation in args:
        break
else:
    sys.exit("Unexpected fake Docker call")
if operation == "config":
    env_out = json.loads(os.getenv("FAKE_COMPOSE_ILLO_ENV", "{}"))
    ws = os.getenv("FAKE_COMPOSE_WORKSPACE", os.getenv("ILLO_WORKSPACE_DIR", ""))
    print(json.dumps({"services": {"illo": {"environment": env_out,
                                            "volumes": [{"target": "/workspace", "source": ws}]}}}))
    sys.exit(0)
if operation == "build" and os.getenv("FAIL_BUILD") == "1":
    sys.exit(1)
if operation == "up" and "postgres" not in args[args.index("up"):]:
    (state / "running").write_text(revision)
if operation == "run" and "migrate" in args and os.getenv("FAIL_MIGRATION") == "1":
    sys.exit(1)
if operation == "run" and "check-routes" in args and os.getenv("FAIL_ROUTES") == "1":
    sys.exit(1)
if operation == "run" and "check-routes" in args and os.getenv("HANG_ROUTES") == "1":
    import time; time.sleep(5); sys.exit(0)
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
"""


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
    env.update(
        {
            "HOME": str(home),
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "ILLO_ENV_FILE": str(home / ".config/illo/env"),
            "ILLO_WORKSPACE_DIR": str(home / "workspace"),
            "ILLO_STATE_DIR": str(home / "state"),
            "ILLO_BACKUP_DIR": str(home / "backups"),
            "ILLO_HEALTH_ATTEMPTS": "1",
            "ILLO_HEALTH_INTERVAL": "0",
            "FAKE_STATE": str(state),
        }
    )
    return root, env, state


def run(install, script, *args, **overrides):
    root, env, _ = install
    return subprocess.run(
        [str(root / "deploy" / script), *map(str, args)],
        cwd=root,
        env=env | overrides,
        text=True,
        capture_output=True,
        check=False,
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
            compose.read_text()
            .replace("brainnet", "newbrainnet")
            .replace("[db]", "[newdb]")
            .replace("[db,", "[newdb,")
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
            assert (
                value and value not in first.stdout + first.stderr + second.stdout + second.stderr
            )
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
    before = len(calls(install))
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
    if failure == "health":
        assert f"rollback {old} ok" in log
    else:
        # Build and migration fail before service replacement; keep the old stack running untouched.
        assert "rollback" not in log
        made = [c["args"] for c in calls(install)[before:]]
        assert not any("--remove-orphans" in a for a in made)
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
        [str(root / "deploy/update.sh"), "--watch", "10"],
        cwd=root,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
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
        assert (state / "revisions" / target / "deploy/compose.yaml").read_text() == project[0][
            "layout"
        ]
    assert (state / "revisions" / old / "deploy/compose.yaml").read_text() == old_layout
    assert not any("down" in c["args"] or "volume" in c["args"] for c in update_calls)


def test_compose_reaps_orphan_processes():
    services = yaml.safe_load((ROOT / "deploy/compose.yaml").read_text())["services"]
    assert services["brain"]["init"] is True
    assert services["illo"]["init"] is True


def test_install_creates_both_environment_files_at_mode_600(install):
    init(install)
    app = Path(install[1]["ILLO_ENV_FILE"])
    brain = app.parent / "brain.env"
    assert app.stat().st_mode & 0o777 == 0o600 and brain.stat().st_mode & 0o777 == 0o600
    assert f"ILLO_BRAIN_ENV_FILE='{brain}'" in app.read_text()
    assert "ILLO_PROVIDER_OPENAI_API_KEY=" in brain.read_text()
    assert "PROVIDER" not in app.read_text().replace("ILLO_BRAIN_ENV_FILE", "")
    # A second install recreates each service whose environment file changed.
    assert run(install, "install.sh").returncode == 0
    assert any(c["args"][-3:] == ["up", "-d", "--no-build"] for c in calls(install))


@pytest.mark.parametrize("line", ["OPENAI_API_KEY='fake-key'", "ILLO_PROVIDER_OPENAI_API_KEY=x",
                                  "export ANTHROPIC_API_KEY=x", "OPENAI_BASE_URL=",
                                  # Compose reads both forms below. A value can name a variable.
                                  "OPENAI_API_KEY = fake-key", "ILLO_FAKE_ALIAS=${OPENAI_API_KEY}"])
def test_install_and_update_refuse_a_provider_key_in_the_app_file(install, line):
    init(install)
    path = Path(install[1]["ILLO_ENV_FILE"])
    path.write_text(path.read_text() + line + "\n")
    result = run(install, "install.sh")
    assert result.returncode == 1
    assert "holds a provider variable" in result.stderr and "fake-key" not in result.stderr
    before = len(calls(install))
    target = remote_commit(install)
    assert run(install, "update.sh").returncode == 1
    # Nothing was built, migrated, or started. The last good revision keeps running.
    assert len(calls(install)) == before
    log = (Path(install[1]["ILLO_STATE_DIR"]) / "update.log").read_text()
    assert f"failed {target}: environment files" in log


def test_the_brain_file_cannot_live_in_the_checkout(install):
    result = run(install, "install.sh", ILLO_BRAIN_ENV_FILE=str(install[0] / "brain.env"))
    assert result.returncode == 1
    assert "outside the checkout" in result.stderr


@pytest.mark.parametrize("place", ["workspace", "checkout"])
def test_update_refuses_a_brain_file_in_the_workspace_or_the_checkout(install, place):
    """The app mounts the workspace. A file under .git is not a local change for git status."""
    init(install)
    brain = Path(install[1]["ILLO_ENV_FILE"]).parent / "brain.env"
    inside = (Path(install[1]["ILLO_WORKSPACE_DIR"]) if place == "workspace"
              else install[0] / ".git") / "brain.env"
    shutil.copy(brain, inside)
    assert run(install, "install.sh", ILLO_BRAIN_ENV_FILE=str(inside)).returncode == 1
    before = len(calls(install))
    target = remote_commit(install)
    result = run(install, "update.sh", ILLO_BRAIN_ENV_FILE=str(inside))
    assert result.returncode == 1 and "outside the checkout" in result.stderr
    assert len(calls(install)) == before
    log = (Path(install[1]["ILLO_STATE_DIR"]) / "update.log").read_text()
    assert f"failed {target}: environment files" in log


def test_update_refuses_a_workspace_that_the_new_revision_rejects(install):
    """The new images check the workspace before the migration. The old services keep running."""
    init(install)
    old = git(install[0], "rev-parse", "HEAD")
    before = len(calls(install))
    target = remote_commit(install)
    result = run(install, "update.sh", FAIL_ROUTES="1")
    assert result.returncode == 1 and "Nothing changed" in result.stderr
    made = [c["args"] for c in calls(install)[before:]]
    assert any(a[-4:] == ["brain", "python", "-m", "brain.providers"] for a in made)
    assert any(a[-2:] == ["illo", "check-routes"] for a in made)
    assert not any("up" in a or "migrate" in a for a in made)
    state = Path(install[1]["ILLO_STATE_DIR"])
    assert (install[2] / "running").read_text() == old
    assert (state / "last-good").read_text().strip() == old
    log = (state / "update.log").read_text()
    assert f"failed {target}: workspace or routes" in log and "rollback" not in log


def test_status_and_backup_read_the_saved_layout_of_the_last_good_revision(install):
    """A refused update leaves the new layout in the checkout. Status and backup use last-good."""
    init(install)
    old = git(install[0], "rev-parse", "HEAD")
    target = remote_commit(install, layout_change=True)
    assert run(install, "update.sh", FAIL_ROUTES="1").returncode == 1
    # Even an exported ILLO_REVISION pointing at the failed target must not override last-good.
    status = run(install, "status.sh", ILLO_REVISION=target)
    assert status.returncode == 0 and f"Last good revision: {old}" in status.stdout
    args = calls(install)[-1]["args"]
    saved = Path(install[1]["ILLO_STATE_DIR"]) / "revisions" / old / "deploy/compose.yaml"
    assert "exec" in args and args[args.index("-f") + 1] == str(saved)
    assert run(install, "backup.sh").returncode == 0
    backup_args = calls(install)[-1]["args"]
    assert "pg_dump" in backup_args and backup_args[backup_args.index("-f") + 1] == str(saved)


def test_update_preflight_times_out_and_keeps_old_services(install):
    init(install)
    old = git(install[0], "rev-parse", "HEAD")
    target = remote_commit(install)
    result = run(install, "update.sh", HANG_ROUTES="1", ILLO_PREFLIGHT_TIMEOUT="1")
    assert result.returncode == 1 and "Nothing changed" in result.stderr
    assert (install[2] / "running").read_text() == old
    log = (Path(install[1]["ILLO_STATE_DIR"]) / "update.log").read_text()
    assert f"failed {target}: workspace or routes" in log and "rollback" not in log


def test_env_check_rejects_bom_provider_key_and_inline_comment_workspace(install):
    init(install)
    app = Path(install[1]["ILLO_ENV_FILE"])
    original = app.read_bytes()
    # UTF-8 BOM before a provider key on line 1 must still be refused.
    app.write_bytes(b"\xef\xbb\xbfOPENAI_API_KEY=fake-bom-key\n" + original)
    res = run(install, "install.sh")
    assert res.returncode == 1 and "holds a provider variable" in res.stderr
    assert "fake-bom-key" not in res.stderr
    # Inline comment on ILLO_WORKSPACE_DIR must resolve the real workspace path.
    ws = Path(install[1]["ILLO_WORKSPACE_DIR"])
    brain_inside = ws / "brain.env"
    ws.mkdir(exist_ok=True)
    shutil.copy(app.parent / "brain.env", brain_inside)
    edited = "\n".join(
        f"ILLO_WORKSPACE_DIR={ws} # inline comment" if line.startswith("ILLO_WORKSPACE_DIR=") else line
        for line in original.decode().splitlines()
    ) + "\n"
    app.write_text(edited)
    env_no_ws = {k: v for k, v in install[1].items() if k != "ILLO_WORKSPACE_DIR"}
    res2 = subprocess.run(
        [str(install[0] / "deploy/install.sh")],
        cwd=install[0],
        env=env_no_ws | {"ILLO_BRAIN_ENV_FILE": str(brain_inside)},
        text=True, capture_output=True, check=False,
    )
    assert res2.returncode == 1 and "outside the checkout" in res2.stderr


def test_compose_gives_provider_keys_to_the_brain_only(tmp_path):
    if subprocess.run(["docker", "compose", "version"], capture_output=True).returncode:
        pytest.skip("Docker Compose is not installed")
    app, brain = tmp_path / "env", tmp_path / "brain.env"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ILLO_", "OPENAI_", "ANTHROPIC_"))}
    env["ILLO_WORKSPACE_DIR"] = str(tmp_path / "workspace")
    subprocess.run([sys.executable, str(ROOT / "deploy/init-env.py"), str(app),
                    str(ROOT / "deploy/env.example"), str(brain),
                    str(ROOT / "deploy/brain.env.example")],
                   env=env, check=True, stdin=subprocess.DEVNULL)
    brain.write_text(brain.read_text().replace(
        "ILLO_PROVIDER_OPENAI_API_KEY=", "ILLO_PROVIDER_OPENAI_API_KEY=fake-provider-key"))
    result = subprocess.run(
        ["docker", "compose", "--env-file", str(app), "-f", str(ROOT / "deploy/compose.yaml"),
         "config", "--format", "json"],
        env=env | {"ILLO_ENV_FILE": str(app)}, capture_output=True, text=True, check=True)
    services = json.loads(result.stdout)["services"]
    forbidden = ("ILLO_PROVIDER_", "OPENAI_", "ANTHROPIC_")
    for name in ("illo", "postgres", "sandbox"):
        assert not [k for k in services[name].get("environment") or {} if k.startswith(forbidden)]
    assert services["brain"]["environment"]["ILLO_PROVIDER_OPENAI_API_KEY"] == "fake-provider-key"
    assert "ILLO_DATABASE_URL" not in services["brain"]["environment"]
    assert "ILLO_VAULT_KEY" not in services["brain"]["environment"]
