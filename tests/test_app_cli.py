"""App startup needs a database and workspace, but no optional channel tokens."""

import json
import shutil

import httpx
import pytest
from cryptography.fernet import Fernet

from collectors.common import CODE_VERSION
from illo.app import SETUP_MODULES
from illo.cli import execute, parser
from illo.config import Config
from illo.db import one
from illo.ledger import sensors
from tests.conftest import ROOT


async def test_health_and_stub_setup(http, app):
    response = await http.get("/healthz")
    assert response.json() == {"status": "ok", "database": "ok", "revision": "test-revision"}
    assert app.state.ctx.config.slack_bot_token is None
    assert all(__import__(name, fromlist=["setup"]).setup for name in SETUP_MODULES)


async def test_reload_rejects_missing_or_remote_auth(http, app):
    assert (await http.post("/admin/reload")).status_code == 403
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("203.0.113.1", 1)), base_url="http://test"
    ) as client:
        assert (
            await client.post("/admin/reload", headers={"Authorization": "Bearer test-reload"})
        ).status_code == 403


async def test_reload_validates_before_swap(app, tmp_path):
    folder = tmp_path / "workspace"
    shutil.copytree(ROOT / "workspace.example", folder)
    app.state.ctx.config.workspace_dir = folder
    old = app.state.ctx.workspace
    (folder / "illo.yaml").write_text("invalid: true")
    with pytest.raises(ValueError):
        await app.state.ctx.reload()
    assert app.state.ctx.workspace is old


async def test_scheduler_reports_error_then_ok(app):
    async def bad_job():
        raise RuntimeError("Private details")

    await app.state.ctx.scheduler._call(bad_job)
    async with app.state.ctx.pool.connection() as conn:
        row = await one(conn, "SELECT * FROM sensors WHERE key LIKE 'job:%bad_job'")
        assert row["last_error_at"] and row["detail"] == "The scheduled job failed"

    async def good_job():
        pass

    await app.state.ctx.scheduler._call(good_job)
    async with app.state.ctx.pool.connection() as conn:
        row = await one(conn, "SELECT * FROM sensors WHERE key LIKE 'job:%good_job'")
        assert row["last_ok_at"]


def test_config_reads_env_and_keeps_secrets_private(monkeypatch):
    monkeypatch.setenv("ILLO_SLACK_BOT_TOKEN", "private-test-token")
    monkeypatch.setenv("ILLO_MIGRATE_ON_START", "false")
    monkeypatch.setenv("ILLO_WORKSPACE_DIR", "/tmp/acme-workspace")
    config = Config.from_env()
    assert config.slack_bot_token.get_secret_value() == "private-test-token"
    assert not config.migrate_on_start
    assert "private-test-token" not in repr(config)


def test_cli_secret_has_no_value_argument():
    args = parser().parse_args(["secret", "set", "example"])
    assert not hasattr(args, "value")
    with pytest.raises(SystemExit):
        parser().parse_args(["secret", "set", "example", "must-not-accept"])


async def test_cli_migrate_token_and_secret(pool, monkeypatch, capsys):
    import io

    config = Config(
        database_url=pool.conninfo,
        workspace_dir=ROOT / "workspace.example",
        vault_key=Fernet.generate_key().decode(),
    )
    assert await execute(parser().parse_args(["migrate"]), config) == 0
    await execute(
        parser().parse_args(["token", "create", "--person", "ada", "--label", "test machine"]),
        config,
    )
    output = capsys.readouterr().out
    raw = output.splitlines()[-1]
    assert len(raw) > 32
    await execute(parser().parse_args(["token", "list"]), config)
    assert "test machine" in capsys.readouterr().out
    monkeypatch.setattr("sys.stdin", io.StringIO("private-test-value\n"))
    await execute(parser().parse_args(["secret", "set", "example"]), config)
    assert "private-test-value" not in capsys.readouterr().out
    await execute(parser().parse_args(["secret", "get-names"]), config)
    assert "example" in capsys.readouterr().out
    await execute(parser().parse_args(["secret", "delete", "example"]), config)


async def test_n3_doctor_checks_the_routes_against_the_brain(app, monkeypatch, capsys):
    from illo.cli import doctor

    ctx = app.state.ctx
    ctx.workspace.config.brain.routes["default"].provider = "gemini"
    ctx.config.database_url = ctx.pool.conninfo
    monkeypatch.setattr("illo.cli.load_workspace", lambda _: ctx.workspace)
    monkeypatch.setattr("illo.brain.client.BrainClient", lambda url: ctx.brain_client)
    assert await doctor(ctx.config) == 1
    assert "The routes need a fix: brain.routes.person_dm: the brain has no provider gemini" in (
        capsys.readouterr().out)


async def test_doctor_reports_recent_collector_versions(app, monkeypatch, capsys):
    from illo.cli import doctor

    ctx = app.state.ctx
    ctx.config.database_url = ctx.pool.conninfo
    monkeypatch.setattr("illo.cli.load_workspace", lambda _: ctx.workspace)
    monkeypatch.setattr("illo.brain.client.BrainClient", lambda url: ctx.brain_client)
    long_version = "a" * 61
    async with ctx.pool.connection() as conn:
        for label, version in {
            "acme-old": "0.1.0",
            "acme-long": long_version,
            "acme-current": CODE_VERSION,
            "acme-current-2": CODE_VERSION,
            "acme-stale": "0.1.0",
        }.items():
            await sensors.ok(conn, f"collector:{label}", json.dumps({"version": version}))
        await sensors.ok(conn, "collector:acme-missing", "{}")
        await sensors.ok(conn, "collector:acme-bad-json", "{")
        await conn.execute("""
            UPDATE sensors SET last_ok_at = now() - interval '2 days'
            WHERE key = 'collector:acme-stale'
        """)

    assert await doctor(ctx.config) == 0
    output = capsys.readouterr().out
    assert (
        "Warning: the collector acme-old runs other code (0.1.0). "
        f"This server has {CODE_VERSION}. Restart the collector, or update its copy of collectors/."
    ) in output
    assert "Collectors: 2 run the code of this server." in output
    assert "acme-current" not in output
    assert "acme-stale" not in output
    assert "the collector acme-missing runs other code (no version)." in output
    assert "the collector acme-bad-json runs other code (no version)." in output
    assert f"the collector acme-long runs other code ({long_version[:60]})." in output


@pytest.mark.parametrize("old_key", [False, True])
async def test_check_routes_reads_the_new_brain_description_without_a_database(
        tmp_path, monkeypatch, capsys, old_key):
    """deploy/update.sh pipes the provider description of the new brain image into this check.
    An old illo.yaml stops the update before the migration, with no change to the services."""
    import io
    import json

    from brain.providers import BUILT_IN, description

    root = tmp_path / "workspace"
    shutil.copytree(ROOT / "workspace.example", root)
    if old_key:
        path = root / "illo.yaml"
        path.write_text(path.read_text().replace("brain:\n", "brain:\n  runtime: claude\n", 1))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(description(BUILT_IN))))
    # No database runs at this address. The check never connects.
    config = Config(workspace_dir=root, database_url="postgresql://illo@127.0.0.1:9/none")
    if old_key:
        with pytest.raises(ValueError, match="brain.routes.default.provider"):
            await execute(parser().parse_args(["check-routes"]), config)
    else:
        assert await execute(parser().parse_args(["check-routes"]), config) == 0
        assert "The workspace and its routes are valid." in capsys.readouterr().out
