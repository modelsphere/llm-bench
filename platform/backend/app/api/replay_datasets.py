"""Admin API for the rolling replay-dataset feed.

Profiles are collection policy (which production traffic, how much, how often);
builds are history. The *current* dataset for a profile is deliberately read
from the pointer file on the datasets volume rather than from the build table —
that pointer is what a benchmark will actually replay, so a disagreement between
it and the history is a real signal worth surfacing, not something to paper over.

Admins manage every profile. A service account (another platform, such as LLM
AutoTune) reads them all, creates its own and manages only those, like the
benchmarks it creates. Triggering a build is proxied to the collector service;
the backend never runs a collection itself (a multi-minute streaming download
has no business inside a request handler or the benchmark worker pool).
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import (
    ensure_can_manage,
    require_admin,
    require_service_or_admin,
    resolve_user_from_token,
)
from app.db.models import (
    Benchmark,
    BenchmarkModule,
    DatasetBuildStatus,
    ReplayDatasetBuild,
    ReplayDatasetProfile,
    User,
    _async_session_factory,
    get_async_session,
    is_admin_or_above,
)
from app.schemas.replay_datasets import (
    BuildListResponse,
    BuildResponse,
    CurrentBuild,
    DraftProbeRequest,
    FeedHealthEntry,
    FeedHealthResponse,
    FreezeBuildRequest,
    FrozenDatasetResponse,
    FrozenListResponse,
    ProbeRequest,
    ProbeResponse,
    ProfileCreate,
    ProfileListResponse,
    ProfileResponse,
    ProfileSource,
    ProfileUpdate,
    source_errors,
    TriggerResponse,
    ValueCount,
    ValuesRequest,
    ValuesResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/replay-datasets", tags=["replay-datasets"])

_PROFILE_FIELDS = (
    "display_name", "description", "enabled", "source_type", "source_url", "models", "statuses",
    "forwarded_to", "uris", "exclude_truncated", "extra_logsql", "window_hours",
    "window_timezone", "subwindow_minutes", "sample_size", "oversample_factor",
    "max_carry_multiple", "max_bytes", "clean", "max_model_len", "keep_response_body",
    "compress", "header_denylist",
    "min_records", "min_buckets", "schedule_interval_hours", "schedule_anchor_hour",
    "max_age_hours", "keep_builds", "min_retain_hours",
)


def collector_base_url() -> str:
    return os.getenv("DATASET_BUILDER_URL", "").rstrip("/")


def _builder_headers() -> dict:
    token = os.getenv("DATASET_BUILDER_TOKEN", "")
    return {"X-Builder-Token": token} if token else {}


async def _collector_call(method: str, path: str, *, json: dict | None = None) -> httpx.Response:
    """One request to the collector, with the shared-secret header. Raises 503
    when no collector is configured and 502 when it is unreachable — the two
    states the caller must distinguish (nothing to talk to vs. it's down)."""
    base = collector_base_url()
    if not base:
        raise HTTPException(
            status_code=503,
            detail="No dataset collector is configured (DATASET_BUILDER_URL is unset).",
        )
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            return await client.request(method, f"{base}{path}", json=json, headers=_builder_headers())
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Collector unreachable: {exc}") from exc


def _collector_detail(resp: httpx.Response) -> str:
    try:
        detail = resp.json().get("detail")
    except (ValueError, AttributeError):
        detail = None
    return str(detail) if detail else resp.text[:300]


async def _frozen_usage(session: AsyncSession) -> dict[str, list[str]]:
    """Map each dataset_path a `fixed` benchmark points at → the benchmarks that
    point at it. Lets the frozen-datasets view warn (and delete refuse) before an
    admin removes a file a benchmark is still configured to replay — which would
    otherwise silently fall back to the worker's default dataset at run time."""
    result = await session.execute(
        select(Benchmark.name, BenchmarkModule.params_json)
        .join(BenchmarkModule, BenchmarkModule.benchmark_id == Benchmark.id)
    )
    usage: dict[str, list[str]] = {}
    for name, params in result.all():
        if not isinstance(params, dict):
            continue
        path = params.get("dataset_path")
        # Default source is 'fixed', so an absent key still counts.
        source = (params.get("dataset_source") or "fixed")
        if path and source == "fixed" and name not in usage.get(path, []):
            usage.setdefault(path, []).append(name)
    return usage


def _frozen_from_disk() -> list[FrozenDatasetResponse]:
    """Read the frozen datasets straight off the volume. The frozen file is the
    source of truth (there is no DB mirror), so this must survive a DB wipe —
    the same reason `_current_build` reads the pointer file, not a build row."""
    from bench.replay_test import dataset_feed

    return [
        FrozenDatasetResponse(
            name=d.name, path=d.path, bytes=d.bytes, records=d.records,
            sha256=d.sha256, frozen_at=d.frozen_at,
            source_profile=d.source_profile, source_build_id=d.source_build_id,
            window_start=d.window_start, window_end=d.window_end,
            frozen_by=d.frozen_by, origin=d.origin,
        )
        for d in dataset_feed.list_frozen(None)
    ]


def _resolve_frozen(name: str):
    from bench.replay_test import dataset_feed

    return dataset_feed.resolve_frozen(None, name)


def _build_response(
    build: ReplayDatasetBuild, *, downloadable: bool | None = None
) -> BuildResponse:
    return BuildResponse(
        id=build.id,
        downloadable=downloadable,
        build_id=build.build_id,
        status=build.status.value if hasattr(build.status, "value") else str(build.status),
        trigger=build.trigger,
        records=build.records,
        size_bytes=build.size_bytes,
        sha256=build.sha256,
        path=build.path,
        window_start=build.window_start,
        window_end=build.window_end,
        stats_json=build.stats_json,
        progress=build.progress,
        error=build.error,
        created_at=build.created_at,
        started_at=build.started_at,
        finished_at=build.finished_at,
    )


def _build_file(profile_name: str, build_id: str) -> Path | None:
    """The on-disk dataset file for a build, or None once retention pruned it.

    Shares the collector's locator, which matches a build id against the names
    already present in the profile's own `builds/` directory rather than
    building a path out of the id — so nothing here can be steered outside the
    feed root even if a build row carried a hostile id."""
    from app.datasets.service import find_build_file

    return find_build_file(profile_name, build_id)


def _existing_build_ids(profile_name: str) -> set[str]:
    """Build ids whose bytes are still on the volume — one directory listing.

    The build TABLE keeps a row forever; the FILE is pruned on the profile's
    retention policy. Without this the UI would offer a download link for every
    historical row and most of them would 404."""
    from bench.replay_test import dataset_feed
    from app.datasets.service import feed_root

    try:
        files = dataset_feed.list_build_files(feed_root(), profile_name)
    except ValueError:  # invalid profile slug — nothing can exist for it
        return set()
    return {dataset_feed.strip_dataset_suffix(p.name) for p in files}


async def _download_admin(
    request: Request, token: str | None, session: AsyncSession
) -> User:
    """Authorize a request the browser makes by plain navigation.

    A download is an `<a href>`, which cannot carry the Authorization header the
    rest of this router takes via `require_admin`; the alternative — pulling a
    multi-hundred-MB dataset into a JS Blob just to keep the header — is not a
    trade worth making. So the credential may arrive as `?token=`, exactly like
    the SSE log stream's, and is resolved through the same shared resolver (so a
    personal API key works here too, e.g. for `curl`).
    """
    user: User | None = None
    if token:
        user = await resolve_user_from_token(token, session)
    if user is None:
        header = request.headers.get("authorization") or ""
        if header.lower().startswith("bearer "):
            user = await resolve_user_from_token(header[7:].strip(), session)
    if user is None:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if not is_admin_or_above(user.role):
        raise HTTPException(status_code=403, detail="Admin required")
    return user


def _current_build(profile: ReplayDatasetProfile) -> CurrentBuild | None:
    """Read the profile's pointer file. Filesystem access from a request handler
    is a stat + a small JSON read on a mounted volume — cheap, and it is the only
    way to report what a benchmark would really replay right now."""
    from bench.replay_test import dataset_feed

    build = dataset_feed.resolve_latest(None, profile.name)
    if build is None:
        return None
    age = build.age_hours()
    stats = build.stats or {}
    return CurrentBuild(
        build_id=build.build_id,
        path=build.path,
        records=build.records,
        bytes=build.bytes,
        sha256=build.sha256,
        built_at=build.built_at,
        age_hours=round(age, 2),
        stale=bool(profile.max_age_hours and age > profile.max_age_hours),
        window_start=build.window_start,
        window_end=build.window_end,
        buckets=stats.get("buckets") if isinstance(stats.get("buckets"), dict) else None,
        summary=stats.get("summary") if isinstance(stats.get("summary"), dict) else None,
    )


async def _latest_build_row(
    session: AsyncSession, profile_id: int
) -> ReplayDatasetBuild | None:
    result = await session.execute(
        select(ReplayDatasetBuild)
        .where(ReplayDatasetBuild.profile_id == profile_id)
        .order_by(ReplayDatasetBuild.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _to_response(session: AsyncSession, profile: ReplayDatasetProfile) -> ProfileResponse:
    last = await _latest_build_row(session, profile.id)
    # The pointer read touches the filesystem; keep it off the event loop.
    current = await asyncio.to_thread(_current_build, profile)
    return ProfileResponse(
        id=profile.id,
        name=profile.name,
        created_by_user_id=profile.created_by_user_id,
        created_at=profile.created_at,
        updated_at=profile.updated_at,
        current=current,
        last_build=_build_response(last) if last else None,
        **{f: getattr(profile, f) for f in _PROFILE_FIELDS},
    )


async def _get_profile(session: AsyncSession, profile_id: int) -> ReplayDatasetProfile:
    profile = await session.get(ReplayDatasetProfile, profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="Profile not found")
    return profile


@router.get("/profiles", response_model=ProfileListResponse)
async def list_profiles(
    _admin: User = Depends(require_service_or_admin),
    session: AsyncSession = Depends(get_async_session),
):
    result = await session.execute(
        select(ReplayDatasetProfile).order_by(ReplayDatasetProfile.name)
    )
    profiles = list(result.scalars().all())
    return ProfileListResponse(
        profiles=[await _to_response(session, p) for p in profiles],
        collector_configured=bool(collector_base_url()),
    )


@router.get("/profiles/{profile_id}", response_model=ProfileResponse)
async def get_profile(
    profile_id: int,
    _admin: User = Depends(require_service_or_admin),
    session: AsyncSession = Depends(get_async_session),
):
    """One profile plus the build it currently resolves to.

    The single call an external system needs to answer "what dataset is this
    benchmark replaying right now?" — `current.sha256`, `current.build_id`,
    record count, traffic window and the collected-content summary all come
    back together, so a caller never has to scrape the list endpoint.
    """
    return await _to_response(session, await _get_profile(session, profile_id))


@router.get("/builds/{build_row_id}", response_model=BuildResponse)
async def get_build(
    build_row_id: int,
    _admin: User = Depends(require_service_or_admin),
    session: AsyncSession = Depends(get_async_session),
):
    """A single build by the id `POST .../build` returned.

    Completes the externally-driven loop: trigger, poll this until `status` is
    `ready` or `failed`, then read `sha256` / `records` / `path` straight off
    it. Without this a caller would have to page the profile's build list and
    match on id.
    """
    build = await session.get(ReplayDatasetBuild, build_row_id)
    if build is None:
        raise HTTPException(status_code=404, detail="Build not found")
    profile = await session.get(ReplayDatasetProfile, build.profile_id)
    downloadable = None
    if profile is not None and build.build_id:
        downloadable = await asyncio.to_thread(
            lambda: _build_file(profile.name, build.build_id) is not None
        )
    return _build_response(build, downloadable=downloadable)


@router.post("/profiles", response_model=ProfileResponse, status_code=status.HTTP_201_CREATED)
async def create_profile(
    body: ProfileCreate,
    user: User = Depends(require_service_or_admin),
    session: AsyncSession = Depends(get_async_session),
):
    existing = await session.execute(
        select(ReplayDatasetProfile).where(ReplayDatasetProfile.name == body.name)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail=f"Profile {body.name!r} already exists")
    profile = ReplayDatasetProfile(
        name=body.name, created_by_user_id=user.id, **body.model_dump(exclude={"name"})
    )
    session.add(profile)
    await session.commit()
    await session.refresh(profile)
    return await _to_response(session, profile)


@router.put("/profiles/{profile_id}", response_model=ProfileResponse)
async def update_profile(
    profile_id: int,
    body: ProfileUpdate,
    user: User = Depends(require_service_or_admin),
    session: AsyncSession = Depends(get_async_session),
):
    profile = await _get_profile(session, profile_id)
    ensure_can_manage(user, profile, "collection profiles")
    changes = body.model_dump(exclude_unset=True)
    errors = source_errors(
        changes.get("source_type", profile.source_type),
        changes.get("source_url", profile.source_url),
        changes.get("extra_logsql", profile.extra_logsql),
    )
    if errors:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="; ".join(errors))
    for key, value in changes.items():
        setattr(profile, key, value)
    await session.commit()
    await session.refresh(profile)
    return await _to_response(session, profile)


@router.delete("/profiles/{profile_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_profile(
    profile_id: int,
    user: User = Depends(require_service_or_admin),
    session: AsyncSession = Depends(get_async_session),
):
    """Delete a profile and its build history.

    The published dataset FILES are left on disk on purpose: a benchmark may
    still name this profile, and a submission that already resolved a build is
    holding a concrete path. Deleting the policy stops future collections; it
    does not yank a dataset out from under a running run. Clean the directory up
    by hand once nothing references it.
    """
    profile = await _get_profile(session, profile_id)
    ensure_can_manage(user, profile, "collection profiles")
    await session.delete(profile)
    await session.commit()


@router.get("/profiles/{profile_id}/builds", response_model=BuildListResponse)
async def list_builds(
    profile_id: int,
    limit: int = 30,
    _admin: User = Depends(require_service_or_admin),
    session: AsyncSession = Depends(get_async_session),
):
    profile = await _get_profile(session, profile_id)
    result = await session.execute(
        select(ReplayDatasetBuild)
        .where(ReplayDatasetBuild.profile_id == profile_id)
        .order_by(ReplayDatasetBuild.created_at.desc())
        .limit(max(1, min(limit, 200)))
    )
    # One directory listing for the whole page, not a stat per row.
    on_disk = await asyncio.to_thread(_existing_build_ids, profile.name)
    return BuildListResponse(builds=[
        _build_response(b, downloadable=bool(b.build_id and b.build_id in on_disk))
        for b in result.scalars().all()
    ])


@router.post("/profiles/{profile_id}/build", response_model=TriggerResponse)
async def trigger_build(
    profile_id: int,
    admin: User = Depends(require_service_or_admin),
    session: AsyncSession = Depends(get_async_session),
):
    """Ask the collector to build this profile now."""
    profile = await _get_profile(session, profile_id)
    base = collector_base_url()
    if not base:
        raise HTTPException(
            status_code=503,
            detail="No dataset collector is configured (DATASET_BUILDER_URL is unset).",
        )
    active = await session.execute(
        select(ReplayDatasetBuild.id).where(
            ReplayDatasetBuild.profile_id == profile.id,
            ReplayDatasetBuild.status.in_(
                [DatasetBuildStatus.PENDING, DatasetBuildStatus.RUNNING]
            ),
        ).limit(1)
    )
    if active.first():
        raise HTTPException(
            status_code=409,
            detail="A build for this profile is already queued or running.",
        )
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{base}/builds",
                json={"profile_id": profile.id, "user_id": admin.id},
                headers=_builder_headers(),
            )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Collector unreachable: {exc}") from exc
    if resp.status_code >= 400:
        raise HTTPException(
            status_code=502, detail=f"Collector refused the build: {resp.text[:300]}"
        )
    data = resp.json()
    return TriggerResponse(
        build_row_id=int(data.get("build_row_id", 0)),
        queue_depth=data.get("queue_depth"),
    )


@router.post("/profiles/{profile_id}/probe", response_model=ProbeResponse)
async def probe_profile(
    profile_id: int,
    body: ProbeRequest,
    user: User = Depends(require_service_or_admin),
    session: AsyncSession = Depends(get_async_session),
):
    """Count what this profile's filters match over the last few hours.

    The point is to catch a misspelled `forwarded_to` or an over-narrow filter
    in the editor — as `matched: 0` — instead of hours later as an empty build.
    """
    profile = await _get_profile(session, profile_id)
    ensure_can_manage(user, profile, "collection profiles")
    from app.datasets.service import profile_to_build_profile

    return await asyncio.to_thread(
        _probe, profile.source_type, profile.source_url,
        profile_to_build_profile(profile).filters, body.hours,
    )


@router.post("/probe", response_model=ProbeResponse)
async def probe_draft(
    body: DraftProbeRequest,
    _user: User = Depends(require_service_or_admin),
):
    """The same count for a profile that is not saved yet: an editor shows
    what its filters match before anything is created."""
    return await asyncio.to_thread(
        _probe, body.source_type, body.source_url, _filters_of(body), body.hours,
    )


@router.post("/values", response_model=ValuesResponse)
async def field_values(
    body: ValuesRequest,
    _user: User = Depends(require_service_or_admin),
):
    """The most common models or backends among the matching records, with
    counts — what an editor offers instead of asking for a name. A file source
    reads every line in the window, so it is held to the last 24 hours."""
    hours = body.hours if body.source_type == "victorialogs" else min(body.hours, 24.0)

    def _run() -> ValuesResponse:
        from app.datasets.errors import LogSourceError
        from app.datasets.sources import source_for

        filters = _filters_of(body)
        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=hours)
        try:
            source = source_for(body.source_type, body.source_url)
        except LogSourceError as exc:
            return ValuesResponse(error=str(exc)[:500])
        try:
            values = source.top_values(filters, start, end, body.field, body.limit)
            return ValuesResponse(
                values=[ValueCount(value=v, count=n) for v, n in values],
                query=source.describe(filters, start, end),
            )
        except LogSourceError as exc:
            return ValuesResponse(error=str(exc)[:500])
        except Exception as exc:  # noqa: BLE001 - surfaced to the editor, not raised
            logger.warning("[replay-datasets] values query failed", exc_info=True)
            return ValuesResponse(error=f"{type(exc).__name__}: {exc}"[:500])
        finally:
            source.close()

    return await asyncio.to_thread(_run)


