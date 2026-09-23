"""Leaderboard router: GET /leaderboard/{benchmark_slug}."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.auth import get_current_user
from app.db.models import (
    Benchmark,
    BenchmarkStatus,
    Submission,
    SubmissionStatus,
    User,
    get_async_session,
    is_admin_or_above,
)

router = APIRouter(prefix="/leaderboard", tags=["leaderboard"])


@router.get("/{benchmark_slug}")
async def get_leaderboard(
    benchmark_slug: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
) -> dict[str, Any]:
    """
    Return ranked submission rows for a benchmark.

    Each row includes submission_id, username, model, score_total, passed,
    and per-module run scores/metrics.
    """
    # Load benchmark
    result = await session.execute(
        select(Benchmark)
        .options(selectinload(Benchmark.modules))
        .where(Benchmark.slug == benchmark_slug)
    )
    benchmark = result.scalar_one_or_none()
    if benchmark is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Benchmark not found")

    viewer_is_admin = is_admin_or_above(user.role)

    # Drafts are admin-only work-in-progress: hide their existence (same 404 as
    # an unknown slug) from everyone else. Archived stays visible — it's history.
    if benchmark.status == BenchmarkStatus.DRAFT and not viewer_is_admin:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Benchmark not found")

    # Load all DONE submissions for this benchmark, ranked by score
    result = await session.execute(
        select(Submission)
        .options(selectinload(Submission.runs), selectinload(Submission.user))
        .where(
            Submission.benchmark_id == benchmark.id,
            Submission.status == SubmissionStatus.DONE,
        )
        .order_by(Submission.score_total.desc().nullslast())
    )
    submissions = result.scalars().unique().all()

    # Module instances in benchmark order (supports duplicate module names)
    module_instances = [
        {"name": bm.module_name, "order_index": bm.order_index}
        for bm in sorted(benchmark.modules, key=lambda m: m.order_index)
    ]
    # Map benchmark_module_id -> order_index for run sorting / matching
    bm_order_map = {bm.id: bm.order_index for bm in benchmark.modules}

    rows = []
    for rank, submission in enumerate(submissions, start=1):
        # Every row links to its detail page: submission detail is readable by
        # any signed-in user (read-only — cancel/logs stay owner/admin).
        rows.append({
            "rank": rank,
            "submission_id": submission.id,
            "created_at": submission.created_at.isoformat() if submission.created_at else None,
            "username": submission.user.username,
            "endpoint_model": submission.endpoint_model,
            # One-line, submitter-authored description of the service/optimizations.
            # The longer markdown detail is on the submission detail page only.
            "description_summary": submission.description_summary,
            "score_total": submission.score_total,
            "passed": submission.passed,
            "config_hash": submission.benchmark_config_hash,
            "runs": [
                {
                    "module_name": run.module_name,
                    "order_index": bm_order_map.get(run.benchmark_module_id, 0),
                    "score": run.score,
                    "passed": run.passed,
                    "metrics": run.metrics_json,
                }
                for run in sorted(
                    submission.runs,
                    key=lambda r: bm_order_map.get(r.benchmark_module_id, 0),
                )
            ],
        })

    return {
        "benchmark_slug": benchmark_slug,
        "benchmark_name": benchmark.name,
        "current_config_hash": benchmark.config_hash,
        "total_submissions": len(rows),
        "modules": module_instances,
        "module_names": [m["name"] for m in module_instances],
        "rows": rows,
    }
