"""Dramatiq jobs: run_submission actor."""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import threading
import time
from datetime import datetime
from decimal import Decimal

import dramatiq

from app.core.config import settings
from app.core.security import decrypt_api_key
from app.queue.broker import broker


# Module-level Redis client (lazy-initialised, reused across calls)
_redis_client = None


def _get_redis():
    global _redis_client
    if _redis_client is None:
        import redis
        _redis_client = redis.from_url(settings.REDIS_URL, decode_responses=False)
    return _redis_client


# Queue-recovery marker. Set when a submission is enqueued, cleared when the
# worker claims it. The marker lives in the SAME Redis as the Dramatiq message,
# so the two share fate: a Redis wipe (pod restart with no persistence) drops
# both. The reaper re-enqueues any QUEUED submission whose marker is missing —
# that signal is per-submission and time-independent, so a job that sits QUEUED
# for hours (workers busy) keeps its marker and is never wrongly re-dispatched.
QUEUE_MARKER_PREFIX = "queued:submission:"


def _queue_marker_key(submission_id: int) -> str:
    return f"{QUEUE_MARKER_PREFIX}{submission_id}"


def clear_queue_marker(submission_id: int) -> None:
    """Drop a submission's queue-recovery marker once the worker has claimed it."""
    try:
        _get_redis().delete(_queue_marker_key(submission_id))
    except Exception:
        logging.getLogger(__name__).warning(
            "Failed to clear queue marker for submission %d", submission_id
        )


def _publish_run_event(submission_id: int, run_data: dict) -> None:
    """Publish a run completion event to Redis so SSE subscribers receive it."""
    try:
        r = _get_redis()
        channel = f"submission:{submission_id}:logs"
        r.publish(channel, json.dumps(run_data, default=str))
    except Exception:
        logging.getLogger(__name__).warning("Failed to publish run event to Redis")


def _publish_progress_event(submission_id: int, run_id: int, module_name: str, fraction: float, message: str) -> None:
    """Publish a progress event to Redis so the frontend progress bar updates in real time.

    run_id keys the event to a specific SubmissionRun, not just the module name —
    a benchmark may legally contain the same module more than once, and without
    run_id every instance would share (and display) the first one's progress.
    """
    import traceback
    try:
        r = _get_redis()
        channel = f"submission:{submission_id}:logs"
        r.publish(channel, json.dumps({
            "event": "progress",
            "run_id": run_id,
            "module_name": module_name,
            "fraction": fraction,
            "message": message,
        }, default=str))
    except Exception:
        logging.getLogger(__name__).warning(
            "Failed to publish progress event to Redis: %s", traceback.format_exc()
        )

# Ensure bench modules are importable.
# BENCH_MODULES_PATH is set in Docker to /app/bench; locally fall back to
# navigating up from platform/backend/app/queue/jobs.py → repo_root/bench.
from pathlib import Path as _Path
_bench_root = os.environ.get("BENCH_MODULES_PATH") or str(
    _Path(__file__).resolve().parents[4] / "bench"
)
if _bench_root not in sys.path:
    sys.path.insert(0, _bench_root)

from bench.modules import get_module
from bench.modules.base import BenchmarkCancelled, EndpointConfig, ModuleResult
from bench.modules.evaluator import evaluate

logger = logging.getLogger(__name__)

# Sentinel appended to a submission's `results` list for a module that was
# SKIPPED by the cascade. It is identity-checked during score aggregation so a
# skipped module is excluded from the weighted average while still forcing the
# submission's overall verdict to not-passed (a prior module failed).
_SKIPPED_RESULT = ModuleResult(error="skipped", passed=False, score=0.0)


class SubmissionCancelled(Exception):
    """Raised by the progress callback when the submission is canceled mid-run."""


def _start_cancel_watcher(submission_id: int) -> tuple[threading.Event, threading.Event, threading.Thread]:
    """Spawn a daemon thread that polls submission status from its OWN session.

    SQLAlchemy `Session` is not thread-safe, so the watcher must never touch the
    main task's session. Returns (cancel_event, stop_event, thread). The caller
    MUST set stop_event and join() the thread in a finally-block so the thread
    does not leak across submissions.
    """
    from app.db.models import Submission, SubmissionStatus, get_sync_session

    cancel_event = threading.Event()
    stop_event = threading.Event()

    def _poll() -> None:
        consecutive_errors = 0
        while not stop_event.is_set():
            if stop_event.wait(timeout=2):
                return
            local_session = None
            try:
                local_session = get_sync_session()
                row = local_session.query(Submission.status).filter_by(id=submission_id).first()
                if row is not None and row[0] == SubmissionStatus.CANCELED:
                    cancel_event.set()
                    return
                consecutive_errors = 0
            except Exception:
                consecutive_errors += 1
                logger.warning(
                    "cancel-watcher poll failed for submission %d (consecutive=%d)",
                    submission_id, consecutive_errors,
                )
                if consecutive_errors >= 10:
                    logger.error(
                        "cancel-watcher giving up for submission %d after %d failures",
                        submission_id, consecutive_errors,
                    )
                    return
            finally:
                if local_session is not None:
                    try:
                        local_session.close()
                    except Exception:
                        pass

    watcher = threading.Thread(target=_poll, daemon=True, name=f"cancel-watcher-{submission_id}")
    watcher.start()
    return cancel_event, stop_event, watcher


def _make_progress_cb(submission_id: int, run_id: int, module_name: str, cancel_event: threading.Event):
    """Return a progress callback that publishes to Redis AND aborts if canceled.

    Takes plain ints, NOT the ORM objects. This callback fires on every progress
    tick for the whole duration of a module, so touching a mapped attribute here
    reaches back into the shared session — and with expire-on-commit that meant a
    refresh SELECT that opened a transaction which then stayed open for the entire
    run (the 2026-07-23 outage; see the sync session factory comment). Holding
    only ints makes that structurally impossible rather than merely fixed.

    Cancellation is read from `cancel_event`, never from submission.status. The
    event is the authoritative signal: _start_cancel_watcher polls status every 2s
    in its OWN short-lived session and sets it, and _start_module_deadline folds
    the module time cap into the same event. Re-reading status through the shared
    session would be both redundant and (with expire_on_commit=False) permanently
    stale, since the object was loaded before the run began.
    """

    def cb(fraction: float, message: str) -> None:
        _publish_progress_event(submission_id, run_id, module_name, fraction, message)
        if cancel_event.is_set():
            raise SubmissionCancelled(f"Submission {submission_id} was canceled")

    return cb


SUBMISSION_MAX_RETRIES = 2
# Total times a worker may START a submission, counted on the row itself
# (submissions.run_attempts) rather than in the broker message. Dramatiq's
# `retries` only covers exception-driven retries; a redelivery after a killed
# worker arrives with retries=0 and would otherwise recycle forever.
SUBMISSION_MAX_ATTEMPTS = int(
    os.getenv("SUBMISSION_MAX_ATTEMPTS", str(SUBMISSION_MAX_RETRIES + 1))
)


def _current_retry_attempt() -> int:
    """Return Dramatiq's retry counter for the in-flight message (0 on first try).

    Used to decide whether this invocation is a fresh run or a redelivery after
    a prior crash, and to log the attempt loudly.
    """
    try:
        from dramatiq.middleware import CurrentMessage
        msg = CurrentMessage.get_current_message()
        if msg is not None:
            return int(msg.options.get("retries", 0) or 0)
    except Exception:
        pass
    return 0


