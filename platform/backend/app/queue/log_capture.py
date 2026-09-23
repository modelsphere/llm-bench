"""Per-submission worker log capture → gzipped DB column.

Each submission runs in its own dramatiq worker process (run_worker.py uses
``--processes N --threads 1``), one task at a time. So a logging handler
attached to the ROOT logger for the duration of one submission captures exactly
that submission's output — dramatiq itself configures the root logger in each
worker with the same line format you see in pod logs. We buffer that output
(bounded, most-recent-wins), gzip it, and persist it to
``Submission.worker_log_gz`` so users can download the full worker log after a
run instead of shelling into the pod.

Isolation (no cross-submission / cross-user bleed):
  * A FRESH handler + buffer is created per submission and removed in a
    ``finally``, so submission B starts with an empty buffer and never inherits
    submission A's lines. Any stale CaptureHandler is also stripped from root on
    enter (belt-and-suspenders for a skipped teardown).
  * Flushes only ever write to the buffer's OWN submission id;
    ``checkpoint_worker_log`` no-ops unless the active handler's id matches.

Crash resilience: besides the final flush, a periodic timer flushes the buffer
to the DB every ``WORKER_LOG_FLUSH_SECONDS`` and the module loop flushes at each
module boundary. So a hard worker crash (OOM/segfault) still leaves the log
through the last completed module, revealing which module killed the worker.

Retention across attempts: a submission can be run more than once (Dramatiq
retry, or a broker redelivery after a hard kill), and every flush REPLACES the
whole ``worker_log_gz`` column — so a retry used to erase the crash log within
seconds of starting, exactly when it was most wanted. Each attempt now reads the
stored log back on entry and carries it as an immutable prefix ahead of its own
lines, bounded by ``WORKER_LOG_CARRY_FRACTION`` of the cap. The blob therefore
stays within ``WORKER_LOG_MAX_BYTES`` no matter how many attempts run: history
is re-trimmed into one fixed reservation each time rather than appended to.

Known limitation: log lines emitted by guidellm-FORKED child processes (the same
reason the HTTPX_STATUS_DUMP_DIR per-PID workaround exists) live in a separate
interpreter and are not captured here.

Accepted cross-submission edge (do NOT rely on threads=1 to prevent it): when a
submission is CANCELED/times-out, worker threads it left draining can emit a few
log lines *after* its capture handler is detached, so those lines land in the
NEXT submission's buffer in this process. This is not replay-specific — it
applies to any module that abandons in-flight threads on cancel: replay and
opencompass shut their pools down with ``wait=False``; case_truncation and
agentic use bounded ``join(timeout=…)`` and abandon stragglers. We accept it
rather than route logs by thread ownership (which would mean tagging every
module's thread pool) because the leaked content is verified METADATA ONLY —
request ids, HTTP status, timings, token counts, finish reasons — never prompt
or response bodies (raw bodies go to the debug-dump file, not the logger).
"""
from __future__ import annotations

import collections
import gzip
import logging
import os
import threading
import time
from contextlib import contextmanager
from typing import Deque, Tuple

# Matches dramatiq/cli.py LOGFORMAT so the downloaded log reads identically to
# stdout. (Hardcoded rather than imported — dramatiq doesn't export it stably.)
_LOG_FORMAT = "[%(asctime)s] [PID %(process)d] [%(threadName)s] [%(name)s] [%(levelname)s] %(message)s"

DEFAULT_MAX_BYTES = int(os.getenv("WORKER_LOG_MAX_BYTES", str(4 * 1024 * 1024)))  # ~4 MB raw text
DEFAULT_FLUSH_SECONDS = float(os.getenv("WORKER_LOG_FLUSH_SECONDS", "30"))

