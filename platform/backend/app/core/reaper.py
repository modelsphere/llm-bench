"""Reaper: detect crashed workers by missing Redis heartbeats and mark
submissions FAILED. Runs as an asyncio background task started from the
FastAPI lifespan.

How it works:
  - Workers maintain a Redis key `worker:submission:<id>` with TTL while
    processing a submission (see app/queue/heartbeat.py).
  - If a worker process crashes (segfault, OOM-kill, SIGKILL), the key
    expires within HEARTBEAT_TTL_SECONDS and the submission is left
    stuck in RUNNING.
  - This task scans RUNNING submissions every REAP_INTERVAL_SECONDS,
    cross-checks the heartbeat key, and marks orphaned submissions FAILED.

GRACE_SECONDS adds slack: a freshly-enqueued submission may not yet have
its heartbeat written. We only flag submissions whose started_at is older
than GRACE_SECONDS AND have no heartbeat.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.core.config import settings
from app.db.models import (
    ModuleRunStatus,
    Submission,
    SubmissionRun,
    SubmissionStatus,
    _async_session_factory,
)
from app.queue.heartbeat import HEARTBEAT_KEY_PREFIX

logger = logging.getLogger(__name__)

REAP_INTERVAL_SECONDS = 30
GRACE_SECONDS = 90  # > HEARTBEAT_TTL_SECONDS (60); avoid false positives on slow start


def _to_naive_utc(dt: datetime | None) -> datetime | None:
    """Normalize a datetime to naive-UTC. Postgres TIMESTAMPTZ columns come
    back as tz-aware while we set fields with datetime.utcnow() (naive);
    mixing the two raises TypeError on comparison."""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


async def _reap_once(redis_client) -> int:
    """One pass. Returns number of submissions reaped."""
    now = datetime.utcnow()
    cutoff = now - timedelta(seconds=GRACE_SECONDS)
    reaped = 0

    async with _async_session_factory() as session:
        result = await session.execute(
            select(Submission).where(
                Submission.status == SubmissionStatus.RUNNING,
                # If started_at is NULL, fall back to created_at via SQL coalesce
                # but SQLAlchemy filtering is cleaner with a simple guard below.
            )
        )
        running = list(result.scalars())

        for sub in running:
            started = _to_naive_utc(sub.started_at or sub.created_at)
            if started is None or started > cutoff:
                continue  # too fresh — wait for heartbeat to land

            key = f"{HEARTBEAT_KEY_PREFIX}{sub.id}"
            try:
                exists = await redis_client.exists(key)
            except Exception:
                logger.warning("reaper: redis EXISTS failed; skipping cycle")
                return reaped

            if exists:
                continue  # worker is alive and heart-beating

            # No heartbeat & past grace → worker died.
            logger.warning(
                "Detected crashed worker for submission %d (started_at=%s) — marking FAILED",
                sub.id, started,
            )
            sub.status = SubmissionStatus.FAILED
            sub.error = "Worker process crashed unexpectedly (no heartbeat)"
            sub.finished_at = now
            # Mark any RUNNING runs FAILED too
            runs_res = await session.execute(
                select(SubmissionRun).where(
                    SubmissionRun.submission_id == sub.id,
                    SubmissionRun.status == ModuleRunStatus.RUNNING,
                )
            )
            stuck_runs = list(runs_res.scalars())
            for run in stuck_runs:
                run.status = ModuleRunStatus.FAILED
                run.passed = False
                run.error = "Worker process crashed unexpectedly"
                run.finished_at = now

            await session.commit()

            # Publish SSE events so any open UI tab updates immediately
            channel = f"submission:{sub.id}:logs"
            for run in stuck_runs:
                payload = {
                    "event": "run_complete",
                    "run_id": run.id,
                    "module_name": run.module_name,
                    "status": "failed",
                    "score": None,
                    "passed": False,
                    "metrics": None,
                    "error": "Worker process crashed unexpectedly",
                }
                try:
                    await redis_client.publish(channel, json.dumps(payload, default=str))
                except Exception:
                    logger.warning("reaper: failed to publish run_complete for run %d", run.id)
            try:
                await redis_client.publish(
                    channel,
                    json.dumps({"event": "done", "status": SubmissionStatus.FAILED.value}),
                )
            except Exception:
                logger.warning("reaper: failed to publish done event for submission %d", sub.id)

            reaped += 1

    return reaped


async def _recover_orphaned_queued(redis_client) -> int:
    """Re-enqueue QUEUED submissions whose Dramatiq message was lost.

    Redis has no persistence here, so a Redis restart (pod reschedule, OOM,
    image upgrade) wipes the Dramatiq queue. A submission already committed to
    Postgres as QUEUED then loses its message and sits QUEUED forever — the
    RUNNING-reap above never sees it. We detect this per-submission via the
    queue-recovery marker (set at enqueue, cleared when the worker claims the
    job; see app.queue.jobs.QUEUE_MARKER_PREFIX): marker present ⇒ the message
    is still live (or the job is just waiting for a free worker) ⇒ leave it;
    marker absent ⇒ Redis dropped both marker and message ⇒ re-enqueue. Because
    the signal is per-submission, a job that legitimately sits QUEUED for hours
    is never wrongly re-dispatched, and a freshly-enqueued job (marker set with
    its message) is skipped — so this never double-dispatches a healthy job.
    """
    from app.queue.jobs import QUEUE_MARKER_PREFIX, run_submission

    now = datetime.utcnow()
    cutoff = now - timedelta(seconds=GRACE_SECONDS)

    async with _async_session_factory() as session:
        result = await session.execute(
            select(Submission.id, Submission.created_at).where(
                Submission.status == SubmissionStatus.QUEUED
            )
        )
        queued = list(result.all())

    recovered = 0
    for sub_id, created_at in queued:
        # Grace window: skip submissions created very recently. The enqueue path
        # commits status=QUEUED and *then* sets the marker, so there is a tiny
        # window where a healthy fresh submission has no marker yet — without
        # this grace the sweep could double-dispatch it. (The marker, not this
        # grace, is what protects long-legitimately-queued jobs.)
        created = _to_naive_utc(created_at)
        if created is not None and created > cutoff:
            continue
        key = f"{QUEUE_MARKER_PREFIX}{sub_id}"
        try:
            has_marker = await redis_client.exists(key)
        except Exception:
            logger.warning("reaper: redis EXISTS failed during queue recovery; skipping cycle")
            return recovered
        if has_marker:
            continue  # message still live, or job genuinely waiting — leave it

        # Claim before sending, via SET NX. The reaper runs in every backend
        # replica, so an unguarded EXISTS-then-send lets two reapers both
        # observe the marker missing and both re-enqueue, running the
        # submission twice concurrently. Only the replica that wins the claim
        # sends. (Re-setting the marker is required either way — it is what
        # stops the next cycle re-dispatching this same job.)
        try:
            claimed = await redis_client.set(key, b"1", nx=True)
        except Exception:
            logger.warning("reaper: redis SET NX failed during queue recovery; skipping cycle")
            return recovered
        if not claimed:
            continue  # another replica's reaper is re-enqueuing this one

        logger.warning(
            "Submission %d stuck QUEUED with no Dramatiq message (Redis lost it) — re-enqueuing",
            sub_id,
        )
        try:
            run_submission.send(sub_id)
            recovered += 1
        except Exception:
            logger.exception("reaper: failed to re-enqueue orphaned submission %d", sub_id)
            # Release the claim, or the marker we just set would look like a
            # live message and this submission would sit QUEUED forever.
            try:
                await redis_client.delete(key)
            except Exception:
                logger.warning("reaper: failed to release claim for submission %d", sub_id)

    return recovered


async def _reaper_loop(stop_event: asyncio.Event) -> None:
    import redis.asyncio as redis_lib
    redis_client = redis_lib.from_url(settings.REDIS_URL)
    logger.info("Reaper started (interval=%ds, grace=%ds)", REAP_INTERVAL_SECONDS, GRACE_SECONDS)
    try:
        while not stop_event.is_set():
            try:
                n = await _reap_once(redis_client)
                if n:
                    logger.warning("Reaper marked %d submission(s) FAILED due to worker crash", n)
            except Exception:
                logger.exception("Reaper cycle failed; will retry")
            try:
                r = await _recover_orphaned_queued(redis_client)
                if r:
                    logger.warning("Reaper re-enqueued %d orphaned QUEUED submission(s)", r)
            except Exception:
                logger.exception("Queue-recovery cycle failed; will retry")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=REAP_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                pass
    finally:
        try:
            await redis_client.close()
        except Exception:
            pass
        logger.info("Reaper stopped")


class ReaperHandle:
    """Lifespan helper: start/stop the reaper task."""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(_reaper_loop(self._stop))

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        try:
            await asyncio.wait_for(self._task, timeout=5)
        except asyncio.TimeoutError:
            self._task.cancel()
        self._task = None