@dramatiq.actor(
    broker=broker,
    time_limit=2*86400000,
    max_retries=SUBMISSION_MAX_RETRIES,
    min_backoff=60_000,
    max_backoff=600_000,
)
def run_submission(submission_id: int) -> None:
    """
    Load a submission from the DB, iterate its benchmark_modules in order (or
    the single module for ad-hoc submissions), call module.run(), and write
    SubmissionRun rows with results.

    Wraps the body in BaseException so any unexpected escape (including OOM
    SystemExit, KeyboardInterrupt, asserts, etc.) is captured as a FAILED
    submission row instead of leaving it stuck in RUNNING. With process
    isolation (--processes N), a hard segfault still kills only this worker
    process — the master will respawn it and the reaper will mark the
    submission FAILED via the heartbeat-TTL signal.

    Retries: on uncaught exception we re-raise, so Dramatiq schedules a
    redelivery (up to SUBMISSION_MAX_RETRIES). Each retry restarts the
    submission from scratch — stale SubmissionRun rows are reset before
    re-running. Partial-resume of already-completed modules is intentionally
    NOT supported: loud restart is preferred over silently mixing prior and
    new results.
    """
    retry_attempt = _current_retry_attempt()
    if retry_attempt > 0:
        logger.warning(
            "Submission %d RETRY attempt %d/%d (Dramatiq redelivery)",
            submission_id, retry_attempt, SUBMISSION_MAX_RETRIES,
        )
        _publish_run_event(submission_id, {
            "event": "retry",
            "attempt": retry_attempt,
            "max_retries": SUBMISSION_MAX_RETRIES,
        })

    # Capture this process's root-logger output for the whole run (including the
    # crash-handling below) into a per-submission buffer, flushed to the DB so
    # users can download the worker log. The context manager's finally flushes +
    # detaches before any re-raise, and guarantees no log bleed into the next
    # submission this process handles. See app.queue.log_capture.
    from app.queue.log_capture import capture_submission_logs
    attempt_marker = (
        f"===== submission {submission_id} attempt "
        f"{retry_attempt + 1}/{SUBMISSION_MAX_RETRIES + 1} ====="
    )
    with capture_submission_logs(submission_id, attempt_marker):
        try:
            _run_submission_body(submission_id, retry_attempt=retry_attempt)
        except BaseException as exc:
            logger.exception(
                "Submission %d worker escaped with %s (attempt %d/%d)",
                submission_id, type(exc).__name__, retry_attempt, SUBMISSION_MAX_RETRIES,
            )
            try:
                from app.db.models import Submission, SubmissionStatus, get_sync_session
                fresh = get_sync_session()
                try:
                    sub = fresh.query(Submission).filter_by(id=submission_id).first()
                    if sub is not None and sub.status not in (
                        SubmissionStatus.DONE, SubmissionStatus.CANCELED,
                    ):
                        retries_left = SUBMISSION_MAX_RETRIES - retry_attempt
                        suffix = (
                            " (final, retries exhausted)"
                            if retries_left <= 0
                            else f" (will retry, {retries_left} attempt(s) left)"
                        )
                        _fail_submission(
                            fresh, sub,
                            f"Worker crashed: {type(exc).__name__}: {exc}{suffix}",
                        )
                finally:
                    fresh.close()
            except Exception:
                logger.exception("Failed to mark submission %d as FAILED after crash", submission_id)
            raise


def enqueue_submission(submission_id: int) -> None:
    """Enqueue a submission for the worker and set its queue-recovery marker.

    Call this instead of ``run_submission.send()`` directly: the marker lets the
    reaper recover the job if Redis loses the Dramatiq message before a worker
    claims it (see QUEUE_MARKER_PREFIX). Marker-set failures are non-fatal — the
    job is still enqueued; it just won't be auto-recovered if Redis is then wiped.
    """
    run_submission.send(submission_id)
    try:
        _get_redis().set(_queue_marker_key(submission_id), b"1")
    except Exception:
        logging.getLogger(__name__).warning(
            "Failed to set queue marker for submission %d (enqueued anyway)", submission_id
        )


def _reset_submission_for_retry(session, submission) -> None:
    """Wipe stale module-run state so a retried submission starts clean.

    Preserves SubmissionRun rows (so row IDs stay stable for any SSE subscriber
    still listening) but clears every result-bearing field on both the
    submission and its runs. Partial-resume of already-DONE modules is
    intentionally not supported — restarting from scratch is preferred over
    silently mixing prior and new results.
    """
    from app.db.models import SubmissionRun, ModuleRunStatus
    runs = session.query(SubmissionRun).filter_by(submission_id=submission.id).all()
    for r in runs:
        r.status = ModuleRunStatus.PENDING
        r.started_at = None
        r.finished_at = None
        r.score = None
        r.passed = None
        r.metrics_json = None
        r.metric_configs_json = []
        r.error = None
        r.artifact_path = None
    submission.error = None
    submission.score_total = None
    submission.passed = None
    submission.started_at = None
    submission.finished_at = None
    session.commit()


