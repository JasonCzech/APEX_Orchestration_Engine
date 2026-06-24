from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from apex.app.logging import configure_logging
from apex.services.connections import get_connection_resolver
from apex.settings import get_settings

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    settings = get_settings()
    app.state.settings = settings
    logger.info(
        "apex.startup",
        environment=settings.environment,
        version=settings.version,
    )
    yield
    await get_connection_resolver().close()
    logger.info("apex.shutdown")
