"""Submissions router: POST submit, GET detail/list, GET logs (SSE)."""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import logging
import os
import time
from collections import OrderedDict
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.auth import (
    get_current_user,
    get_session_or_service_user,
    require_admin,
)
from app.core.config import clamp_concurrency_override, settings
from app.core.security import encrypt_api_key
from app.db.models import (
    Benchmark,
    BenchmarkStatus,
    ModuleRunStatus,
    Submission,
    SubmissionRun,
    SubmissionStatus,
    User,
    _async_session_factory,
    get_async_session,
    is_admin_or_above,
    is_service_or_above,
)
from app.schemas.benchmarks import (
    JudgePreflightRequest,
    PaginatedSubmissions,
    PreflightRequest,
    PreflightResult,
    QuotaStatus,
    SubmissionCreate,
    SubmissionDetailResponse,
    SubmissionExtraParams,
    SubmissionResponse,
    SubmissionRunResponse,
)
from app.core.metric_configs import merge_display_defaults

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/submissions", tags=["submissions"])


async def _reject_if_cordoned() -> None:
    """Raise 503 if an operator has cordoned the platform for a disruptive deploy.

    The flag is a Redis key checked off the event loop (sync redis client),
    mirroring how enqueue_submission is dispatched. Cordon fails open, so a Redis
    blip never blocks submissions. See app.core.cordon.
    """
    from app.core.cordon import cordon_reason
    reason = await asyncio.get_running_loop().run_in_executor(None, cordon_reason)
    if reason:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=reason)


async def _active_submission_ids(user: User, session: AsyncSession) -> list[int]:
    """IDs of the caller's QUEUED/RUNNING submissions, oldest first."""
    result = await session.execute(
        select(Submission.id)
        .where(
            Submission.user_id == user.id,
            Submission.status.in_((SubmissionStatus.QUEUED, SubmissionStatus.RUNNING)),
        )
        .order_by(Submission.id)
    )
    return [row[0] for row in result.all()]


async def _enforce_submission_quota(user: User, session: AsyncSession) -> None:
    """Reject with 429 when the caller is at MAX_ACTIVE_SUBMISSIONS_PER_USER
    active (queued or running) submissions. Admins are exempt; <= 0 disables.

    Service accounts are exempt too: the platform behind one (LLM AutoTune)
    already bounds its own concurrency — one benchmark per GPU slot it holds —
    and a 429 there does not spread load, it just stalls a tuning window.

    The count and the insert share one transaction, but two simultaneous
    submits can still both pass the check — this is a soft fairness cap, not
    an invariant, so no locking on purpose."""
    limit = settings.MAX_ACTIVE_SUBMISSIONS_PER_USER
    if limit <= 0 or is_service_or_above(user.role):
        return
    active_ids = await _active_submission_ids(user, session)
    if len(active_ids) >= limit:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"Active submission limit reached ({len(active_ids)}/{limit}). "
                "Wait for a run to finish or cancel one before submitting again. "
                f"Active submission IDs: {', '.join(map(str, active_ids))}"
            ),
        )


def _normalize_extra_params(extra: SubmissionExtraParams | None) -> dict | None:
    """Turn the request's extra_params into the JSON bag stored on the submission,
    clamping each value as needed. Returns None when nothing is set so the column
    stays NULL. Add future extra_params keys here as they are introduced."""
    if extra is None:
        return None
    bag: dict = {}
    concurrency = clamp_concurrency_override(extra.concurrency_override)
    if concurrency is not None:
        bag["concurrency_override"] = concurrency
    per_module = {
        name: clamp_concurrency_override(value)
        for name, value in (extra.module_concurrency_overrides or {}).items()
    }
    per_module = {name: v for name, v in per_module.items() if v is not None}
    if per_module:
        bag["module_concurrency_overrides"] = per_module
    return bag or None


def _reject_unknown_override_modules(
    extra: SubmissionExtraParams | None, valid_names: set[str]
) -> None:
    """400 when the per-module concurrency map names a module that isn't part of
    this submission — a silent no-op override would be worse than an error."""
    unknown = sorted(set((extra.module_concurrency_overrides or {}) if extra else ()) - valid_names)
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"module_concurrency_overrides names unknown modules: {', '.join(unknown)}. "
                f"Valid module names for this submission: {', '.join(sorted(valid_names))}."
            ),
        )


