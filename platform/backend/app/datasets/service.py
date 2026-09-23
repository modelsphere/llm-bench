"""The rolling-dataset collector: an always-on pod that builds replay datasets.

Runs as its own Deployment (`replicas: 1`, `strategy: Recreate`) from the backend
image. Two things live in this process:

  - a **scheduler thread** that ticks once a minute, decides which enabled
    profiles are due, and queues them;
  - a **single build worker thread** that drains that queue one build at a time,
    so a manual "rebuild now" arriving mid-build queues instead of racing;
  - a tiny **FastAPI app** so the platform can trigger and inspect builds
    (`POST /builds`, `GET /builds/{id}`, `GET /status`, `GET /healthz`).

Why a long-lived pod rather than a CronJob: an operator can ask for a build from
the admin UI without the backend needing RBAC to create Jobs, and the collector
keeps its own queue and progress, which is what makes the admin page useful
while a build is in flight.

Rules this process must not break
---------------------------------
**Never hold a database transaction across a collection.** A build streams from
the log store for minutes; a session held open that long is the exact shape that
blocked a migration and took the site down on 2026-07-23. Every DB touch here is
a short open→write→commit→close, and the collection itself runs with no session.

**Never trust that this is the only collector.** The Deployment is single-replica
and Recreate, but a rescheduled pod can briefly overlap and an operator can run
the dry-run CLI by hand. Builds are therefore claimed with a conditional UPDATE
and kept alive with a heartbeat, the same idiom the benchmark worker uses; a
claim whose heartbeat has gone stale is reclaimable, a fresh one is not.
"""
from __future__ import annotations

import logging
import os
import queue
import socket
import threading
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Header, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select

from bench.replay_test import dataset_feed

from app.datasets.builder import BuildProfile, build_dataset, profile_from_dict
from app.datasets.sources import LogSource, source_for
from app.datasets.victorialogs import LogFilters
from app.db.models import (
    DatasetBuildStatus,
    ReplayDatasetBuild,
    ReplayDatasetProfile,
    get_sync_session,
)

logger = logging.getLogger(__name__)

SCHEDULER_TICK_SECONDS = 60.0
HEARTBEAT_INTERVAL_SECONDS = 30.0
# A claim is reclaimable once its heartbeat is this stale. Three intervals, the
# same ratio the submission reaper uses — long enough that a slow tick never
# steals a live build, short enough that a killed pod's build is retried soon.
CLAIM_STALE_SECONDS = HEARTBEAT_INTERVAL_SECONDS * 3


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: "datetime | None") -> "datetime | None":
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _is_missing_schema(exc: BaseException) -> bool:
    """True when the failure is "the feed tables don't exist yet".

    Matched on the driver's message rather than a psycopg2 error class so it
    holds whether the error arrives wrapped in SQLAlchemy's ProgrammingError or
    raw. Deliberately narrow: anything else must keep its full traceback.
    """
    text = str(exc).lower()
    return "undefinedtable" in text or (
        "does not exist" in text and "replay_dataset" in text
    )


def worker_identity() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def profile_to_build_profile(row: ReplayDatasetProfile) -> BuildProfile:
    """DB row → the builder's plain-dataclass profile."""
    return profile_from_dict({
        "name": row.name,
        "models": row.models or [],
        "statuses": row.statuses or ["200"],
        "forwarded_to": row.forwarded_to or [],
        "uris": row.uris or ["/v1/chat/completions"],
        "exclude_truncated": bool(row.exclude_truncated),
        "extra_logsql": row.extra_logsql or "",
        "window_hours": row.window_hours,
        "window_timezone": row.window_timezone,
        "subwindow_minutes": row.subwindow_minutes,
        "sample_size": row.sample_size,
        "min_records": row.min_records,
        "min_buckets": row.min_buckets,
        "max_bytes": row.max_bytes,
        "oversample_factor": row.oversample_factor,
        "max_carry_multiple": row.max_carry_multiple,
        "clean": row.clean,
        "max_model_len": row.max_model_len,
        "keep_response_body": row.keep_response_body,
        "compress": row.compress,
        "header_denylist": row.header_denylist or [],
        "keep_builds": row.keep_builds,
        "min_retain_hours": row.min_retain_hours,
        "schedule_interval_hours": row.schedule_interval_hours,
    })


