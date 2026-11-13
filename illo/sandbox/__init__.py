"""Wire the shell client and repository schedule into the gateway."""

from illo.sandbox.client import SandboxClient
from illo.sandbox.repos import setup as setup_repos


async def setup(ctx):
    ctx.sandbox = SandboxClient(ctx.config.sandbox_url) if ctx.config.sandbox_url else None
    await setup_repos(ctx)