def _run_submission_body(submission_id: int, retry_attempt: int = 0) -> None:
    # Import here so the actor only touches the DB inside the actor process
    from app.db.models import (
        BenchmarkModule,
        Submission,
        SubmissionRun,
        SubmissionStatus,
        get_sync_session,
    )

    session = get_sync_session()

    try:
        submission = session.query(Submission).filter_by(id=submission_id).first()
    except Exception:
        logger.exception("Failed to load submission %d", submission_id)
        return

    if submission is None:
        logger.error("Submission %d not found", submission_id)
        return

    # Skip if already in a non-retryable terminal state.
    #
    # FAILED is deliberately NOT in this list: if Dramatiq is redelivering us
    # (or the broker-level redelivery fired after a hard worker crash), we
    # want to restart from scratch even if a prior attempt (or the reaper)
    # already marked the submission FAILED. LLM-endpoint errors never reach
    # this path — they are caught in _run_module_loop's per-module
    # `except Exception` and stored as failed SubmissionRun rows without
    # raising — so a FAILED status here implies a real worker/backend crash.
    if submission.status in (SubmissionStatus.CANCELED, SubmissionStatus.DONE):
        logger.info(
            "Submission %d is already %s — skipping worker run",
            submission_id, submission.status.value,
        )
        return

    # Bound the TOTAL number of attempts, across both retry paths.
    #
    # Dramatiq's `retries` is not sufficient: a broker redelivery after a killed
    # worker (OOM, SIGKILL, drain-budget expiry at deploy) arrives as a fresh
    # delivery with retries=0, so SUBMISSION_MAX_RETRIES is never consumed and the
    # submission can resurrect forever — re-running every module from scratch,
    # usually straight back into whatever killed it. This counter is on the row,
    # so it survives redelivery, worker death and pod replacement.
    #
    # Incremented and committed BEFORE the run so an attempt that dies hard still
    # counts; otherwise the very failure mode this bounds would never be counted.
    submission.run_attempts = (submission.run_attempts or 0) + 1
    session.commit()
    if submission.run_attempts > SUBMISSION_MAX_ATTEMPTS:
        logger.error(
            "Submission %d has already been attempted %d time(s) (limit %d) — refusing to "
            "run it again. Re-submit explicitly if this needs another try.",
            submission_id, submission.run_attempts - 1, SUBMISSION_MAX_ATTEMPTS,
        )
        _fail_submission(
            session, submission,
            f"Giving up after {SUBMISSION_MAX_ATTEMPTS} attempts — the worker died or the "
            f"run was redelivered repeatedly. Re-submit to try again.",
        )
        return

    # If this is a Dramatiq retry, OR we're recovering from a crash that left
    # the submission in a non-fresh state (FAILED by prior handler/reaper, or
    # RUNNING with stale module rows because the worker died before commit),
    # reset everything so we re-run cleanly.
    needs_reset = (
        retry_attempt > 0
        or submission.status in (SubmissionStatus.FAILED, SubmissionStatus.RUNNING)
    )
    if needs_reset:
        logger.warning(
            "Submission %d restarting fresh (prior status=%s, retry_attempt=%d) — "
            "stale run results will be discarded",
            submission_id, submission.status.value, retry_attempt,
        )
        _reset_submission_for_retry(session, submission)

    # Decrypt API key
    try:
        api_key = decrypt_api_key(submission.endpoint_api_key_enc)
    except Exception:
        _fail_submission(session, submission, "Failed to decrypt API key")
        return

    endpoint = EndpointConfig(
        api_url=submission.endpoint_url,
        model=submission.endpoint_model,
        api_key=api_key,
    )

    # Mark submission as running
    submission.status = SubmissionStatus.RUNNING
    submission.started_at = datetime.utcnow()
    session.commit()
    # Claimed — drop the queue-recovery marker so the reaper won't re-enqueue us.
    # Done AFTER the RUNNING commit so there is never a (status=QUEUED, marker=absent)
    # window for a healthy job: the only way the marker is missing while still
    # QUEUED is a Redis wipe, which is exactly what we want the reaper to recover.
    clear_queue_marker(submission_id)
    logger.info(
        "Submission %d started — model=%s endpoint=%s benchmark_id=%s module=%s",
        submission_id, submission.endpoint_model, submission.endpoint_url,
        submission.benchmark_id, submission.module_name,
    )

    if submission.benchmark_id is not None:
        # Full benchmark: run modules in order
        modules = (
            session.query(BenchmarkModule)
            .filter_by(benchmark_id=submission.benchmark_id)
            .order_by(BenchmarkModule.order_index)
            .all()
        )
        if not modules:
            _fail_submission(session, submission, "Benchmark has no modules")
            return
        _run_modules(session, submission, modules, endpoint)
    elif submission.module_name is not None:
        # Single ad-hoc module
        run = session.query(SubmissionRun).filter_by(
            submission_id=submission.id, module_name=submission.module_name
        ).first()
        if run is None:
            _fail_submission(session, submission, "No run found for ad-hoc submission")
            return
        module = type("ModuleDef", (), {
            "module_name": submission.module_name,
            "params_json": run.params_json,
        })()
        _run_modules(session, submission, [module], endpoint)
    else:
        _fail_submission(session, submission, "Submission has no benchmark_id or module_name")
        return


class DatasetFeedUnavailable(Exception):
    """A module asked for a rolling dataset profile that has published nothing."""


def dataset_pin_owner(submission_id, run) -> str:
    """Token identifying who holds a dataset pin. Encodes the submission id so
    the sweeper can ask "is this submission still in flight?" the same way the
    output-dir sweeper does."""
    return f"s{submission_id}-r{getattr(run, 'id', 0)}"


def _release_dataset_pin(submission_id, run) -> None:
    """Drop this run's dataset pin. Best-effort: a leaked pin costs one extra
    dataset on disk until the sweeper collects it, which is far cheaper than
    letting a cleanup failure surface as a run failure."""
    try:
        from bench.replay_test import dataset_feed

        released = dataset_feed.unpin_owner(None, dataset_pin_owner(submission_id, run))
        if released:
            logger.info(
                "Submission %s: released %d rolling-dataset pin(s)", submission_id, released
            )
    except Exception:
        logger.warning(
            "Submission %s: failed to release rolling-dataset pin", submission_id, exc_info=True
        )


def _sweep_stale_dataset_pins() -> None:
    """Release pins left by workers that died mid-run.

    Fails SAFE, exactly like `_sweep_stale_output_dirs`: if the liveness check
    cannot be made, nothing is swept. A stale pin only holds disk; deleting a
    live run's dataset would fail the run.
    """
    try:
        from bench.replay_test import dataset_feed
    except Exception:
        return

    def _submission_id(owner: str) -> "int | None":
        head = owner.split("-", 1)[0]
        if head.startswith("s") and head[1:].isdigit():
            return int(head[1:])
        return None

    try:
        profiles = dataset_feed.list_profiles(None)
    except Exception:
        return
    if not profiles:
        return

    owners: set[str] = set()
    for profile in profiles:
        owners.update(owner for owner, _ in dataset_feed.list_pins(None, profile))
    ids = [sid for sid in (_submission_id(o) for o in owners) if sid is not None]
    if not ids:
        return
    try:
        live = _non_terminal_submission_ids(ids)
    except Exception:
        logger.warning("dataset-pin sweep skipped: liveness check failed", exc_info=True)
        return

    def _is_live(owner: str) -> bool:
        sid = _submission_id(owner)
        # An owner we cannot parse is treated as live — never guess in the
        # direction that deletes.
        return sid is None or sid in live

    swept = 0
    for profile in profiles:
        swept += dataset_feed.sweep_pins(None, profile, is_live=_is_live)
    if swept:
        logger.info("Swept %d stale rolling-dataset pin(s)", swept)


