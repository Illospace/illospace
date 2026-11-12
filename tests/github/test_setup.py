"""Setup reads credentials without a live GitHub call."""

from pydantic import SecretStr

from illo.sources.github import setup
from illo.vault import Vault


async def test_setup_keeps_github_disabled_without_credentials(app, monkeypatch):
    ctx = app.state.ctx
    ctx.github_client = None
    jobs = []
    monkeypatch.setattr(ctx, "every", lambda seconds, fn: jobs.append((seconds, fn)))
    await setup(ctx)
    assert ctx.github_client is None
    assert "github_create_issue" in ctx.draft_performers
    assert [s for s, fn in jobs] == [60, 120]


async def test_setup_token_and_sensor_report_flush(app, monkeypatch):
    ctx = app.state.ctx
    ctx.github_client = None
    ctx.config.github_token = SecretStr("example-test-token")
    jobs = []
    monkeypatch.setattr(ctx, "every", lambda seconds, fn: jobs.append((seconds, fn)))
    await setup(ctx)
    assert ctx.github_client.token == "example-test-token"
    await ctx.github_client.report(False, "GitHub rate limit reached")
    flush = next(fn for seconds, fn in jobs if seconds == 5)
    await flush()
    from illo.db import one

    async with ctx.pool.connection() as conn:
        row = await one(conn, "SELECT * FROM sensors WHERE key='github'")
        assert row["last_error_at"] and row["detail"] == "GitHub rate limit reached"


async def test_setup_app_key_from_vault(app, monkeypatch):
    from cryptography.fernet import Fernet

    ctx = app.state.ctx
    ctx.github_client = None
    ctx.config.github_app_id = "123"
    ctx.config.github_app_installation_id = "456"
    ctx.config.vault_key = SecretStr(Fernet.generate_key().decode())
    async with ctx.pool.connection() as conn:
        await Vault(ctx.config.vault_key.get_secret_value()).set(
            conn, "github_app_private_key", "example-test-key"
        )
    monkeypatch.setattr(ctx, "every", lambda *args: None)
    await setup(ctx)
    assert ctx.github_client.private_key == "example-test-key"
    assert ctx.github_client.installation_id == "456"
