#!/usr/bin/env python3
"""Worker entrypoint: runs pending submission jobs via Dramatiq.

Usage:
    python run_worker.py [--processes N] [--threads M]
    ./run_worker.py              (from platform/backend/)

Environment variables (override defaults; CLI flags take final precedence):
    WORKER_PROCESSES  number of OS worker processes (default: 4)
    WORKER_THREADS    threads per worker process    (default: 1)

Why processes-not-threads: dramatiq with `--threads N` is 1 OS process × N
threads. A segfault, OOM-kill, or unhandled fatal signal in any one task
takes down the entire process and every in-flight task with it.
`--processes N --threads 1` isolates each task in its own OS process, so a
single crash kills only that one task and dramatiq's master respawns the
worker automatically. The tradeoff is RAM: each process carries the full
bench + asyncio + sqlalchemy import tree.

Sets PYTHONPATH and BENCH_MODULES_PATH so that Dramatiq worker processes
(which use multiprocessing.spawn on macOS, starting from a fresh interpreter)
can import both app.* and bench.* modules correctly.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

# __file__ = platform/backend/run_worker.py
# parents: [0]=platform/backend, [1]=platform, [2]=repo_root
_this_file = os.path.abspath(__file__)
_backend_dir = os.path.dirname(_this_file)            # platform/backend
_platform_dir = os.path.dirname(_backend_dir)         # platform
_repo_root = os.path.dirname(_platform_dir)           # llm-bench

# Worker processes (spawned, not forked) need these in the env
os.environ["PYTHONPATH"] = f"{_repo_root}:{_backend_dir}"
os.environ["BENCH_MODULES_PATH"] = os.path.join(_repo_root, "bench")

# Dramatiq claims a message at FETCH time and by default prefetches
# threads*2 = 2 per process, so a busy process also "owns" one unstarted
# submission that no idle process may steal — on k8s that held a queued
# submission hostage for a 12h deploy drain (docs/k8s-deployment.md §10f).
# Pin to 1 so a process only ever owns the message it is running. The k8s
# worker sets this in the helm chart (app.worker.queuePrefetch); this
# default keeps dev behavior identical. Lowercase is dramatiq's spelling.
os.environ.setdefault("dramatiq_queue_prefetch", "1")


def _parse_args() -> tuple[int, int]:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--processes", type=int,
        default=int(os.environ.get("WORKER_PROCESSES", "4")),
        help="Number of OS worker processes (env: WORKER_PROCESSES, default 4)",
    )
    parser.add_argument(
        "--threads", type=int,
        default=int(os.environ.get("WORKER_THREADS", "1")),
        help="Threads per worker process (env: WORKER_THREADS, default 1)",
    )
    ns = parser.parse_args()
    if ns.processes < 1 or ns.threads < 1:
        parser.error("--processes and --threads must be >= 1")
    return ns.processes, ns.threads


if __name__ == "__main__":
    processes, threads = _parse_args()
    if threads > 1:
        # Per-submission worker-log capture attaches a handler to the root logger
        # and relies on process-per-task (--threads 1) for isolation: with >1
        # thread, concurrent submissions in one process share the root logger and
        # their logs mix together (and crash isolation degrades). See
        # app/queue/log_capture.py.
        print(
            f"WARNING: --threads {threads} (>1): per-submission worker-log isolation "
            "is DISABLED — concurrent submissions in a process will have mixed logs. "
            "Use --threads 1 unless you accept this.",
            file=sys.stderr,
        )
    venv_python = sys.executable
    proc = subprocess.Popen(
        [
            venv_python, "-m", "dramatiq",
            "app.queue.broker",
            "app.queue.jobs",
            "--processes", str(processes),
            "--threads", str(threads),
        ],
        env=os.environ,
    )
    print(f"Worker started (PID {proc.pid}, processes={processes}, threads={threads})")
    sys.exit(0)