def _resolve_dataset_feed(session, run, module_cls, params, module_name: str):
    """Pin this run to ONE concrete dataset file when the module is configured
    to replay a rolling collection profile.

    Resolution happens HERE, once, before the module starts — not inside the
    module — for a specific reason: ReplayTest re-opens its dataset by path
    several times per run (a sizing pass, the bounded tool-support preflight
    scan, and the streaming dispatch pass, which re-opens on every make_iter()).
    If the collector published a new build between two of those passes, the run
    would size against one dataset and replay another, and the record count it
    planned for would no longer match the file. Resolving up front makes a
    mid-run publish a non-event.

    The resolved build is recorded onto `run.params_json['dataset_resolved']`,
    which the submission-detail page already renders. That provenance is the
    whole reason a rotating dataset is acceptable: a score can always be traced
    to the exact bytes that produced it.

    Returns the (possibly rebuilt) params. A no-op for every module that does
    not declare `dataset_feed_fields()`, and for `dataset_source='fixed'`.
    """
    fields = module_cls.dataset_feed_fields()
    if fields is None:
        return params
    if str(getattr(params, fields.source, "") or "") != fields.auto_value:
        return params

    from bench.replay_test import dataset_feed

    submission_id = getattr(run, "submission_id", 0)
    profile = str(getattr(params, fields.profile, "") or "").strip()
    build = dataset_feed.resolve_latest(None, profile)
    if build is None:
        # Fail loudly. The alternative — quietly replaying whatever fixed file
        # the params happen to name — would produce a plausible-looking score
        # against the wrong dataset, which is worse than a clear failure.
        raise DatasetFeedUnavailable(
            f"Rolling dataset profile {profile!r} has no published build to "
            f"replay. Check the collector on the admin Replay Datasets page."
        )

    # Hard-link the build into pins/ and run from THAT path. Replay re-opens its
    # dataset several times across a run that can last hours; a link means
    # retention deleting builds/<id>.jsonl underneath it cannot break the run,
    # rather than merely being unlikely to. Released in _run_module_loop's
    # finally, and swept if the worker dies first.
    pinned = dataset_feed.pin(None, profile, build, dataset_pin_owner(submission_id, run))
    dataset_path = str(pinned) if pinned is not None else build.path
    if pinned is None:
        logger.warning(
            "Submission %s: could not hard-link rolling dataset %r build %s — running "
            "from the shared builds/ path instead. Retention's age floor and "
            "in-flight-pin check still protect it, but they are policy, not a guarantee.",
            submission_id, profile, build.build_id,
        )

    age_hours = build.age_hours()
    max_age = float(getattr(params, fields.max_age, 0.0) or 0.0) if fields.max_age else 0.0
    stale = bool(max_age and age_hours > max_age)
    if stale:
        # By design this warns instead of failing: a stale feed is an operations
        # problem, and stopping every submission is a worse outcome than running
        # slightly older traffic. The warning lands in the submission's worker
        # log, and `stale` is recorded on the run.
        logger.warning(
            "Submission %d module %s: rolling dataset %r build %s is %.1fh old "
            "(profile expects <= %.1fh) — running it anyway",
            getattr(run, "submission_id", 0), module_name, profile,
            build.build_id, age_hours, max_age,
        )
    logger.info(
        "Submission %d module %s: resolved rolling dataset %r → build %s "
        "(%d records, %.1fh old, window %s → %s)",
        getattr(run, "submission_id", 0), module_name, profile, build.build_id,
        build.records, age_hours, build.window_start, build.window_end,
    )

    params = params.model_copy(update={fields.path: dataset_path})

    try:
        provenance = build.provenance()
        provenance["stale"] = stale
        provenance["age_hours"] = round(age_hours, 2)
        # `path` stays the canonical builds/ location (that is the dataset's
        # identity); `pinned_path` is the link this run actually reads, and its
        # absence records that the link could not be made.
        if pinned is not None:
            provenance["pinned_path"] = str(pinned)
        # Rewrite the run's param snapshot so the recorded dataset_path is the
        # file that actually ran, not the placeholder the benchmark stores.
        snapshot = dict(run.params_json or {})
        snapshot[fields.path] = dataset_path
        snapshot["dataset_resolved"] = provenance
        run.params_json = snapshot
        session.commit()
    except Exception:  # recording is best-effort — never fail a run over it
        logger.warning(
            "Submission %d: failed to record dataset provenance for run %s",
            getattr(run, "submission_id", 0), getattr(run, "id", "?"), exc_info=True,
        )
        session.rollback()
    return params


def _clamp_to_field_range(field, value: int) -> int:
    """Clamp `value` to a Pydantic v2 field's numeric ge/gt/le/lt constraints
    (stored as annotated-types objects in field.metadata). Keeps a forced
    override inside the module's own declared bounds instead of failing validation."""
    import annotated_types as at
    lo = hi = None
    for meta in getattr(field, "metadata", ()) or ():
        if isinstance(meta, at.Ge):
            lo = meta.ge if lo is None else max(lo, meta.ge)
        elif isinstance(meta, at.Gt):
            lo = meta.gt + 1 if lo is None else max(lo, meta.gt + 1)
        elif isinstance(meta, at.Le):
            hi = meta.le if hi is None else min(hi, meta.le)
        elif isinstance(meta, at.Lt):
            hi = meta.lt - 1 if hi is None else min(hi, meta.lt - 1)
    if lo is not None:
        value = max(int(lo), value)
    if hi is not None:
        value = min(int(hi), value)
    return value


def _apply_concurrency_override(module_cls, params, submission, module_name):
    """Force the submission-level concurrency override — the global
    `concurrency_override` or this module's `module_concurrency_overrides` entry
    — onto this module's `concurrency` param so it genuinely takes effect (the
    modules read `params.concurrency` to size their worker pools).

    Returns the (possibly rebuilt) params object. The value is clamped to the
    module's own ge/le range and the params are re-validated, so an override that
    a single module caps lower still produces a valid request rather than failing
    the run. Logs the outcome for EVERY module (applied / clamped / not-applicable)
    so the override is auditable in the worker log when each module starts. An
    absent override (None) is a no-op and logs nothing."""
    from app.core.module_caps import resolve_concurrency_override
    override = resolve_concurrency_override(
        getattr(submission, "extra_params", None), module_name
    )
    if override is None:
        return params
    # Re-enforce the global [1, MAX_ALLOWED_CONCURRENCY] guard at the point of
    # effect (the API also clamps on submit; this covers a lowered ceiling or any
    # value that reached the DB another way) before the per-module clamp below.
    from app.core.config import clamp_concurrency_override
    override = clamp_concurrency_override(override)
    # The concurrency-equivalent param may be spelled differently per module
    # (e.g. opencompass's `max_workers`); resolve it via the alias list.
    pname = module_cls.concurrency_param_name()
    if pname is None:
        logger.info(
            "Submission %d module %s: concurrency_override=%s requested but this module "
            "has no concurrency-equivalent param — not applied.",
            submission.id, module_name, override,
        )
        return params
    prev = getattr(params, pname, None)
    eff = _clamp_to_field_range(module_cls.ParamsSchema.model_fields[pname], int(override))
    try:
        data = params.model_dump()
        data[pname] = eff
        params = module_cls.ParamsSchema(**data)
    except Exception as exc:  # an override must never break the run
        logger.warning(
            "Submission %d module %s: failed to apply concurrency_override=%s to %r (%s) — "
            "keeping module's own value=%s.",
            submission.id, module_name, override, pname, exc, prev,
        )
        return params
    if eff != override:
        logger.info(
            "Submission %d module %s: CONCURRENCY OVERRIDE %s=%s -> %d "
            "(requested %s, clamped to this module's allowed range).",
            submission.id, module_name, pname, prev, eff, override,
        )
    else:
        logger.info(
            "Submission %d module %s: CONCURRENCY OVERRIDE %s=%s -> %d.",
            submission.id, module_name, pname, prev, eff,
        )
    return params


def _record_applied_concurrency(session, run, module_cls, params, submission) -> None:
    """Persist onto `run` the concurrency the module will actually execute at when
    a submission-level override applied — read straight off the (already-overridden)
    `params`. Stored because it can't be reconstructed once the module's cap or the
    global limit changes: the detail page then reports what truly ran, not a value
    recomputed against a moved goalpost. A no-op when no override applies."""
    from app.core.module_caps import resolve_concurrency_override
    override = resolve_concurrency_override(
        getattr(submission, "extra_params", None), getattr(run, "module_name", None)
    )
    if override is None:
        return
    pname = module_cls.concurrency_param_name()
    if pname is None:
        return
    try:
        run.applied_concurrency = int(getattr(params, pname))
        session.commit()
    except Exception:  # recording is best-effort — never fail a run over it
        logger.warning(
            "Submission %d: failed to record applied_concurrency for run %s",
            submission.id, getattr(run, "id", "?"), exc_info=True,
        )
        session.rollback()


