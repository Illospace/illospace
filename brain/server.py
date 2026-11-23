"""The gateway starts runs through an authenticated JSON lines stream."""

import asyncio
import json
import logging
import os
import re
import secrets
from contextlib import aclosing
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse

from brain import providers
from brain.errors import RunError, identifier
from brain.models import ChatGPTAuth, RunRequest, event
from brain.process import run_worker
from brain.tools import check_echo

MAX_AUDIO_BYTES = 25 * 1024 * 1024
FAKE_TRANSCRIPT = "This is the fake transcript."


def redact(item: dict, body: RunRequest, secret: str, keys) -> str:
    """Give the JSON line of an event. A provider cannot echo a credential into the stream."""
    line = json.dumps(item)
    for value in (
        *(image.data.get_secret_value() for image in body.images),
        body.mcp_token.get_secret_value(),
        secret,
        *keys,
        *((body.provider_auth.access_token.get_secret_value(),
           body.provider_auth.account_id.get_secret_value())
          if isinstance(body.provider_auth, ChatGPTAuth) else ()),
    ):
        if value:
            line = line.replace(json.dumps(value)[1:-1], "[redacted]")
    return line


def create_app(*, concurrency: int | None = None, runner=run_worker, table=None,
               transport=None) -> FastAPI:
    """`table` and `transport` (for transcription requests) are for tests."""
    app = FastAPI(title="Illo Brain", docs_url=None, redoc_url=None, openapi_url=None)
    default_concurrency, cap = providers.boot_settings()
    slots = asyncio.Semaphore(concurrency or default_concurrency)
    table = providers.load() if table is None else table
    keys = [provider.api_key for provider in table.values() if provider.api_key]

    def authorize(request: Request) -> str:
        secret = os.getenv("ILLO_BRAIN_SECRET")
        if not secret:
            raise HTTPException(status_code=503, detail="Set ILLO_BRAIN_SECRET before a run")
        supplied = request.headers.get("X-Illo-Brain-Secret", "")
        if not secrets.compare_digest(supplied.encode(), secret.encode()):
            raise HTTPException(status_code=401, detail="Unauthorized")
        return secret

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        # FastAPI's default response can echo the MCP token in rejected input.
        return JSONResponse(status_code=422, content={"detail": "Invalid run request"})

    @app.get("/healthz")
    async def health():
        return {"status": "ok"}

    @app.get("/providers")
    async def describe(request: Request):
        authorize(request)
        return providers.description(table)

    @app.post("/transcribe")
    async def transcribe(request: Request, provider: str, model: str, extension: str):
        authorize(request)
        item = table.get(provider)
        https = item is not None and (item.wire == "fake" or (
            item.base_url is not None and urlsplit(item.base_url).scheme == "https"))
        usable = item is not None and item.transcription and https
        if not usable or not re.fullmatch(r"[a-z0-9]{1,8}", extension) or not 0 < len(model) <= 128:
            return JSONResponse(status_code=422, content={"category": "config"})
        audio = await request.body()
        if len(audio) > MAX_AUDIO_BYTES:
            return JSONResponse(status_code=413, content={"category": "limit"})
        if item.wire == "fake":
            return {"text": FAKE_TRANSCRIPT}
        try:
            async with httpx.AsyncClient(timeout=60, trust_env=False, follow_redirects=False,
                                         transport=transport) as client:
                async with client.stream(
                    "POST",
                    item.base_url + "/audio/transcriptions",
                    headers={"Authorization": "Bearer " + item.api_key},
                    data={"model": model, "response_format": "json"},
                    files={"file": (f"clip.{extension}", audio, "application/octet-stream")},
                ) as result:
                    if result.status_code != 200:
                        return JSONResponse(status_code=502, content={"category": "runtime"})
                    raw = bytearray()
                    async for chunk in result.aiter_bytes():
                        if len(raw) + len(chunk) > 262144:
                            return JSONResponse(status_code=502, content={"category": "runtime"})
                        raw.extend(chunk)
            reply = json.loads(raw)
            # The transcript goes into the inbound message. It never holds the provider key.
            check_echo(reply, item.api_key)
            text = reply.get("text")
        except (httpx.HTTPError, ValueError, AttributeError, RunError):
            text = None
        if not isinstance(text, str) or not text.strip():
            return JSONResponse(status_code=502, content={"category": "runtime"})
        return {"text": text.strip()}

    @app.post("/run")
    async def run(body: RunRequest, request: Request):
        secret = authorize(request)
        reason = providers.problem(body, table)

        async def events():
            yield json.dumps(event("started", run_id=body.run_id)) + "\n"
            if reason is not None:
                yield json.dumps(event("error", run_id=body.run_id, category="config",
                                       message=reason)) + "\n"
                return
            async with slots:
                try:
                    fallback = table.get(body.fallback_provider or "")
                    async with (asyncio.timeout(min(body.timeout_s, cap)),
                                aclosing(runner(body, table[body.provider], fallback)) as stream):
                        async for item in stream:
                            item["run_id"] = body.run_id
                            line = redact(item, body, secret, keys)
                            if item["type"] == "error":
                                safe = json.loads(line)
                                logging.getLogger(__name__).warning(
                                    "Brain error: category=%s http_status=%s code=%s type=%s reason=%s",
                                    identifier(safe.get("category")),
                                    safe.get("http_status")
                                    if isinstance(safe.get("http_status"), int)
                                    else None,
                                    identifier(safe.get("code")),
                                    identifier(safe.get("provider_type")),
                                    # A fixed text of this service, after the redaction above.
                                    str(safe.get("message") or "")[:120],
                                )
                            yield line + "\n"
                            if item["type"] in ("final", "error"):
                                return
                except TimeoutError:
                    yield json.dumps(event("error", run_id=body.run_id, category="timeout",
                                           message="The brain run timed out")) + "\n"
                except Exception:
                    yield json.dumps(event("error", run_id=body.run_id, category="internal",
                                           message="The brain run failed")) + "\n"

        return StreamingResponse(
            events(),
            media_type="application/x-ndjson",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    return app


app = create_app()
