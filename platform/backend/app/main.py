"""FastAPI application — LLM Benchmark Leaderboard Platform."""
from __future__ import annotations

import logging
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import (
    get_redoc_html,
    get_swagger_ui_html,
    get_swagger_ui_oauth2_redirect_html,
)
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST

from app.core.config import settings
from app.api.admin import router as admin_router
from app.api.auth import router as auth_router
from app.api.benchmarks import router as benchmarks_router
from app.api.card_types import router as card_types_router
from app.api.modules import router as modules_router
from app.api.replay_datasets import router as replay_datasets_router
from app.api.submissions import router as submissions_router
from app.api.leaderboard import router as leaderboard_router

REQUEST_COUNT = Counter(
    "http_requests_total",
    "Total HTTP requests",
    ["method", "endpoint", "status_code"],
)
REQUEST_LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency",
    ["method", "endpoint"],
)

# Ensure bench modules are importable from the app
sys.path.insert(0, settings.BENCH_MODULES_PATH)

# Configure root logger so app log.info/exception are visible alongside uvicorn
# access logs. Without this, getLogger(__name__).info(...) is silently dropped
# (root level defaults to WARNING when uvicorn is launched directly).
if not logging.getLogger().handlers:
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stdout,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Logging filters
# ---------------------------------------------------------------------------

class _PollAccessFilter(logging.Filter):
    """Suppress noisy Uvicorn access logs from frontend polling."""

    def filter(self, record: logging.LogRecord) -> bool:
        # Uvicorn access args: (client_addr, method, path, version, status_code)
        args = getattr(record, "args", None)
        if isinstance(args, tuple) and len(args) >= 3:
            method, path = args[1], args[2]
            if method == "GET" and isinstance(path, str) and path.startswith("/submissions/"):
                return False
        return True


