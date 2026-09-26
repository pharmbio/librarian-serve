import os
import tempfile

import pytest

# Before db_service is imported: the engine is built from this at import time.
os.environ["DATABASE_URL"] = f"sqlite:///{tempfile.mkdtemp()}/test.sqlite3"
os.environ.pop("API_KEY", None)

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import delete  # noqa: E402

from db_service import config  # noqa: E402
from db_service.database import SessionLocal  # noqa: E402
from db_service.main import app  # noqa: E402
from db_service.models import LoginSession, Query, Run, User  # noqa: E402


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as client:  # runs the migrations
        yield client


@pytest.fixture(autouse=True)
def empty_database(client):
    yield
    with SessionLocal() as db:
        for model in (Run, Query, LoginSession, User):
            db.execute(delete(model))
        db.commit()


@pytest.fixture
def api_key(monkeypatch):
    monkeypatch.setattr(config, "API_KEY", "s3cret")
    return "s3cret"