def _filters_of(body: ProfileSource):
    from app.datasets.builder import profile_from_dict

    return profile_from_dict({**body.model_dump(), "name": "draft"}).filters


def _probe(source_type: str, source_url: str, filters, hours: float) -> ProbeResponse:
    """One probe of a source, any failure returned as `error` for the editor."""
    from app.datasets.errors import LogSourceError
    from app.datasets.sources import source_for

    try:
        source = source_for(source_type, source_url)
    except LogSourceError as exc:
        return ProbeResponse(matched=0, query="", error=str(exc)[:500])
    try:
        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=hours)
        return ProbeResponse(**source.probe(filters, start, end))
    except LogSourceError as exc:
        return ProbeResponse(matched=0, query="", error=str(exc)[:500])
    except Exception as exc:  # noqa: BLE001 - surfaced to the editor, not raised
        logger.warning("[replay-datasets] probe failed", exc_info=True)
        return ProbeResponse(matched=0, query="", error=f"{type(exc).__name__}: {exc}"[:500])
    finally:
        source.close()


@router.get("/health", response_model=FeedHealthResponse)
async def feed_health(
    _admin: User = Depends(require_service_or_admin),
    session: AsyncSession = Depends(get_async_session),
):
    """Per-profile freshness. This is the page that should catch a broken
    collector BEFORE a stale build quietly ends up in someone's benchmark run."""
    result = await session.execute(
        select(ReplayDatasetProfile).order_by(ReplayDatasetProfile.name)
    )
    entries: list[FeedHealthEntry] = []
    for profile in result.scalars().all():
        current = await asyncio.to_thread(_current_build, profile)
        if current is None:
            entries.append(FeedHealthEntry(
                name=profile.name, display_name=profile.display_name,
                enabled=profile.enabled, ok=False, max_age_hours=profile.max_age_hours,
                detail="no published build",
            ))
            continue
        entries.append(FeedHealthEntry(
            name=profile.name,
            display_name=profile.display_name,
            enabled=profile.enabled,
            ok=not current.stale,
            age_hours=current.age_hours,
            max_age_hours=profile.max_age_hours,
            records=current.records,
            build_id=current.build_id,
            detail=None if not current.stale else (
                f"newest build is {current.age_hours:.1f}h old"
            ),
        ))
    return FeedHealthResponse(profiles=entries)


