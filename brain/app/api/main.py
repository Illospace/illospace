"""Illo Brain Dashboard API — FastAPI entry point."""
import asyncio
import logging
import os
from collections.abc import Mapping
from contextlib import asynccontextmanager

import structlog
from fastapi import Depends, FastAPI, Request
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import JSONResponse

from brain.app.api.config import CORS_ORIGINS, SECRET_KEY, validate_auth_config
from brain.app.api.deps import get_db

validate_auth_config()

structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.dev.ConsoleRenderer(),
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    logger_factory=structlog.stdlib.LoggerFactory(),
)

logger = structlog.get_logger(__name__)


_OPS_TRIGGER_EVENTS = frozenset(
    {
        "run_started",
        "step_started",
        "tool_started",
        "tool_finished",
        "run_completed",
        "status_change",
    }
)
_ops_pending = False
_OPS_THROTTLE_SEC = 2.0
_run_event_consumer_task: asyncio.Task | None = None
_GLOBAL_WS_EVENT_ALLOWLIST: frozenset[str] = frozenset()

# Reference to the main asyncio event loop, set during lifespan startup.
# Used by the product event publisher to schedule coroutines from background threads.
_main_loop: asyncio.AbstractEventLoop | None = None


def _should_start_inline_runner() -> bool:
    """Return whether the API process should consume the Cortex queue."""
    enabled_values = {"1", "true", "yes", "on"}
    disabled_values = {"0", "false", "no", "off"}
    explicit_runner = os.getenv("CORTEX_INLINE_RUNNER", "").strip().lower()
    if explicit_runner in enabled_values:
        return True
    if explicit_runner in disabled_values:
        return False
    return os.getenv("CORTEX_INLINE_DISPATCHER", "").strip().lower() in enabled_values


def _should_start_run_event_consumer() -> bool:
    """Return whether the API process should replay durable run events."""
    return os.getenv("CORTEX_EVENT_FANOUT_ENABLED", "1").strip().lower() not in {
        "0", "false", "no", "off",
    }


def _run_event_consumer_running() -> bool:
    """Return whether the replay consumer task is currently active."""
    return _run_event_consumer_task is not None and not _run_event_consumer_task.done()


async def _flush_ops_snapshot():
    """Send one ops snapshot after the throttle window, then reset."""
    global _ops_pending
    await asyncio.sleep(_OPS_THROTTLE_SEC)
    _ops_pending = False
    try:
        from brain.systems.runs.cortex.read_models import RunReadScope, serialize_active_runs_async
        from brain.app.api.routers.ws import ws_manager
        for org_id in ws_manager.connected_org_ids:
            snapshot = await serialize_active_runs_async(RunReadScope.for_org(org_id))
            await ws_manager.broadcast_to_org(
                org_id,
                "ops_update",
                {"runs": snapshot},
            )
    except Exception as e:
        logger.warning("flush_ops_snapshot_failed", error=str(e))


async def _run_event_consumer_loop():
    """Replay durable run events into the websocket manager."""
    from brain.app.api.routers.ws import ws_manager
    from brain.app.api.ws.run_events import fanout_run_events

    await fanout_run_events(ws_manager, logger=logger)


async def _ensure_starting_skill_bundle() -> None:
    """Materialize the bundled starter skills for fresh installations."""
    try:
        from brain.systems.skills.builtin import ensure_builtin_skills_cached

        await ensure_builtin_skills_cached(ttl_seconds=0)
        logger.info("starting_skill_bundle_ensured")
    except Exception as exc:
        logger.warning("starting_skill_bundle_ensure_failed", error=str(exc))


def _log_publish_failure(future):
    try:
        future.result()
    except Exception as exc:
        logger.warning("product_event_publish_failed", error=str(exc))


