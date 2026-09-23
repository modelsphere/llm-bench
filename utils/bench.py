import asyncio
import os
import signal
import threading
import time
from typing import Any, Dict, Tuple, Optional
from guidellm.benchmark.entrypoints import benchmark_generative_text
from guidellm.benchmark.schemas import BenchmarkScenario, GenerativeBenchmarksReport
from bench.modules.base import BenchmarkCancelled, BenchmarkTimeout
from utils.logger import logger

# Every wait around guidellm must be bounded. A run was observed wedging AFTER
# its request phase finished cleanly (full duration, all HTTP 200s) — the
# scheduler deadlocked in teardown and sat there for 7+ hours, pinning a worker
# slot, with its 8 forked processes parked in ep_poll. Nothing inside guidellm
# aborted: killing every worker produced only a "died unexpectedly" log line.
# So the deadline below is the ONLY thing that can bound such a run.

# Cleanup budget for draining the loop's leftover tasks once the main task is
# cancelled. Bounded because the drain itself can hang on a task that ignores
# cancellation.
DRAIN_TIMEOUT_SECONDS = float(os.getenv("GUIDELLM_DRAIN_TIMEOUT_SECONDS", "60"))
# Extra time granted after the deadline for the in-loop cancellation to unwind
# gracefully, before the run thread is abandoned outright.
CANCEL_GRACE_SECONDS = float(os.getenv("GUIDELLM_CANCEL_GRACE_SECONDS", "120"))
# Fallback wall-clock cap for callers that pass no explicit timeout. 0 = none
# (preserves the old unbounded behaviour); set it to give even un-updated call
# sites a ceiling.
DEFAULT_MAX_RUN_SECONDS = float(os.getenv("GUIDELLM_MAX_RUN_SECONDS", "0"))


def _kill_orphaned_children(reason: str) -> int:
    """SIGKILL every surviving child process of this worker. Best-effort.

    guidellm forks worker processes; when its scheduler wedges they outlive the
    run and keep the worker slot pinned (10 such processes were seen alive 7h
    after the benchmark finished). SIGTERM is useless here — the children are
    forks of the dramatiq worker, so they inherit its SIGTERM handler and hang
    in its graceful-shutdown path (observed: they logged "Stopping worker
    process..." and then never exited). Hence SIGKILL, which cannot be caught.

    Linux-only (reads /proc); silently does nothing elsewhere.
    """
    killed = 0
    try:
        me = os.getpid()
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            pid = int(entry)
            ppid = None
            try:
                with open(f"/proc/{pid}/status", "r", encoding="utf-8") as fh:
                    for line in fh:
                        if line.startswith("PPid:"):
                            ppid = int(line.split()[1])
                            break
            except (OSError, ValueError):
                continue
            if ppid == me:
                try:
                    os.kill(pid, signal.SIGKILL)
                    killed += 1
                except OSError:
                    pass
    except Exception:  # never let cleanup mask the real failure
        logger.warning("guidellm child reaping failed", exc_info=True)
    if killed:
        logger.warning("Killed %d orphaned guidellm child process(es) after %s", killed, reason)
    return killed