@router.get("/names")
async def list_profile_names(
    _admin: User = Depends(require_service_or_admin),
    session: AsyncSession = Depends(get_async_session),
):
    """Enabled profile names, for the benchmark editor's dataset_profile field
    (so an admin picks from a list instead of retyping a slug)."""
    result = await session.execute(
        select(ReplayDatasetProfile.name, ReplayDatasetProfile.display_name)
        .where(ReplayDatasetProfile.enabled.is_(True))
        .order_by(ReplayDatasetProfile.name)
    )
    return {"profiles": [{"name": n, "display_name": d} for n, d in result.all()]}


@router.get("/profiles/{profile_id}/builds/{build_row_id}/download")
async def download_build(
    profile_id: int,
    build_row_id: int,
    request: Request,
    token: str | None = None,
):
    """Download one build's dataset file, exactly as it sits on the volume.

    Served straight off the datasets volume the backend already mounts (the same
    reason `/frozen` reads it directly): a download must keep working while the
    collector is down, and proxying hundreds of MB through the collector would
    buy nothing. The bytes are the published artifact untouched — same gzip, same
    sha256 as the build row reports — so a downloaded file can be diffed against
    a run's recorded provenance.

    Auth comes from `?token=` or the Authorization header; see `_download_admin`.

    Takes NO session dependency on purpose. A yield-dependency is torn down only
    after the response has finished sending, so `Depends(get_async_session)`
    would pin a pooled connection for the whole transfer — minutes, on a slow
    link, per concurrent download. Every DB read happens in a short-lived
    session that is closed before a single byte streams, the same discipline the
    SSE log stream follows.
    """
    async with _async_session_factory() as session:
        await _download_admin(request, token, session)
        profile = await _get_profile(session, profile_id)
        build = await session.get(ReplayDatasetBuild, build_row_id)
        if build is None or build.profile_id != profile.id:
            raise HTTPException(status_code=404, detail="Build not found for this profile")
        if build.status != DatasetBuildStatus.READY or not build.build_id:
            raise HTTPException(
                status_code=409, detail="Only a completed (ready) build can be downloaded."
            )
        # Read off the ORM objects before the session closes and they detach.
        profile_name, build_id = profile.name, build.build_id

    path = await asyncio.to_thread(_build_file, profile_name, build_id)
    if path is None:
        # The row outlives the file: retention prunes builds/, the history stays.
        raise HTTPException(
            status_code=404,
            detail=(
                f"Build {build_id} is no longer on disk — retention pruned it. "
                f"Freeze a build to keep it permanently."
            ),
        )
    # Keep the published extension (.jsonl or .jsonl.gz) — the project's own
    # stripper, not Path.suffixes, so a dot in a build id can't skew it.
    from bench.replay_test.jsonl_io import strip_dataset_suffix

    suffix = path.name[len(strip_dataset_suffix(path.name)):]
    return FileResponse(
        path,
        # The gzipped file is served as-is: never let a proxy or the framework
        # re-encode it, or the sha256 an admin verifies against won't match.
        media_type="application/gzip" if path.name.endswith(".gz") else "application/x-ndjson",
        filename=f"{profile_name}-{build_id}{suffix}",
        headers={
            # nginx buffers a proxied response to a temp file by default, which
            # for a multi-hundred-MB dataset means disk churn and a long silent
            # wait before the browser sees a byte. Same escape hatch the SSE
            # stream uses.
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-store",
        },
    )


