"""A shell service on a private Docker network."""

import asyncio
import math
import os
import resource
import shutil
import signal
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

CAP = 64_000
MARK = b"\n[output cut]\n"


class Run(BaseModel):
    run_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    command: str = Field(max_length=8000)
    timeout_s: float = Field(default=30, ge=1, le=120, allow_inf_nan=False, strict=True)
    cwd: str | None = None


class Cleanup(BaseModel):
    run_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")


class Capture:
    """Keep a fixed head and tail. Read and discard the rest."""

    def __init__(self):
        self.head = bytearray()
        self.tail = bytearray()
        self.total = 0

    async def read(self, stream):
        half = (CAP - len(MARK)) // 2
        while chunk := await stream.read(8192):
            self.total += len(chunk)
            needed = max(0, half - len(self.head))
            self.head.extend(chunk[:needed])
            self.tail.extend(chunk[needed:])
            if len(self.tail) > CAP - half:
                del self.tail[:len(self.tail) - (CAP - half)]

    def text(self):
        if self.total <= CAP:
            value = self.head + self.tail
        else:
            value = self.head + MARK + self.tail[-(CAP - len(self.head) - len(MARK)):]
        return value.decode("utf-8", errors="replace")


def create_app(*, work_root=None, repos_root=None, slot_wait_s=10, process_limit=128):
    work = Path(work_root or os.getenv("ILLO_SANDBOX_WORK_ROOT", "/work")).resolve()
    repos = Path(repos_root or os.getenv("ILLO_SANDBOX_REPOS_ROOT", "/repos")).resolve()
    config = str(Path(__file__).with_name("gitconfig").resolve())
    slots = asyncio.Semaphore(2)
    active = {}

    def expire():
        work.mkdir(parents=True, exist_ok=True)
        for path in work.iterdir():
            if (path.is_dir() and not path.is_symlink() and path.name not in active
                    and time.time() - path.stat().st_mtime > 6 * 3600):
                shutil.rmtree(path)

    @asynccontextmanager
    async def lifespan(app):
        expire()

        async def sweeper():
            while True:
                await asyncio.sleep(1800)
                expire()

        task = asyncio.create_task(sweeper())
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    # No auth header: only the gateway joins this internal Docker network.
    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(RequestValidationError)
    async def invalid(request, error):
        return JSONResponse(status_code=422, content={"detail": "The sandbox request is invalid."})

    @app.get("/healthz")
    async def health():
        return {"status": "ok"}

    def home_for(run_id):
        home = work / run_id
        if home.is_symlink() or home.resolve() != home or (home.exists() and not home.is_dir()):
            raise HTTPException(422, "The run folder is invalid.")
        home.mkdir(parents=True, exist_ok=True)
        return home

    @app.post("/cleanup")
    async def cleanup(body: Cleanup):
        if body.run_id in active:
            raise HTTPException(409, "The run still has an active command.")
        home = home_for(body.run_id)
        shutil.rmtree(home)
        return {"ok": True}

    @app.post("/exec")
    async def execute(body: Run):
        home = home_for(body.run_id)
        try:
            cwd = Path(body.cwd) if body.cwd is not None else home
            if not cwd.is_absolute() or ".." in cwd.parts or "\x00" in str(cwd):
                raise ValueError
            cwd = cwd.resolve()
            if not (cwd.is_relative_to(repos) or cwd.is_relative_to(home)) or not cwd.is_dir():
                raise ValueError
        except (OSError, RuntimeError, ValueError):
            raise HTTPException(422, "The command folder is invalid.") from None
        if "\x00" in body.command:
            raise HTTPException(422, "The command is invalid.")
        try:
            await asyncio.wait_for(slots.acquire(), timeout=slot_wait_s)
        except TimeoutError:
            raise HTTPException(429, "The sandbox is busy. Try again.") from None
        active[body.run_id] = active.get(body.run_id, 0) + 1
        started = time.monotonic()
        process = None
        readers = []
        stdout, stderr = Capture(), Capture()
        timed_out = False

        def limits():
            cpu = math.ceil(body.timeout_s) + 5
            for key, value in (
                (resource.RLIMIT_CPU, cpu),
                (resource.RLIMIT_AS, int(os.getenv("ILLO_SANDBOX_MEMORY_BYTES", str(8 * 1024**3)))),
                (resource.RLIMIT_FSIZE, int(os.getenv("ILLO_SANDBOX_FILE_BYTES", str(256 * 1024**2)))),
                (resource.RLIMIT_NOFILE, 256), (resource.RLIMIT_NPROC, process_limit),
                (resource.RLIMIT_CORE, 0),
            ):
                resource.setrlimit(key, (value, value))

        def kill_group():
            if process:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)

        try:
            process = await asyncio.create_subprocess_exec(
                "bash", "-lc", body.command, cwd=str(cwd),
                env={"HOME": str(home), "PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8",
                     "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_GLOBAL": config, "PAGER": "cat"},
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                start_new_session=True, preexec_fn=limits,
            )
            readers = [asyncio.create_task(stdout.read(process.stdout)),
                       asyncio.create_task(stderr.read(process.stderr))]
            try:
                async with asyncio.timeout(body.timeout_s):
                    await process.wait()
                    kill_group()
                    await asyncio.gather(*readers)
            except TimeoutError:
                timed_out = True
                kill_group()
                await process.wait()
                await asyncio.gather(*readers)
            return {"exit_code": process.returncode, "stdout": stdout.text(), "stderr": stderr.text(),
                    "truncated": stdout.total > CAP or stderr.total > CAP, "timed_out": timed_out,
                    "duration_ms": int((time.monotonic() - started) * 1000)}
        finally:
            kill_group()
            if process:
                await process.wait()
            for reader in readers:
                reader.cancel()
            if readers:
                await asyncio.gather(*readers, return_exceptions=True)
            active[body.run_id] -= 1
            if not active[body.run_id]:
                del active[body.run_id]
            with suppress(OSError):
                if home.is_dir() and not home.is_symlink():
                    home.touch(exist_ok=True)
            slots.release()

    return app


app = create_app()
