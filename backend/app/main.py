"""FastAPI application factory."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.action_items.router import router as action_items_router
from app.agendas.router import router as agendas_router
from app.auth.dependencies import CSRF_HEADER
from app.auth.router import router as auth_router
from app.calls.router import router as calls_router
from app.common.config import get_settings
from app.common.db import check_db_role_safety, dispose_engine, get_engine
from app.common.errors import error_response, register_exception_handlers
from app.common.logging import configure_logging
from app.common.middleware import REQUEST_ID_HEADER, RequestContextMiddleware
from app.common.rate_limit import client_ip
from app.contacts.router import router as contacts_router
from app.jobs.service import worker_loop
from app.tenants.router import router as tenants_router
from app.users.router import router as users_router

logger = logging.getLogger(__name__)

_PROXY_HEADERS = (
    "x-forwarded-for",
    "x-forwarded-proto",
    "x-forwarded-host",
    "x-real-ip",
    "true-client-ip",
    "cf-connecting-ip",
    "forwarded",
    "via",
)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    if settings.enforce_db_role_safety:
        await check_db_role_safety(get_engine(), strict=settings.is_production)
    import app.common.handlers  # noqa: F401  (registers job handlers and mock AI handlers)

    stop = asyncio.Event()
    worker: asyncio.Task[None] | None = None
    if settings.jobs_worker_enabled and settings.app_env != "test":
        worker = asyncio.create_task(worker_loop(stop), name="jobs-worker")
    logger.info("startup_complete", extra={"env": settings.app_env})
    yield
    stop.set()
    if worker is not None:
        await worker
    await dispose_engine()


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)

    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        lifespan=lifespan,
        # Interactive docs are a development aid only.
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None,
        openapi_url=None if settings.is_production else "/openapi.json",
    )
    register_exception_handlers(app)

    api = APIRouter(prefix="/api/v1")
    api.include_router(auth_router)
    api.include_router(users_router)
    api.include_router(tenants_router)
    api.include_router(contacts_router)
    api.include_router(calls_router)
    api.include_router(action_items_router)
    api.include_router(agendas_router)
    app.include_router(api)

    @app.get("/health", tags=["health"], include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready", tags=["health"], include_in_schema=False, response_model=None)
    async def ready() -> object:
        try:
            async with get_engine().connect() as conn:
                await conn.execute(text("SELECT 1"))
        except Exception:
            logger.exception("readiness_db_check_failed")
            return error_response(503, "not_ready", "Database unavailable")
        return {"status": "ready"}

    @app.get("/health/client-ip", tags=["health"], include_in_schema=False, response_model=None)
    async def client_ip_diagnostics(request: Request) -> object:
        """Deployment diagnostic: shows how the API sees the caller's address. Only returns the
        caller's own request data. Disabled (404) unless DIAGNOSTICS_ENABLED=true."""
        if not settings.diagnostics_enabled:
            return error_response(404, "not_found", "Not Found")
        return {
            "peer": request.client.host if request.client else None,
            "proxy_headers": {
                name: request.headers.getlist(name)
                for name in _PROXY_HEADERS
                if name in request.headers
            },
            "trusted_proxy_hops": settings.trusted_proxy_hops,
            "resolved_client_ip": client_ip(request),
        }

    # Middleware added last runs first: CORS is outermost so even error responses carry
    # CORS headers.
    app.add_middleware(RequestContextMiddleware, hsts=settings.is_production)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
        allow_headers=["Authorization", "Content-Type", CSRF_HEADER, REQUEST_ID_HEADER],
        expose_headers=[REQUEST_ID_HEADER],
        max_age=600,
    )
    return app


app = create_app()