def _add_card_normalized_tpm(result, submission, module_cls, module_name) -> None:
    """Augment a perf module's result with display-only card-normalized TPM
    metrics (see app.core.card_normalize). A module opts in by declaring a
    TestModule.card_norm_baseline; the computation runs in the worker because
    the card count lives on the Submission's hardware section, which the
    bench-layer modules never see. Appends matching extra_display_configs rows
    so the frontend renders the new keys; a no-op for modules without a
    baseline, failed runs, or results without recognizable throughput metrics."""
    baseline = getattr(module_cls, "card_norm_baseline", None)
    if not baseline:
        return
    if result is None or result.error or not isinstance(result.metrics, dict):
        return
    from app.core.card_normalize import card_normalized_tpm, total_card_count

    cards = total_card_count(
        getattr(submission, "cards_per_machine", None),
        getattr(submission, "machine_count", None),
    )
    normalized = {
        k: v for k, v in card_normalized_tpm(result.metrics, cards, baseline).items()
        if k not in result.metrics
    }
    if not normalized:
        return
    result.metrics.update(normalized)
    result.extra_display_configs.extend({"key": key, "role": "display"} for key in normalized)
    logger.info(
        "Submission %d module %s: card-normalized TPM (baseline=%d, cards=%s%s): %s",
        submission.id, module_name, baseline,
        cards if cards is not None else baseline,
        "" if cards is not None else " assumed",
        ", ".join(f"{k}={v:.0f}" for k, v in normalized.items()),
    )


def _add_dataset_identity(result, run, module_cls, module_name) -> None:
    """Stamp WHICH dataset a run replayed onto its metrics, as display-only keys.

    Computed in the worker for the same reason card-normalization is: the module
    is handed a path and never sees the build behind it. The source is the
    provenance block `_resolve_dataset_feed` already pinned onto the run, so the
    metric and the params snapshot can never disagree.

    This matters more for a rolling dataset than it looks. The dataset changes
    under the benchmark, and two submissions started minutes apart can replay
    different traffic — so "same score" is only meaningful between runs that
    share these keys. Putting it in `metrics` (not just params) means it shows up
    where people actually compare runs.

    A fixed dataset gets `dataset_id` (the filename) but no hash: those files are
    multi-GB and hashing one per run is real I/O for little gain.
    """
    if module_cls.dataset_feed_fields() is None:
        return
    if result is None or result.error or not isinstance(result.metrics, dict):
        return

    params = run.params_json if isinstance(run.params_json, dict) else {}
    resolved = params.get("dataset_resolved")
    identity: dict = {}
    if isinstance(resolved, dict):
        identity["dataset_id"] = str(resolved.get("build_id") or "")
        sha = str(resolved.get("sha256") or "")
        identity["dataset_sha256"] = sha[:16]
    else:
        fields = module_cls.dataset_feed_fields()
        path = str(params.get(fields.path) or "")
        if not path:
            return
        identity["dataset_id"] = os.path.basename(path)
        identity["dataset_sha256"] = ""

    identity = {k: v for k, v in identity.items() if k not in result.metrics}
    if not identity:
        return
    result.metrics.update(identity)
    result.extra_display_configs.extend({"key": key, "role": "display"} for key in identity)
    logger.info(
        "Submission %s module %s: dataset identity %s",
        getattr(run, "submission_id", "?"), module_name,
        ", ".join(f"{k}={v or '-'}" for k, v in identity.items()),
    )


def _merge_extra_display_configs(metric_configs: list, result) -> list:
    """Append a module's runtime display-only config rows (ModuleResult.
    extra_display_configs) to the run's metric-config snapshot, so metrics with
    dynamic keys (e.g. perf_guidellm_sweep's per-level `*_c{N}`) render in the
    frontend, which only shows configured keys.

    Guard rails: only role="display" rows are accepted (modules must never
    influence score/redline evaluation this way) and keys already configured —
    by the admin or module defaults — are left untouched."""
    extra = getattr(result, "extra_display_configs", None) or []
    if not extra:
        return metric_configs
    existing_keys = {c.get("key") for c in metric_configs if isinstance(c, dict)}
    merged = list(metric_configs)
    for c in extra:
        if not isinstance(c, dict) or c.get("role") != "display":
            continue
        key = c.get("key")
        if not key or key in existing_keys:
            continue
        existing_keys.add(key)
        merged.append(c)
    return merged


# Per-module wall-clock backstops, in seconds. Deliberately generous — these are
# runaway guards, not schedules, and a healthy run should never come close. One
# flat value cannot work here: anything low enough to be useful for a 60-second
# module would abort opencompass, whose own default budget is 16h. Override any
# entry with MODULE_TIME_CAP_<MODULE_NAME_UPPERCASED>, e.g.
# MODULE_TIME_CAP_REPLAY=90000.
MODULE_TIME_CAPS_SECONDS = {
    "functional_acceptance":  1 * 3600,
    "case_truncation":        1 * 3600,
    "perf_guidellm":          4 * 3600,
    "perf_guidellm_sweep":    8 * 3600,
    "replay":        24 * 3600,
    "opencompass":           48 * 3600,
}
MODULE_TIME_CAP_DEFAULT_SECONDS = float(
    os.getenv("MODULE_TIME_CAP_DEFAULT_SECONDS", str(4 * 3600))
)
# A module can be CONFIGURED to run longer than its table entry — replay's
# max_seconds alone accepts up to 7 days — so the table value is a floor, not a
# ceiling. The effective cap is the larger of it and the module's own declared
# budget plus slack (TestModule.time_budget_seconds). The backstop must never sit
# below the bound the module itself enforces, or it would abort a perfectly
# healthy run before that module's own, cleaner stop could fire.
MODULE_TIME_CAP_SLACK = float(os.getenv("MODULE_TIME_CAP_SLACK", "1.5"))
MODULE_TIME_CAP_HEADROOM_SECONDS = float(os.getenv("MODULE_TIME_CAP_HEADROOM_SECONDS", "1800"))


def _module_time_cap(module_cls, params, module_name: str) -> float:
    """Wall-clock cap for one module run — see MODULE_TIME_CAPS_SECONDS above."""
    floor = float(MODULE_TIME_CAPS_SECONDS.get(module_name, MODULE_TIME_CAP_DEFAULT_SECONDS))
    override = os.getenv(f"MODULE_TIME_CAP_{module_name.upper()}")
    if override:
        try:
            floor = float(override)
        except ValueError:
            logger.warning(
                "Ignoring non-numeric MODULE_TIME_CAP_%s=%r", module_name.upper(), override
            )

    derived = 0.0
    try:
        budget = module_cls.time_budget_seconds(params)
        if budget and budget > 0:
            derived = budget * MODULE_TIME_CAP_SLACK + MODULE_TIME_CAP_HEADROOM_SECONDS
    except Exception:
        logger.warning(
            "module %s: time_budget_seconds() raised; using the configured cap only",
            module_name, exc_info=True,
        )

    return max(floor, derived)


