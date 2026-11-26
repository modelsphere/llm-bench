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

# Install from the lockfile, so the image carries exactly what CI tested. The
# lock pins each wheel's URL (files.pythonhosted.org, download.pytorch.org),
# so an index mirror does not apply here; on a slow link, raise the
# per-request timeout instead of retrying a failed build:
#   docker build --build-arg UV_HTTP_TIMEOUT=600 ...
ARG UV_HTTP_TIMEOUT=120
ARG UV_HTTP_RETRIES=3
ENV UV_HTTP_TIMEOUT=${UV_HTTP_TIMEOUT} UV_HTTP_RETRIES=${UV_HTTP_RETRIES}
COPY platform/backend/pyproject.toml platform/backend/uv.lock /app/
# Persist uv's download cache across image builds via a BuildKit cache mount,
# so a busted layer re-resolves without re-downloading torch and friends.
# UV_LINK_MODE=copy silences hardlink warnings when materialising the venv from
# the cache mount (a separate filesystem).
ENV UV_LINK_MODE=copy
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# BFCL owns a separate environment because its NumPy/filelock pins differ.
# Downloads and build-tool bootstrap may use the network. Installation into the
# final BFCL environment, source-package builds, and CLI checks run offline.
FROM python:3.12-slim AS bfcl-builder
ARG TARGETARCH
# Install git/ca-certificates before COPY and BFCL_PIP_INDEX_URL so mirror or
# lock changes reuse the apt layer.
RUN mkdir -p /opt/bfcl \
    && if [ "$TARGETARCH" = amd64 ]; then \
      apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
      && rm -rf /var/lib/apt/lists/*; \
    fi
ARG BFCL_PIP_INDEX_URL=https://pypi.org/simple
ARG BFCL_PIP_TIMEOUT=120
ENV PIP_INDEX_URL=${BFCL_PIP_INDEX_URL} PIP_DEFAULT_TIMEOUT=${BFCL_PIP_TIMEOUT}
COPY bench/bfcl/requirements.lock /tmp/bfcl-requirements.lock
# The BFCL lock covers the x86_64 CPU workers. Keep the existing arm64
# backend release usable for all other modules.
RUN --mount=type=cache,target=/root/.cache/pip \
    if [ "$TARGETARCH" = amd64 ]; then \
      grep -E '^(setuptools|wheel|packaging)==' /tmp/bfcl-requirements.lock \
           > /tmp/bfcl-bootstrap.lock \
      && python -m pip install --require-hashes --no-deps -r /tmp/bfcl-bootstrap.lock \
      && python -m pip download --require-hashes --only-binary=:all: \
        --no-binary=google-search-results --no-build-isolation \
        -r /tmp/bfcl-requirements.lock -d /wheels \
      && git init /bfcl-source \
      && git -C /bfcl-source fetch --depth=1 https://github.com/ShishirPatil/gorilla.git \
           6ea57973c7a6097fd7c5915698c54c17c5b1b6c8 \
      && git -C /bfcl-source checkout --detach FETCH_HEAD; \
    fi
RUN --network=none if [ "$TARGETARCH" = amd64 ]; then \
      sed -E 's|^torch @ [^ ]+ |torch==2.14.0+cpu |' \
        /tmp/bfcl-requirements.lock > /tmp/bfcl-offline.lock \
      && python -m venv /opt/bfcl \
      && /opt/bfcl/bin/pip install --no-index --find-links=/wheels --no-deps \
        --require-hashes -r /tmp/bfcl-bootstrap.lock \
      && /opt/bfcl/bin/pip install --no-index --find-links=/wheels --no-build-isolation \
        --require-hashes -r /tmp/bfcl-offline.lock \
      && git -C /bfcl-source rev-parse HEAD > /opt/bfcl/llmbench-bfcl-revision \
      && /opt/bfcl/bin/pip install --no-index --no-deps --no-build-isolation \
        /bfcl-source/berkeley-function-call-leaderboard \
      && /opt/bfcl/bin/pip check \
      && /opt/bfcl/bin/python -m bfcl_eval generate --help \
      && /opt/bfcl/bin/python -m bfcl_eval evaluate --help; \
    fi

# ---------------------------------------------------------------------------
# The tokenizer the throughput modules size their prompts with
# ---------------------------------------------------------------------------
# Qwen3-0.6B's (Apache-2.0), at a pinned revision, so a benchmark never needs
# huggingface.co at run time: many clusters cannot reach it, and a first-use
# download from the hub has been seen to hang with no timeout. Behind a mirror:
#   docker build --build-arg HF_ENDPOINT=https://hf-mirror.com ...
FROM python:3.12-slim AS tokenizer
ARG HF_ENDPOINT=https://huggingface.co
COPY scripts/fetch_tokenizer.py /fetch_tokenizer.py
RUN HF_ENDPOINT="$HF_ENDPOINT" python /fetch_tokenizer.py --repo Qwen/Qwen3-0.6B \
      --revision c1899de289a04d12100db370d81485cdf75e47ca --out /tokenizer

# ---------------------------------------------------------------------------
# Production stage
# ---------------------------------------------------------------------------
FROM python:3.12-slim

WORKDIR /app

# Install uv
COPY --from=ghcr.io/astral-sh/uv:0.9.22 /uv /usr/local/bin/uv

# Copy installed packages from builder
COPY --from=builder /app/.venv /app/.venv
COPY --from=bfcl-builder /opt/bfcl /opt/bfcl
ENV BFCL_PYTHON="/opt/bfcl/bin/python"
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
# The default tokenizer (bench/tests/base.py); PROCESSOR_PATH and a module's
# processor_path still override it.
COPY --from=tokenizer /tokenizer /app/tokenizers/qwen3-0.6b
ENV PROCESSOR_DEFAULT="/app/tokenizers/qwen3-0.6b"
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
