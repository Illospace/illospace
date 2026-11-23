"""Sync real local Git copies without network access or stored credentials."""

import asyncio
import os
import subprocess
import time
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from illo.db import one
from illo.ledger import kv
from illo.sandbox import repos
from illo.workspace import SandboxConfig

TOKEN = "fake-acme-token"


def local_git(root, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return subprocess.check_output(["git", "-C", str(root), *args], env=env,
                                  stderr=subprocess.DEVNULL, text=True).strip()


@pytest.fixture
async def mirror_ctx(app, tmp_path):
    ctx = app.state.ctx
    ctx.workspace = ctx.workspace.model_copy(deep=True)
    ctx.workspace.config.sandbox = SandboxConfig(repositories=["Acme/app"], branches=["staging", "absent"], depth=1)
    ctx.config = ctx.config.model_copy(update={"repos_dir": tmp_path / "repos"})
    writer = tmp_path / "writer"
    writer.mkdir()
    local_git(writer, "init", "--initial-branch=main")
    local_git(writer, "config", "user.name", "Acme Tests")
    local_git(writer, "config", "user.email", "tests@example.com")
    (writer / "file.txt").write_text("first")
    local_git(writer, "add", ".")
    local_git(writer, "commit", "-m", "first")
    local_git(writer, "switch", "-c", "staging")
    (writer / "file.txt").write_text("stage")
    local_git(writer, "commit", "-am", "stage")
    local_git(writer, "switch", "main")
    bare = tmp_path / "remote.git"
    local_git(writer, "clone", "--bare", str(writer), str(bare))
    local_git(writer, "remote", "add", "origin", str(bare))

    async def auth(path):
        assert path == "/repos/Acme/app"
        return TOKEN

    ctx.github_client = SimpleNamespace(_auth=auth)
    ctx.sandbox_repo_url = lambda _: bare.as_uri()
    return ctx, writer, bare


async def state(ctx):
    async with ctx.pool.connection() as conn:
        return await kv.get(conn, repos.KEY)


async def sensor(ctx):
    async with ctx.pool.connection() as conn:
        return await one(conn, "SELECT * FROM sensors WHERE key=%s", (repos.KEY,))


async def test_clone_fetch_extra_ref_credentials_and_readme(mirror_ctx, monkeypatch):
    ctx, writer, _ = mirror_ctx
    calls = []
    real = asyncio.create_subprocess_exec

    async def record(*argv, **kwargs):
        calls.append((argv, kwargs))
        return await real(*argv, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", record)
    # The child must discard the repository selectors of its parent.
    monkeypatch.setenv("GIT_DIR", "/not-a-repository")
    monkeypatch.setenv("GIT_WORK_TREE", "/not-a-tree")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "99")
    await repos.sync(ctx)
    path = ctx.config.repos_dir / "Acme/app"
    assert (path / "file.txt").read_text() == "first"
    assert local_git(path, "show", "origin/staging:file.txt") == "stage"
    assert local_git(path, "rev-parse", "--is-shallow-repository") == "true"
    assert TOKEN not in (path / ".git/config").read_text()
    assert "extraheader" not in (path / ".git/config").read_text()
    assert path.stat().st_mode & 0o055 == 0o055
    assert all(TOKEN not in str(argv) for argv, _ in calls)
    assert all("GIT_DIR" not in kw["env"] and "GIT_WORK_TREE" not in kw["env"] for _, kw in calls)
    assert all(kw["env"]["GIT_CONFIG_COUNT"] == "1" for _, kw in calls)
    assert all(kw["env"]["GIT_CONFIG_VALUE_0"].startswith("Authorization: Basic ") for _, kw in calls)
    readme = (ctx.config.repos_dir / "README.txt").read_text()
    assert "Acme/app: default=main" in readme and "origin/staging" in readme and "absent" not in readme
    assert TOKEN not in readme
    assert (await state(ctx))["last_success_at"] > 0
    assert (await sensor(ctx))["last_ok_at"]
    (writer / "file.txt").write_text("second")
    local_git(writer, "commit", "-am", "second")
    local_git(writer, "push", "origin", "main")
    await repos.sync(ctx)
    assert (path / "file.txt").read_text() == "second"
    ctx.workspace.config.sandbox.branches = []
    await repos.sync(ctx)
    assert not local_git(path, "for-each-ref", "refs/remotes/origin/staging")
    ctx.workspace.config.sandbox.repositories = []
    await repos.sync(ctx)
    assert not path.exists()
    assert (await state(ctx))["repositories"] == {}


async def test_size_guard_skips_until_setting_changes(mirror_ctx, monkeypatch):
    ctx, _, _ = mirror_ctx
    monkeypatch.setattr(repos, "size_bytes", lambda _: 2 * 1024**2)
    ctx.workspace.config.sandbox.max_repo_mb = 1
    await repos.sync(ctx)
    assert not (ctx.config.repos_dir / "Acme/app").exists()
    saved = await state(ctx)
    assert saved["repositories"]["Acme/app"]["blocked"]
    assert saved["last_success_at"] == 0
    assert "2.0 MB" in (await sensor(ctx))["detail"]

    def forbidden(_):
        pytest.fail("A blocked copy must not fetch again")

    ctx.sandbox_repo_url = forbidden
    await repos.sync(ctx)
    ctx.sandbox_repo_url = lambda _: mirror_ctx[2].as_uri()
    ctx.workspace.config.sandbox.max_repo_mb = 3
    await repos.sync(ctx)
    assert (await state(ctx))["repositories"]["Acme/app"]["ok"]
    assert (await sensor(ctx))["last_ok_at"] > (await sensor(ctx))["last_error_at"]


async def test_unreadable_repository_sensor_and_no_token(mirror_ctx):
    ctx, _, _ = mirror_ctx

    async def fail(_):
        raise ValueError(TOKEN)

    ctx.github_client._auth = fail
    await repos.sync(ctx)
    assert (await sensor(ctx))["last_error_at"]
    assert TOKEN not in str(await state(ctx)) + str(await sensor(ctx))
    assert "Check GitHub access" in (await sensor(ctx))["detail"]


async def test_ensure_fresh_and_two_process_lock(mirror_ctx, monkeypatch):
    ctx, _, _ = mirror_ctx
    await repos.ensure_fresh(ctx)
    before = await state(ctx)
    await repos.ensure_fresh(ctx)
    assert await state(ctx) == before
    async with ctx.pool.connection() as conn:
        await kv.set(conn, repos.KEY, {**before, "last_success_at": time.time() - 3600})
    started, release = asyncio.Event(), asyncio.Event()
    calls = 0
    real = repos.git

    async def slow(*args, **kwargs):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return await real(*args, **kwargs)

    monkeypatch.setattr(repos, "git", slow)
    first = asyncio.create_task(repos.ensure_fresh(ctx))
    await started.wait()
    second = asyncio.create_task(repos.ensure_fresh(ctx))
    # A separate context models a second gateway process.
    other = SimpleNamespace(config=ctx.config, workspace=ctx.workspace, pool=ctx.pool,
        github_client=ctx.github_client, sandbox_repo_url=ctx.sandbox_repo_url)
    third = asyncio.create_task(repos.ensure_fresh(other))
    await asyncio.sleep(0.05)
    assert calls == 1
    release.set()
    await asyncio.gather(first, second, third)
    assert (await state(ctx))["last_success_at"] > before["last_success_at"]
    assert calls == 6  # One pass reads refs, fetches, sets heads, and resets the checkout.


async def test_disabled_returns_without_io():
    await repos.ensure_fresh(SimpleNamespace(config=SimpleNamespace(repos_dir=None)))
    await repos.ensure_fresh(SimpleNamespace(config=SimpleNamespace(repos_dir="/unused"), github_client=None))


@pytest.mark.parametrize("settings", [{"depth": 0}, {"depth": 1001}, {"sync_minutes": 4},
    {"repositories": ["../bad"]}, {"repositories": ["Acme/.."]}, {"branches": ["../escape"]},
    {"branches": ["-bad"]}, {"branches": ["feature/.hidden"]}, {"branches": ["a.lock"]}])
def test_config_rejects_invalid_values(settings):
    with pytest.raises(ValidationError):
        SandboxConfig(**settings)


async def test_doctor_status(mirror_ctx):
    ctx, _, _ = mirror_ctx
    async with ctx.pool.connection() as conn:
        assert await repos.status(conn) == "Repository mirrors: 0. Last clean sync: never."
    await repos.sync(ctx)
    async with ctx.pool.connection() as conn:
        assert await repos.status(conn) == "Repository mirrors: 1. Last clean sync: 0 minutes ago."


async def test_new_default_branch_and_config_apply_before_max_age(mirror_ctx):
    ctx, writer, bare = mirror_ctx
    await repos.ensure_fresh(ctx)
    local_git(bare, "symbolic-ref", "HEAD", "refs/heads/staging")
    # A changed setting must apply even when the last pass is recent.
    ctx.workspace.config.sandbox.depth = 2
    await repos.ensure_fresh(ctx)
    path = ctx.config.repos_dir / "Acme/app"
    assert local_git(path, "symbolic-ref", "--short", "HEAD") == "staging"
    assert local_git(path, "symbolic-ref", "--short", "refs/remotes/origin/HEAD") == "origin/staging"
    assert (path / "file.txt").read_text() == "stage"
    assert (await state(ctx))["repositories"]["Acme/app"]["default_branch"] == "staging"


async def test_config_check_deletes_a_copy_with_stored_credentials(mirror_ctx):
    ctx, _, _ = mirror_ctx
    await repos.sync(ctx)
    path = ctx.config.repos_dir / "Acme/app"
    local_git(path, "config", "http.https://github.com/.extraheader", "Authorization: " + TOKEN)
    await repos.sync(ctx)
    assert not path.exists()
    assert not (await state(ctx))["repositories"]["Acme/app"]["ok"]
    assert TOKEN not in str(await state(ctx))


async def test_disabled_setup_has_no_jobs():
    ctx = SimpleNamespace(config=SimpleNamespace(repos_dir=None))
    await repos.setup(ctx)


async def test_sync_works_when_another_user_owns_the_root(mirror_ctx, monkeypatch):
    """A gateway with its own user cannot change the mode of the volume root."""
    ctx, _, _ = mirror_ctx
    root = ctx.config.repos_dir
    root.mkdir(parents=True)
    real = type(root).chmod

    def chmod(self, mode, **kwargs):
        if self == root.resolve():
            raise PermissionError("Operation not permitted")
        return real(self, mode, **kwargs)

    monkeypatch.setattr(type(root), "chmod", chmod)
    await repos.sync(ctx)
    assert (await state(ctx))["repositories"]["Acme/app"]["ok"]
