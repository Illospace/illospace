"""Async boundaries for blocking operating-system I/O."""
from __future__ import annotations

import asyncio
import shutil
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, TypeVar

import httpx

T = TypeVar("T")


async def run_blocking(func: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    return await asyncio.to_thread(func, *args, **kwargs)


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
