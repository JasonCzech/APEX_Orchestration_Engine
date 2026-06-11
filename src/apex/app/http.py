"""FastAPI composition root for the APEX domain API (/v1).

Mounted into the LangGraph server via `langgraph.json` -> http.app. The LangGraph
built-in surface (/assistants, /threads, /runs) coexists with these routes; docs and
the OpenAPI document live under /v1 to avoid shadowing built-in endpoints.
"""

from fastapi import FastAPI

from apex.app.errors import register_exception_handlers
from apex.app.lifespan import lifespan
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