# Share of DEFAULT_MAX_BYTES reserved for the log of PRIOR attempts when a
# submission is re-run (see capture_submission_logs). The live attempt gets
# whatever is left, so the persisted log NEVER exceeds the cap however many
# times a submission is retried: each attempt re-trims the carried tail into the
# same fixed reservation instead of appending to an ever-growing blob. Set to 0
# to disable retention (the pre-2026-09 behaviour, where a retry's first flush
# overwrote the crashed attempt's log).
WORKER_LOG_CARRY_FRACTION = min(
    0.9, max(0.0, float(os.getenv("WORKER_LOG_CARRY_FRACTION", "0.25")))
)
# Floor on the live attempt's own budget: carried history must never squeeze the
# running attempt's log down to nothing, whatever the fraction works out to.
_MIN_LIVE_BYTES = int(os.getenv("WORKER_LOG_MIN_LIVE_BYTES", str(64 * 1024)))
# Reading the prior blob back is best-effort, but a transient failure costs
# exactly the crash log we are trying to preserve, so allow one cheap retry.
_CARRY_READ_ATTEMPTS = 2

_CARRY_SEPARATOR = (
    "===== end of the previous attempt's log; this attempt's log continues below "
    "=====\n"
)
_CARRY_ELIDED = (
    "[... earlier lines of the previous attempt(s) dropped to keep the worker log "
    "within its size cap ...]\n"
)
_CARRY_READ_FAILED = (
    "[worker-log retention: could not read this submission's previously stored log; "
    "if this run is a retry, the earlier attempt's log is not preserved here - see "
    "the worker pod's stdout]\n"
)

# Retry the best-effort DB write a few times before giving up. When PGDATA lives
# on a network/FUSE volume (e.g. JuiceFS), a slow write can be interrupted
# mid-syscall by a backend timer (SIGALRM), which psycopg2 surfaces as
# InternalError: could not extend file "..." Interrupted system call. That's
# transient — the next attempt almost always succeeds — so a small retry saves
# the log instead of dropping it. Tunable per-deployment via env.
PERSIST_MAX_ATTEMPTS = max(1, int(os.getenv("WORKER_LOG_PERSIST_ATTEMPTS", "3")))
PERSIST_RETRY_BACKOFF = float(os.getenv("WORKER_LOG_PERSIST_BACKOFF", "0.5"))  # seconds, ×attempt

_log = logging.getLogger(__name__)


def _nbytes(text: str) -> int:
    """Encoded size of ``text`` — the unit every budget in this module is in."""
    return len(text.encode("utf-8", "replace"))


def _tail_lines_within(text: str, budget: int) -> str:
    """Return the longest whole-line SUFFIX of ``text`` fitting in ``budget`` bytes.

    Suffix, not prefix, for the same reason BoundedLogBuffer evicts from the
    front: the interesting part of a crashed run is at the end. Cutting on line
    boundaries keeps the result parseable (and can never split a multi-byte
    character mid-sequence); a single line longer than the whole budget yields
    "", which callers treat as "nothing carried".
    """
    if budget <= 0 or not text:
        return ""
    out: list[str] = []
    total = 0
    for line in reversed(text.splitlines(keepends=True)):
        n = _nbytes(line)
        if total + n > budget:
            break
        out.append(line)
        total += n
    out.reverse()
    return "".join(out)


