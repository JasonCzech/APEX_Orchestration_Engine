"""FastAPI composition root for the APEX domain API (/v1).

Mounted into the LangGraph server via `langgraph.json` -> http.app. The LangGraph
built-in surface (/assistants, /threads, /runs) coexists with these routes; docs and
the OpenAPI document live under /v1 to avoid shadowing built-in endpoints.
"""

from fastapi import FastAPI

from apex.app.errors import register_exception_handlers
from apex.app.lifespan import lifespan
from apex.routers.artifacts import router as artifacts_router
from apex.routers.catalog import router as catalog_router
from apex.routers.connections import router as connections_router
from apex.routers.consumers import router as consumers_router
from apex.routers.context import router as context_router
from apex.routers.documents import router as documents_router
from apex.routers.drafts import router as drafts_router
from apex.routers.pipelines import router as pipelines_router
from apex.routers.prompts import router as prompts_router
from apex.routers.system import router as system_router
from apex.settings import get_settings

settings = get_settings()

app = FastAPI(
    title="APEX Orchestration Engine — Domain API",
    version=settings.version,
    lifespan=lifespan,
    docs_url="/v1/docs",
    redoc_url=None,
    openapi_url="/v1/openapi.json",
)

register_exception_handlers(app)

app.include_router(system_router, prefix="/v1")
app.include_router(pipelines_router, prefix="/v1")
app.include_router(prompts_router, prefix="/v1")
app.include_router(catalog_router, prefix="/v1")
app.include_router(documents_router, prefix="/v1")
app.include_router(artifacts_router, prefix="/v1")
app.include_router(drafts_router, prefix="/v1")
app.include_router(context_router, prefix="/v1")
app.include_router(consumers_router, prefix="/v1")
app.include_router(connections_router, prefix="/v1")