# ---------------------------------------------------------------------------
# Frozen datasets — promote a rolling build to a permanent, static file
# ---------------------------------------------------------------------------


@router.post(
    "/profiles/{profile_id}/builds/{build_row_id}/freeze",
    response_model=FrozenDatasetResponse,
    status_code=status.HTTP_201_CREATED,
)
async def freeze_build(
    profile_id: int,
    build_row_id: int,
    body: FreezeBuildRequest,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_async_session),
):
    """Freeze one build into a permanent dataset the rolling retention can't touch.

    The build stays where it is; freezing hard-links its bytes into a stable
    `frozen/<name>.jsonl[.gz]` and returns the path an admin pastes into a
    `fixed`-source benchmark. The heavy lifting happens on the collector (the
    writer of the datasets volume); this only validates and proxies, exactly like
    'rebuild now'."""
    profile = await _get_profile(session, profile_id)
    build = await session.get(ReplayDatasetBuild, build_row_id)
    if build is None or build.profile_id != profile.id:
        raise HTTPException(status_code=404, detail="Build not found for this profile")
    if build.status != DatasetBuildStatus.READY or not build.build_id:
        raise HTTPException(
            status_code=409,
            detail="Only a completed (ready) build can be frozen.",
        )
    frozen_by = (
        getattr(admin, "username", None) or getattr(admin, "email", None) or str(admin.id)
    )
    resp = await _collector_call("POST", "/freeze", json={
        "name": body.name,
        "profile": profile.name,
        "build_id": build.build_id,
        "records": build.records or 0,
        "sha256": build.sha256 or "",
        "window_start": build.window_start.isoformat() if build.window_start else None,
        "window_end": build.window_end.isoformat() if build.window_end else None,
        "frozen_by": frozen_by,
    })
    if resp.status_code in (400, 404, 409):
        raise HTTPException(status_code=resp.status_code, detail=_collector_detail(resp))
    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Collector refused the freeze: {resp.text[:300]}")
    return FrozenDatasetResponse(**resp.json())