def _build_run_response(
    run: SubmissionRun,
    *,
    redact_secrets: bool,
    submission_extra_params: dict | None = None,
) -> SubmissionRunResponse:
    """Serialize one module run. ``redact_secrets`` masks credential values in
    params_json (snapshotted from the benchmark's module config at submit time,
    so for benchmark submissions it can contain the ADMIN's judge api key —
    which the submission owner and other signed-in viewers must not see).

    ``submission_extra_params`` is the submission's extra_params bag; when a
    concurrency override (global or per-module) applied to this module, the
    response carries a ``concurrency_override`` record describing the value the
    run actually used."""
    from app.core.module_caps import concurrency_override_report, resolve_concurrency_override
    from app.core.param_secrets import redact_params

    return SubmissionRunResponse(
        id=run.id,
        module_name=run.module_name,
        params_json=redact_params(run.params_json) if redact_secrets else run.params_json,
        status=run.status.value,
        score=run.score,
        passed=run.passed,
        metrics_json=run.metrics_json,
        metric_configs_json=merge_display_defaults(run.module_name, run.metric_configs_json),
        error=run.error,
        started_at=run.started_at,
        finished_at=run.finished_at,
        artifact_path=run.artifact_path,
        benchmark_module_id=run.benchmark_module_id,
        # Prefer the concurrency the worker RECORDED for this run
        # (run.applied_concurrency) — it survives later cap/limit changes that
        # would otherwise make a reconstructed value wrong. `original` is read
        # from the raw (pre-redaction) params — concurrency is never a credential.
        concurrency_override=concurrency_override_report(
            run.module_name, run.params_json,
            resolve_concurrency_override(submission_extra_params, run.module_name),
            run.applied_concurrency,
        ),
    )


@router.post("/preflight", response_model=PreflightResult)
async def preflight_endpoint(
    body: PreflightRequest,
    user: User = Depends(get_session_or_service_user),
):
    """Quick, low-cost probe of an endpoint before submitting an expensive run.

    Fires one tiny chat/completions call (plus a best-effort /models lookup and a
    streaming probe) and reports connectivity, auth, model availability, and
    response validity. Mirrors the worker's real request path. The api_key is
    used only for this live check and is never persisted.

    A browser session, or the API key of a service account or admin — not a
    person's CI key, which has no use for a pre-test. Another platform (LLM
    AutoTune) calls it before committing a submission. Either way it can't be
    used as an open SSRF probe: it adds no reach beyond what submitting a run
    already grants. The blocking requests calls run in a thread so the event
    loop is never tied up.
    """
    from app.core.preflight import run_preflight

    result = await asyncio.get_running_loop().run_in_executor(
        None, run_preflight, body.endpoint_url, body.model, body.api_key,
    )
    logger.info(
        "Preflight by user %d — endpoint=%s model=%s ok=%s",
        user.id, body.endpoint_url, body.model, result.get("ok"),
    )
    return result


@router.post("/judge-preflight", response_model=PreflightResult)
async def judge_preflight_endpoint(
    body: JudgePreflightRequest,
    user: User = Depends(get_session_or_service_user),
):
    """Live probe of an LLM-JUDGE endpoint (module judge_* params) with a real
    grading-shaped call at the configured judge_max_tokens.

    Judge failures at run time degrade to silent NOT_ATTEMPTED grades rather
    than errors, so this is the only cheap moment to catch a bad key / wrong
    model / too-small token budget — the benchmark editor exposes it as a
    "Test judge endpoint" button. Same gating rationale as /preflight above:
    session-only (no programmatic reach), the api_key is used for the live
    check and never persisted.
    """
    from app.core.preflight import run_judge_preflight

    result = await asyncio.get_running_loop().run_in_executor(
        None, run_judge_preflight, body.endpoint_url, body.model, body.api_key,
        body.max_tokens,
    )
    logger.info(
        "Judge preflight by user %d — endpoint=%s model=%s ok=%s",
        user.id, body.endpoint_url, body.model, result.get("ok"),
    )
    return result


