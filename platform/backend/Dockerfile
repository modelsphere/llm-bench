# NOTE: intentionally NO `# syntax=docker/dockerfile:1` directive. That pins the
# external BuildKit frontend image (docker/dockerfile:1 on Docker Hub) as a
# MUTABLE tag, so BuildKit periodically re-HEADs registry-1.docker.io to check it
# for updates — which hard-fails the whole build on a dev box that can't reach
# Docker Hub (DeadlineExceeded / i/o timeout), even when every base image is
# already cached locally. The built-in frontend (Docker/BuildKit >= 23.0) already
# supports the only extended feature we use — `RUN --mount=type=cache` below — so
# the external frontend buys us nothing. Do NOT re-add the syntax line.
# Build stage: install dependencies
FROM python:3.12-slim AS builder

WORKDIR /app

# Install uv for fast package installs
COPY --from=ghcr.io/astral-sh/uv:0.9.22 /uv /usr/local/bin/uv

# Install from the lockfile, so the image carries exactly what CI tested.
# Behind a package mirror, pass it in rather than editing this file:
#   docker build --build-arg UV_INDEX_URL=https://mirror.example/simple ...
# (torch comes from the PyTorch CPU index, see [tool.uv.index] in pyproject.toml;
# override that one with UV_EXTRA_INDEX_URL if it is also mirrored.)
ARG UV_INDEX_URL
ARG UV_EXTRA_INDEX_URL
COPY platform/backend/pyproject.toml platform/backend/uv.lock /app/
# Persist uv's download cache across image builds via a BuildKit cache mount,
# so a busted layer re-resolves without re-downloading torch and friends.
# UV_LINK_MODE=copy silences hardlink warnings when materialising the venv from
# the cache mount (a separate filesystem).
ENV UV_LINK_MODE=copy
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# ---------------------------------------------------------------------------
# Production stage
# ---------------------------------------------------------------------------
FROM python:3.12-slim

WORKDIR /app

# Install uv
COPY --from=ghcr.io/astral-sh/uv:0.9.22 /uv /usr/local/bin/uv

# Copy installed packages from builder
COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"

# Copy source — repo root is the build context
COPY bench/                          /app/bench/
COPY utils/                          /app/utils/
COPY benchmarks/                     /app/benchmarks/
# The fake model the quickstart and the module tests benchmark against.
COPY mock_server.py                   /app/mock_server.py
COPY platform/backend/app/           /app/app/
COPY platform/backend/alembic.ini    /app/alembic.ini
COPY platform/backend/alembic/        /app/alembic/
COPY platform/backend/seed.py        /app/seed.py
# Runs before `alembic upgrade head` in the migrate Job — refuses to start when a
# long-lived transaction would make the DDL block the whole table. MUST be copied
# explicitly: this stage cherry-picks files, so a new top-level script that isn't
# listed here is simply absent at runtime and the Job dies with "can't open file".
COPY platform/backend/preflight_migration.py /app/preflight_migration.py

ENV PYTHONPATH="/app"
ENV BENCH_MODULES_PATH="/app/bench"
# Writable cache locations. In k8s the pod runs as a pinned non-root UID
# (podSecurityContext runAsUser: 1000) with no /etc/passwd entry, so HOME
# resolves to / and HuggingFace/transformers tries to use /.cache — unwritable,
# which crashes guidellm's synthetic-data deserializer when it loads the target
# tokenizer to size prompts (PermissionError: [Errno 13] '/.cache'). Point HOME
# and the HF/XDG caches at /tmp (mode 1777, writable by any UID).
ENV HOME="/tmp"
ENV HF_HOME="/tmp/.cache/huggingface"
ENV XDG_CACHE_HOME="/tmp/.cache"

# Build info — passed via --build-arg so the running image can self-identify
# (the admin /admin/version endpoint reads this file). Defaults to "unknown"
# so plain `docker build` without build-args still produces a valid image.
ARG GIT_SHA=unknown
ARG GIT_REF=unknown
ARG BUILD_TIME=unknown
ARG IMAGE_TAG=unknown
RUN printf '{"component":"backend","git_sha":"%s","git_ref":"%s","build_time":"%s","image_tag":"%s"}\n' \
    "$GIT_SHA" "$GIT_REF" "$BUILD_TIME" "$IMAGE_TAG" > /app/build_info.json

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
