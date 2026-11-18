"""Read bounded image blobs from configured mirrors without a shell or credentials."""

import asyncio
import os
import re
import subprocess

from illo.sandbox.repos import mirror_path
from illo.tasks.pages.schema import ImageRef

MAX_BYTES = 8 * 1024 * 1024


def media_type(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    raise ValueError("The file is not a PNG, JPEG, WebP, or GIF image.")


def _read(ctx, image, data=True):
    image = ImageRef.model_validate(image)
    configured = ctx.workspace.config.sandbox.repositories
    repos = ctx.workspace.config.github.repositories if configured is None else configured
    if image.repo not in repos or not ctx.config.repos_dir:
        raise ValueError("The image needs a configured repository mirror.")
    path = mirror_path(ctx.config.repos_dir.resolve(), image.repo)
    env = {"PATH": os.defpath, "LANG": "C.UTF-8", "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0"}

    def git(*args):
        try:
            return subprocess.run(["git", "-C", str(path), *args], env=env, check=True,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            raise ValueError("The picture is not available.") from None

    if image.ref.startswith("-") or any(ord(c) < 32 for c in image.ref):
        raise ValueError("The image reference is invalid.")
    # Mirrors keep fetched branches under origin, and may not keep a local branch with that name.
    sha = None
    for ref in (image.ref, "refs/remotes/origin/" + image.ref):
        try:
            candidate = git("rev-parse", "--verify", "--end-of-options", ref + "^{commit}").decode().strip()
            if re.fullmatch(r"[0-9a-f]{40}", candidate):
                sha = candidate
                break
        except ValueError:
            pass
    if not sha:
        raise ValueError("The image commit is not available.")
    obj = sha + ":" + image.path
    if git("cat-file", "-t", obj).strip() != b"blob":
        raise ValueError("The image must be a file.")
    if int(git("cat-file", "-s", obj)) > MAX_BYTES:
        raise ValueError("The image exceeds 8 MB.")
    if not data:
        # The page only needs to know that the picture exists. The file route reads it.
        return {"repo": image.repo, "ref": sha, "path": image.path}, None, None
    data = git("cat-file", "blob", obj)
    return {"repo": image.repo, "ref": sha, "path": image.path}, data, media_type(data)


async def read(ctx, image, *, data=True):
    return await asyncio.to_thread(_read, ctx, image, data)
