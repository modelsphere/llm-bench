"""Application settings loaded from environment variables."""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


def find_bench_dir(start: Path) -> Path:
    """The `bench/` package nearest above `start`: the first ancestor holding
    `bench/modules`. Falls back to `<ancestor>/bench` two levels up (the image's
    /app/bench) when nothing is found, never raising for a shallow path."""
    here = start.resolve()
    for parent in here.parents:
        if (parent / "bench" / "modules").is_dir():
            return parent / "bench"
    parents = here.parents
    return parents[min(2, len(parents) - 1)] / "bench"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Database
    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/llmbench"
    DATABASE_URL_SYNC: str = "postgresql://postgres:postgres@localhost:5432/llmbench"

    # JWT
    SECRET_KEY: str = "change-me-in-production"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 30  # 30 days
    # How long an approved password-reset token stays valid before the user must
    # request a fresh one. Short by design — the token grants a password change.
    RESET_TOKEN_TTL_MINUTES: int = 60

    # Fernet encryption (for stored API keys)
    PLATFORM_SECRET_KEY: str = "change-me-32-bytes-base64!"

    # Redis / Dramatiq
    REDIS_URL: str = "redis://localhost:6379/0"

    # Auth rate limiting (Redis-backed; fails open if Redis is down). Login counts
    # only FAILED attempts — per-IP (spray from one host) and per-account (brute
    # force / lockout) — so a legitimate user (who succeeds) never accrues toward
    # the cap even behind a shared NAT IP. Registration counts all attempts per-IP
    # (spam + enumeration probing).
    LOGIN_IP_MAX_FAILURES: int = 20
    LOGIN_IP_WINDOW_SECONDS: int = 600  # 10 minutes
    LOGIN_EMAIL_MAX_FAILURES: int = 5
    LOGIN_EMAIL_WINDOW_SECONDS: int = 900  # 15 minutes
    REGISTER_IP_MAX_ATTEMPTS: int = 10
    REGISTER_IP_WINDOW_SECONDS: int = 3600  # 1 hour

    # Hard ceiling for a submission's optional `concurrency_override`. The user
    # value is clamped to [1, MAX_ALLOWED_CONCURRENCY] before it is applied to any
    # module (and then further clamped to each module's own range). Settable via
    # the MAX_ALLOWED_CONCURRENCY env var.
    MAX_ALLOWED_CONCURRENCY: int = 128

    # Per-user cap on ACTIVE (queued or running) submissions — a fairness guard
    # so one user can't monopolize the shared worker pool (single modules run
    # for hours). Admins and super-admins are exempt; <= 0 disables the cap.
    # Settable via the MAX_ACTIVE_SUBMISSIONS_PER_USER env var (helm:
    # app.maxActiveSubmissionsPerUser).
    MAX_ACTIVE_SUBMISSIONS_PER_USER: int = 8

    # CORS
    ALLOWED_ORIGINS: str = "http://localhost:5173"

    # Path to the bench modules (for imports). The container image copies them to
    # /app/bench (and sets the env); a source checkout resolves ./bench relative
    # to the repo root. Found by walking up rather than by counting parents: the
    # image flattens the tree (/app/app/core/config.py), and a fixed depth there
    # is an IndexError before any setting is read.
    BENCH_MODULES_PATH: str = str(find_bench_dir(Path(__file__)))

    # Expose real exception messages in 500 responses. Defaults OFF so a public
    # deploy that sets nothing fails safe (generic "Internal error", and the
    # startup secret guard in main.py engages). Local dev sets DEBUG=true (see
    # dev.py) to get readable tracebacks.
    DEBUG: bool = False

    # ASGI root path. Empty for local dev (uvicorn serves /docs, /openapi.json
    # at root). In the cluster the frontend nginx exposes the backend under
    # /api/ and STRIPS that prefix, so set ROOT_PATH=/api there — FastAPI then
    # generates /api/openapi.json + /api/docs/oauth2-redirect so Swagger UI at
    # /api/docs can actually load the spec through the proxy.
    ROOT_PATH: str = ""


settings = Settings()


def clamp_concurrency_override(value: int | None) -> int | None:
    """Clamp a user-supplied concurrency override to integers in
    [1, MAX_ALLOWED_CONCURRENCY]. None passes through unchanged (no override).

    This is the single global guard on the user value; each module additionally
    clamps it to its own declared range when applying it."""
    if value is None:
        return None
    return max(1, min(int(value), settings.MAX_ALLOWED_CONCURRENCY))
