"""The gateway starts runs through an authenticated JSON lines stream."""

import asyncio
import json
import logging
import os
import secrets
from contextlib import aclosing

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse

from brain.models import ChatGPTAuth, RunRequest, event
from brain.process import run_worker
from brain.runtimes.claude import has_auth
from brain.runtimes.provider_errors import identifier


def create_app(*, concurrency: int | None = None, runner=run_worker) -> FastAPI:
    app = FastAPI(title="Illo Brain", docs_url=None, redoc_url=None, openapi_url=None)
    slots = asyncio.Semaphore(concurrency or int(os.getenv("ILLO_BRAIN_CONCURRENCY", "4")))

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        # FastAPI's default response can echo the MCP token in rejected input.
        return JSONResponse(status_code=422, content={"detail": "Invalid run request"})

    @app.get("/healthz")
    async def health():
        return {
            "status": "ok",
            "runtimes": {
                "claude": {"usable": has_auth()},
                "openai_compat": {
                    "usable": bool(os.getenv("OPENAI_API_KEY") or os.getenv("OPENAI_BASE_URL")),
                    "experimental": True,
                },
                "openai_responses": {
                    "usable": bool(os.getenv("OPENAI_API_KEY")),
                    "api_key": bool(os.getenv("OPENAI_API_KEY")),
                    "per_run_chatgpt": True,
                },
                "echo": {"usable": True},
            },
        }

    @app.post("/run")
    async def run(body: RunRequest, request: Request):
        secret = os.getenv("ILLO_BRAIN_SECRET")
        if not secret:
            raise HTTPException(status_code=503, detail="Set ILLO_BRAIN_SECRET before a run")
        supplied = request.headers.get("X-Illo-Brain-Secret", "")
        if not secrets.compare_digest(supplied.encode(), secret.encode()):
            raise HTTPException(status_code=401, detail="Unauthorized")

        async def events():
            yield json.dumps(event("started", run_id=body.run_id)) + "\n"
            async with slots:
                try:
                    limit = min(body.timeout_s, float(os.getenv("ILLO_BRAIN_TIMEOUT_S", "120")))
                    async with asyncio.timeout(limit), aclosing(runner(body)) as stream:
                        async for item in stream:
                            item["run_id"] = body.run_id
                            # Do not let a provider echo a credential into the stream.
                            line = json.dumps(item)
                            for value in (
                                body.mcp_token.get_secret_value(),
                                secret,
                                os.getenv("ANTHROPIC_API_KEY"),
                                os.getenv("ANTHROPIC_AUTH_TOKEN"),
                                os.getenv("OPENAI_API_KEY"),
                                body.provider_auth.access_token.get_secret_value()
                                if isinstance(body.provider_auth, ChatGPTAuth)
                                else None,
                                body.provider_auth.account_id.get_secret_value()
                                if isinstance(body.provider_auth, ChatGPTAuth)
                                else None,
                            ):
                                if value:
                                    line = line.replace(json.dumps(value)[1:-1], "[redacted]")
                            if item["type"] == "error":
                                safe = json.loads(line)
                                logging.getLogger(__name__).warning(
                                    "Brain error: category=%s http_status=%s code=%s type=%s",
                                    identifier(safe.get("category")),
                                    safe.get("http_status")
                                    if isinstance(safe.get("http_status"), int)
                                    else None,
                                    identifier(safe.get("code")),
                                    identifier(safe.get("provider_type")),
                                )
                            yield line + "\n"
                            if item["type"] in ("final", "error"):
                                return
                except TimeoutError:
                    yield (
                        json.dumps(
                            event(
                                "error",
                                run_id=body.run_id,
                                category="timeout",
                                message="The brain run timed out",
                            )
                        )
                        + "\n"
                    )
                except Exception:
                    yield (
                        json.dumps(
                            event(
                                "error",
                                run_id=body.run_id,
                                category="internal",
                                message="The brain run failed",
                            )
                        )
                        + "\n"
                    )

        return StreamingResponse(
            events(),
            media_type="application/x-ndjson",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    return app


app = create_app()
