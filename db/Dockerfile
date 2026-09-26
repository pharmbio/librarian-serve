# The db service: owns all persistence and serves it as a REST API on :8000.
# The SQLite file lives on the volume mounted at /data; the schema is migrated
# at startup. Build from this folder:  docker build -t librarian-db .
FROM ghcr.io/astral-sh/uv:0.11.21 AS uv

# Build stage: resolve the locked dependencies into /opt/venv.
FROM python:3.12.14-slim-trixie AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /srv/db
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Runtime stage: the venv and the code, no uv.
FROM python:3.12.14-slim-trixie
ENV PATH="/opt/venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    DATABASE_URL=sqlite:////data/librarian.sqlite3
COPY --from=build /opt/venv /opt/venv
WORKDIR /srv/db
COPY alembic.ini ./
COPY migrations/ migrations/
COPY db_service/ db_service/

# Docker seeds an empty named volume from the image's /data, ownership
# included, so the unprivileged user can write to it.
RUN useradd --system --uid 10001 db && mkdir /data && chown db /data
USER db
VOLUME /data

EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=2)"]
CMD ["uvicorn", "db_service.main:app", "--host", "0.0.0.0", "--port", "8000"]