def client_for(row: ReplayDatasetProfile) -> LogSource:
    """The log source a profile collects from. Credentials and mounts come from
    the environment, never from the profile row — the database holds collection
    policy, the pod holds the secrets (see app.datasets.sources.source_for)."""
    return source_for(getattr(row, "source_type", "") or "", row.source_url)


def feed_root() -> str:
    return os.getenv("REPLAY_FEED_ROOT", "") or str(dataset_feed.feed_root())


def frozen_root() -> str:
    return os.getenv("REPLAY_FROZEN_ROOT", "") or str(dataset_feed.frozen_root())


def _iso(value: "datetime | None") -> "str | None":
    return value.astimezone(timezone.utc).isoformat() if value else None


def find_build_file(profile: str, build_id: str):
    """The on-disk dataset file for a published build, or None if it is gone.

    Located strictly under the profile's own `builds/` directory — the profile
    name is a validated slug, so this cannot be steered at an arbitrary path — so
    a freeze can only ever promote a real build the collector produced. A build
    whose file has been pruned returns None (the caller turns that into a 404)."""
    for path in dataset_feed.list_build_files(feed_root(), profile):
        if dataset_feed.strip_dataset_suffix(path.name) == build_id:
            return path
    return None


# ---------------------------------------------------------------------------
# Build bookkeeping (short transactions only)
# ---------------------------------------------------------------------------


def enqueue_build(profile_id: int, *, trigger: str = "schedule",
                  user_id: "int | None" = None) -> int:
    """Insert a PENDING build row and return its id."""
    session = get_sync_session()
    try:
        build = ReplayDatasetBuild(
            profile_id=profile_id,
            status=DatasetBuildStatus.PENDING,
            trigger=trigger,
            triggered_by_user_id=user_id,
        )
        session.add(build)
        session.commit()
        return build.id
    finally:
        session.close()


def claim_build(build_id: int, identity: str) -> bool:
    """Claim a PENDING build (or reclaim one whose heartbeat has gone stale).

    Conditional UPDATE, not read-then-write: two collectors racing on the same
    row must not both proceed, and the database is the only place that decision
    can be made atomically.
    """
    session = get_sync_session()
    try:
        cutoff = _now() - timedelta(seconds=CLAIM_STALE_SECONDS)
        updated = session.query(ReplayDatasetBuild).filter(
            ReplayDatasetBuild.id == build_id,
            (ReplayDatasetBuild.status == DatasetBuildStatus.PENDING)
            | (
                (ReplayDatasetBuild.status == DatasetBuildStatus.RUNNING)
                & (ReplayDatasetBuild.heartbeat_at < cutoff)
            ),
        ).update(
            {
                "status": DatasetBuildStatus.RUNNING,
                "claimed_by": identity,
                "heartbeat_at": _now(),
                "started_at": _now(),
            },
            synchronize_session=False,
        )
        session.commit()
        return bool(updated)
    finally:
        session.close()


def touch_build(build_id: int, progress: "str | None" = None) -> None:
    session = get_sync_session()
    try:
        values: dict = {"heartbeat_at": _now()}
        if progress is not None:
            values["progress"] = progress[:2000]
        session.query(ReplayDatasetBuild).filter(
            ReplayDatasetBuild.id == build_id
        ).update(values, synchronize_session=False)
        session.commit()
    except Exception:  # noqa: BLE001 - a missed heartbeat must not kill a build
        logger.warning("[collector] heartbeat failed for build %s", build_id, exc_info=True)
        session.rollback()
    finally:
        session.close()


