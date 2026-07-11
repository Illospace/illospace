"""Async boundaries for blocking operating-system I/O."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import contextvars
from contextlib import contextmanager
from functools import partial
import inspect
import os
import shutil
import subprocess
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, TypeVar

import httpx

T = TypeVar("T")


def _env_int(name: str, default: int, *, minimum: int) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except ValueError:
        return default


_TOOL_EXECUTOR_MAX_WORKERS = _env_int("AGENT_SYNC_TOOL_MAX_WORKERS", 16, minimum=1)
_TOOL_EXECUTOR_MAX_QUEUE = _env_int("AGENT_SYNC_TOOL_MAX_QUEUE", 32, minimum=0)
_TOOL_EXECUTOR = ThreadPoolExecutor(
    max_workers=_TOOL_EXECUTOR_MAX_WORKERS,
    thread_name_prefix="agent-tool",
)
_TOOL_ADMISSION = threading.BoundedSemaphore(
    _TOOL_EXECUTOR_MAX_WORKERS + _TOOL_EXECUTOR_MAX_QUEUE
)


class BlockingInvocationCancelled(asyncio.CancelledError):
    """Cancellation raised only after an unavoidable sync call has settled."""

    def __init__(self, *, result: Any = None, error: Exception | None = None):
        super().__init__("blocking invocation settled after cancellation")
        self.result = result
        self.error = error


class ToolExecutorOverloaded(RuntimeError):
    """The isolated synchronous-tool pool has no bounded admission capacity."""


class InvocationProbe:
    """Call-local state distinguishing a live sync phase from returned async work."""

    def __init__(self):
        self.side_effect_started = False
        self._blocking_lock = threading.Lock()
        self._active_blocking_states: set[_BlockingCallState] = set()

    def enter_blocking_call(self, state: "_BlockingCallState") -> None:
        with self._blocking_lock:
            self._active_blocking_states.add(state)

    def leave_blocking_call(self, state: "_BlockingCallState") -> None:
        with self._blocking_lock:
            self._active_blocking_states.discard(state)

    def blocking_snapshot(self) -> tuple[bool, bool]:
        """Return whether a sync call is active and whether one has started."""
        with self._blocking_lock:
            states = tuple(self._active_blocking_states)
        return bool(states), any(state.started.is_set() for state in states)

    @property
    def active_blocking_calls(self) -> int:
        with self._blocking_lock:
            return len(self._active_blocking_states)


class _BlockingCallState:
    def __init__(self):
        self.started = threading.Event()


_CURRENT_INVOCATION_PROBE: contextvars.ContextVar[InvocationProbe | None] = contextvars.ContextVar(
    "agent_tool_invocation_probe",
    default=None,
)


@contextmanager
def bind_invocation_probe(probe: InvocationProbe):
    token = _CURRENT_INVOCATION_PROBE.set(probe)
    try:
        yield probe
    finally:
        _CURRENT_INVOCATION_PROBE.reset(token)


def mark_side_effect_started() -> None:
    """Mark the point after preflight where a mutating handler may take effect."""
    probe = _CURRENT_INVOCATION_PROBE.get()
    if probe is not None:
        probe.side_effect_started = True


async def _await_task_uninterruptibly(task):
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                return task.result()


async def run_tool_blocking(func: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """Run a sync tool in an isolated executor with bounded admission."""
    probe = _CURRENT_INVOCATION_PROBE.get()
    return await _run_tool_blocking(_BlockingCallState(), func, args, kwargs, probe=probe)


async def _run_tool_blocking(
    state: _BlockingCallState,
    func: Callable[..., T],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    *,
    probe: InvocationProbe | None = None,
) -> T:
    loop = asyncio.get_running_loop()
    admitted = False
    if probe is not None:
        probe.enter_blocking_call(state)
    try:
        try:
            timeout = max(0.001, float(os.getenv("AGENT_SYNC_TOOL_ADMISSION_TIMEOUT_SECONDS", "2")))
        except ValueError:
            timeout = 2.0
        deadline = loop.time() + timeout
        while not _TOOL_ADMISSION.acquire(blocking=False):
            if loop.time() >= deadline:
                raise ToolExecutorOverloaded(
                    "Synchronous tool capacity is saturated; retry after other tool calls finish"
                )
            await asyncio.sleep(min(0.01, max(0.001, deadline - loop.time())))
        admitted = True
        context = contextvars.copy_context()
        call = partial(func, *args, **kwargs)

        def run_call():
            state.started.set()
            return context.run(call)

        concurrent_worker = _TOOL_EXECUTOR.submit(run_call)
        worker = asyncio.wrap_future(concurrent_worker, loop=loop)
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError as cancellation:
            # ``shield`` protects submitted work from the caller's cancellation.
            # If the executor has not started it yet, cancel the underlying
            # concurrent future directly so a canceled tool cannot run later.
            if not state.started.is_set() and concurrent_worker.cancel():
                raise
            try:
                result = await _await_task_uninterruptibly(worker)
            except Exception as exc:
                raise BlockingInvocationCancelled(error=exc) from cancellation
            raise BlockingInvocationCancelled(result=result) from cancellation
    finally:
        if admitted:
            _TOOL_ADMISSION.release()
        if probe is not None:
            probe.leave_blocking_call(state)


async def run_blocking(func: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    return await asyncio.to_thread(func, *args, **kwargs)


def callable_runs_on_event_loop(func: Callable[..., Any]) -> bool:
    call = getattr(func, "__call__", None)
    return (
        inspect.iscoroutinefunction(func)
        or inspect.iscoroutinefunction(call)
        or bool(getattr(func, "_illo_run_on_event_loop", False))
    )


def callable_uses_blocking_thread(func: Callable[..., Any]) -> bool:
    explicit = getattr(func, "_illo_uses_blocking_thread", None)
    if explicit is not None:
        return bool(explicit)
    if getattr(func, "_illo_run_on_event_loop", False):
        wrapped = getattr(func, "__wrapped__", None)
        return callable_uses_blocking_thread(wrapped) if callable(wrapped) else False
    return not callable_runs_on_event_loop(func)


async def invoke_maybe_async(func: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """Invoke async callables on-loop and offload synchronous work.

    Context variables are copied into the isolated tool executor, so runtime-bound
    AgentRun context remains visible to synchronous handlers without blocking
    heartbeats, cancellation, or other runner slots.
    """

    if callable_runs_on_event_loop(func):
        result = func(*args, **kwargs)
    else:
        probe = _CURRENT_INVOCATION_PROBE.get()
        state = _BlockingCallState()
        blocking_task = asyncio.create_task(
            _run_tool_blocking(state, func, args, kwargs, probe=probe),
            name=f"blocking-{getattr(func, '__name__', 'callable')}",
        )
        try:
            result = await asyncio.shield(blocking_task)
        except asyncio.CancelledError as cancellation:
            if not state.started.is_set():
                blocking_task.cancel()
                try:
                    await _await_task_uninterruptibly(blocking_task)
                except BaseException:
                    pass
                raise
            try:
                result = await _await_task_uninterruptibly(blocking_task)
            except Exception as exc:
                raise BlockingInvocationCancelled(error=exc) from cancellation
            if inspect.isawaitable(result):
                if inspect.iscoroutine(result):
                    result.close()
                else:
                    cancel = getattr(result, "cancel", None)
                    if callable(cancel):
                        cancel()
                raise
            raise BlockingInvocationCancelled(result=result) from cancellation
    if inspect.isawaitable(result):
        return await result
    return result


async def ensure_dir(path: str | Path, *, parents: bool = True, exist_ok: bool = True) -> Path:
    resolved = Path(path)
    await run_blocking(resolved.mkdir, parents=parents, exist_ok=exist_ok)
    return resolved


async def path_exists(path: str | Path) -> bool:
    return await run_blocking(Path(path).exists)


async def path_is_file(path: str | Path) -> bool:
    return await run_blocking(Path(path).is_file)


async def path_stat(path: str | Path):
    return await run_blocking(Path(path).stat)


async def read_text(path: str | Path, *, encoding: str = "utf-8") -> str:
    return await run_blocking(Path(path).read_text, encoding=encoding)


async def write_text(path: str | Path, data: str, *, encoding: str = "utf-8") -> int:
    return await run_blocking(Path(path).write_text, data, encoding=encoding)


async def read_bytes(path: str | Path) -> bytes:
    return await run_blocking(Path(path).read_bytes)


async def write_bytes(path: str | Path, data: bytes) -> int:
    return await run_blocking(Path(path).write_bytes, data)


async def glob_paths(path: str | Path, pattern: str) -> list[Path]:
    root = Path(path)
    return await run_blocking(lambda: list(root.glob(pattern)))


async def iter_dir(path: str | Path) -> list[Path]:
    root = Path(path)
    return await run_blocking(lambda: list(root.iterdir()))


async def copy_file(source: str | Path, target: str | Path) -> Path:
    copied = await run_blocking(shutil.copy2, source, target)
    return Path(copied)


async def remove_tree(path: str | Path, *, ignore_errors: bool = False) -> None:
    await run_blocking(shutil.rmtree, path, ignore_errors=ignore_errors)


async def rename_path(source: str | Path, target: str | Path) -> None:
    await run_blocking(Path(source).rename, target)


async def run_subprocess(
    args: Sequence[str | Path],
    *,
    timeout: float | None = None,
    **kwargs: Any,
) -> subprocess.CompletedProcess[Any]:
    return await run_blocking(subprocess.run, args, timeout=timeout, **kwargs)


async def check_output(
    args: Sequence[str | Path],
    *,
    timeout: float | None = None,
    **kwargs: Any,
) -> bytes | str:
    return await run_blocking(subprocess.check_output, args, timeout=timeout, **kwargs)


async def popen(args: Sequence[str | Path], **kwargs: Any) -> subprocess.Popen[Any]:
    return await run_blocking(subprocess.Popen, args, **kwargs)


def run_subprocess_sync(
    args: Sequence[str | Path],
    *,
    timeout: float | None = None,
    **kwargs: Any,
) -> subprocess.CompletedProcess[Any]:
    return subprocess.run(args, timeout=timeout, **kwargs)


def check_output_sync(
    args: Sequence[str | Path],
    *,
    timeout: float | None = None,
    **kwargs: Any,
) -> bytes | str:
    return subprocess.check_output(args, timeout=timeout, **kwargs)


def popen_sync(args: Sequence[str | Path], **kwargs: Any) -> subprocess.Popen[Any]:
    return subprocess.Popen(args, **kwargs)


def sync_http_client(**kwargs: Any) -> httpx.Client:
    return httpx.Client(**kwargs)


def async_http_client(**kwargs: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(**kwargs)


def http_get(url: str, *, timeout: float | httpx.Timeout | None = None, **kwargs: Any) -> httpx.Response:
    return httpx.get(url, timeout=timeout, **kwargs)


def http_post(url: str, *, timeout: float | httpx.Timeout | None = None, **kwargs: Any) -> httpx.Response:
    return httpx.post(url, timeout=timeout, **kwargs)


async def async_http_get(
    url: str,
    *,
    timeout: float | httpx.Timeout | None = None,
    **kwargs: Any,
) -> httpx.Response:
    async with async_http_client(timeout=timeout) as client:
        return await client.get(url, **kwargs)


async def async_http_post(
    url: str,
    *,
    timeout: float | httpx.Timeout | None = None,
    **kwargs: Any,
) -> httpx.Response:
    async with async_http_client(timeout=timeout) as client:
        return await client.post(url, **kwargs)
