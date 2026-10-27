# syntax=docker/dockerfile:1
FROM node:22-bookworm-slim AS frontend
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim-bookworm AS dependencies
WORKDIR /app
ENV UV_PROJECT_ENVIRONMENT=/opt/venv UV_LINK_MODE=copy
RUN pip install --no-cache-dir uv==0.10.12
COPY pyproject.toml uv.lock ./
COPY src/ ./src/
RUN uv sync --frozen --no-dev --no-editable --extra music --extra production

FROM python:3.12-slim-bookworm AS runtime
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    DATABASE_PATH=/app/data/lifeplus.sqlite3 CHROMA_PATH=/app/data/chroma \
    APP_MODE=showcase LIFEPLUS_ROOT=/app ANONYMIZED_TELEMETRY=False HOME=/app/data
WORKDIR /app
RUN groupadd --gid 10001 lifeplus && useradd --uid 10001 --gid lifeplus --no-create-home lifeplus \
    && mkdir -p /app/data && chown lifeplus:lifeplus /app/data
COPY --from=dependencies /opt/venv /opt/venv
COPY src/ ./src/
COPY skills/ ./skills/
COPY knowledge/ ./knowledge/
COPY --from=frontend /build/frontend/dist ./frontend/dist
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).read()"]
# One process owns the in-process scheduler, locks and MCP connection.
CMD ["uvicorn", "car_agent.api.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers", "--no-access-log"]
