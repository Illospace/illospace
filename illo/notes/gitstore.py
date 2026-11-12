"""Commit one workspace file. A failed push leaves the file on disk."""

import asyncio
import os
from pathlib import Path

from illo.ledger import sensors


async def _git(root: Path, *args: str, capture: bool = False) -> bool | str:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(
        GIT_TERMINAL_PROMPT="0",
        GIT_AUTHOR_NAME="Illo",
        GIT_AUTHOR_EMAIL="illo@example.invalid",
        GIT_COMMITTER_NAME="Illo",
        GIT_COMMITTER_EMAIL="illo@example.invalid",
    )
    process = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(root),
        *args,
        env=env,
        stdout=asyncio.subprocess.PIPE if capture else asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        async with asyncio.timeout(30):
            output, _ = await process.communicate()
            if capture:
                return output.decode().strip() if process.returncode == 0 else ""
            return process.returncode == 0
    except BaseException:
        process.kill()
        await process.wait()
        raise


async def store(ctx, conn, path: Path, title: str, *, commit: bool = False) -> bool:
    """Do not push unless the workspace explicitly enables it and has a remote."""
    if not commit and not ctx.workspace.config.workspace.git_push:
        return True
    root = ctx.workspace.root
    try:
        if not await _git(root, "rev-parse", "--is-inside-work-tree"):
            return True
        if commit and await _git(root, "rev-parse", "--show-toplevel", capture=True) != str(root.resolve()):
            return True
        # Read names only. Never print a remote URL or Git output.
        remotes = (await _git(root, "remote", capture=True)).splitlines()
        if not remotes and not commit:
            return True
        remote = "origin" if "origin" in remotes else (remotes[0] if remotes else None)
        relative = str(path.relative_to(root))
        if not await _git(root, "add", "--", relative):
            raise RuntimeError
        if not await _git(root, "diff", "--cached", "--quiet", "--", relative):
            if not await _git(
                root,
                "-c",
                "core.hooksPath=/dev/null",
                "commit",
                "--only",
                "-m",
                f"workspace: {title}" if commit else f"note: {title}",
                "--",
                relative,
            ):
                raise RuntimeError
        if ctx.workspace.config.workspace.git_push and remote and not await _git(root, "push", "--", remote, "HEAD"):
            raise RuntimeError
    except Exception:
        await sensors.error(
            conn, "workspace", "The file is saved. The workspace commit or push failed."
        )
        return False
    await sensors.ok(conn, "workspace", "The connector file is saved." if commit else "The note is saved and pushed.")
    return True