def finish_build(build_id: int, result) -> None:
    session = get_sync_session()
    try:
        values: dict = {
            "status": (
                DatasetBuildStatus.READY if result.status == "ready"
                else DatasetBuildStatus.FAILED
            ),
            "finished_at": _now(),
            "error": (result.error or None),
            "stats_json": result.stats or None,
            "window_start": result.window_start,
            "window_end": result.window_end,
            "seed": result.seed,
        }
        if result.build is not None:
            values.update({
                "build_id": result.build.build_id,
                "records": result.build.records,
                "size_bytes": result.build.bytes,
                "sha256": result.build.sha256,
                "path": result.build.path,
            })
        session.query(ReplayDatasetBuild).filter(
            ReplayDatasetBuild.id == build_id
        ).update(values, synchronize_session=False)
        session.commit()
    finally:
        session.close()


class PinLookupFailed(RuntimeError):
    """The set of in-flight dataset pins could not be read. Retention must then
    do nothing rather than delete against an unknown protected set — leaking a
    build costs disk, deleting one a benchmark is using costs a run."""


def protected_build_ids(profile_name: str) -> set[str]:
    """Build ids that GC must not delete because a submission is still using
    them. Cheap belt-and-braces on top of the retention age floor: a queued or
    running replay pinned a concrete path at resolve time, and deleting it
    mid-run would fail an otherwise healthy benchmark."""
    from app.db.models import Submission, SubmissionRun, SubmissionStatus

    session = get_sync_session()
    try:
        rows = session.execute(
            select(SubmissionRun.params_json)
            .join(Submission, Submission.id == SubmissionRun.submission_id)
            .where(Submission.status.in_([SubmissionStatus.QUEUED, SubmissionStatus.RUNNING]))
        ).all()
    except Exception as exc:  # noqa: BLE001 - caller decides; see build_dataset
        logger.warning("[collector] could not read in-flight dataset pins", exc_info=True)
        raise PinLookupFailed(str(exc)) from exc
    finally:
        session.close()

    pinned: set[str] = set()
    for (params,) in rows:
        resolved = (params or {}).get("dataset_resolved") if isinstance(params, dict) else None
        if isinstance(resolved, dict) and resolved.get("profile") == profile_name:
            build_id = resolved.get("build_id")
            if build_id:
                pinned.add(str(build_id))
    return pinned


def due_profiles() -> list[int]:
    """Enabled profiles whose next scheduled build is due, as ids.

    "Due" = no build has ever finished, or the newest finished build is older
    than `schedule_interval_hours` — and, when `schedule_anchor_hour` is set, the
    profile's local hour matches it. Anchor + interval covers "daily at 03:00"
    and "every 6 hours" without pulling in a cron parser.
    """
    session = get_sync_session()
    due: list[int] = []
    try:
        profiles = session.execute(
            select(ReplayDatasetProfile).where(ReplayDatasetProfile.enabled.is_(True))
        ).scalars().all()
        for profile in profiles:
            # schedule_interval_hours == 0 means MANUAL ONLY: this profile is
            # rebuilt when something calls the build endpoint and never on a
            # tick. Checked before everything else so a manual-only profile is
            # never picked up, not even the "no build yet, so it's due" path
            # below — which would otherwise collect once behind the operator's
            # back the moment the profile is created.
            if (profile.schedule_interval_hours or 0) <= 0:
                continue
            # An in-flight (or queued) build means this profile is already busy.
            active = session.execute(
                select(ReplayDatasetBuild.id).where(
                    ReplayDatasetBuild.profile_id == profile.id,
                    ReplayDatasetBuild.status.in_(
                        [DatasetBuildStatus.PENDING, DatasetBuildStatus.RUNNING]
                    ),
                ).limit(1)
            ).first()
            if active:
                continue
            if profile.schedule_anchor_hour is not None:
                local_hour = _now().astimezone(
                    profile_to_build_profile(profile).tzinfo()
                ).hour
                if local_hour != int(profile.schedule_anchor_hour):
                    continue
            last = session.execute(
                select(ReplayDatasetBuild.finished_at)
                .where(
                    ReplayDatasetBuild.profile_id == profile.id,
                    ReplayDatasetBuild.status == DatasetBuildStatus.READY,
                )
                .order_by(ReplayDatasetBuild.finished_at.desc())
                .limit(1)
            ).scalar_one_or_none()
            if last is not None:
                age = (_now() - _aware(last)).total_seconds()
                if age < profile.schedule_interval_hours * 3600.0:
                    continue
            due.append(profile.id)
        return due
    finally:
        session.close()