@router.get("/frozen", response_model=FrozenListResponse)
async def list_frozen_datasets(
    _admin: User = Depends(require_service_or_admin),
    session: AsyncSession = Depends(get_async_session),
):
    """Every frozen dataset, with the benchmarks (if any) that currently replay
    it. Reads the volume directly, so it works even when no collector is up."""
    frozen = await asyncio.to_thread(_frozen_from_disk)
    usage = await _frozen_usage(session)
    for dataset in frozen:
        dataset.used_by = usage.get(dataset.path, [])
    return FrozenListResponse(frozen=frozen, collector_configured=bool(collector_base_url()))


@router.delete("/frozen/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_frozen_dataset(
    name: str,
    force: bool = False,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_async_session),
):
    """Delete a frozen dataset. Refuses (409) when a `fixed` benchmark still
    points at it — deleting it would make that benchmark silently fall back to
    the worker's default dataset — unless `force=true`."""
    dataset = await asyncio.to_thread(_resolve_frozen, name)
    if dataset is None:
        raise HTTPException(status_code=404, detail=f"No frozen dataset named {name!r}")
    if not force:
        used = (await _frozen_usage(session)).get(dataset.path, [])
        if used:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Frozen dataset {name!r} is still referenced by: "
                    f"{', '.join(used)}. Repoint those benchmarks first, or "
                    f"pass force=true."
                ),
            )
    resp = await _collector_call("DELETE", f"/frozen/{name}")
    if resp.status_code in (400, 404):
        raise HTTPException(status_code=resp.status_code, detail=_collector_detail(resp))
    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Collector refused the delete: {resp.text[:300]}")