@router.post("/benchmarks/{slug}/submit", response_model=SubmissionResponse, status_code=status.HTTP_202_ACCEPTED)
async def submit_benchmark(
    slug: str,
    body: SubmissionCreate,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    """Submit a full benchmark for evaluation. Enqueues a Dramatiq job."""
    await _reject_if_cordoned()
    result = await session.execute(
        select(Benchmark)
        .options(selectinload(Benchmark.modules))
        .where(Benchmark.slug == slug, Benchmark.status == BenchmarkStatus.ACTIVE)
    )
    benchmark = result.scalar_one_or_none()
    if benchmark is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Benchmark not found")

    _reject_unknown_override_modules(
        body.extra_params, {m.module_name for m in benchmark.modules}
    )
    await _enforce_submission_quota(user, session)

    user_id = user.id
    submission = Submission(
        user_id=user_id,
        benchmark_id=benchmark.id,
        module_name=None,
        endpoint_url=body.endpoint_url,
        endpoint_model=body.model,
        endpoint_api_key_enc=encrypt_api_key(body.api_key),
        contributor=body.contributor or None,
        extra_params=_normalize_extra_params(body.extra_params),
        cards_per_machine=body.cards_per_machine,
        machine_count=body.machine_count,
        card_type=body.card_type or None,
        description_summary=body.description_summary,
        description_detail=body.description_detail,
        source_url=body.source_url,
        status=SubmissionStatus.QUEUED,
        benchmark_config_hash=benchmark.config_hash,
    )
    session.add(submission)
    await session.flush()

    # Pre-create all module runs so the frontend knows the total count upfront
    for mod in benchmark.modules:
        run = SubmissionRun(
            submission_id=submission.id,
            benchmark_module_id=mod.id,
            module_name=mod.module_name,
            params_json=mod.params_json,
            status=ModuleRunStatus.PENDING,
        )
        session.add(run)

    await session.commit()
    await session.refresh(submission)

    # Enqueue the Dramatiq job
    from app.queue.jobs import enqueue_submission
    # Dramatiq's broker send + Redis marker set are synchronous; run them off the
    # event loop so a slow Redis round-trip can't stall other requests on this
    # single-worker process. The enqueued message is identical to a direct call.
    await asyncio.get_running_loop().run_in_executor(None, enqueue_submission, submission.id)
    logger.info(
        "Submission %d queued — benchmark=%s model=%s endpoint=%s user=%d",
        submission.id, slug, body.model, body.endpoint_url, user_id,
    )

    return SubmissionResponse(
        id=submission.id,
        user_id=submission.user_id,
        benchmark_id=submission.benchmark_id,
        benchmark_slug=slug,
        module_name=submission.module_name,
        endpoint_url=submission.endpoint_url,
        endpoint_model=submission.endpoint_model,
        status=submission.status.value,
        score_total=submission.score_total,
        passed=submission.passed,
        error=submission.error,
        contributor=submission.contributor,
        extra_params=submission.extra_params,
        cards_per_machine=submission.cards_per_machine,
        machine_count=submission.machine_count,
        card_type=submission.card_type,
        description_summary=submission.description_summary,
        description_detail=submission.description_detail,
        source_url=submission.source_url,
        created_at=submission.created_at,
        started_at=submission.started_at,
        finished_at=submission.finished_at,
    )


@router.post("/modules/{name}/submit", response_model=SubmissionResponse, status_code=status.HTTP_202_ACCEPTED)
async def submit_module(
    name: str,
    body: SubmissionCreate,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    """Submit a single module for ad-hoc evaluation."""
    await _reject_if_cordoned()
    from app.db.models import TestModule
    result = await session.execute(select(TestModule).where(TestModule.name == name))
    if result.scalar_one_or_none() is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Module {name!r} not found")

    _reject_unknown_override_modules(body.extra_params, {name})
    await _enforce_submission_quota(user, session)

    user_id = user.id
    submission = Submission(
        user_id=user_id,
        benchmark_id=None,
        module_name=name,
        endpoint_url=body.endpoint_url,
        endpoint_model=body.model,
        endpoint_api_key_enc=encrypt_api_key(body.api_key),
        contributor=body.contributor or None,
        extra_params=_normalize_extra_params(body.extra_params),
        cards_per_machine=body.cards_per_machine,
        machine_count=body.machine_count,
        card_type=body.card_type or None,
        description_summary=body.description_summary,
        description_detail=body.description_detail,
        source_url=body.source_url,
        status=SubmissionStatus.QUEUED,
    )
    session.add(submission)
    await session.flush()

    # Create a single pending run using the module's default params
    result = await session.execute(select(TestModule).where(TestModule.name == name))
    module_desc = result.scalar_one()
    run = SubmissionRun(
        submission_id=submission.id,
        module_name=name,
        params_json=module_desc.default_params_json,
        status=ModuleRunStatus.PENDING,
    )
    session.add(run)
    await session.commit()
    await session.refresh(submission)

    from app.queue.jobs import enqueue_submission
    # Dramatiq's broker send + Redis marker set are synchronous; run them off the
    # event loop so a slow Redis round-trip can't stall other requests on this
    # single-worker process. The enqueued message is identical to a direct call.
    await asyncio.get_running_loop().run_in_executor(None, enqueue_submission, submission.id)
    logger.info(
        "Submission %d queued — module=%s model=%s endpoint=%s user=%d",
        submission.id, name, body.model, body.endpoint_url, user_id,
    )

    return SubmissionResponse(
        id=submission.id,
        user_id=submission.user_id,
        benchmark_id=None,
        benchmark_slug=None,
        module_name=submission.module_name,
        endpoint_url=submission.endpoint_url,
        endpoint_model=submission.endpoint_model,
        status=submission.status.value,
        score_total=None,
        passed=None,
        error=None,
        contributor=submission.contributor,
        extra_params=submission.extra_params,
        cards_per_machine=submission.cards_per_machine,
        machine_count=submission.machine_count,
        card_type=submission.card_type,
        description_summary=submission.description_summary,
        description_detail=submission.description_detail,
        source_url=submission.source_url,
        created_at=submission.created_at,
        started_at=None,
        finished_at=None,
    )


@router.post("/{submission_id}/cancel", response_model=SubmissionResponse)
async def cancel_submission(
    submission_id: int,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    """Cancel a queued or running submission. Only the owner or an admin can cancel."""
    result = await session.execute(
        select(Submission).where(Submission.id == submission_id)
    )
    submission = result.scalar_one_or_none()
    if submission is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Submission not found")

    user_id = user.id
    if submission.user_id != user_id and not is_admin_or_above(user.role):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

    if submission.status not in (SubmissionStatus.QUEUED, SubmissionStatus.RUNNING):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot cancel submission in {submission.status.value} state",
        )

    logger.info(
        "Submission %d cancel requested by user %d (was %s)",
        submission_id, user_id, submission.status.value,
    )
    submission.status = SubmissionStatus.CANCELED
    submission.finished_at = datetime.utcnow()
    await session.commit()
    await session.refresh(submission)
    logger.info("Submission %d marked CANCELED", submission_id)

    # Look up benchmark slug for the response
    benchmark_slug = None
    if submission.benchmark_id:
        bm_result = await session.execute(
            select(Benchmark.slug).where(Benchmark.id == submission.benchmark_id)
        )
        benchmark_slug = bm_result.scalar_one_or_none()

    return SubmissionResponse(
        id=submission.id,
        user_id=submission.user_id,
        benchmark_id=submission.benchmark_id,
        benchmark_slug=benchmark_slug,
        module_name=submission.module_name,
        endpoint_url=submission.endpoint_url,
        endpoint_model=submission.endpoint_model,
        status=submission.status.value,
        score_total=submission.score_total,
        passed=submission.passed,
        error=submission.error,
        contributor=submission.contributor,
        extra_params=submission.extra_params,
        cards_per_machine=submission.cards_per_machine,
        machine_count=submission.machine_count,
        card_type=submission.card_type,
        description_summary=submission.description_summary,
        description_detail=submission.description_detail,
        source_url=submission.source_url,
        created_at=submission.created_at,
        started_at=submission.started_at,
        finished_at=submission.finished_at,
    )


@router.get("/quota", response_model=QuotaStatus)
async def my_quota(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    """Caller's active-submission quota. The submit pages read this to show the
    current usage and to warn + disable the submit button BEFORE a 429 would
    happen. Registered before GET /{submission_id} so 'quota' is never parsed
    as a submission id."""
    limit = settings.MAX_ACTIVE_SUBMISSIONS_PER_USER
    exempt = limit <= 0 or is_service_or_above(user.role)
    active_ids = await _active_submission_ids(user, session)
    return QuotaStatus(
        limit=limit,
        active=len(active_ids),
        exempt=exempt,
        active_ids=active_ids,
    )


@router.get("/me", response_model=list[SubmissionResponse])
async def my_submissions(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    result = await session.execute(
        select(Submission)
        .where(Submission.user_id == user.id)
        .order_by(Submission.created_at.desc())
    )
    submissions = result.scalars().all()

    # Bulk-load benchmark slugs so we can show them in the list
    bm_ids = {s.benchmark_id for s in submissions if s.benchmark_id}
    slug_map: dict[int, str] = {}
    if bm_ids:
        bm_result = await session.execute(
            select(Benchmark.id, Benchmark.slug).where(Benchmark.id.in_(bm_ids))
        )
        slug_map = {row[0]: row[1] for row in bm_result}

    return [
        SubmissionResponse(
            id=s.id, user_id=s.user_id, benchmark_id=s.benchmark_id,
            benchmark_slug=slug_map.get(s.benchmark_id) if s.benchmark_id else None,
            module_name=s.module_name, endpoint_url=s.endpoint_url,
            endpoint_model=s.endpoint_model, status=s.status.value,
            score_total=s.score_total, passed=s.passed, error=s.error,
            benchmark_config_hash=s.benchmark_config_hash,
            contributor=s.contributor,
            cards_per_machine=s.cards_per_machine, machine_count=s.machine_count,
            card_type=s.card_type,
            description_summary=s.description_summary,
            description_detail=s.description_detail,
            source_url=s.source_url,
            created_at=s.created_at, started_at=s.started_at, finished_at=s.finished_at,
        )
        for s in submissions
    ]


@router.get("/admin/all", response_model=PaginatedSubmissions)
async def all_submissions(
    limit: int = 50,
    offset: int = 0,
    _: User = Depends(require_admin),
    session: AsyncSession = Depends(get_async_session),
):
    """Admin: list ALL submissions (any user / any benchmark), newest first.

    Paginated and intentionally lightweight — returns only summary rows (status,
    score, owner). No per-submission detail or live streaming here; the admin
    clicks through to /submissions/{id} for that. Declared before the
    ``/{submission_id}`` route so the literal ``admin`` path isn't captured.
    """
    limit = max(1, min(limit, 200))
    offset = max(0, offset)

    total = (
        await session.execute(select(func.count()).select_from(Submission))
    ).scalar_one()

    result = await session.execute(
        select(Submission)
        .order_by(Submission.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    submissions = result.scalars().all()

    # Bulk-load benchmark slugs and owner usernames for display (2 queries, not N).
    bm_ids = {s.benchmark_id for s in submissions if s.benchmark_id}
    slug_map: dict[int, str] = {}
    if bm_ids:
        bm_result = await session.execute(
            select(Benchmark.id, Benchmark.slug).where(Benchmark.id.in_(bm_ids))
        )
        slug_map = {row[0]: row[1] for row in bm_result}

    user_ids = {s.user_id for s in submissions}
    name_map: dict[int, str] = {}
    if user_ids:
        u_result = await session.execute(
            select(User.id, User.username).where(User.id.in_(user_ids))
        )
        name_map = {row[0]: row[1] for row in u_result}

    items = [
        SubmissionResponse(
            id=s.id, user_id=s.user_id, username=name_map.get(s.user_id),
            benchmark_id=s.benchmark_id,
            benchmark_slug=slug_map.get(s.benchmark_id) if s.benchmark_id else None,
            module_name=s.module_name, endpoint_url=s.endpoint_url,
            endpoint_model=s.endpoint_model, status=s.status.value,
            score_total=s.score_total, passed=s.passed, error=s.error,
            benchmark_config_hash=s.benchmark_config_hash,
            contributor=s.contributor,
            cards_per_machine=s.cards_per_machine, machine_count=s.machine_count,
            card_type=s.card_type,
            description_summary=s.description_summary,
            description_detail=s.description_detail,
            source_url=s.source_url,
            created_at=s.created_at, started_at=s.started_at, finished_at=s.finished_at,
        )
        for s in submissions
    ]
    return PaginatedSubmissions(items=items, total=total, limit=limit, offset=offset)


@router.get("/{submission_id}", response_model=SubmissionDetailResponse)
async def get_submission(
    submission_id: int,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    result = await session.execute(
        select(Submission)
        .options(selectinload(Submission.runs))
        .where(Submission.id == submission_id)
    )
    s = result.scalar_one_or_none()
    if s is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Submission not found")

    # Owner and admin always allowed. Benchmark submissions are readable by
    # EVERY signed-in user (platform policy: leaderboard entries and their
    # detail pages are public, read-only — cancel and worker logs stay
    # owner/admin-only). Ad-hoc module submissions never appear on a
    # leaderboard and stay private to their owner.
    is_owner = s.user_id == user.id
    is_admin = is_admin_or_above(user.role)

    if not is_owner and not is_admin and s.benchmark_id is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

    # Enrich with benchmark metadata when applicable
    benchmark_slug = None
    benchmark_name = None
    if s.benchmark_id is not None:
        from app.db.models import Benchmark
        bench_result = await session.execute(
            select(Benchmark).where(Benchmark.id == s.benchmark_id)
        )
        bench = bench_result.scalar_one_or_none()
        if bench is not None:
            benchmark_slug = bench.slug
            benchmark_name = bench.name

    return SubmissionDetailResponse(
        id=s.id, user_id=s.user_id, benchmark_id=s.benchmark_id,
        module_name=s.module_name, endpoint_url=s.endpoint_url,
        endpoint_model=s.endpoint_model, status=s.status.value,
        score_total=s.score_total, passed=s.passed, error=s.error,
        benchmark_config_hash=s.benchmark_config_hash,
        contributor=s.contributor,
        cards_per_machine=s.cards_per_machine, machine_count=s.machine_count,
        card_type=s.card_type,
        description_summary=s.description_summary,
        description_detail=s.description_detail,
        source_url=s.source_url,
        created_at=s.created_at, started_at=s.started_at, finished_at=s.finished_at,
        # Admins see run params verbatim. The owner does too for AD-HOC module
        # submissions (they authored those params, credentials included) — but
        # for benchmark submissions params were snapshotted from the benchmark
        # config and may carry the admin's credentials, so everyone below admin
        # gets them redacted.
        runs=[
            _build_run_response(
                r,
                redact_secrets=not (is_admin or (is_owner and s.module_name is not None)),
                submission_extra_params=s.extra_params,
            )
            for r in s.runs
        ],
        benchmark_slug=benchmark_slug,
        benchmark_name=benchmark_name,
    )


# --- Worker-log download: poll-safe caching layer ------------------------------
#
# The log can be downloaded WHILE a submission is still running (the worker
# flushes worker_log_gz to the DB incrementally — see queue/log_capture.py), so
# a live-log UI may poll this endpoint frequently. To keep that from turning into
# a stream of multi-MB BLOB reads + gzip.decompress on the request path, we put a
# short in-process TTL cache in front of the decompressed body:
#
#   * A poll within the TTL is served from memory — DB blob reads are bounded to
#     ≤1 per submission per TTL per backend process, no matter the poll rate.
#   * ETag + If-None-Match lets an unchanged poll return 304 with no body and no
#     decompression.
#   * A per-submission fetch lock makes a cache miss single-flight, so a burst of
#     concurrent pollers triggers one DB read, not a thundering herd.
#
# Staleness is bounded by the TTL (a few seconds) — acceptable for a log view.
# Nothing here touches the worker / write path, so submission execution is
# unaffected. A "no log yet → 409" result is cached too, but with a much shorter
# TTL, so (a) a freshly-flushed log still becomes downloadable within ~1s, and
# (b) the fetch lock stays tied to a cache entry and is reclaimed by LRU eviction
# (an uncached negative would leak a per-submission lock for every run polled
# during its pre-flush window).
_LOG_DL_CACHE_TTL = float(os.getenv("WORKER_LOG_DOWNLOAD_CACHE_TTL", "3"))
_LOG_DL_NEG_TTL = min(_LOG_DL_CACHE_TTL, float(os.getenv("WORKER_LOG_DOWNLOAD_NEG_TTL", "1")))
_LOG_DL_CACHE_MAX = int(os.getenv("WORKER_LOG_DOWNLOAD_CACHE_MAX", "32"))
# submission_id -> (monotonic_fetched_at, etag, body); body is None for a cached
# 409 (log not available yet).
_log_dl_cache: "OrderedDict[int, tuple[float, str | None, str | None]]" = OrderedDict()
_log_dl_cache_guard = asyncio.Lock()  # guards the OrderedDict + fetch-lock dict
_log_dl_fetch_locks: dict[int, asyncio.Lock] = {}

_LOG_NOT_READY = HTTPException(
    status_code=status.HTTP_409_CONFLICT,
    detail="Logs are not available until the run starts producing output",
)


def _log_dl_fresh(entry: tuple[float, str | None, str | None], now: float) -> bool:
    fetched, _etag, body = entry
    ttl = _LOG_DL_NEG_TTL if body is None else _LOG_DL_CACHE_TTL
    return now - fetched < ttl


async def _get_worker_log_body(
    session: AsyncSession, submission_id: int, status_value: str
) -> tuple[str, str]:
    """Return (etag, body) for a submission's worker log, via the TTL cache.

    Raises 409 if no log has been flushed yet and the run is still in flight.
    """
    async with _log_dl_cache_guard:
        hit = _log_dl_cache.get(submission_id)
        if hit and _log_dl_fresh(hit, time.monotonic()):
            _log_dl_cache.move_to_end(submission_id)
            if hit[2] is None:
                raise _LOG_NOT_READY
            return hit[1], hit[2]
        fetch_lock = _log_dl_fetch_locks.setdefault(submission_id, asyncio.Lock())

    async with fetch_lock:
        # Re-check: another coroutine may have refreshed while we waited.
        async with _log_dl_cache_guard:
            hit = _log_dl_cache.get(submission_id)
            if hit and _log_dl_fresh(hit, time.monotonic()):
                _log_dl_cache.move_to_end(submission_id)
                if hit[2] is None:
                    raise _LOG_NOT_READY
                return hit[1], hit[2]

        # Cache miss — pull just the two log columns (blob included this time).
        result = await session.execute(
            select(Submission.worker_log_gz, Submission.worker_log_truncated).where(
                Submission.id == submission_id
            )
        )
        row = result.one_or_none()
        gz = row[0] if row is not None else None
        truncated = bool(row[1]) if row is not None else False

        etag: str | None = None
        if gz is None and status_value in ("queued", "running"):
            # No log flushed yet and still in flight — cache a short negative.
            body: str | None = None
        elif gz is None:
            # Terminal but no log: worker likely crashed before any flush.
            body = (
                "No worker logs were captured for this submission. "
                "The worker may have crashed before any logs were flushed.\n"
            )
        else:
            body = gzip.decompress(gz).decode("utf-8", errors="replace")
            if truncated:
                body = (
                    "[log truncated — oldest lines were dropped to fit the size cap; "
                    "most-recent output is preserved]\n\n" + body
                )
        if body is not None:
            etag = 'W/"' + hashlib.sha1(body.encode("utf-8", "replace")).hexdigest() + '"'

        async with _log_dl_cache_guard:
            _log_dl_cache[submission_id] = (time.monotonic(), etag, body)
            _log_dl_cache.move_to_end(submission_id)
            while len(_log_dl_cache) > _LOG_DL_CACHE_MAX:
                evicted, _ = _log_dl_cache.popitem(last=False)
                _log_dl_fetch_locks.pop(evicted, None)

        if body is None:
            raise _LOG_NOT_READY
        return etag, body  # type: ignore[return-value]


@router.get("/{submission_id}/logs/download")
async def download_submission_logs(
    submission_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    """Download the worker log for a submission as a text file.

    Works mid-run: the worker flushes the log incrementally, so this returns the
    log-so-far while the submission is still running (poll-friendly — see the
    caching layer above). Returns 409 only before the first flush.

    Access: owner or admin only. Submission detail pages are public to every
    signed-in user, but worker logs are NOT — they can contain replayed
    request content and the endpoint URL.

    Note: logs from guidellm-forked child processes are not captured.
    """
    # Cheap PK lookup for authz + status — the deferred blob is NOT loaded here.
    result = await session.execute(
        select(Submission.user_id, Submission.status).where(Submission.id == submission_id)
    )
    row = result.one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Submission not found")
    if row[0] != user.id and not is_admin_or_above(user.role):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

    etag, body = await _get_worker_log_body(session, submission_id, row[1].value)

    inm = request.headers.get("if-none-match")
    if inm and etag in {t.strip() for t in inm.split(",")}:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers={"ETag": etag})

    return Response(
        content=body,
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="submission-{submission_id}.log"',
            "ETag": etag,
            "Cache-Control": "no-cache",
        },
    )


@router.get("/{submission_id}/logs")
async def submission_logs(
    submission_id: int,
    token: str | None = None,
):
    """
    SSE stream of run completion events for a submission.
    Workers publish to Redis after each module run; this endpoint subscribes and streams.

    Auth: pass token as query param (?token=...) for EventSource compatibility.

    Real-time log watching is a SECONDARY feature, so it must never destabilise
    the rest of the API: it holds NO pooled DB connection across the (possibly
    long-lived) stream. Auth + the access check run in a short-lived session that
    is released before streaming starts; the in-stream terminal-state check opens
    its own short-lived session and is best-effort.
    """
    import json
    import redis.asyncio as aioredis
    from fastapi.security import OAuth2PasswordBearer
    from app.api.auth import resolve_user_from_token

    oauth2_scheme_local = OAuth2PasswordBearer(tokenUrl="/auth/login", auto_error=False)

    # Resolve via the shared token->user resolver so this endpoint accepts BOTH
    # JWTs and personal API keys, exactly like every get_current_user route.
    # Order: query token first (EventSource can't set headers), then header
    # (programmatic clients, incl. API keys via Authorization: Bearer). Done in a
    # short-lived session so no connection is pinned for the stream's lifetime.
    async with _async_session_factory() as session:
        effective_user: User | None = None

        if token:
            effective_user = await resolve_user_from_token(token, session)

        if effective_user is None:
            header_token = await oauth2_scheme_local(None)
            if header_token:
                effective_user = await resolve_user_from_token(header_token, session)

        if effective_user is None:
            raise HTTPException(status_code=401, detail="Unauthorized")

        # Verify access
        result = await session.execute(
            select(Submission).where(Submission.id == submission_id)
        )
        sub = result.scalar_one_or_none()
        if sub is None:
            raise HTTPException(status_code=404, detail="Submission not found")
        if sub.user_id != effective_user.id and not is_admin_or_above(effective_user.role):
            raise HTTPException(status_code=403, detail="Access denied")
        # Capture as a plain string before the session closes (the object detaches).
        initial_status = sub.status.value

    channel = f"submission:{submission_id}:logs"
    _TERMINAL = ("done", "failed", "canceled")

    async def event_generator():
        # Async Redis pubsub: each viewer's stream awaits messages without
        # blocking the event loop. The prior sync client's blocking
        # get_message(timeout=1.0) tied up the loop ~1s/tick per open stream,
        # which degraded the whole API once several users watched runs at once.
        r = aioredis.from_url(settings.REDIS_URL, decode_responses=False)
        pubsub = r.pubsub()
        await pubsub.subscribe(channel)

        try:
            yield f"data: {json.dumps({'event': 'connected', 'submission_id': submission_id})}\n\n"

            # The run may already be terminal when the stream opens.
            if initial_status in _TERMINAL:
                yield f"data: {json.dumps({'event': 'done', 'status': initial_status})}\n\n"
                return

            loop = asyncio.get_running_loop()
            last_db_check = loop.time()
            while True:
                msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if msg and msg["type"] == "message":
                    yield f"data: {msg['data'].decode()}\n\n"

                # Fallback terminal-state check (the worker also publishes a 'done'
                # event; this also ends a stream opened after the run finished).
                # Throttled to ~5s and run in its OWN short-lived session so the
                # stream never holds a pooled connection; best-effort so a DB blip
                # degrades only log-watching, never the rest of the API.
                if loop.time() - last_db_check >= 5.0:
                    last_db_check = loop.time()
                    try:
                        async with _async_session_factory() as s:
                            res = await s.execute(
                                select(Submission.status).where(Submission.id == submission_id)
                            )
                            status_val = res.scalar_one_or_none()
                        if status_val is not None and status_val.value in _TERMINAL:
                            yield f"data: {json.dumps({'event': 'done', 'status': status_val.value})}\n\n"
                            break
                    except Exception:
                        logger.warning(
                            "SSE terminal-state check failed for submission %s (stream continues)",
                            submission_id, exc_info=True,
                        )

                await asyncio.sleep(0.5)
        finally:
            try:
                await pubsub.unsubscribe(channel)
                await pubsub.aclose()
                await r.aclose()
            except Exception:
                pass

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