def load_profile(profile_id: int) -> "tuple[BuildProfile, str] | None":
    """Snapshot a profile row into (build profile, source_url) and close the
    session — the collection that follows must not hold one."""
    session = get_sync_session()
    try:
        row = session.get(ReplayDatasetProfile, profile_id)
        if row is None or not row.enabled:
            return None
        return profile_to_build_profile(row), row.source_url
    finally:
        session.close()


# ---------------------------------------------------------------------------
# The collector
# ---------------------------------------------------------------------------


class Collector:
    """Owns the build queue, the single build worker, and the scheduler tick."""

    def __init__(self) -> None:
        self._queue: "queue.Queue[tuple[int, int]]" = queue.Queue()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._identity = worker_identity()
        self._current: "dict | None" = None
        self._lock = threading.Lock()

    # -- lifecycle ------------------------------------------------------

    def start(self) -> None:
        self._threads = [
            threading.Thread(target=self._build_loop, name="dataset-build", daemon=True),
            threading.Thread(target=self._schedule_loop, name="dataset-schedule", daemon=True),
        ]
        for thread in self._threads:
            thread.start()
        logger.info("[collector] started as %s, feed root %s", self._identity, feed_root())

    def stop(self) -> None:
        """Ask the in-flight build to abandon itself. A partially collected
        dataset is discarded, never published — the previous build keeps
        serving, which is the whole point of staging + atomic publish."""
        self._stop.set()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    # -- public API used by the HTTP layer -------------------------------

    def submit(self, profile_id: int, *, trigger: str = "manual",
               user_id: "int | None" = None) -> int:
        build_id = enqueue_build(profile_id, trigger=trigger, user_id=user_id)
        self._queue.put((profile_id, build_id))
        return build_id

    def freeze(
        self,
        *,
        name: str,
        profile: str,
        build_id: str,
        records: int = 0,
        sha256: str = "",
        window_start: "datetime | None" = None,
        window_end: "datetime | None" = None,
        frozen_by: str = "",
    ) -> dict:
        """Promote a published build to a permanent frozen dataset.

        Synchronous — a freeze is a hard link (or, if the filesystem refuses, a
        one-shot copy), not a collection, so it does not go through the build
        queue and can safely run alongside an in-flight build (which only ever
        writes a *new* build id). Raises FileNotFoundError when the build's file
        is gone (pruned), FileExistsError when the frozen name is taken, and
        ValueError on a bad name/profile — the HTTP layer maps those to 404/409/400.
        """
        dataset_feed.validate_profile(profile)
        dataset_feed.validate_frozen_name(name)
        source = find_build_file(profile, build_id)
        if source is None:
            raise FileNotFoundError(
                f"build {build_id!r} of profile {profile!r} is not on disk "
                f"(it may have been pruned) — nothing to freeze"
            )
        dataset = dataset_feed.freeze(
            frozen_root(), name, source,
            records=records, sha256=sha256,
            source_profile=profile, source_build_id=build_id,
            window_start=window_start, window_end=window_end,
            frozen_by=frozen_by,
        )
        logger.info(
            "[collector] froze %s build %s -> %s", profile, build_id, dataset.path
        )
        return {
            "name": dataset.name,
            "path": dataset.path,
            "bytes": dataset.bytes,
            "records": dataset.records,
            "sha256": dataset.sha256,
            "frozen_at": _iso(dataset.frozen_at),
            "source_profile": dataset.source_profile,
            "source_build_id": dataset.source_build_id,
            "window_start": _iso(dataset.window_start),
            "window_end": _iso(dataset.window_end),
            "frozen_by": dataset.frozen_by,
        }

    def status(self) -> dict:
        with self._lock:
            current = dict(self._current) if self._current else None
        return {
            "identity": self._identity,
            "feed_root": feed_root(),
            "queue_depth": self._queue.qsize(),
            "current": current,
            "stopping": self.stopping,
        }

    # -- threads ---------------------------------------------------------

    def _schedule_loop(self) -> None:
        waiting_for_schema = False
        while not self._stop.wait(SCHEDULER_TICK_SECONDS):
            try:
                for profile_id in due_profiles():
                    logger.info("[collector] profile %s is due — queueing", profile_id)
                    self.submit(profile_id, trigger="schedule")
                waiting_for_schema = False
            except Exception as exc:  # noqa: BLE001 - a bad tick must not end scheduling
                # The chart's migrate Job is a POST-upgrade hook, so this pod
                # starts before its tables exist. That window is expected; log it
                # once as a one-liner instead of a traceback every 60s, which
                # reads like a broken deploy when it is a normal ordering.
                if _is_missing_schema(exc):
                    if not waiting_for_schema:
                        logger.warning(
                            "[collector] replay_dataset_* tables not present yet — "
                            "waiting for the migration to land (normal right after a deploy)"
                        )
                        waiting_for_schema = True
                    continue
                logger.exception("[collector] scheduler tick failed")

    def _build_loop(self) -> None:
        while not self._stop.is_set():
            try:
                profile_id, build_row_id = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                self._run_one(profile_id, build_row_id)
            except Exception:  # noqa: BLE001 - one bad build must not end the loop
                logger.exception("[collector] build %s crashed", build_row_id)
            finally:
                self._queue.task_done()

    def _run_one(self, profile_id: int, build_row_id: int) -> None:
        if not claim_build(build_row_id, self._identity):
            logger.info("[collector] build %s already claimed elsewhere — skipping", build_row_id)
            return
        loaded = load_profile(profile_id)
        if loaded is None:
            finish_build(build_row_id, _Failure("profile is missing or disabled"))
            return
        profile, _source_url = loaded

        stop_heartbeat = threading.Event()
        last_progress: list[str] = [""]

        def _heartbeat() -> None:
            while not stop_heartbeat.wait(HEARTBEAT_INTERVAL_SECONDS):
                touch_build(build_row_id, last_progress[0] or None)

        def _progress(message: str) -> None:
            last_progress[0] = message
            logger.info("[collector][%s] %s", profile.name, message)
            with self._lock:
                if self._current:
                    self._current["progress"] = message

        beat = threading.Thread(target=_heartbeat, name="dataset-heartbeat", daemon=True)
        beat.start()
        with self._lock:
            self._current = {
                "build_row_id": build_row_id,
                "profile": profile.name,
                "started_at": _now().isoformat(),
                "progress": "starting",
            }

        source = None
        try:
            row_session = get_sync_session()
            try:
                row = row_session.get(ReplayDatasetProfile, profile_id)
                source = client_for(row)
            finally:
                row_session.close()

            try:
                protected, allow_prune = protected_build_ids(profile.name), True
            except PinLookupFailed:
                protected, allow_prune = set(), False

            result = build_dataset(
                source, profile,
                feed_root=feed_root(),
                progress=_progress,
                should_stop=lambda: self._stop.is_set(),
                protected_builds=protected,
                allow_prune=allow_prune,
            )
            finish_build(build_row_id, result)
            logger.info(
                "[collector][%s] build %s finished: %s%s",
                profile.name, build_row_id, result.status,
                f" ({result.error})" if result.error else "",
            )
        finally:
            stop_heartbeat.set()
            if source is not None:
                source.close()
            with self._lock:
                self._current = None


