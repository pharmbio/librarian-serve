# The app service: the web UI and its API on :8080. It calls the librarian at
# LIBRARIAN_URL, keeps its data in Supabase's Postgres at DATABASE_URL, and
# starts whether or not they are up.
# Build from this folder:  docker build -t librarian-app .
FROM ghcr.io/astral-sh/uv:0.11.21 AS uv

# Build stage: resolve the locked dependencies into /opt/venv.
FROM python:3.12.14-slim-trixie AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

ENV USER=libraian-app
ENV HOME=/home/$USER

RUN useradd -m -u 1000 $USER

WORKDIR $HOME/app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Runtime stage: the venv, the code and the UI, no uv.
FROM python:3.12.14-slim-trixie
ENV PATH="/opt/venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY --from=build /opt/venv /opt/venv

ENV USER=libraian-app
ENV HOME=/home/$USER

RUN useradd -m -u 1000 $USER

WORKDIR $HOME/app

COPY . .


USER $USER

EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=2)"]
# Trust X-Forwarded-Proto from any proxy in front of the container, so the app
# knows when the browser is on HTTPS and marks the session cookie Secure.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080", "--forwarded-allow-ips", "*"]