def _run_coro_sync(coro, cancel_event=None, deadline: Optional[float] = None):
    """Run an asyncio coroutine with optional cancellation and a wall-clock deadline.

    `deadline` is a time.monotonic() timestamp. The watcher thread cancels the
    task on either the cancel_event or the deadline; the two are distinguished
    so the caller gets BenchmarkCancelled vs BenchmarkTimeout.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    task = loop.create_task(coro)
    timed_out = threading.Event()

    def _watcher():
        while not task.done():
            if cancel_event is not None and cancel_event.is_set():
                loop.call_soon_threadsafe(task.cancel)
                return
            if deadline is not None and time.monotonic() >= deadline:
                timed_out.set()
                loop.call_soon_threadsafe(task.cancel)
                return
            time.sleep(0.5)

    watcher = threading.Thread(target=_watcher, daemon=True, name="guidellm-watchdog")
    watcher.start()

    try:
        return loop.run_until_complete(task)
    except asyncio.CancelledError:
        if timed_out.is_set():
            raise BenchmarkTimeout("guidellm run exceeded its wall-clock budget")
        raise BenchmarkCancelled("Benchmark was cancelled")
    finally:
        watcher.join(timeout=5)
        pending = asyncio.all_tasks(loop)
        for t in pending:
            t.cancel()
        if pending:
            # BOUNDED: a task that ignores cancellation must not hang cleanup
            # forever — that was a second latent forever-wait on this path.
            #
            # asyncio.wait (NOT wait_for(gather(...))): on timeout, wait_for
            # cancels the inner future and then AWAITS it, so a task that
            # swallows CancelledError hangs the "bounded" drain anyway. wait()
            # simply returns once the timeout elapses and hands back whatever is
            # still pending, which is the only genuinely bounded option here.
            try:
                _done, still_pending = loop.run_until_complete(
                    asyncio.wait(pending, timeout=DRAIN_TIMEOUT_SECONDS)
                )
                if still_pending:
                    logger.warning(
                        "guidellm task drain: %d task(s) ignored cancellation within %.0fs "
                        "— abandoning them",
                        len(still_pending), DRAIN_TIMEOUT_SECONDS,
                    )
            except Exception:
                logger.warning("guidellm task drain raised", exc_info=True)
        try:
            loop.close()
        except Exception:
            pass


def benchmark_generative_text_sync(
    args: BenchmarkScenario,
    cancel_event=None,
    timeout: Optional[float] = None,
    **constraints: Any,
) -> Tuple[GenerativeBenchmarksReport, Dict[str, Any]]:
    """
    Synchronous wrapper for guidellm.benchmark.entrypoints.benchmark_generative_text().

    Returns (report, outputs) where `outputs` maps output kind -> path, e.g.
    `outputs["json"]`. guidellm returns those pairs as a list of tuples; they
    are normalised to a dict here so callers have one shape to handle.

    `timeout` (seconds) is a HARD wall-clock budget for the whole run. It is
    enforced in two layers, because guidellm can wedge past the first:
      1. the watchdog cancels the task at the deadline, then
      2. if that fails to unwind within CANCEL_GRACE_SECONDS, the run thread is
         abandoned (it is a daemon) and guidellm's forked children are SIGKILLed
         so the worker slot is reclaimed.
    Without layer 2 a wedged scheduler blocks the caller forever, since
    `run_until_complete` cannot be interrupted from outside its own thread.

    The coroutine always runs in a separate thread with its own event loop —
    that is what makes the bounded join possible (and it works whether or not
    the caller already has a running loop).
    """
    if timeout is None and DEFAULT_MAX_RUN_SECONDS > 0:
        timeout = DEFAULT_MAX_RUN_SECONDS

    coro = benchmark_generative_text(args, **constraints)
    deadline = (time.monotonic() + timeout) if timeout else None

    result: Optional[Tuple[GenerativeBenchmarksReport, Dict[str, Any]]] = None
    error: Optional[BaseException] = None

    def _runner():
        nonlocal result, error
        try:
            result = _run_coro_sync(coro, cancel_event, deadline)
        except BaseException as e:
            error = e

    t = threading.Thread(target=_runner, daemon=True, name="guidellm-runner")
    t.start()
    t.join(timeout + CANCEL_GRACE_SECONDS if timeout else None)

    if t.is_alive():
        killed = _kill_orphaned_children("guidellm wall-clock timeout")
        raise BenchmarkTimeout(
            f"guidellm run exceeded its {timeout:.0f}s budget and did not unwind within "
            f"{CANCEL_GRACE_SECONDS:.0f}s of being cancelled; abandoned the run thread "
            f"and killed {killed} orphaned child process(es)"
        )

    if error is not None:
        if isinstance(error, BenchmarkTimeout):
            # Cancelled cleanly at the deadline, but guidellm's forked children
            # outlive the coroutine — reap them or they pin the worker slot.
            _kill_orphaned_children("guidellm wall-clock timeout")
        raise error
    if result is None:
        raise RuntimeError("Benchmark coroutine finished without returning a result.")
    report, outputs = result
    return report, dict(outputs)

def _benchmark_load_level(bench: Dict[str, Any]) -> float:
    """Load level of one benchmark entry: concurrency for closed-loop profiles,
    arrival rate for open-loop ones. Read from the per-benchmark strategy — the
    report's old top-level `args.rate` list no longer exists."""
    strategy = (bench.get("config") or {}).get("strategy") or {}
    for key in ("max_concurrency", "streams", "rate"):
        val = strategy.get(key)
        if isinstance(val, (int, float)):
            return float(val)
    return 0.0


def analyze_results(results: Dict[str, Any], target_concurrency: float=16.0) -> Dict[str, Any]:
    """Summarise the benchmark whose load level is closest to target_concurrency."""
    benchmarks = results["benchmarks"]
    levels = [_benchmark_load_level(b) for b in benchmarks]
    # find idx which has the closest level to target_concurrency
    idx = min(range(len(levels)), key=lambda i: abs(levels[i] - target_concurrency))
    bench_res = benchmarks[idx]  # the benchmark result whose concurrency is closest to target_concurrency
    metrics = bench_res["metrics"]  # extract the metrics from the selected benchmark result
    rps = metrics["requests_per_second"]["total"]
    ttft = metrics["time_to_first_token_ms"]["total"]
    tpot = metrics["time_per_output_token_ms"]["total"]
    itl = metrics["inter_token_latency_ms"]["total"]
    input_tps = metrics["prompt_tokens_per_second"]["total"]
    output_tps = metrics["output_tokens_per_second"]["total"]
    total_tps = metrics["tokens_per_second"]["total"]

    results_summary = {
        "rps_mean": rps["mean"],
        "ttft_p95": ttft["percentiles"]["p95"],
        "ttft_p99": ttft["percentiles"]["p99"],
        "tpot_mean": tpot["mean"],
        "tpot_p95": tpot["percentiles"]["p95"],
        "tpot_p99": tpot["percentiles"]["p99"],
        "itl_mean": itl["mean"],
        "itl_p95": itl["percentiles"]["p95"],
        "itl_p99": itl["percentiles"]["p99"],
        "input_tps_mean": input_tps["mean"],
        "input_tps_p95": input_tps["percentiles"]["p95"],
        "input_tps_p99": input_tps["percentiles"]["p99"],
        "output_tps_mean": output_tps["mean"],
        "output_tps_p95": output_tps["percentiles"]["p95"],
        "output_tps_p99": output_tps["percentiles"]["p99"],
        "total_tps_mean": total_tps["mean"],
        "total_tps_p95": total_tps["percentiles"]["p95"],
        "total_tps_p99": total_tps["percentiles"]["p99"],
    }

    return results_summary
