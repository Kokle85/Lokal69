# syntax=docker/dockerfile:1.7
# =============================================================================
# suv-deals runtime image: api, worker, scheduler, dispatcher and reconciler are the SAME image
# with different commands (compose.yaml / compose.production.yaml). Spec sections 4, 27, 29.
#
# Release step (docs/runbook.md "Release"): pin both base images by their verified digests, e.g.
#   --build-arg PYTHON_IMAGE=python:3.13-slim@sha256:<verified digest>
#   --build-arg UV_IMAGE=ghcr.io/astral-sh/uv:0.11.32@sha256:<verified digest>
# and record the resulting image digest in the release report. No digest is invented here.
#
# No secret is baked into any layer: settings come from the environment at run time
# (env_file / secrets on the host); .env files are excluded by .dockerignore.
# =============================================================================
ARG PYTHON_IMAGE=python:3.13-slim
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11.32

FROM ${UV_IMAGE} AS uv

# --- build: resolve exactly uv.lock into /app/.venv ---------------------------------------
FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project
COPY src ./src
# The project itself is installed in editable mode against /app/src so that the repository
# layout (config/, supabase/migrations/, scripts/) resolves exactly as in a checkout.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# --- runtime -------------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:${PATH}" \
    APP_ENV=production \
    CONFIG_DIR=/app/config \
    SNAPSHOT_LOCAL_DIR=/app/var/snapshots \
    SOURCE_NETWORK_ENABLED=false \
    ALLOW_EXTERNAL_NOTIFICATIONS=false
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --home-dir /app --no-create-home --shell /usr/sbin/nologin app
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY src ./src
COPY config ./config
COPY supabase/migrations ./supabase/migrations
COPY scripts/migrate.sh scripts/rollback.sh ./scripts/
# /app/var/snapshots is the worker's evidence volume mount point (the root file system is
# read-only in compose); created here so a new named volume inherits the app user's ownership.
RUN mkdir -p /app/var/snapshots && chown -R app:app /app/var
USER app:app
EXPOSE 8000
# Liveness of the api command only (GET /healthz never touches dependencies); worker-type
# services disable it in compose. Readiness (/readyz) is for the reverse proxy / operator.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"]
# Inside the container the API binds all interfaces; compose publishes it on 127.0.0.1 only and
# the host's reverse proxy terminates HTTPS (docs/runbook.md).
CMD ["suv-deals", "api", "serve", "--host", "0.0.0.0", "--allow-non-loopback", "--port", "8000", "--forwarded-allow-ips", "127.0.0.1"]
