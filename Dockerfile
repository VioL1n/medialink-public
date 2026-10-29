# syntax=docker/dockerfile:1
ARG NODE_IMAGE=node:24-bookworm-slim
ARG PYTHON_IMAGE=python:3.12-slim-bookworm
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.13
ARG OPENLIST_IMAGE=openlistteam/openlist:v4.2.6

FROM ${NODE_IMAGE} AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/index.html frontend/tsconfig.json frontend/vite.config.ts ./
COPY frontend/src ./src
RUN npm run build

FROM ${UV_IMAGE} AS uv
FROM ${PYTHON_IMAGE} AS dependencies
COPY --from=uv /uv /uvx /bin/
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# The upstream image's empty data directory is root-owned. Fix it in the image,
# so fresh named volumes are populated for its documented unprivileged UID.
FROM ${OPENLIST_IMAGE} AS openlist
USER root
RUN mkdir -p /opt/openlist/data /opt/openlist/strm \
    && chown 1001:1001 /opt/openlist/data /opt/openlist/strm \
    && chmod 750 /opt/openlist/data /opt/openlist/strm
USER 1001:1001

FROM ${PYTHON_IMAGE} AS medialink
RUN apt-get update \
    && apt-get install -y --no-install-recommends libstdc++6 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1001 medialink \
    && useradd --uid 1001 --gid 1001 --create-home medialink \
    && mkdir /data \
    && chown 1001:1001 /data \
    && chmod 700 /data
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app/backend \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MEDIALINK_DATA=/data \
    MEDIALINK_VAULT_BACKEND=file \
    MEDIALINK_VAULT_DIR=/data/vault \
    MEDIALINK_SETUP_MODE=cli \
    MEDIALINK_ENABLED_SEARCH_SOURCES=knaben,limetorrents,nyaa,telegram
WORKDIR /app
COPY --from=dependencies /app/.venv /app/.venv
COPY backend ./backend
COPY --from=frontend /build/dist ./frontend/dist
USER 1001:1001
EXPOSE 8766
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8766/api/v1/health', timeout=3).read()"
CMD ["python", "-m", "uvicorn", "medialink.app:create_app", "--factory", "--app-dir", "backend", "--host", "0.0.0.0", "--port", "8766", "--no-access-log", "--log-level", "warning"]
