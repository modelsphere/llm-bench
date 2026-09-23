"""Admin router: deployment/version diagnostics.

Returns the backend image's baked-in build info plus the DB's current alembic
revision, so admins can verify that the running k8s pods match the code they
just built. Frontend fetches /build-info.json directly from nginx; this
endpoint covers the backend half.

Build info comes from /app/build_info.json (written at docker build time).
When running locally (uvicorn against a checkout), that file doesn't exist
— we fall back to `git rev-parse HEAD` so the version page is still useful
in dev.
"""
from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import require_admin
from app.db.models import get_async_session

log = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])

_BUILD_INFO_PATH = Path("/app/build_info.json")


class BuildInfo(BaseModel):
    component: str = "backend"
    git_sha: str = "unknown"
    git_ref: str = "unknown"
    build_time: str = "unknown"
    image_tag: str = "unknown"


class VersionResponse(BaseModel):
    backend: BuildInfo
    alembic_revision: str | None


def _git(*args: str) -> str:
    """Run a git command from the repo root. Returns 'unknown' on any failure."""
    try:
        out = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        return out.stdout.strip() or "unknown"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return "unknown"


def _read_build_info() -> BuildInfo:
    # Prefer the baked-in JSON (real deployments)
    if _BUILD_INFO_PATH.exists():
        try:
            data = json.loads(_BUILD_INFO_PATH.read_text())
            info = BuildInfo(**data)
            if info.git_sha != "unknown":
                return info
        except (json.JSONDecodeError, ValueError, OSError) as exc:
            log.warning("Could not parse %s: %s", _BUILD_INFO_PATH, exc)

    # Fallback: query git directly (works in local uvicorn dev)
    return BuildInfo(
        component="backend",
        git_sha=_git("rev-parse", "HEAD"),
        git_ref=_git("rev-parse", "--abbrev-ref", "HEAD"),
        build_time="local-dev",
        image_tag="local-dev",
    )


@router.get("/version", response_model=VersionResponse)
async def get_version(
    _: object = Depends(require_admin),
    session: AsyncSession = Depends(get_async_session),
) -> VersionResponse:
    revision: str | None = None
    try:
        result = await session.execute(text("SELECT version_num FROM alembic_version LIMIT 1"))
        revision = result.scalar_one_or_none()
    except Exception as exc:  # pragma: no cover — non-fatal
        log.warning("Could not read alembic_version: %s", exc)

    return VersionResponse(backend=_read_build_info(), alembic_revision=revision)
