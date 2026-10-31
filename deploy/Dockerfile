# syntax=docker/dockerfile:1
# Registry digests verified 2026-09-15. Rebuilds must deliberately update pins.
ARG PYTHON_IMAGE=python:3.12.14-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254
ARG NODE_IMAGE=node:22.23.2-bookworm-slim@sha256:83f487e0a63425e5b4d146fb5e5be574bcbe1b7b843d3ebafdd95eaf7767a7e5
ARG CADDY_IMAGE=caddy:2.11.4-alpine@sha256:5f5c8640aae01df9654968d946d8f1a56c497f1dd5c5cda4cf95ab7c14d58648

FROM ${PYTHON_IMAGE} AS python-build
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
ARG ENABLE_EMBEDDING=false
WORKDIR /app
RUN pip install --no-cache-dir uv==0.12.9
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    if [ "$ENABLE_EMBEDDING" = true ]; then uv sync --frozen --no-dev --no-editable --extra embedding; \
    elif [ "$ENABLE_EMBEDDING" = false ]; then uv sync --frozen --no-dev --no-editable; \
    else echo 'ENABLE_EMBEDDING must be true or false' >&2; exit 2; fi

FROM python-build AS ingestion-build
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-editable --extra ingestion

FROM ${PYTHON_IMAGE} AS runtime
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    MATERIALS_ROOT=/data/materials CONTEXT_ROOT=/data/context SKILLS_ROOT=/app/skills
WORKDIR /app
RUN groupadd --gid 10001 kefu && useradd --uid 10001 --gid 10001 --no-create-home kefu \
    && mkdir -p /data/materials /data/context /data/privacy /models /backups \
    && chown -R kefu:kefu /data /models /backups
COPY --from=python-build --chown=kefu:kefu /app/.venv /app/.venv
COPY --chown=kefu:kefu skills /app/skills
USER 10001:10001
EXPOSE 8000
CMD ["python", "-m", "kefu.api"]

# Database clients and migration files only exist in the offline maintenance image.
FROM runtime AS maintenance
USER root
ARG PG_CLIENT_VERSION=17.11-1.pgdg12+2
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && mkdir -p /etc/apt/keyrings \
    && curl --fail --silent --show-error https://www.postgresql.org/media/keys/ACCC4CF8.asc -o /etc/apt/keyrings/postgresql.asc \
    && echo 'deb [signed-by=/etc/apt/keyrings/postgresql.asc] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main' > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update && apt-get install -y --no-install-recommends "postgresql-client-17=${PG_CLIENT_VERSION}" \
    && apt-get purge -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*
COPY --chown=kefu:kefu alembic.ini /app/alembic.ini
COPY --chown=kefu:kefu migrations /app/migrations
COPY --chown=kefu:kefu data/sources.jsonl data/demo.jsonl data/README.md /app/data/
USER 10001:10001
CMD ["python", "-m", "kefu.cli", "--help"]

FROM maintenance AS importer
COPY --from=ingestion-build --chown=kefu:kefu /app/.venv /app/.venv

FROM ${NODE_IMAGE} AS web-build
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web ./
RUN npm run build

FROM ${CADDY_IMAGE} AS web-server
RUN addgroup -g 10001 kefu && adduser -D -u 10001 -G kefu kefu \
    && mkdir -p /srv /data /config && chown -R kefu:kefu /srv /data /config
COPY --from=web-build --chown=kefu:kefu /web/dist /srv
COPY deploy/Caddyfile /etc/caddy/Caddyfile
USER 10001:10001
EXPOSE 8080 8443
