"""Keep connector changes in the same workspace file and Git path as notes."""

import yaml

from illo.connectors.config import Connector
from illo.notes import gitstore
from illo.notes.files import file_path, write
from illo.workspace import load_workspace


async def save(ctx, conn, config: Connector, *, create=False, expected=None):
    async with ctx.reload_lock:
        if expected is not None and expected != next(
            (c for c in ctx.workspace.connectors if c.key == config.key), None
        ):
            raise ValueError("The connector changed. Ask Illo for a new link.")
        path = file_path(ctx.workspace.root, "connectors", config.key + ".yaml")
        if create and path.exists():
            raise ValueError("This connector already exists")
        path = write(
            ctx.workspace.root,
            "connectors",
            path.name,
            yaml.safe_dump(config.model_dump(exclude_none=True), sort_keys=False),
        )
        ctx.workspace = load_workspace(ctx.workspace.root)
        if config.kind == "google":
            from illo.connectors.google import register_search

            register_search(ctx)
        return await gitstore.store(ctx, conn, path, "Save connector " + config.key, commit=True)


async def remove(ctx, conn, config):
    async with ctx.reload_lock:
        path = file_path(ctx.workspace.root, "connectors", config.key + ".yaml")
        path.unlink()
        ctx.workspace = load_workspace(ctx.workspace.root)
        return await gitstore.store(ctx, conn, path, "Remove connector " + config.key, commit=True)
