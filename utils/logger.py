# -*- coding: utf-8 -*-
import logging
import os
import re
import threading

_level = getattr(logging, os.environ.get("LOGLEVEL", "INFO").upper(), logging.INFO)

root = logging.getLogger()
root.setLevel(_level)
for handler in root.handlers:
    handler.setLevel(_level)

# basicConfig is a no-op if handlers already exist (e.g. Dramatiq worker),
# so we force the level above.
logging.basicConfig(
    format="%(asctime)s %(name)-12s %(levelname)-4s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=_level,
)
logger = logging.getLogger(__file__)


HTTPX_STATUS_DUMP_DIR_ENV = "HTTPX_STATUS_DUMP_DIR"


class _HttpRequestFilter(logging.Filter):
    """Count httpx responses by exact status code, persisted via per-PID append logs.

    Per-request httpx INFO lines pass through unchanged. The actual requests
    happen in guidellm's forked worker processes (mp_context_type='fork'), not
    in the parent dramatiq worker — so counts must be persisted from each
    child somehow. Mechanism:

      - register_at_fork → each forked child re-inits its filter state and
        re-reads HTTPX_STATUS_DUMP_DIR from env (which the parent set before
        forking).
      - On every httpx response, append one line `<code>\\n` to {dump_dir}/{pid}.log.
        Small (≤4 bytes) writes are atomic on POSIX, so concurrent threads in
        one process don't interleave. We avoid keeping the file open across
        events: opening O_APPEND|O_WRONLY each time is cheap and survives any
        kind of child termination (SIGTERM, SIGKILL, os._exit) — whatever
        reached disk is what the parent will count.
      - The parent (perf_guidellm) aggregates all *.log files after guidellm
        returns by counting lines per code.

    Previous design used multiprocessing.util.Finalize at child exit, which
    doesn't fire under SIGKILL/os._exit and silently lost all counts.
    """

    _re_status = re.compile(r'"HTTP/\d(?:\.\d)?\s+(\d{3})')

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._dump_dir: str | None = None
        self._log_path: str | None = None
        self._refresh_dump_dir()

    def _refresh_dump_dir(self) -> None:
        d = os.environ.get(HTTPX_STATUS_DUMP_DIR_ENV)
        if d:
            self._dump_dir = d
            self._log_path = os.path.join(d, f"{os.getpid()}.log")
        else:
            self._dump_dir = None
            self._log_path = None

    def _reinit_in_child(self) -> None:
        # threads don't survive fork(); re-init lock. Re-read env in case the
        # parent set HTTPX_STATUS_DUMP_DIR just before forking. PID changes
        # post-fork, so the log path must be re-computed.
        self._lock = threading.Lock()
        self._refresh_dump_dir()

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name != "httpx":
            return True
        msg = record.getMessage()
        if not msg.startswith("HTTP Request:"):
            return True
        m = self._re_status.search(msg)
        if m and self._log_path is not None:
            _append_code(self._log_path, m.group(1))
        return True  # let the per-request line through


def _append_code(log_path: str, code: str) -> None:
    """Append `<code>\\n` to log_path. Never raise — logging must not break the test."""
    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        # O_APPEND on POSIX makes the write atomic; no need for a lock between
        # threads in the same process for the actual write. Open+write+close
        # per event keeps the file's tail durable even on hard kill.
        fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, (code + "\n").encode("ascii"))
        finally:
            os.close(fd)
    except Exception:
        pass


def read_httpx_status_dump(dump_dir: str) -> dict[int, int]:
    """Aggregate all per-PID .log files under dump_dir into {status_code: count}."""
    totals: dict[int, int] = {}
    if not os.path.isdir(dump_dir):
        return totals
    for name in os.listdir(dump_dir):
        if not name.endswith(".log"):
            continue
        try:
            with open(os.path.join(dump_dir, name)) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        code = int(line)
                    except ValueError:
                        continue
                    totals[code] = totals.get(code, 0) + 1
        except Exception:
            continue
    return totals


_httpx_filter = _HttpRequestFilter()
logging.getLogger("httpx").addFilter(_httpx_filter)
if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_httpx_filter._reinit_in_child)
