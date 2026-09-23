"""
Single-level guidellm run, executed as an isolated subprocess.

Usage: python -m bench.probe_runner <probe_config.json>

perf_guidellm_sweep's auto mode runs one of these per concurrency level
instead of calling guidellm in-process. The process isolation is deliberate:
guidellm holds process-wide singletons (Scheduler/Benchmarker) whose locks
survive an abandoned run, its worker teardown can wedge after a clean run,
and its forked worker processes are non-daemon and leak silently when a
join times out. A disposable process per level — started in its own session
so the parent can SIGKILL the whole group — makes every probe independently
killable and leaves nothing behind for the next one.

Protocol (parent side lives in bench/modules/perf_guidellm_sweep.py):
  - argv[1] is a JSON config file; the endpoint API key travels via the
    GUIDELLM_PROBE_API_KEY env var, never on disk.
  - On any completion — success, all-requests-failed, crash — the child
    writes a result JSON to config["result_path"] atomically (tmp file +
    os.replace) and exits 0. A missing result file or nonzero exit means
    the child died abnormally (killed, wedged, import failure).
  - Result shape: {"ok": true, "metrics": {...}} or
    {"ok": false, "error": "...", "all_requests_failed": bool}.

Two watchdog daemon threads back up the parent's deadline enforcement:
  - hard-exit (os._exit) once config["deadline_seconds"] elapses, so a
    wedged guidellm teardown cannot hold the probe open; and
  - hard-exit if the parent process disappears (getppid changes), so a
    hard-killed parent (e.g. the platform's module time cap) cannot leave
    an orphan hammering the submitter's endpoint.
os._exit is used because a wedged run ignores cooperative shutdown — the
same lesson as the platform's module time cap.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time

EXIT_DEADLINE = 124
EXIT_ORPHANED = 125

_ORPHAN_POLL_SECONDS = 5.0


def _start_watchdogs(deadline_seconds: float) -> None:
    parent_pid = os.getppid()

    def _deadline() -> None:
        time.sleep(deadline_seconds)
        print(f"[probe_runner] deadline of {deadline_seconds:.0f}s exceeded — hard exit",
              flush=True)
        os._exit(EXIT_DEADLINE)

    def _orphan() -> None:
        while True:
            time.sleep(_ORPHAN_POLL_SECONDS)
            if os.getppid() != parent_pid:
                os._exit(EXIT_ORPHANED)

    for fn in (_deadline, _orphan):
        threading.Thread(target=fn, daemon=True).start()


def main(argv: list[str]) -> int:
    with open(argv[1], encoding="utf-8") as f:
        cfg = json.load(f)
    _start_watchdogs(float(cfg["deadline_seconds"]))

    # Deferred so the watchdogs are armed before the heavy guidellm import.
    from bench.modules.base import AllRequestsFailed, EndpointConfig, _noop_progress
    from bench.modules.perf_guidellm import run_guidellm_load

    endpoint = EndpointConfig(
        api_url=cfg["api_url"],
        model=cfg["model"],
        api_key=os.environ.get("GUIDELLM_PROBE_API_KEY", ""),
    )
    try:
        metrics = run_guidellm_load(
            endpoint,
            rates=[float(cfg["concurrency"])],
            input_tokens=int(cfg["input_tokens"]),
            output_tokens=int(cfg["output_tokens"]),
            max_seconds=float(cfg["max_seconds"]),
            request_timeout=float(cfg["request_timeout"]),
            warmup_seconds=float(cfg["warmup_seconds"]),
            dataset_path=cfg.get("dataset_path") or None,
            processor_path=cfg.get("processor_path") or None,
            output_dir=cfg["output_dir"],
            progress_cb=_noop_progress,
            cancel_event=None,
            log_tag=cfg.get("log_tag", "probe_runner"),
            requests_per_concurrency=cfg.get("requests_per_concurrency"),
            random_seed=cfg.get("random_seed"),
            warmup_fraction=cfg.get("warmup_fraction"),
            cooldown_fraction=cfg.get("cooldown_fraction"),
        )
        if metrics is None:
            result = {"ok": False, "error": "guidellm produced no metrics (see probe log)"}
        else:
            result = {"ok": True, "metrics": metrics}
    except AllRequestsFailed as exc:
        result = {"ok": False, "error": str(exc), "all_requests_failed": True}
    except BaseException as exc:  # noqa: BLE001 — a written result beats any traceback
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    tmp_path = cfg["result_path"] + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(result, f, default=float)
    os.replace(tmp_path, cfg["result_path"])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
