"""
Type D: Agentic multi-turn bench using llm-perf-bench threading engine.

Uses the conversation-directory dataset format (pre-collected multi-turn conversations)
instead of GuideLLM's synthetic request generator.  Each virtual user cycles through
a contiguous shard of pre-baked conversation histories, sending the full message array
(including past assistant turns) on every request — exercising realistic prefill lengths
and KV-cache pressure typical of agentic workloads.

Hyperparameters — all configurable via environment variable:

  Required:
    AGENTIC_TEST_DATASET_PATH   Path to conversation JSON directory.

  Test shape:
    AGENTIC_TEST_MAX_SECONDS    Seconds to run at the concurrency level  (default: 300)
    AGENTIC_TEST_CONCURRENCY    Number of concurrent virtual users       (default: 50)
    AGENTIC_TEST_UPTIME_FLOOR   Min uptime fraction to consider passing  (default: 0.90)

  HTTP / model (constructor args take precedence, then env vars):
    LLM_PERF_BASE_URL           Inference endpoint base URL
    LLM_PERF_MODEL              Model name passed in the payload
    LLM_PERF_API_KEY            Bearer / x-api-key value
    LLM_PERF_MAX_TOKENS_CAP     Cap output tokens; 0 = no cap            (default: 0)
    LLM_PERF_TEMPERATURE        Sampling temperature                     (default: 0.0)
    LLM_PERF_TOP_P              Top-p sampling                           (default: 1.0)
    LLM_PERF_CONNECT_TIMEOUT    HTTP connect timeout in seconds          (default: 10)
    LLM_PERF_READ_TIMEOUT       HTTP read/stream timeout in seconds      (default: 600)

Metrics produced (suffixed _c{concurrency}, plus top-level summary keys):
  ttft_p50_ms / ttft_p99_ms   Time to first streaming event (ms)
  itl_p50_ms  / itl_p99_ms    Inter-streaming-event latency (ms, excl. TTFT)
  output_cps_mean             Output characters per second (decode phase)
  uptime                      Fraction of successful requests
  total_queries / failed_queries

Note: output_cps_mean is characters/sec (not vocab tokens/sec).  TTFT and ITL are
directly comparable to GuideLLM-derived equivalents in b_realworld.py.
"""
from __future__ import annotations

import os
import sys
import time
import threading
from pathlib import Path
from queue import Empty, Queue
from statistics import mean
from traceback import format_exc
from typing import Dict, List

from bench.result import TestResult
from bench.tests.base import BaseTest
from utils.api import join_endpoint, to_base_url
from utils.logger import logger

# ---------------------------------------------------------------------------
# Path: make llm-perf-bench importable without installing it as a package
# ---------------------------------------------------------------------------
_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_TESTS_DIR))
_LLM_PERF_DIR = os.path.join(_REPO_ROOT, "bench", "llm-perf-bench")
if _LLM_PERF_DIR not in sys.path:
    sys.path.insert(0, _LLM_PERF_DIR)

import model_query as _mq          # noqa: E402 — after sys.path mutation
from executor import Executor       # noqa: E402
from llm_benchmark import parse_testset  # noqa: E402