class _PoolDisconnectFilter(logging.Filter):
    """Suppress SQLAlchemy pool ERRORs caused by client disconnect (CancelledError)."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if "Exception terminating connection" not in msg:
            return True
        # CancelledError is in the exception traceback, not the message
        exc_info = record.exc_info
        if exc_info and exc_info[0] is not None:
            exc_name = exc_info[0].__name__ if hasattr(exc_info[0], "__name__") else str(exc_info[0])
            if exc_name == "CancelledError":
                return False
            # Also check the traceback text for nested CancelledError
            import traceback
            tb_text = "".join(traceback.format_exception(*exc_info))
            if "CancelledError" in tb_text:
                return False
        return True


# Apply once at import time
logging.getLogger("uvicorn.access").addFilter(_PollAccessFilter())
logging.getLogger("sqlalchemy.pool").addFilter(_PoolDisconnectFilter())


# ---------------------------------------------------------------------------
# Lifespan: sync MODULE_REGISTRY → test_modules table on startup
# ---------------------------------------------------------------------------

def _assert_production_secrets() -> None:
    """Fail closed if a non-debug (i.e. public) deploy still has default secrets.

    A default SECRET_KEY lets anyone forge a JWT for any user/role — total auth
    bypass, including super_admin. A default PLATFORM_SECRET_KEY (Fernet) lets
    anyone who reads the DB decrypt stored endpoint API keys. These MUST be set
    from the environment before the service is exposed. We only enforce this when
    DEBUG is off, so local dev keeps working with the checked-in defaults.
    """
    if settings.DEBUG:
        return
    weak = []
    if settings.SECRET_KEY == "change-me-in-production":
        weak.append("SECRET_KEY")
    if settings.PLATFORM_SECRET_KEY == "change-me-32-bytes-base64!":
        weak.append("PLATFORM_SECRET_KEY")
    if weak:
        raise RuntimeError(
            "Refusing to start with default "
            + ", ".join(weak)
            + " while DEBUG is off. Set them via the environment before exposing the service."
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    _assert_production_secrets()
    await _sync_modules()
    from app.core.reaper import ReaperHandle
    reaper = ReaperHandle()
    reaper.start()
    try:
        yield
    finally:
        await reaper.stop()


async def _sync_modules() -> None:
    """Sync bench.modules.MODULE_REGISTRY into the test_modules DB table.

    Errors are logged at ERROR level. We do NOT swallow exceptions silently
    (that left the table out of sync on k8s and surfaced as stale form
    schemas in the admin UI). Failure is non-fatal at startup so the API
    can still serve while an operator investigates — but it's loud.
    """
    from sqlalchemy import select
    from app.db.models import TestModule, _async_session_factory
    from bench.modules import MODULE_REGISTRY

    try:
        async with _async_session_factory() as session:
            updated, inserted = 0, 0
            for name, cls in MODULE_REGISTRY.items():
                result = await session.execute(select(TestModule).where(TestModule.name == name))
                existing = result.scalar_one_or_none()

                descriptor = cls.descriptor()
                params_schema = descriptor["params_schema"]
                default_params = descriptor["default_params"]
                metrics_schema = {
                    "metrics_descriptors": descriptor.get("metrics_descriptors", []),
                    "default_metric_configs": descriptor.get("default_metric_configs", []),
                }

                if existing:
                    existing.display_name = descriptor["display_name"]
                    existing.description = descriptor["description"]
                    existing.params_schema_json = params_schema
                    existing.default_params_json = default_params
                    existing.metrics_schema_json = metrics_schema
                    updated += 1
                else:
                    session.add(TestModule(
                        name=name,
                        display_name=descriptor["display_name"],
                        description=descriptor["description"],
                        params_schema_json=params_schema,
                        default_params_json=default_params,
                        metrics_schema_json=metrics_schema,
                    ))
                    inserted += 1

            await session.commit()
            log.info(
                "Module registry sync complete: %d inserted, %d updated", inserted, updated
            )

    except Exception:
        # Loud, but non-fatal: the API can still serve reads with whatever's
        # in the DB. Operator must check pod logs and fix.
        log.error("=" * 78)
        log.exception("MODULE REGISTRY SYNC FAILED — admin UI may show stale module schemas")
        log.error("=" * 78)


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

# Path prefix the frontend nginx serves the backend under (e.g. "/api"), used
# ONLY to build the URLs embedded in the docs HTML + the openapi `servers` entry.
# We deliberately do NOT pass this as FastAPI(root_path=...): nginx STRIPS the
# /api prefix before forwarding, so the backend must match bare paths (/docs,
# /static, /openapi.json). Setting root_path would push the StaticFiles *Mount*
# to /api/static (Mounts honor root_path; plain Routes don't), and nginx's
# stripped /static/... request would then 404 — leaving Swagger's JS unloaded
# and the page blank. So: serve bare, prefix only what the browser sees.
_DOCS_PREFIX = settings.ROOT_PATH.rstrip("/")

app = FastAPI(
    title="LLM Benchmark Platform API",
    description="Multi-tenant LLM benchmark leaderboard with modular test units",
    version="0.1.0",
    lifespan=lifespan,
    # `servers` makes Swagger "Try it out" target {prefix}/... (e.g. /api/auth/login)
    # so requests route back through nginx. Routing itself stays at bare paths.
    servers=[{"url": _DOCS_PREFIX}] if _DOCS_PREFIX else None,
    # Disable the built-in docs routes: their HTML loads swagger-ui / redoc /
    # fonts / favicon from cdn.jsdelivr.net + fastapi.tiangolo.com, which the
    # air-gapped cluster (no internet egress) cannot reach -> blank pages. We
    # re-add /docs and /redoc below, served entirely from vendored static assets.
    docs_url=None,
    redoc_url=None,
)


# ---------------------------------------------------------------------------
# Self-hosted API docs (no CDN — cluster has no internet egress)
#
# Assets are vendored in app/static/docs/ (swagger-ui-dist + redoc), copied
# into the image by the existing `COPY platform/backend/app/` Dockerfile line.
# URLs are prefixed with _DOCS_PREFIX so the browser routes them through nginx's
# /api/ proxy; the Mount + routes stay at bare paths to match the stripped path.
# ---------------------------------------------------------------------------

app.mount(
    "/static",
    StaticFiles(directory=Path(__file__).resolve().parent / "static"),
    name="static",
)


@app.get("/docs", include_in_schema=False)
async def swagger_ui_html():
    return get_swagger_ui_html(
        openapi_url=f"{_DOCS_PREFIX}{app.openapi_url}",
        title=f"{app.title} - Swagger UI",
        oauth2_redirect_url=f"{_DOCS_PREFIX}{app.swagger_ui_oauth2_redirect_url}",
        swagger_js_url=f"{_DOCS_PREFIX}/static/docs/swagger-ui-bundle.js",
        swagger_css_url=f"{_DOCS_PREFIX}/static/docs/swagger-ui.css",
        swagger_favicon_url=f"{_DOCS_PREFIX}/static/docs/favicon-32x32.png",
    )


@app.get(app.swagger_ui_oauth2_redirect_url, include_in_schema=False)
async def swagger_ui_redirect():
    return get_swagger_ui_oauth2_redirect_html()


@app.get("/redoc", include_in_schema=False)
async def redoc_html():
    return get_redoc_html(
        openapi_url=f"{_DOCS_PREFIX}{app.openapi_url}",
        title=f"{app.title} - ReDoc",
        redoc_js_url=f"{_DOCS_PREFIX}/static/docs/redoc.standalone.js",
        redoc_favicon_url=f"{_DOCS_PREFIX}/static/docs/favicon-32x32.png",
        # ReDoc otherwise pulls Montserrat/Roboto from fonts.googleapis.com.
        with_google_fonts=False,
    )

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS.split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Metrics middleware
@app.middleware("http")
async def metrics_middleware(request: Request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    duration = time.perf_counter() - start
    route = request.url.path
    REQUEST_COUNT.labels(request.method, route, response.status_code).inc()
    REQUEST_LATENCY.labels(request.method, route).observe(duration)
    return response


# Global exception handler
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    log.exception("Unhandled exception on %s %s", request.method, request.url.path)
    detail = f"{type(exc).__name__}: {exc}" if settings.DEBUG else "Internal error"
    return JSONResponse(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, content={"detail": detail})


# Routers
app.include_router(auth_router)
app.include_router(modules_router)
app.include_router(benchmarks_router)
app.include_router(card_types_router)
app.include_router(submissions_router)
app.include_router(leaderboard_router)
app.include_router(admin_router)
app.include_router(replay_datasets_router)


@app.get("/metrics", response_class=PlainTextResponse)
async def metrics():
    return PlainTextResponse(
        content=generate_latest(),
        media_type=CONTENT_TYPE_LATEST,
    )


@app.get("/livez")
async def livez():
    """Liveness: the process is up and serving HTTP.

    Deliberately checks NO external dependencies. A transient DB/Redis/DNS blip
    must NOT make kubelet kill and restart the pod — on a flaky-DNS node that
    turns a momentary blip into a crash loop (the fresh pod then has to
    re-resolve everything, making it worse). Dependency health belongs in
    readiness (/health), which only removes the pod from the Service until the
    dependency recovers — no restart.
    """
    return {"status": "alive"}


@app.get("/health")
async def health():
    from sqlalchemy import text
    from app.db.models import _async_session_factory
    import redis.asyncio as redis_lib

    try:
        async with _async_session_factory() as session:
            await session.execute(text("SELECT 1"))
    except Exception:
        log.exception("Health check failed: DB unreachable")
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "unhealthy", "detail": "database"},
        )

    try:
        r = redis_lib.from_url(settings.REDIS_URL)
        await r.ping()
        await r.close()
    except Exception:
        log.exception("Health check failed: Redis unreachable")
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "unhealthy", "detail": "redis"},
        )

    return {"status": "ok"}