# Grace after the cap fires. Setting abort_event only ASKS the module to stop —
# it must cooperatively observe the event (via the progress callback, or its own
# cancel_event checks). A module that ignores it — a C-level hang, a library
# deadlock like guidellm's teardown — would otherwise block the worker fork
# forever while the heartbeat thread keeps beating, so the reaper (which only
# sees MISSING heartbeats) never notices: the exact "hung-but-alive worker" gap.
# This is how long we wait for a cooperative return before concluding the module
# is not listening and hard-exiting the fork. Because dramatiq runs one task per
# process (--threads 1), that abandons ONLY this submission; the master respawns
# the fork and the reaper marks the submission FAILED via the missing heartbeat —
# the existing, well-tested crash path. 0 disables the escalation (cooperative
# only — the pre-2026-07-24 behaviour).
MODULE_ABORT_GRACE_SECONDS = float(os.getenv("MODULE_ABORT_GRACE_SECONDS", "300"))
# Distinctive exit code so a self-terminated wedge is greppable and not confused
# with an OOM kill (137) or a clean exit (0).
WORKER_WEDGE_EXIT_CODE = 75


def _worker_hard_exit(code: int) -> None:  # pragma: no cover - terminates the process
    """Terminate this worker process immediately. Isolated so tests can stub it."""
    os._exit(code)


def _safe_checkpoint_worker_log(submission_id: int) -> None:
    """Best-effort worklog flush; never raises, never blocks the caller for long."""
    try:
        from app.queue.log_capture import checkpoint_worker_log
        checkpoint_worker_log(submission_id)
    except Exception:
        pass


def _start_module_deadline(submission_id: int, module_name: str, cancel_event, cap_seconds: float):
    """Return ``(abort_event, timed_out, stop)`` governing one module run.

    A module stops only cooperatively, through the event it is handed, so the
    time cap has to reach it by the same route as a user cancel. Both are folded
    into one ``abort_event``, and ``timed_out`` records which one fired — without
    that the caller would mislabel a runaway module as "Canceled" and hide the
    very thing worth alerting on.

    Enforcement (MODULE_ABORT_GRACE_SECONDS): after abort is signalled, a module
    that does not return within the grace window is not observing its abort, so
    the worker process is hard-exited to free the slot. See the grace constant.
    """
    abort = threading.Event()
    timed_out = threading.Event()
    stop = threading.Event()
    deadline = (time.monotonic() + cap_seconds) if cap_seconds > 0 else None

    def _hard_exit(reason: str) -> None:
        logger.critical(
            "Submission %d module %s did not stop within %.0fs of %s — the module is not "
            "observing its abort signal; hard-exiting worker pid=%d (code %d) to free the "
            "slot. The dramatiq master will respawn it; the reaper marks the submission "
            "FAILED (missing heartbeat).",
            submission_id, module_name, MODULE_ABORT_GRACE_SECONDS, reason,
            os.getpid(), WORKER_WEDGE_EXIT_CODE,
        )
        # Flush the worker log so the wedge is diagnosable, but never let that
        # block the exit — the wedge may be in the DB path too, so bound it.
        flush = threading.Thread(
            target=_safe_checkpoint_worker_log, args=(submission_id,), daemon=True
        )
        flush.start()
        flush.join(timeout=10)
        _worker_hard_exit(WORKER_WEDGE_EXIT_CODE)

    def _poll() -> None:
        while not stop.wait(1.0):
            if cancel_event.is_set():
                abort.set()
                reason = "cancellation"
            elif deadline is not None and time.monotonic() >= deadline:
                timed_out.set()
                abort.set()
                reason = "the %.0fs module time cap" % cap_seconds
                logger.error(
                    "Submission %d module %s exceeded the %.0fs module time cap — aborting",
                    submission_id, module_name, cap_seconds,
                )
            else:
                continue
            # Abort signalled. Give the module the grace window to unwind on its
            # own (which preserves partial results and a clean FAILED); escalate
            # to a hard exit only if it does not.
            if MODULE_ABORT_GRACE_SECONDS <= 0 or stop.wait(MODULE_ABORT_GRACE_SECONDS):
                return  # disabled, or the module returned and the loop's finally set stop
            _hard_exit(reason)
            return

    thread = threading.Thread(
        target=_poll, daemon=True, name=f"module-deadline-{submission_id}-{module_name}"
    )
    thread.start()

    def _stop() -> None:
        stop.set()
        thread.join(timeout=5)

    return abort, timed_out, _stop


OUTPUT_DIR_PREFIX = "llmbench_submission_"
# What to do with a submission's scratch dir when it finishes:
#   on-failure (default) — delete after a clean run, keep it when something went
#                          wrong, which is exactly when the artifacts are wanted
#   always               — never delete (old behaviour; leaks, see below)
#   never                — always delete, even on failure
OUTPUT_DIR_KEEP = os.getenv("LLMBENCH_OUTPUT_KEEP", "on-failure").strip().lower()
# How long a KEPT dir survives. "Keep on failure" alone still leaks: nothing ever
# removed them, workers are long-lived, and the container has no
# ephemeral-storage limit — so they pile up until the NODE hits DiskPressure and
# the kubelet evicts pods (postgres shares that node). One replay with
# save_responses=true leaves ~1.4GB. 0 disables the sweep.
OUTPUT_DIR_RETENTION_HOURS = float(os.getenv("LLMBENCH_OUTPUT_RETENTION_HOURS", "72"))


def _output_dir_submission_id(name: str) -> "int | None":
    """Recover the submission id from a scratch-dir name, or None if malformed."""
    try:
        return int(name[len(OUTPUT_DIR_PREFIX):].split("_", 1)[0])
    except (ValueError, IndexError):
        return None


def _sweep_stale_output_dirs() -> None:
    """Delete leftover submission scratch dirs older than the retention window.

    Never deletes a dir whose submission is still QUEUED/RUNNING. That check is
    not paranoia: /tmp is shared by every dramatiq fork in the pod, and an old
    mtime is NOT a liveness signal — a single module can legitimately run for
    many hours without touching its parent dir (the parent's mtime only moves
    when a module subdir is created). Retention is 72h by default while the
    module caps now sum to ~86h, so mtime alone would eventually delete a live
    run's artifacts out from under it.

    Best-effort and never raises. Fails SAFE: if the liveness check cannot be
    made, nothing is swept — leaking disk is recoverable, deleting a running
    submission's evidence is not.
    """
    if OUTPUT_DIR_RETENTION_HOURS <= 0:
        return
    import shutil
    import time

    cutoff = time.time() - OUTPUT_DIR_RETENTION_HOURS * 3600
    root = tempfile.gettempdir()

    candidates: list[tuple[int | None, str]] = []
    try:
        for name in os.listdir(root):
            if not name.startswith(OUTPUT_DIR_PREFIX):
                continue
            path = os.path.join(root, name)
            try:
                if not os.path.isdir(path) or os.path.getmtime(path) >= cutoff:
                    continue
            except OSError:
                continue
            candidates.append((_output_dir_submission_id(name), path))
    except Exception:
        logger.warning("output-dir sweep could not list %s", root, exc_info=True)
        return

    if not candidates:
        return

    ids = [sid for sid, _ in candidates if sid is not None]
    try:
        live = _non_terminal_submission_ids(ids)
    except Exception:
        logger.warning("output-dir sweep skipped: liveness check failed", exc_info=True)
        return

    swept = 0
    for sid, path in candidates:
        if sid is not None and sid in live:
            continue
        try:
            shutil.rmtree(path, ignore_errors=True)
            swept += 1
        except OSError:
            continue
    if swept:
        logger.info(
            "Swept %d submission output dir(s) older than %.0fh", swept, OUTPUT_DIR_RETENTION_HOURS
        )