async def _publish_product_event(event_type, data):
    """Resolve product scope and broadcast a live event without sync DB access."""
    from brain.app.api.routers.ws import ws_manager
    from brain.systems.cortex.events import resolve_event_org_id_async

    payload = dict(data)
    org_id = str(payload.get("org_id") or "").strip()
    if not org_id:
        try:
            org_id = await resolve_event_org_id_async(dict(payload)) or ""
        except Exception as exc:
            logger.warning(
                "product_event_org_resolve_failed",
                event_type=event_type,
                error=str(exc),
            )
    if org_id:
        payload["org_id"] = org_id
    await ws_manager.broadcast_product_event(
        event_type,
        payload,
        org_id=org_id or None,
        allow_global=str(event_type) in _GLOBAL_WS_EVENT_ALLOWLIST,
    )


def _schedule_product_event_publish(event_type, data):
    """Schedule product websocket fanout from the event bus."""
    global _ops_pending
    if _main_loop is None:
        logger.warning("product_event_publish_before_loop", event_type=event_type)
        return

    if not isinstance(data, Mapping):
        logger.warning("product_event_publish_dropped_non_mapping", event_type=event_type)
        return

    publish_coro = _publish_product_event(event_type, data)
    try:
        if _main_loop.is_running():
            future = asyncio.run_coroutine_threadsafe(publish_coro, _main_loop)
            future.add_done_callback(_log_publish_failure)
            publish_coro = None
        else:
            _main_loop.run_until_complete(publish_coro)
            publish_coro = None
    except Exception as e:
        if publish_coro is not None:
            publish_coro.close()
        logger.warning("product_event_publish_failed", event_type=event_type, error=str(e))

    # Throttled ops snapshot: at most one push per _OPS_THROTTLE_SEC
    if event_type in _OPS_TRIGGER_EVENTS and not _ops_pending:
        _ops_pending = True
        snapshot_coro = _flush_ops_snapshot()
        try:
            asyncio.run_coroutine_threadsafe(snapshot_coro, _main_loop)
            snapshot_coro = None
        except Exception as e:
            if snapshot_coro is not None:
                snapshot_coro.close()
            _ops_pending = False
            logger.warning("product_event_ops_snapshot_failed", error=str(e))


@asynccontextmanager
async def lifespan(app):
    """Wire the brain event bus to WebSocket broadcasting on startup."""
    global _main_loop, _run_event_consumer_task
    _main_loop = asyncio.get_running_loop()
    inline_runner_started = False

    from brain.systems.cortex.events import set_publisher
    set_publisher(_schedule_product_event_publish)
    await _ensure_starting_skill_bundle()
    if _should_start_run_event_consumer():
        _run_event_consumer_task = asyncio.create_task(_run_event_consumer_loop())
        logger.info("run_event_consumer_started")
    else:
        logger.info("run_event_consumer_skipped", mode="disabled")
    try:
        from brain.systems.runtime_settings.oauth_callback_server import ensure_callback_server

        status = ensure_callback_server()
        if status.available:
            logger.info("openai_oauth_callback_server_started", redirect_uri=status.redirect_uri)
        else:
            logger.info("openai_oauth_callback_server_skipped", detail=status.detail)
    except Exception as exc:
        logger.warning("openai_oauth_callback_server_start_failed", error=str(exc))
    # The standalone cortex-worker should own queue consumption in production.
    # Starting a second runner inside the API process can lead to mixed
    # runtimes consuming the same queue and inconsistent worker behavior.
    if _should_start_inline_runner():
        try:
            from brain.systems.runs.cortex import start_runner
            from brain.systems.cycles import start_cycle_scheduler
            start_runner()
            start_cycle_scheduler()
            inline_runner_started = True
            logger.info("run_worker_started", mode="inline")
        except Exception as e:
            logger.warning("run_worker_start_failed", error=str(e), mode="inline")
    else:
        logger.info("run_worker_skipped", mode="standalone")
    try:
        yield
    finally:
        if _run_event_consumer_task is not None:
            _run_event_consumer_task.cancel()
            try:
                await _run_event_consumer_task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.warning("run_event_consumer_stop_failed", error=str(exc))
            _run_event_consumer_task = None
        if inline_runner_started:
            try:
                from brain.systems.runs.cortex import stop_runner
                from brain.systems.cycles import stop_cycle_scheduler
                stop_cycle_scheduler()
                stop_runner()
            except Exception as e:
                logger.warning("run_worker_stop_failed", error=str(e), mode="inline")


