"""Maintain disposable repository copies with credentials outside each copy."""

import asyncio
import base64
import os
import re
import shutil
import signal
import time
from contextlib import suppress
from datetime import UTC, datetime

from illo.ledger import kv, sensors

KEY = "sandbox:repos"
REPO = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*/[A-Za-z0-9_-][A-Za-z0-9_.-]*")


def enabled(ctx):
    return bool(ctx.config.repos_dir and ctx.github_client)


def url_for(repo):
    return f"https://github.com/{repo}.git"


async def git(args, *, token, timeout=600):
    """Keep tokens in the child environment. Discard all Git error output."""
    header = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    env = {"PATH": os.defpath, "LANG": "C.UTF-8", "GIT_TERMINAL_PROMPT": "0",
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
           "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
           "GIT_CONFIG_VALUE_0": "Authorization: Basic " + header}
    argv = ["git", *map(str, args)]
    if ionice := shutil.which("ionice"):
        argv = [ionice, "-c3", *argv]
    process = await asyncio.create_subprocess_exec(*argv, env=env, start_new_session=True,
        preexec_fn=lambda: os.nice(10), stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        async with asyncio.timeout(timeout):
            # These commands return only ref names. Reject any larger answer.
            output = bytearray()
            while chunk := await process.stdout.read(8192):
                if len(output) + len(chunk) > 64_000:
                    raise ValueError("The Git result is too large.")
                output.extend(chunk)
            await process.wait()
            if process.returncode:
                raise ValueError("Git could not read the repository.")
            return output.decode("utf-8", errors="replace").strip()
    finally:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        while await process.stdout.read(8192):
            pass
        await process.wait()


def size_bytes(path):
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file() and not p.is_symlink())


def readable(path):
    for item in [path, *path.rglob("*")]:
        if not item.is_symlink():
            mode = item.stat().st_mode
            item.chmod(mode | 0o044 | (0o011 if item.is_dir() or mode & 0o111 else 0))


def credential_free(path, token):
    text = (path / ".git/config").read_text()
    encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    if (token and token in text) or encoded in text or re.search(
            r"^\s*(?:extraheader\s*=|\[credential\b)|https?://[^/\s]+@", text, re.I | re.M):
        raise ValueError("The repository config contains a credential.")


def mirror_path(root, repo):
    if not REPO.fullmatch(repo):
        raise ValueError("The repository name is invalid.")
    path = root / repo
    if path.resolve() != path or path.parent.is_symlink() or path.is_symlink():
        raise ValueError("The repository folder is invalid.")
    return path


