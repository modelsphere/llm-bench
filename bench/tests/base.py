"""
Base classes for all benchmark tests.

GuideLLMBaseTest wraps the existing run_bench / benchmark_generative_text_sync
infrastructure so GuideLLM-backed tests share one implementation path.
"""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from typing import Any, Dict

from bench.result import TestResult
from utils.bench_config import BenchConfig
from utils.logger import logger

# guidellm-specific imports are deferred to GuideLLMBaseTest methods so that
# BaseTest (and subclasses like LongOutputTest / ReplayTest) remain importable
# without guidellm installed (e.g. local dev, bench.modules.* usage).

# Project root = three levels up from bench/tests/base.py.
# Resolves to /app under platform/backend's image.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# The tokenizer guidellm uses to size synthetic prompts and count tokens. A
# Hugging Face repo id by default, downloaded once into HF_HOME — small, and
# any tokenizer produces comparable token counts for sizing purposes. Set
# PROCESSOR_PATH (or a module's `processor_path` param) to a LOCAL directory to
# use the real model's tokenizer, which is what you want when the token counts
# themselves are being compared against production, and what you must do on a
# host with no route to huggingface.co.
_DEFAULT_PROCESSOR = os.getenv("PROCESSOR_DEFAULT", "Qwen/Qwen3-0.6B")


def resolve_processor_path(value: str | None) -> str:
    """
    Resolve a per-run processor: a local directory, or a Hugging Face repo id.

    Priority: explicit `value` (from benchmark/module params) > PROCESSOR_PATH
    env var > the built-in default above.

    Env is read at call time (NOT module import) so a long-lived worker
    process picks up env changes without restart — and, more importantly,
    so a per-submission `value` always wins cleanly.

    A relative `value` is resolved against the project root only when that
    directory exists there. A repo id (`Qwen/Qwen3-0.6B`) also contains a
    slash, so "has a separator" cannot tell the two apart — existence can.
    Anything else is passed through unchanged for the tokenizer loader to
    interpret as a repo id.
    """
    if value:
        if os.path.isabs(value):
            return value
        local = os.path.join(_PROJECT_ROOT, value)
        if value.startswith(".") or os.path.isdir(local):
            return local
        return value
    return os.getenv("PROCESSOR_PATH") or _DEFAULT_PROCESSOR


# Back-compat shim: kept so external imports don't break. Reads env at call
# time. Prefer resolve_processor_path() in new code.
def __getattr__(name: str) -> str:
    if name == "PROCESSOR_PATH":
        return resolve_processor_path(None)
    raise AttributeError(name)


class BaseTest(ABC):
    """Abstract base for every test in the suite."""

    #: Human-readable name used in TestResult.name
    name: str = "unnamed"

    def __init__(self, api_url: str, model: str, api_key: str, output_dir: str) -> None:
        self.api_url = api_url
        self.model = model
        self.api_key = api_key
        self.output_dir = output_dir
        # Subclasses (currently GuideLLM-backed tests) may set this to override
        # PROCESSOR_PATH per-run. None → fall back to env/default.
        self.processor_path: str | None = None
        # Hard wall-clock budget (seconds) for the underlying guidellm run.
        # Callers that know the expected duration set this so a wedged run is
        # abandoned instead of pinning the worker forever. None → only the
        # GUIDELLM_MAX_RUN_SECONDS fallback (if set) applies.
        self.run_timeout: float | None = None

    @abstractmethod
    def run(self) -> TestResult:
        """Execute the test and return a TestResult."""
        raise NotImplementedError


