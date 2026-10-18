# syntax=docker/dockerfile:1
# Decision Ledger container for Cloud Run (DL-006).
# Stage 1 resolves dependencies with uv from the lockfile; stage 2 runs as a non-root user.

FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.24 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --all-extras --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --all-extras --no-editable

FROM python:3.12-slim AS runtime
RUN useradd --create-home --uid 10001 app
WORKDIR /app
COPY --from=build --chown=app:app /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    DL_HOST=0.0.0.0 \
    DL_PORT=8080 \
    DL_DB_PATH=/tmp/decision-ledger.db
USER app
EXPOSE 8080
# Cloud Run sets $PORT; fall back to DL_PORT locally.
CMD ["sh", "-c", "exec decision-ledger serve --host 0.0.0.0 --port ${PORT:-8080}"]