class BoundedLogBuffer:
    """Thread-safe ring buffer of formatted log lines, capped by total bytes.

    Keeps the MOST RECENT lines (evicts oldest) — a crash's context is at the
    end. ``version`` bumps on every append so the flush timer can skip no-op
    writes.

    ``prefix`` is immutable carried-over text — the previous attempt's log, on a
    re-run — emitted ahead of the live lines. It is charged against ``max_bytes``
    UP FRONT, leaving the live ring only the remainder, so ``getvalue()`` stays
    bounded by ``max_bytes`` no matter how much history is carried. A prefix
    over the allowance is trimmed here as a last resort, so the bound holds even
    if a caller miscomputes the budget.
    """

    def __init__(
        self,
        max_bytes: int = DEFAULT_MAX_BYTES,
        prefix: str = "",
        prefix_truncated: bool = False,
    ) -> None:
        # Reserve the live floor before anything else, then let the prefix take
        # at most what remains. The floor scales down for a cap smaller than it,
        # so a tiny WORKER_LOG_MAX_BYTES still leaves room for the header rather
        # than silently dropping the line that says which attempt this is.
        allowance = max(0, max_bytes - min(_MIN_LIVE_BYTES, max_bytes // 2))
        if prefix and _nbytes(prefix) > allowance:
            prefix = _tail_lines_within(prefix, allowance)
            prefix_truncated = True
        self.prefix = prefix          # immutable after __init__ — safe to read unlocked
        self.prefix_truncated = prefix_truncated
        self.max_bytes = max(0, max_bytes - _nbytes(prefix))
        self._lines: Deque[Tuple[str, int]] = collections.deque()  # (line, nbytes)
        self._bytes = 0
        self._lock = threading.Lock()
        self.truncated = False
        self.version = 0
        self.persisted_version = -1  # last version successfully written to the DB

    def append(self, line: str) -> None:
        nbytes = len(line.encode("utf-8", "replace"))
        with self._lock:
            self._lines.append((line, nbytes))
            self._bytes += nbytes
            self.version += 1
            while self._bytes > self.max_bytes and len(self._lines) > 1:
                _, dropped = self._lines.popleft()
                self._bytes -= dropped
                self.truncated = True

    def getvalue(self) -> Tuple[str, bool]:
        with self._lock:
            live = "".join(line for line, _ in self._lines)
        return self.prefix + live, (self.truncated or self.prefix_truncated)


class CaptureHandler(logging.Handler):
    """Root-logger handler that appends formatted records to a BoundedLogBuffer."""

    def __init__(self, submission_id: int, buffer: BoundedLogBuffer) -> None:
        super().__init__()
        self.submission_id = submission_id
        self.buffer = buffer

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.buffer.append(self.format(record) + "\n")
        except Exception:  # never let a logging failure escape into the actor
            self.handleError(record)


# Per-process registry of active capture handlers, keyed by submission id.
#
# Correctness depends on PROCESS ISOLATION (dramatiq `--threads 1`): with one
# task per process this dict holds at most one entry, every record the process
# emits belongs to that submission, and capture is both complete and leak-free.
# Under `--threads >1` multiple submissions run concurrently in one process and
# all share the root logger — capture would mix their lines. We can't fix that
# at the handler level (logging fans every record to every root handler), so we
# detect it and warn loudly; run with `--threads 1` for correct per-submission
# logs (the project default everywhere except, historically, docker-compose).
_active_lock = threading.Lock()
_active_handlers: dict[int, CaptureHandler] = {}
_warned_concurrent = False


def _carry_budget(max_bytes: int) -> int:
    """Bytes of the cap reserved for previous attempts' logs (0 = retention off)."""
    if WORKER_LOG_CARRY_FRACTION <= 0:
        return 0
    return min(int(max_bytes * WORKER_LOG_CARRY_FRACTION), max(0, max_bytes - _MIN_LIVE_BYTES))


def _load_prior_log(submission_id: int) -> Tuple[str, bool, bool]:
    """Read back this submission's stored log. Returns ``(text, truncated, failed)``.

    Read on EVERY run, not just ones we can identify as retries: the retry path
    that loses logs most often is a broker redelivery after a hard kill (OOM,
    SIGKILL, the abort-grace wedge exit), and those arrive with Dramatiq
    ``retries=0`` — indistinguishable from a first attempt at this point. On a
    genuine first attempt the column is NULL and this costs one trivial SELECT.

    ``failed`` distinguishes "nothing stored" from "could not tell", so the
    caller can say so in the log rather than silently discarding history.
    """
    from sqlalchemy import select

    from app.db.models import Submission, get_sync_session

    last_exc: BaseException | None = None
    for attempt in range(1, _CARRY_READ_ATTEMPTS + 1):
        session = None
        try:
            session = get_sync_session()
            row = session.execute(
                select(Submission.worker_log_gz, Submission.worker_log_truncated).where(
                    Submission.id == submission_id
                )
            ).one_or_none()
            if row is None or row[0] is None:
                return "", False, False
            return gzip.decompress(row[0]).decode("utf-8", "replace"), bool(row[1]), False
        except Exception as exc:
            last_exc = exc
            try:
                if session is not None:
                    session.rollback()
            except Exception:
                pass
        finally:
            if session is not None:
                session.close()
        if attempt < _CARRY_READ_ATTEMPTS:
            time.sleep(PERSIST_RETRY_BACKOFF)

    _log.warning(
        "Could not read the stored worker log for submission %s; a retry will not "
        "preserve the previous attempt's log",
        submission_id,
        exc_info=last_exc,
    )
    return "", False, True


def _build_carry_prefix(prior_text: str, budget: int) -> Tuple[str, bool]:
    """Fit ``prior_text`` plus its banner into ``budget``. Returns ``(prefix, trimmed)``.

    Everything the prefix will occupy — banner and elision note included — is
    charged against the budget here, so the caller's arithmetic stays exact.
    """
    if not prior_text or budget <= 0:
        return "", False
    room = budget - _nbytes(_CARRY_SEPARATOR)
    if room <= 0:
        return "", False  # cap too small to carry anything meaningfully
    if _nbytes(prior_text) <= room:
        return prior_text + _CARRY_SEPARATOR, False
    room -= _nbytes(_CARRY_ELIDED)
    tail = _tail_lines_within(prior_text, room) if room > 0 else ""
    if not tail:
        return "", True  # nothing of the old log survives; don't emit a bare banner
    return _CARRY_ELIDED + tail + _CARRY_SEPARATOR, True


def _persist(submission_id: int, buffer: BoundedLogBuffer) -> None:
    """Best-effort write of the buffer to submissions.worker_log_gz.

    Uses a FRESH sync session per attempt (the actor's session may be in a
    broken transaction after a crash, and a failed attempt aborts its own). A
    persist failure must never break the submission or mask the real exception
    — it is retried a few times (see PERSIST_MAX_ATTEMPTS) and, if it still
    fails, logged and swallowed. Skips the write entirely when nothing new has
    been buffered since the last successful persist, to avoid rewriting the
    same (TOAST-ed) blob on every module boundary.
    """
    version = buffer.version
    if version == buffer.persisted_version:
        return  # no new lines since last successful write — nothing to do

    try:
        text, truncated = buffer.getvalue()
        gz = gzip.compress(text.encode("utf-8", "replace"))
    except Exception:
        _log.warning("Failed to encode worker log for submission %s", submission_id, exc_info=True)
        return

    from app.db.models import Submission, get_sync_session

    last_exc: BaseException | None = None
    for attempt in range(1, PERSIST_MAX_ATTEMPTS + 1):
        session = None
        try:
            session = get_sync_session()
            session.query(Submission).filter_by(id=submission_id).update(
                {
                    Submission.worker_log_gz: gz,
                    Submission.worker_log_truncated: truncated,
                },
                synchronize_session=False,
            )
            session.commit()
            buffer.persisted_version = version
            return
        except Exception as exc:  # transient (e.g. FUSE EINTR) or otherwise — retry then swallow
            last_exc = exc
            try:
                if session is not None:
                    session.rollback()
            except Exception:
                pass
        finally:
            if session is not None:
                session.close()
        if attempt < PERSIST_MAX_ATTEMPTS:
            time.sleep(PERSIST_RETRY_BACKOFF * attempt)

    _log.warning(
        "Failed to persist worker log for submission %s after %d attempt(s)",
        submission_id,
        PERSIST_MAX_ATTEMPTS,
        exc_info=last_exc,
    )


def checkpoint_worker_log(submission_id: int) -> None:
    """Flush the active submission's buffer to the DB now (called at module boundaries)."""
    with _active_lock:
        handler = _active_handlers.get(submission_id)
    if handler is not None:
        _persist(submission_id, handler.buffer)


@contextmanager
def capture_submission_logs(
    submission_id: int,
    attempt_marker: str,
    max_bytes: int = DEFAULT_MAX_BYTES,
    flush_interval: float = DEFAULT_FLUSH_SECONDS,
    carry_prior: bool = True,
):
    """Attach a per-submission root-logger capture handler for the duration of a run.

    Periodically flushes to the DB (timer), and always does a final flush +
    detach in ``finally`` — so logs are persisted before the ``raise`` that
    triggers a dramatiq retry, and the handler never leaks across submissions.

    Retention across attempts: ``_persist`` overwrites the whole
    ``worker_log_gz`` column, so without this the first flush of a retry would
    erase the crashed attempt's log — the one you actually need. We therefore
    read the stored log back on entry and carry it as an immutable prefix, held
    to ``_carry_budget(max_bytes)`` while the live attempt keeps the rest. The
    persisted blob is bounded by ``max_bytes`` after any number of retries: each
    attempt re-trims the accumulated history into the SAME reservation, so the
    third attempt's carry costs no more than the second's. Best-effort
    throughout — every failure degrades to "no history carried", never to a
    failed run.
    """
    global _warned_concurrent
    root = logging.getLogger()

    # The marker, and any retention note, ride in the PREFIX rather than the ring:
    # they are the header that makes a multi-attempt log readable, and as ordinary
    # appends they were the first lines evicted once an attempt got chatty.
    header = attempt_marker + "\n"
    carry_prefix, carry_truncated = "", False
    if carry_prior:
        try:
            prior_text, prior_truncated, failed = _load_prior_log(submission_id)
            if failed:
                header = _CARRY_READ_FAILED + header
            else:
                carry_prefix, trimmed = _build_carry_prefix(
                    prior_text, _carry_budget(max_bytes) - _nbytes(header)
                )
                carry_truncated = prior_truncated or trimmed
        except Exception:  # never let log retention break a run
            _log.warning(
                "Worker-log retention failed for submission %s; continuing without "
                "the previous attempt's log",
                submission_id,
                exc_info=True,
            )

    buffer = BoundedLogBuffer(
        max_bytes, prefix=carry_prefix + header, prefix_truncated=carry_truncated
    )
    handler = CaptureHandler(submission_id, buffer)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT))

    with _active_lock:
        # Detect threaded concurrency (--threads >1): another submission is
        # already capturing in this process. Warn once — isolation needs
        # process-per-task. Don't remove the sibling's handler (that would break
        # its capture); the mix is unavoidable under threads.
        others = [sid for sid in _active_handlers if sid != submission_id]
        if others and not _warned_concurrent:
            _warned_concurrent = True
            _log.warning(
                "worker-log capture sees concurrent submissions %s in one process — "
                "per-submission log isolation requires dramatiq --threads 1 (process "
                "isolation). Logs may be incomplete or mixed until that's fixed.",
                sorted(others + [submission_id]),
            )
        # Strip only truly-orphaned CaptureHandlers (left by a teardown a hard
        # kill skipped) — never an actively-registered sibling.
        active = set(_active_handlers.values())
        for h in list(root.handlers):
            if isinstance(h, CaptureHandler) and h not in active:
                root.removeHandler(h)
                h.close()
        root.addHandler(handler)
        _active_handlers[submission_id] = handler

    stop = threading.Event()

    def _timer_loop() -> None:
        while not stop.wait(flush_interval):
            _persist(submission_id, buffer)  # self-skips when nothing changed

    timer = threading.Thread(target=_timer_loop, name=f"worklog-flush-{submission_id}", daemon=True)
    timer.start()

    try:
        yield handler
    finally:
        stop.set()
        timer.join(timeout=5)
        _persist(submission_id, buffer)  # final flush
        root.removeHandler(handler)
        handler.close()
        with _active_lock:
            if _active_handlers.get(submission_id) is handler:
                del _active_handlers[submission_id]