class GuideLLMBaseTest(BaseTest):
    """
    Base for tests that drive GuideLLM.

    Subclasses implement `_make_config()` to return a fully-populated BenchConfig.
    The rest of the pipeline (arg-building, execution, metric extraction) is shared.
    """

    @abstractmethod
    def _make_config(self) -> BenchConfig:
        """Return the BenchConfig for this test."""
        raise NotImplementedError

    def _extract_metrics(self, guidellm_results: Dict[str, Any]) -> Dict[str, Any]:
        """
        Extract metrics from the GuideLLM JSON output.
        Default: uses utils.bench.analyze_results at default concurrency.
        Subclasses may override to extract different concurrency levels or metrics.
        """
        from utils.bench import analyze_results
        return analyze_results(guidellm_results)

    def run(self, cancel_event=None) -> TestResult:
        cfg = self._make_config()
        import json
        from traceback import format_exc
        from bench.modules.base import AllRequestsFailed, BenchmarkTimeout
        from utils.guidellm_args import build_guidellm_args
        from utils.bench import benchmark_generative_text_sync
        effective_processor = resolve_processor_path(self.processor_path)
        guidellm_args = build_guidellm_args(cfg, effective_processor)
        # Log the artifact location BEFORE the run, not after it returns: a
        # cancelled or crashed run never reaches the success path, and its
        # guidellm report is exactly what you need to diagnose why. The dir is
        # what guidellm was told to write to, so it is valid regardless of outcome.
        logger.info(
            "[%s] guidellm output dir: %s (benchmarks.json written here on "
            "success, partial artifacts on failure/cancel)",
            self.name, os.path.abspath(cfg.guidellm_output_dir or "."),
        )
        per_level = getattr(cfg, "max_requests_per_level", None)
        if per_level:
            # Built into the scenario's constraint list by build_guidellm_args;
            # logged here because it changes how long each level runs.
            logger.info(
                "[%s] per-level request caps: %s (duration cap %ss still applies)",
                self.name, per_level, cfg.max_seconds,
            )
        try:
            _report, outputs = benchmark_generative_text_sync(
                guidellm_args, cancel_event=cancel_event, timeout=self.run_timeout,
            )
            logger.info("[%s] guidellm benchmarks.json: %s", self.name, os.path.abspath(outputs["json"]))
            with open(outputs["json"], "r", encoding="utf-8") as f:
                guidellm_results = json.load(f)
            metrics = self._extract_metrics(guidellm_results)
            failure = _all_requests_failed(guidellm_results)
            if failure is not None:
                logger.error("[%s] %s", self.name, failure)
                raise AllRequestsFailed(failure)
            logger.info("[%s] completed. metrics=%s", self.name, metrics)
            return TestResult(name=self.name, passed=True, metrics=metrics)
        except (AllRequestsFailed, BenchmarkTimeout):
            # Must reach the worker as an error, not decay into "no metrics".
            # "timed out after Ns" is the whole diagnosis for a wedged run.
            raise
        except Exception:
            err = format_exc()
            logger.error("[%s] failed: %s", self.name, err)
            return TestResult(name=self.name, passed=False, metrics={}, error=err)


def _all_requests_failed(results: dict) -> str | None:
    """Return a diagnostic if a completed guidellm report contains no successes.

    Returns None whenever any benchmark recorded a successful request, so a
    partially-degraded run (or one level of a sweep failing) is left alone — only
    a total wipeout is worth aborting on. The per-request ``info.error`` strings
    carry the actual cause, so they are surfaced verbatim rather than summarised.

    Note the request lists are *samples* (``accumulator.*.get_sampled()``), not
    totals — they are used here only as presence/absence evidence, never counted.
    """
    benchmarks = results.get("benchmarks") or []
    errors: list[str] = []
    for bench in benchmarks:
        requests_by_status = bench.get("requests") or {}
        if requests_by_status.get("successful"):
            return None
        for entry in requests_by_status.get("errored") or []:
            message = ((entry.get("info") or {}).get("error") or "").strip()
            if message and message not in errors:
                errors.append(message)
    if not errors:
        return None
    shown = "; ".join(errors[:3])
    more = f" (+{len(errors) - 3} other distinct errors)" if len(errors) > 3 else ""
    return (
        f"every request failed — 0 succeeded, so all metrics are 0 and the score "
        f"is meaningless. Error(s): {shown}{more}"
    )