def _non_terminal_submission_ids(ids: list) -> set:
    """Subset of `ids` whose submissions are still QUEUED or RUNNING."""
    if not ids:
        return set()
    from app.db.models import Submission, SubmissionStatus, get_sync_session

    session = get_sync_session()
    try:
        rows = (
            session.query(Submission.id)
            .filter(
                Submission.id.in_(ids),
                Submission.status.in_(
                    [SubmissionStatus.QUEUED, SubmissionStatus.RUNNING]
                ),
            )
            .all()
        )
        return {row[0] for row in rows}
    finally:
        session.close()


def _cleanup_output_dir(submission_id: int, output_dir: str, results: list) -> None:
    """Drop a submission's scratch dir unless it still has debug value.

    On a clean run the artifacts are redundant — every number the run produced is
    already in the DB — while on a failure they are the whole diagnosis. So the
    default keeps them only when something failed, and the retention sweep ages
    those out so "keep" does not silently become "leak".
    """
    import shutil

    if OUTPUT_DIR_KEEP == "always":
        return
    # An EMPTY results list is a failure, not a success: it means the run never
    # got a module far enough to record one — a crash, a cancel, or a config
    # error. `any()` over an empty list is False, so without the explicit check
    # the artifacts would be deleted in exactly the case they are most wanted.
    failed = (not results) or any(
        getattr(r, "error", None) or not getattr(r, "passed", False) for r in results
    )
    if failed and OUTPUT_DIR_KEEP != "never":
        logger.info(
            "Keeping output dir for submission %d (run had failures): %s",
            submission_id, output_dir,
        )
        return
    try:
        shutil.rmtree(output_dir, ignore_errors=True)
    except Exception:
        logger.warning("Failed to remove output dir %s", output_dir, exc_info=True)


def _run_modules(session, submission, modules, endpoint) -> None:
    """Execute a list of module defs against an endpoint and update the DB."""
    _sweep_stale_output_dirs()
    _sweep_stale_dataset_pins()

    results: list[ModuleResult] = []
    output_dir = tempfile.mkdtemp(prefix=f"{OUTPUT_DIR_PREFIX}{submission.id}_")
    cancel_event, stop_event, watcher = _start_cancel_watcher(submission.id)

    try:
        _run_module_loop(
            session, submission, modules, endpoint,
            results, output_dir, cancel_event,
        )
    finally:
        stop_event.set()
        watcher.join(timeout=5)
        if watcher.is_alive():
            logger.warning("cancel-watcher for submission %d did not exit cleanly", submission.id)
        _cleanup_output_dir(submission.id, output_dir, results)