class _Failure:
    """Minimal stand-in for a BuildResult when the build never started."""

    def __init__(self, error: str) -> None:
        self.status = "failed"
        self.error = error
        self.build = None
        self.stats: dict = {}
        self.window_start = None
        self.window_end = None
        self.seed = 0


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------


class BuildRequest(BaseModel):
    profile_id: int
    user_id: "int | None" = None


class FreezeRequest(BaseModel):
    """Freeze a published build into a permanent dataset. Provenance fields
    (records/sha256/window) are passed through from the build row the backend
    already holds, so the collector doesn't re-read the DB just to stamp them
    onto the frozen dataset's meta sidecar."""

    name: str
    profile: str
    build_id: str
    records: int = 0
    sha256: str = ""
    window_start: "datetime | None" = None
    window_end: "datetime | None" = None
    frozen_by: str = ""


collector = Collector()
app = FastAPI(title="Replay dataset collector", docs_url=None, redoc_url=None)


def _check_token(token: "str | None") -> None:
    """Shared-secret gate. The collector is a ClusterIP service, so this stops
    any other pod in the namespace from triggering builds; it is not a user
    auth boundary (the backend does that before it ever calls here)."""
    expected = os.getenv("DATASET_BUILDER_TOKEN", "")
    if expected and token != expected:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="bad builder token")