app = FastAPI(
    title="Illo Brain Dashboard API",
    version="6.0.0",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
    lifespan=lifespan,
)

app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob: https: http:; "
        "font-src 'self'; "
        "connect-src 'self' ws: wss:; "
        "frame-src 'self' blob: data: about:; "
        "object-src 'none'; "
        "base-uri 'self'"
    )
    return response


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    import traceback, os
    tb = traceback.format_exc()
    logger.error("unhandled_error", path=request.url.path, error=str(exc), traceback=tb)
    detail = {"error": "Internal server error"}
    if os.getenv("DEBUG", "").lower() in ("1", "true", "yes"):
        detail["detail"] = str(exc)
        detail["traceback"] = tb
    return JSONResponse(status_code=500, content=detail)


from brain.app.api.routers.ws import router as ws_router
from brain.app.api.routers.auth import router as auth_router
from brain.app.api.routers import brain, cortex, cortex_intel, memory, skills, vault, system, team, costs, journal, domains, workspace_apps, workspace_pins, onboarding
from brain.app.api.routers import agent_bridge, agent_connections, agent_mcp
from brain.app.api.routers.cycles import router as cycles_router
from brain.app.api.routers.chat import router as chat_router
from brain.app.api.routers.notifications import router as notifications_router
from brain.systems.runtime_settings.router import router as runtime_settings_router

app.include_router(ws_router)
app.include_router(auth_router)
app.include_router(brain.router)
app.include_router(cortex.router)
app.include_router(cortex_intel.router)
app.include_router(memory.router)
app.include_router(skills.router)
app.include_router(vault.router)
app.include_router(system.router)
app.include_router(runtime_settings_router)
app.include_router(onboarding.router)
app.include_router(agent_connections.router)
app.include_router(agent_bridge.router)
app.include_router(agent_mcp.router)
app.include_router(cycles_router)
app.include_router(team.router)
app.include_router(costs.router)
app.include_router(journal.router)
app.include_router(domains.router)
app.include_router(workspace_apps.router)
app.include_router(workspace_pins.router)
app.include_router(chat_router)
app.include_router(notifications_router)


@app.get("/api/health")
async def health(db: AsyncSession = Depends(get_db)):
    from brain.app.ops.health import compatibility_health_snapshot

    return await compatibility_health_snapshot(
        consumer_running=_run_event_consumer_running(),
        session=db,
    )


# --- Static file serving + SPA fallback (must be LAST) ---
from pathlib import Path
from starlette.staticfiles import StaticFiles
from starlette.responses import FileResponse

_build_dir = Path(__file__).resolve().parents[3] / "frontend" / "build"

_upload_dir = Path(__file__).resolve().parents[2] / "uploads"
_upload_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static/uploads", StaticFiles(directory=str(_upload_dir)), name="uploads")

if _build_dir.exists():
    app.mount("/", StaticFiles(directory=str(_build_dir), html=True), name="spa")


@app.exception_handler(404)
async def spa_fallback(request: Request, exc):
    if request.url.path.startswith("/api/") or request.url.path.startswith("/ws"):
        detail = getattr(exc, "detail", None)
        if detail not in {None, "Not Found"}:
            return JSONResponse(status_code=404, content={"detail": detail})
        return JSONResponse(status_code=404, content={"error": "Not found"})
    index = _build_dir / "index.html"
    if index.exists():
        return FileResponse(str(index))
    return JSONResponse(status_code=404, content={"error": "Not found"})