async def sync(ctx, *, max_age_minutes=None):
    """Hold a database lock for the whole pass, including all Git calls."""
    if not enabled(ctx):
        return
    async with ctx.pool.connection() as conn:
        # A transaction lock releases on cancellation, failure, and pool return.
        async with conn.transaction():
            async with asyncio.timeout(300):
                await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (KEY,))
            previous = await kv.get(conn, KEY) or {}
            settings = ctx.workspace.config.sandbox
            root = ctx.config.repos_dir.resolve()
            configured = settings.repositories
            repositories = list(ctx.workspace.config.github.repositories if configured is None else configured)
            identity = {**settings.model_dump(mode="json"), "repositories": repositories, "root": str(root)}
            if (max_age_minutes is not None and previous.get("settings") == identity
                    and time.time() - previous.get("last_success_at", 0) < max_age_minutes * 60):
                return
            root.mkdir(parents=True, exist_ok=True)
            root.chmod(root.stat().st_mode | 0o055)
            results = {}
            old_results = previous.get("repositories", {})
            for repo in old_results.keys() - set(repositories):
                path = mirror_path(root, repo)
                if path.exists():
                    await asyncio.to_thread(shutil.rmtree, path)
            for repo in dict.fromkeys(repositories):
                path = None
                try:
                    path = mirror_path(root, repo)
                    old = old_results.get(repo, {})
                    if old.get("blocked") and previous.get("settings") == identity:
                        results[repo] = old
                        continue
                    async with asyncio.timeout(600):
                        token = await ctx.github_client._auth(f"/repos/{repo}")
                        remote = getattr(ctx, "sandbox_repo_url", url_for)(repo)
                        if not path.exists():
                            path.parent.mkdir(parents=True, exist_ok=True)
                            path.parent.chmod(path.parent.stat().st_mode | 0o055)
                            await git(["clone", "--depth", settings.depth, "--no-tags", remote, path], token=token)
                        credential_free(path, token)
                        refs = await git(["-C", path, "ls-remote", "--symref", "origin", "HEAD",
                                          *["refs/heads/" + b for b in settings.branches]], token=token)
                        match = re.search(r"^ref: refs/heads/(.+)\tHEAD$", refs, re.M)
                        if not match:
                            raise ValueError("The repository has no default branch.")
                        default = match[1]
                        available = set(re.findall(r"\trefs/heads/(.+)$", refs, re.M))
                        branches = [b for b in settings.branches if b in available and b != default]
                        wanted = [default, *branches]
                        await git(["-C", path, "fetch", "--depth", settings.depth, "--prune", "--no-tags", "origin",
                                   *[f"+refs/heads/{b}:refs/remotes/origin/{b}" for b in wanted]], token=token)
                        # Delete refs that no longer belong to this mirror's settings.
                        present = await git(["-C", path, "for-each-ref", "--format=%(refname)", "refs/remotes/origin/"], token=token)
                        for ref in present.splitlines():
                            if ref.removeprefix("refs/remotes/origin/") not in {*wanted, "HEAD"}:
                                await git(["-C", path, "update-ref", "-d", ref], token=token)
                        await git(["-C", path, "symbolic-ref", "HEAD", f"refs/heads/{default}"], token=token)
                        await git(["-C", path, "symbolic-ref", "refs/remotes/origin/HEAD",
                                   f"refs/remotes/origin/{default}"], token=token)
                        await git(["-C", path, "reset", "--hard", f"refs/remotes/origin/{default}"], token=token)
                        credential_free(path, token)
                        size = await asyncio.to_thread(size_bytes, path)
                        if size > settings.max_repo_mb * 1024**2:
                            await asyncio.to_thread(shutil.rmtree, path)
                            results[repo] = {"ok": False, "blocked": True,
                                "reason": f"{repo} uses {size / 1024**2:.1f} MB. The size limit is {settings.max_repo_mb} MB."}
                            continue
                        await asyncio.to_thread(readable, path)
                        results[repo] = {"ok": True, "default_branch": default, "extra_refs": branches,
                                         "synced_at": datetime.now(UTC).isoformat()}
                except Exception:
                    # Do not expose Git output, URLs, headers, or token source errors.
                    if path and path.exists():
                        await asyncio.to_thread(shutil.rmtree, path)
                    results[repo] = {"ok": False, "reason": f"{repo}: The repository sync failed. Check GitHub access."}
            clean = all(result["ok"] for result in results.values())
            now = time.time()
            state = {"settings": identity, "last_attempt_at": now,
                     "last_success_at": now if clean else previous.get("last_success_at", 0), "repositories": results}
            lines = ["Repository mirrors", "Last sync: " + datetime.now(UTC).isoformat()]
            for repo, result in results.items():
                lines.append(f"{repo}: default={result['default_branch']}; extra refs="
                    + ", ".join("origin/" + b for b in result["extra_refs"])
                    + "; last sync=" + result["synced_at"] if result["ok"] else result["reason"])
            readme = root / "README.txt"
            temporary = root / "README.txt.tmp"
            temporary.write_text("\n".join(lines) + "\n")
            temporary.chmod(0o644)
            temporary.replace(readme)
            await kv.set(conn, KEY, state)
            report = sensors.ok if clean else sensors.error
            detail = f"{len(results)} repository mirrors." if clean else " ".join(r["reason"] for r in results.values() if not r["ok"])
            await report(conn, KEY, detail, expected_every_s=settings.sync_minutes * 60)


async def ensure_fresh(ctx, max_age_minutes: int = 15) -> None:
    """Wait up to five minutes for a current pass. Share one task per process."""
    if not enabled(ctx):
        return
    task = getattr(ctx, "_sandbox_sync_task", None)
    if task is None or task.done():
        task = asyncio.create_task(sync(ctx, max_age_minutes=max_age_minutes))
        ctx._sandbox_sync_task = task
        if scheduler := getattr(ctx, "scheduler", None):
            scheduler.tasks[:] = [item for item in scheduler.tasks if not item.done()]
            scheduler.tasks.append(task)
    try:
        async with asyncio.timeout(300):
            await asyncio.shield(task)
    except TimeoutError:
        return


async def status(conn):
    state = await kv.get(conn, KEY) or {}
    count = sum(r.get("ok", False) for r in state.get("repositories", {}).values())
    at = state.get("last_success_at")
    age = f"{int((time.time() - at) / 60)} minutes ago" if at else "never"
    return f"Repository mirrors: {count}. Last clean sync: {age}."


async def setup(ctx):
    if not enabled(ctx):
        return

    async def tick():
        if not enabled(ctx):
            return
        async with ctx.pool.connection() as conn:
            state = await kv.get(conn, KEY) or {}
        if time.time() - state.get("last_attempt_at", 0) >= ctx.workspace.config.sandbox.sync_minutes * 60:
            await ensure_fresh(ctx, max_age_minutes=0)

    ctx.every(60, tick)