# ---------------------------------------------------------------------------
# Moving frozen datasets between installations: download one, import a file
# ---------------------------------------------------------------------------

# Imports bigger than this are refused while they stream in, before they fill
# the datasets volume. REPLAY_IMPORT_MAX_GB overrides.
_IMPORT_MAX_BYTES = int(float(os.getenv("REPLAY_IMPORT_MAX_GB", "20")) * (1 << 30))
_GZIP_MAGIC = b"\x1f\x8b"


@router.get("/frozen/{name}/download")
async def download_frozen(name: str, request: Request, token: str | None = None):
    """Download a frozen dataset file as it sits on the volume, to import it
    into another installation. Admin, via `?token=` or the Authorization
    header, like a build download; no session is held while it streams."""
    async with _async_session_factory() as session:
        await _download_admin(request, token, session)
    dataset = await asyncio.to_thread(_resolve_frozen, name)
    if dataset is None:
        raise HTTPException(status_code=404, detail=f"No frozen dataset named {name!r}")
    path = Path(dataset.path)
    return FileResponse(
        path,
        media_type="application/gzip" if path.name.endswith(".gz") else "application/x-ndjson",
        filename=path.name,
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-store"},
    )


@router.put(
    "/frozen/{name}", response_model=FrozenDatasetResponse,
    status_code=status.HTTP_201_CREATED,
)
async def import_frozen_dataset(
    name: str,
    request: Request,
    source_profile: str = "",
    source_build_id: str = "",
    token: str | None = None,
):
    """Import a dataset file as the frozen dataset `name`: the request body is
    the file, `.jsonl` or `.jsonl.gz` (told apart by its first bytes), such as
    a build or frozen dataset downloaded from another installation. Imports
    are always frozen: nothing collects into them.

    The body is streamed straight into the datasets volume's staging directory
    and published by rename, with no copy in between and no database session
    held while it arrives. Every record is checked to be one the replay runner
    can read before the dataset appears; `sha256` is of the uncompressed
    content, as a build's is, so an imported build keeps the hash it had where
    it was built. Admin only. Refuses a taken name (409), a file that is not a
    replay dataset (422), and one past REPLAY_IMPORT_MAX_GB (413)."""
    from bench.replay_test import dataset_feed

    async with _async_session_factory() as session:
        admin = await _download_admin(request, token, session)
        frozen_by = getattr(admin, "username", None) or str(admin.id)
    try:
        dataset_feed.validate_frozen_name(name)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    if await asyncio.to_thread(_resolve_frozen, name) is not None:
        raise HTTPException(status_code=409, detail=f"A frozen dataset named {name!r} exists")

    staging = dataset_feed.frozen_root(None) / dataset_feed.STAGING_DIRNAME
    await asyncio.to_thread(staging.mkdir, parents=True, exist_ok=True)
    staged = staging / f"import-{name}-{os.getpid()}-{id(request)}.part"
    received = 0
    head = b""
    try:
        with open(staged, "wb") as out:
            async for chunk in request.stream():
                if not chunk:
                    continue
                if len(head) < 2:
                    head += chunk[: 2 - len(head)]
                received += len(chunk)
                if received > _IMPORT_MAX_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f"larger than the {_IMPORT_MAX_BYTES >> 30} GB import limit "
                               "(REPLAY_IMPORT_MAX_GB)",
                    )
                await asyncio.to_thread(out.write, chunk)
        if not received:
            raise HTTPException(status_code=422, detail="the request body is empty")
        try:
            dataset = await asyncio.to_thread(
                dataset_feed.import_frozen, None, name, staged,
                compressed=head == _GZIP_MAGIC,
                source_profile=source_profile.strip()[:64],
                source_build_id=source_build_id.strip()[:64],
                frozen_by=frozen_by,
            )
        except FileExistsError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"not a replay dataset: {exc}") from None
    finally:
        staged.unlink(missing_ok=True)
    logger.info("[replay-datasets] %s imported frozen dataset %s (%d records)",
                frozen_by, name, dataset.records)
    return FrozenDatasetResponse(
        name=dataset.name, path=dataset.path, bytes=dataset.bytes, records=dataset.records,
        sha256=dataset.sha256, frozen_at=dataset.frozen_at,
        source_profile=dataset.source_profile, source_build_id=dataset.source_build_id,
        window_start=dataset.window_start, window_end=dataset.window_end,
        frozen_by=dataset.frozen_by, origin=dataset.origin,
    )
