"""FastAPI composition root for the APEX domain API (/v1).

Mounted into the LangGraph server via `langgraph.json` -> http.app. The LangGraph
built-in surface (/assistants, /threads, /runs) coexists with these routes; docs and
the OpenAPI document live under /v1 to avoid shadowing built-in endpoints.
"""

from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware

from apex.app.distributed_limits import RedisDistributedLimitBackend
from apex.app.errors import register_exception_handlers
from apex.app.lifespan import check_runtime_readiness, lifespan
from apex.app.security import (
    AuthAuditMiddleware,
    RateLimitMiddleware,
    RequestBodyLimitMiddleware,
    SecurityHeadersMiddleware,
)
from apex.routers.analytics import router as analytics_router
from apex.routers.artifacts import router as artifacts_router
from apex.routers.auth import router as auth_router
from apex.routers.catalog import router as catalog_router
from apex.routers.compliance import router as compliance_router
from apex.routers.connections import router as connections_router
from apex.routers.consumers import router as consumers_router
from apex.routers.context import router as context_router
from apex.routers.documents import router as documents_router
from apex.routers.drafts import router as drafts_router
from apex.routers.engines import router as engines_router
from apex.routers.inventory import router as inventory_router
from apex.routers.logs import router as logs_router
from apex.routers.pipelines import router as pipelines_router
from apex.routers.prompts import router as prompts_router
from apex.routers.system import router as system_router
from apex.routers.work_tracking import router as work_tracking_router
from apex.services.usage import UsageTrackingMiddleware
from apex.settings import get_settings

settings = get_settings()
docs_enabled = not settings.is_locked_down

app = FastAPI(
    title="APEX Orchestration Engine — Domain API",
    version=settings.version,
    lifespan=lifespan,
    docs_url="/v1/docs" if docs_enabled else None,
    redoc_url=None,
    openapi_url="/v1/openapi.json" if docs_enabled else None,
)

register_exception_handlers(app)


@app.get(
    "/ready",
    include_in_schema=False,
    operation_id="runtimeReadiness",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def runtime_readiness(request: Request) -> Response:
    """Opaque, unauthenticated dependency-aware orchestrator probe."""

    try:
        await check_runtime_readiness(request.app)
    except Exception as exc:
        # Dependency diagnostics may contain database/Redis endpoints. Keep the
        # public probe deliberately opaque; detailed failures remain in logs.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Service is not ready",
        ) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)

limit_backend = (
    RedisDistributedLimitBackend(settings.redis_uri)
    if settings.rate_limit.backend == "redis"
    else None
)
app.state.distributed_limit_backend = limit_backend
app.add_middleware(
    RateLimitMiddleware,
    settings=settings.rate_limit,
    backend=limit_backend,
)
app.add_middleware(
    AuthAuditMiddleware,
    settings=settings.rate_limit,
    backend=limit_backend,
)

# Usage analytics (M6): one best-effort event per matched /v1 operation.
app.add_middleware(UsageTrackingMiddleware)
# The body cap must wrap analytics/auth/routing, including when LangGraph merges
# this FastAPI middleware around its built-in routes.
app.add_middleware(RequestBodyLimitMiddleware, settings=settings.request_body)
# Security and CORS wrap direct 413/429 responses as well as routed responses.
app.add_middleware(SecurityHeadersMiddleware, settings=settings.security_headers)
if settings.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=[
            "authorization",
            "content-type",
            "idempotency-key",
            "last-event-id",
            "x-api-key",
            "x-request-id",
        ],
    )

app.include_router(system_router, prefix="/v1")
app.include_router(auth_router, prefix="/v1")
app.include_router(pipelines_router, prefix="/v1")
app.include_router(prompts_router, prefix="/v1")
app.include_router(catalog_router, prefix="/v1")
app.include_router(documents_router, prefix="/v1")
app.include_router(artifacts_router, prefix="/v1")
app.include_router(drafts_router, prefix="/v1")
app.include_router(engines_router, prefix="/v1")
app.include_router(work_tracking_router, prefix="/v1")
app.include_router(logs_router, prefix="/v1")
app.include_router(inventory_router, prefix="/v1")
app.include_router(context_router, prefix="/v1")
app.include_router(consumers_router, prefix="/v1")
app.include_router(connections_router, prefix="/v1")
app.include_router(compliance_router, prefix="/v1")
app.include_router(analytics_router, prefix="/v1")
