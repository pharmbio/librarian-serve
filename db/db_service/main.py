"""The db service: owns all persistence and serves it as a REST API.

    GET  /health    liveness probe
    GET  /ready     the database answers
    /api/v1/...     users, sessions, queries and runs (api.py)
    GET  /docs      the OpenAPI docs

No other service touches the database. At startup the schema is migrated to
the latest version (../migrations).
"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from sqlalchemy import text

from db_service import api, config, errors
from db_service.database import engine
from db_service.errors import ApiError
from db_service.schemas import ErrorResponse

ROOT = Path(__file__).resolve().parent.parent
logger = logging.getLogger("uvicorn.error")


def migrate() -> None:
    """Bring the schema up to the latest migration."""
    alembic_config = Config(str(ROOT / "alembic.ini"))
    alembic_config.set_main_option("script_location", str(ROOT / "migrations"))
    command.upgrade(alembic_config, "head")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    migrate()
    logger.info("Database %s is at the latest schema", engine.url.render_as_string())
    if not config.API_KEY:
        logger.warning(
            "API_KEY is not set, so /api/v1 needs no key. Keep this port private."
        )
    yield


app = FastAPI(
    title="Librarian db service",
    version="1.0.0",
    description="Owns all persistence: users, sessions, queries and runs.",
    lifespan=lifespan,
)
errors.install(app)
app.include_router(api.router)


@app.get("/health", tags=["health"])
def health() -> dict[str, str]:
    """Liveness probe: the process is up."""
    return {"status": "ok"}


@app.get("/ready", tags=["health"], responses={503: {"model": ErrorResponse}})
def ready() -> dict[str, str]:
    """Readiness probe: the database answers a query."""
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:
        raise ApiError(503, "The database is not reachable.") from exc
    return {"status": "ready"}