def _run_module_loop(
    session, submission, modules, endpoint,
    results, output_dir, cancel_event,
) -> None:
    """Inner module loop. Kept separate so _run_modules can wrap it in try/finally
    for watcher cleanup without re-indenting the whole body on every refactor."""
    from app.db.models import SubmissionRun, ModuleRunStatus, SubmissionStatus

    from app.queue.log_capture import checkpoint_worker_log

    # Tracks whether the immediately-preceding module in run order blocked the
    # chain (failed, was skipped, or breached a redline). Drives the
    # skip_if_prev_failed cascade below.
    prev_blocked = False

    for mod in modules:
        module_name = getattr(mod, "module_name", None) or getattr(mod, "name", None)
        params_json = mod.params_json
        benchmark_module_id = getattr(mod, "id", None)
        order_index = getattr(mod, "order_index", 0)
        skip_flag = bool(getattr(mod, "skip_if_prev_failed", False))
        # Disambiguate output dir when the same module appears multiple times
        dir_suffix = f"{module_name}_{benchmark_module_id}" if benchmark_module_id else module_name

        # Check for cancellation before starting each module
        session.refresh(submission)
        if submission.status == SubmissionStatus.CANCELED:
            logger.info("Submission %d canceled before module %s — stopping", submission.id, module_name)
            break

        # Resolve (or create) this module's run row up front so it can be marked
        # SKIPPED / RUNNING / FAILED consistently below.
        # Key runs by benchmark_module_id when available (supports duplicate modules)
        if benchmark_module_id is not None:
            run = session.query(SubmissionRun).filter_by(
                submission_id=submission.id, benchmark_module_id=benchmark_module_id
            ).first()
        else:
            run = session.query(SubmissionRun).filter_by(
                submission_id=submission.id, module_name=module_name
            ).first()
        if run is None:
            run = SubmissionRun(
                submission_id=submission.id,
                benchmark_module_id=benchmark_module_id,
                module_name=module_name,
                params_json=params_json,
            )
            session.add(run)
            session.flush()

        # Cascade skip: short-circuit this module (and, transitively, the rest of
        # the chain) when the immediately-preceding module blocked.
        if skip_flag and prev_blocked:
            logger.info(
                "Submission %d skipping module %s (bm_id=%s order=%d) — an earlier "
                "module in the chain failed",
                submission.id, module_name, benchmark_module_id, order_index,
            )
            _skip_run(session, run, "Skipped — an earlier module in the chain failed.")
            results.append(_SKIPPED_RESULT)
            prev_blocked = True  # a skip cascades to the next module
            checkpoint_worker_log(submission.id)
            continue

        logger.info("Submission %d starting module %s (bm_id=%s order=%d)",
                    submission.id, module_name, benchmark_module_id, order_index)
        run.status = ModuleRunStatus.RUNNING
        run.started_at = datetime.utcnow()
        session.commit()

        result: ModuleResult | None = None
        if module_name is None:
            _fail_run(session, run, "Module name is None")
            results.append(ModuleResult(error="Module name is None", passed=False, score=0.0))
            prev_blocked = True
            checkpoint_worker_log(submission.id)
            continue

        try:
            module_cls = get_module(module_name)
        except KeyError:
            _fail_run(session, run, f"Unknown module {module_name!r}")
            results.append(ModuleResult(error=f"Unknown module {module_name!r}", passed=False, score=0.0))
            prev_blocked = True
            checkpoint_worker_log(submission.id)
            continue

        timed_out = None
        stop_deadline = None
        try:
            params = module_cls.ParamsSchema(**params_json)
            # Pin a rolling dataset build BEFORE anything else touches params, so
            # the run's dataset can't change under it mid-flight.
            params = _resolve_dataset_feed(session, run, module_cls, params, module_name)
            params = _apply_concurrency_override(module_cls, params, submission, module_name)
            _record_applied_concurrency(session, run, module_cls, params, submission)
            # Fold the user-cancel signal and the module time cap into one event
            # (see _start_module_deadline) and hand THAT to the module.
            time_cap = _module_time_cap(module_cls, params, module_name)
            abort_event, timed_out, stop_deadline = _start_module_deadline(
                submission.id, module_name, cancel_event, time_cap
            )
            progress_cb = _make_progress_cb(submission.id, run.id, module_name, abort_event)
            # Close out any transaction the setup above opened (params, the
            # concurrency override, the run row) BEFORE handing control to a module
            # that can run for hours. Without this the connection would sit `idle
            # in transaction` for the whole run, blocking DDL on submissions and
            # pinning autovacuum. expire_on_commit=False keeps the already-loaded
            # attributes usable afterwards, so this costs nothing.
            session.commit()
            try:
                result = module_cls().run(
                    endpoint=endpoint,
                    params=params,
                    output_dir=os.path.join(output_dir, dir_suffix),
                    progress_cb=progress_cb,
                    cancel_event=abort_event,
                )
            except TypeError:
                result = module_cls().run(
                    endpoint=endpoint,
                    params=params,
                    output_dir=os.path.join(output_dir, dir_suffix),
                    progress_cb=progress_cb,
                )

            _add_card_normalized_tpm(result, submission, module_cls, module_name)
            _add_dataset_identity(result, run, module_cls, module_name)

            # Resolve metric_configs: use benchmark-level config, fall back to module defaults
            from dataclasses import asdict
            metric_configs: list = getattr(mod, "metric_configs_json", None) or []
            if not metric_configs:
                metric_configs = [asdict(mc) for mc in module_cls.default_metric_configs]
            metric_configs = _merge_extra_display_configs(metric_configs, result)

            if result.error:
                result.score = 0.0
                result.passed = False
            else:
                result.score, result.passed = evaluate(result.metrics, metric_configs)

            _complete_run(session, run, result, metric_configs)
            logger.info(
                "Submission %d module %s done — passed=%s score=%.4f",
                submission.id, module_name, result.passed, result.score,
            )
        except (SubmissionCancelled, BenchmarkCancelled):
            # The module was handed one event for both signals, so disambiguate:
            # a blown time cap is a module FAILURE worth surfacing, not a cancel.
            if timed_out is not None and timed_out.is_set():
                msg = (
                    f"Module exceeded the worker's {time_cap:.0f}s time cap and was "
                    f"aborted"
                )
                logger.error("Submission %d module %s: %s", submission.id, module_name, msg)
                _fail_run(session, run, msg)
                result = ModuleResult(error=msg, passed=False, score=0.0)
                # Fall through: record it and let the skip-cascade handle the rest,
                # exactly as any other module failure would.
            else:
                logger.info(
                    "Submission %d module %s aborted mid-run (canceled)", submission.id, module_name
                )
                _fail_run(session, run, "Canceled")
                break
        except Exception as exc:
            logger.exception("Submission %d module %s failed: %s", submission.id, module_name, exc)
            _fail_run(session, run, str(exc))
            result = ModuleResult(error=str(exc), passed=False, score=0.0)
        finally:
            if stop_deadline is not None:
                stop_deadline()
            # Release the dataset pin as soon as THIS module is done, not at the
            # end of the submission: a later module can run for many more hours,
            # and holding a multi-GB dataset alive for that whole time is exactly
            # the disk pressure the retention policy exists to avoid.
            _release_dataset_pin(submission.id, run)

        results.append(result)
        # Did this module block the chain? Read from the committed run row — its
        # status/passed were just set authoritatively by _complete_run/_fail_run.
        prev_blocked = _module_blocks_chain(run)

        # Durably flush the worker log at each module boundary, so a hard crash
        # in a later module still leaves the log through the modules that
        # finished — pinning which module killed the worker.
        checkpoint_worker_log(submission.id)

    # Re-read status in case it was canceled while the worker was running
    session.refresh(submission)

    # Aggregate scores (do this AFTER refresh so the values survive the commit)
    if results:
        weighted = 0.0
        total_weight = Decimal("0")
        passed = True

        if submission.benchmark_id is not None:
            for mod, result in zip(modules, results):
                if result is _SKIPPED_RESULT:
                    # A skipped module is excluded from the weighted average (it
                    # produced no score), but it means a prior module failed, so
                    # the submission as a whole did not pass.
                    passed = False
                    continue
                w = Decimal(str(getattr(mod, "weight", 1.0)))
                weighted += (result.score or 0.0) * float(w)
                total_weight += w
                if not result.passed:
                    passed = False

            submission.score_total = round(weighted / float(total_weight) if total_weight else 0, 5)
        else:
            submission.score_total = results[0].score if results else None
            passed = results[0].passed if results else False

        submission.passed = passed

    if submission.status != SubmissionStatus.CANCELED:
        submission.status = SubmissionStatus.DONE
        submission.finished_at = datetime.utcnow()
        logger.info(
            "Submission %d finished — passed=%s score=%s modules_run=%d",
            submission.id, submission.passed, submission.score_total, len(results),
        )
    else:
        logger.info("Submission %d worker exiting — already marked CANCELED", submission.id)
    session.commit()


def _fail_run(session, run, error: str) -> None:
    from app.db.models import ModuleRunStatus
    run.status = ModuleRunStatus.FAILED
    run.passed = False
    run.error = error
    run.finished_at = datetime.utcnow()
    session.commit()
    _publish_run_event(run.submission_id, {
        "event": "run_complete",
        "run_id": run.id,
        "module_name": run.module_name,
        "status": "failed",
        "score": None,
        "passed": False,
        "metrics": None,
        "error": error,
    })


def _skip_run(session, run, reason: str) -> None:
    """Mark a run SKIPPED (not executed) because the chain blocked upstream.

    Distinct from _fail_run: nothing ran, so there is no error — the reason is
    stored in `error` only as the human-readable explanation the UI shows in a
    neutral (non-error) style. passed/score stay null (not evaluated)."""
    from app.db.models import ModuleRunStatus
    run.status = ModuleRunStatus.SKIPPED
    run.passed = None
    run.score = None
    run.error = reason
    run.finished_at = datetime.utcnow()
    session.commit()
    _publish_run_event(run.submission_id, {
        "event": "run_complete",
        "run_id": run.id,
        "module_name": run.module_name,
        "status": "skipped",
        "score": None,
        "passed": None,
        "metrics": None,
        "error": reason,
    })


def _module_blocks_chain(run) -> bool:
    """True if this run's outcome should skip a `skip_if_prev_failed` successor."""
    from app.queue.skip_logic import blocks_chain
    return blocks_chain(run.status, run.passed)


def _fail_submission(session, submission, error: str) -> None:
    from app.db.models import SubmissionStatus
    submission.status = SubmissionStatus.FAILED
    submission.error = error
    submission.finished_at = datetime.utcnow()
    session.commit()


def _complete_run(session, run, result: ModuleResult, metric_configs: list | None = None) -> None:
    from app.db.models import ModuleRunStatus
    run.status = ModuleRunStatus.DONE
    run.score = result.score
    run.passed = result.passed
    run.metrics_json = result.metrics
    run.metric_configs_json = metric_configs or []
    run.error = result.error
    run.artifact_path = result.artifacts[0] if result.artifacts else None
    run.finished_at = datetime.utcnow()
    session.commit()
    from app.core.metric_configs import merge_display_defaults
    _publish_run_event(run.submission_id, {
        "event": "run_complete",
        "run_id": run.id,
        "module_name": run.module_name,
        "status": "done",
        "score": result.score,
        "passed": result.passed,
        "metrics": result.metrics,
        "metric_configs": merge_display_defaults(run.module_name, metric_configs),
        "error": result.error,
    })
