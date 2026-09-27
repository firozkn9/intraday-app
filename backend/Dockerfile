# Backend image, used by both the api and engine containers (different commands).
# Build context: repository root (needs config/defaults.yaml).
#   docker build -f backend/Dockerfile -t intraday-backend .

FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY backend/src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim AS runtime
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin app
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    INTRADAY_DEFAULTS_PATH=/app/config/defaults.yaml \
    INTRADAY_WATCHLIST_SEED_PATH=/app/config/watchlist.seed.yaml
WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY backend/alembic.ini ./alembic.ini
COPY backend/migrations ./migrations
COPY config ./config
USER app
CMD ["python", "-m", "intraday.engine.main"]
