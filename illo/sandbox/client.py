"""Call the private shell service without proxy or credential settings."""

import asyncio

import httpx
from pydantic import BaseModel, ValidationError


class SandboxError(RuntimeError):
    """A safe error that the model can read."""


class ExecResult(BaseModel):
    exit_code: int
    stdout: str
    stderr: str
    truncated: bool
    timed_out: bool
    duration_ms: int


class SandboxClient:
    def __init__(self, url: str, *, transport=None):
        self.url = url.rstrip("/")
        self.transport = transport

    async def _request(self, method, path, *, timeout=15, **kwargs):
        try:
            async with asyncio.timeout(timeout), httpx.AsyncClient(
                base_url=self.url, trust_env=False, timeout=timeout, transport=self.transport
            ) as client:
                response = await client.request(method, path, **kwargs)
                response.raise_for_status()
                return response.json()
        except httpx.HTTPStatusError as error:
            text = ("The sandbox is busy. Try again." if error.response.status_code == 429
                    else "The sandbox refused the request.")
            raise SandboxError(text) from None
        except (httpx.HTTPError, ValueError, TimeoutError):
            raise SandboxError("The sandbox did not answer.") from None

    async def run(self, *, run_id: str | int, command: str, timeout_s: float = 30,
                  cwd: str | None = None) -> ExecResult:
        try:
            return ExecResult.model_validate(await self._request("POST", "/exec", timeout=timeout_s + 15,
                json={"run_id": str(run_id), "command": command, "timeout_s": timeout_s, "cwd": cwd}))
        except ValidationError:
            raise SandboxError("The sandbox sent an invalid result.") from None

    async def cleanup(self, run_id) -> None:
        await self._request("POST", "/cleanup", json={"run_id": str(run_id)})

    async def healthy(self) -> bool:
        try:
            return await self._request("GET", "/healthz") == {"status": "ok"}
        except SandboxError:
            return False
