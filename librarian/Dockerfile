# The librarian service: the agent behind a small HTTP API on :7680
# (orchestrator.py). Stateless; the LLM backend is set by LLM_*. Its settings,
# secrets included, are set on the container (librarian/.env.example): on the
# hosting platform, or with docker run --env-file librarian/.env. No image has
# them.
# Build from this folder:  docker build -t librarian .
FROM ghcr.io/astral-sh/uv:0.11.21 AS uv

# Build stage: resolve the locked dependencies into /opt/venv.
FROM python:3.12.14-slim-trixie AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

ENV USER=librarian-core
ENV HOME=/home/$USER    

RUN useradd -m -u 1000 $USER

WORKDIR $HOME/app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Runtime stage: the venv and the code the server needs, no uv.
FROM python:3.12.14-slim-trixie
ENV PATH="/opt/venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY --from=build /opt/venv /opt/venv

ENV USER=librarian-core
ENV HOME=/home/$USER  

RUN useradd -m -u 1000 $USER

WORKDIR $HOME/app
COPY . .
# Executable whatever its mode in the checkout.
COPY --chmod=0755 start-script.sh ./

EXPOSE 7680

USER $USER

HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:7680/health', timeout=2)"]
# Serve starts the container with the start-up script in WORKDIR.
CMD ["./start-script.sh"]