@app.on_event("startup")
def _startup() -> None:
    collector.start()


@app.on_event("shutdown")
def _shutdown() -> None:
    collector.stop()


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "stopping": collector.stopping}


@app.get("/status")
def get_status(x_builder_token: "str | None" = Header(default=None)) -> dict:
    _check_token(x_builder_token)
    return collector.status()


@app.post("/builds", status_code=status.HTTP_202_ACCEPTED)
def post_build(body: BuildRequest, x_builder_token: "str | None" = Header(default=None)) -> dict:
    _check_token(x_builder_token)
    if collector.stopping:
        raise HTTPException(status_code=503, detail="collector is shutting down")
    loaded = load_profile(body.profile_id)
    if loaded is None:
        raise HTTPException(status_code=404, detail="profile not found or disabled")
    build_row_id = collector.submit(body.profile_id, trigger="manual", user_id=body.user_id)
    return {"build_row_id": build_row_id, "queue_depth": collector.status()["queue_depth"]}


@app.post("/freeze", status_code=status.HTTP_201_CREATED)
def post_freeze(body: FreezeRequest, x_builder_token: "str | None" = Header(default=None)) -> dict:
    _check_token(x_builder_token)
    try:
        return collector.freeze(
            name=body.name, profile=body.profile, build_id=body.build_id,
            records=body.records, sha256=body.sha256,
            window_start=body.window_start, window_end=body.window_end,
            frozen_by=body.frozen_by,
        )
    except FileExistsError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@app.delete("/frozen/{name}")
def delete_frozen_endpoint(name: str, x_builder_token: "str | None" = Header(default=None)) -> dict:
    _check_token(x_builder_token)
    try:
        removed = dataset_feed.delete_frozen(frozen_root(), name)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    if not removed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no frozen dataset named {name!r}",
        )
    return {"removed": True}


def main() -> None:  # pragma: no cover - process entry point
    import uvicorn

    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.getenv("DATASET_BUILDER_PORT", "8080")),
        access_log=False,
    )


if __name__ == "__main__":  # pragma: no cover
    main()


# Re-exported for tests and the admin API's "test connection" probe.
__all__ = [
    "Collector", "LogFilters", "app", "collector", "client_for", "due_profiles",
    "enqueue_build", "feed_root", "frozen_root", "find_build_file",
    "profile_to_build_profile", "protected_build_ids",
]