# ---------------------------------------------------------------------------
# Module-level hyperparameters
# ---------------------------------------------------------------------------
AGENTIC_TEST_DATASET_PATH = os.getenv("AGENTIC_TEST_DATASET_PATH", "")
AGENTIC_TEST_MAX_SECONDS  = int(os.getenv("AGENTIC_TEST_MAX_SECONDS", "300"))
AGENTIC_TEST_CONCURRENCY  = int(os.getenv("AGENTIC_TEST_CONCURRENCY", "50"))
AGENTIC_TEST_UPTIME_FLOOR = float(os.getenv("AGENTIC_TEST_UPTIME_FLOOR", "0.90"))

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _percentile(values: List[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(int(len(s) * p / 100), len(s) - 1)
    return s[idx]


def _split_samples(samples: list, n: int) -> List[list]:
    """Contiguous equal-size shards, mirroring throughput_test._split_samples_for_threads."""
    n = min(n, len(samples))
    chunk = (len(samples) + n - 1) // n
    return [samples[i * chunk: min((i + 1) * chunk, len(samples))] for i in range(n)]


def _compute_metrics(records: List[dict], deadline: float = 0.0) -> Dict[str, float]:
    """
    Derive latency / throughput stats from raw Executor query records.

    Three buckets for in-window records:
    - good:       success=True, has tokens  → used for latency/throughput stats
    - errored:    success=False due to HTTP error / timeout / exception
    - incomplete: success=False due to "response without valid token" (stream
                  opened but zero tokens arrived — almost always a deadline
                  cut-off rather than a service fault)
    Post-deadline records are also incomplete regardless of outcome.

    Uptime = 1 - (errored / total)  where total excludes incomplete.
    """
    # Separate by completion time
    if deadline > 0:
        in_window = [r for r in records if r.get("queued_at", float('inf')) <= deadline]
        post_deadline = [r for r in records if r.get("queued_at", float('inf')) > deadline]
    else:
        in_window = records
        post_deadline = []

    # In-window failures: real errors vs. empty stream (deadline cut-off)
    errored = 0
    empty_resp = 0
    error_counts: Dict[str, int] = {}
    valid: list = []

    for r in in_window:
        if r.get("success"):
            if r.get("ts_token"):
                valid.append(r)
        else:
            err = r.get("err_msg", "")
            if err == "response without valid token":
                empty_resp += 1
            else:
                errored += 1
                err_key = err.split(":")[0].strip() if err else "Unknown"
                error_counts[err_key] = error_counts.get(err_key, 0) + 1

    incomplete = len(post_deadline) + empty_resp
    total = len(valid) + errored

    if error_counts:
        logger.warning("[agentic] Error breakdown: %s", dict(error_counts))
    if empty_resp:
        logger.warning("[agentic] %d empty responses (likely deadline cut-off) counted as incomplete", empty_resp)
    if post_deadline:
        logger.warning("[agentic] %d requests incomplete (received after deadline)", len(post_deadline))

    if total == 0:
        return {
            "uptime": 0.0, "total_queries": 0, "errored_queries": 0,
            "incomplete_queries": incomplete,
        }

    good = valid

    ttft_ms: List[float] = []
    itl_ms: List[float] = []
    output_cps: List[float] = []

    for r in good:
        ts = r["ts_token"]
        toks = r["tokens"]
        if not ts:
            continue
        ttft_ms.append(ts[0] * 1000.0)
        # inter-token latencies — decode phase only (exclude TTFT)
        itl_ms.extend((ts[i] - ts[i - 1]) * 1000.0 for i in range(1, len(ts)))
        # output chars/sec over the decode phase
        if len(ts) > 1:
            total_chars = sum(len(t) for t in toks)
            decode_time = ts[-1] - ts[0]
            if decode_time > 0:
                output_cps.append(total_chars / decode_time)

    # Uptime = fraction of requests that completed successfully within time window
    uptime = (total - errored) / total if total > 0 else 0.0

    return {
        "total_queries": total,
        "errored_queries": errored,
        "incomplete_queries": incomplete,
        "uptime": uptime,
        "ttft_p50_ms": _percentile(ttft_ms, 50),
        "ttft_p99_ms": _percentile(ttft_ms, 99),
        "ttft_mean_ms": mean(ttft_ms) if ttft_ms else 0.0,
        "itl_p50_ms": _percentile(itl_ms, 50),
        "itl_p99_ms": _percentile(itl_ms, 99),
        "itl_mean_ms": mean(itl_ms) if itl_ms else 0.0,
        "output_cps_mean": mean(output_cps) if output_cps else 0.0,
    }


# ---------------------------------------------------------------------------
# Test class
# ---------------------------------------------------------------------------

class AgenticBenchTest(BaseTest):
    """
    Type D: agentic multi-turn bench driven by the llm-perf-bench threading engine.

    Spins up CONCURRENCY virtual-user threads, each cycling through a pre-baked
    conversation dataset, for MAX_SECONDS seconds.  Collects per-request timing
    records and computes percentile latency and throughput metrics.
    """

    name = "agentic"

    # Class-level defaults — mirror module-level env vars; override by subclassing.
    DATASET_PATH: str  = AGENTIC_TEST_DATASET_PATH
    MAX_SECONDS: int   = AGENTIC_TEST_MAX_SECONDS
    CONCURRENCY: int   = AGENTIC_TEST_CONCURRENCY
    UPTIME_FLOOR: float = AGENTIC_TEST_UPTIME_FLOOR

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _configure_model_query(self) -> None:
        """
        Patch model_query module globals so constructor args (api_url, model,
        api_key) take precedence over any LLM_PERF_* env vars baked in at import.
        """
        _mq.BASE_URL = to_base_url(self.api_url)
        _mq.MESSAGES_URL = join_endpoint(self.api_url, "messages")
        _mq.CHAT_COMPLETIONS_URL = join_endpoint(self.api_url, "chat/completions")
        _mq.MODEL_NAME = self.model
        _mq.API_KEY = self.api_key

    def _run_bench(self, samples: list) -> Dict[str, float]:
        """
        Spin up CONCURRENCY Executor threads, drain their output for MAX_SECONDS,
        then stop everything and compute metrics.

        Records are tagged with queued_at timestamp to distinguish:
        - In-window: completed before deadline → counted in uptime
        - Incomplete: completed after deadline → excluded from total
        """
        stop_event = threading.Event()
        q: Queue = Queue()
        records: List[dict] = []

        shards = _split_samples(samples, self.CONCURRENCY)
        threads = [
            Executor(f"T-{i:03d}", stop_event, {"mode": "conversation_dir", "samples": shard}, q)
            for i, shard in enumerate(shards)
        ]

        for t in threads:
            t.start()

        deadline = time.time() + self.MAX_SECONDS
        while time.time() < deadline:
            try:
                record = q.get(timeout=1.0)
                record["queued_at"] = time.time()
                records.append(record)
            except Empty:
                pass

        stop_event.set()

        join_timeout = max(int(_mq.READ_TIMEOUT) + 10, 30)
        for t in threads:
            t.join(timeout=join_timeout)

        # Drain any records that arrived after the time window closed
        # These are "incomplete" - the test ended before they finished
        while True:
            try:
                record = q.get_nowait()
                record["queued_at"] = time.time()
                records.append(record)
            except Empty:
                break

        return _compute_metrics(records, deadline)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def run(self) -> TestResult:
        if not self.DATASET_PATH:
            return TestResult(
                name=self.name,
                passed=False,
                metrics={},
                error="AGENTIC_TEST_DATASET_PATH is not set — cannot run agentic bench",
            )

        dataset_path = Path(self.DATASET_PATH)
        if not dataset_path.exists():
            return TestResult(
                name=self.name,
                passed=False,
                metrics={},
                error=f"Dataset path not found: {dataset_path}",
            )

        try:
            self._configure_model_query()

            test_set = parse_testset(dataset_path)
            samples = test_set["samples"]
            if not samples:
                raise RuntimeError(f"No samples loaded from {dataset_path}")

            logger.info(
                "[%s] dataset=%s  samples=%d  concurrency=%d  duration=%ds",
                self.name, dataset_path, len(samples), self.CONCURRENCY, self.MAX_SECONDS,
            )

            m = self._run_bench(samples)
            conc = self.CONCURRENCY

            # Store per-concurrency metrics (consistent with b_realworld.py _cN convention)
            metrics: Dict[str, float] = {f"{k}_c{conc}": v for k, v in m.items()}
            metrics["reported_concurrency"] = conc

            logger.info(
                "[%s] c%d  uptime=%.3f  ttft_p99=%.1fms  itl_p99=%.1fms  cps=%.1f  incomplete=%d",
                self.name, conc,
                m.get("uptime", 0), m.get("ttft_p99_ms", 0),
                m.get("itl_p99_ms", 0), m.get("output_cps_mean", 0),
                m.get("incomplete_queries", 0),
            )

            if m.get("uptime", 0.0) < self.UPTIME_FLOOR:
                logger.warning(
                    "[%s] uptime %.3f is below floor %.2f",
                    self.name, m["uptime"], self.UPTIME_FLOOR,
                )

            # Promote to top-level summary keys
            for stat in ("ttft_p50_ms", "ttft_p99_ms", "ttft_mean_ms",
                         "itl_p50_ms", "itl_p99_ms", "itl_mean_ms",
                         "output_cps_mean", "uptime", "total_queries",
                         "errored_queries", "incomplete_queries"):
                if stat in m:
                    metrics[stat] = m[stat]

            return TestResult(name=self.name, passed=True, metrics=metrics)

        except Exception:
            err = format_exc()
            logger.error("[%s] failed:\n%s", self.name, err)
            return TestResult(name=self.name, passed=False, metrics={}, error=err)
