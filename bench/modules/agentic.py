"""
Agentic Multi-Turn Benchmark

Wraps bench/tests/d_agentic.py (AgenticBenchTest) to replay pre-baked
multi-turn conversation datasets under concurrent load.

Score and pass/fail are NOT computed here — the platform evaluator uses
per-benchmark MetricConfig rules (stored in BenchmarkModule.metric_configs_json).

Default evaluation (default_metric_configs):
  Score: output_cps_mean / baseline  [ratio, uncapped]
  Redlines: TTFT/ITL P99 & P50 max thresholds, uptime min threshold
"""
from __future__ import annotations

import os

from pydantic import BaseModel, Field

from bench.modules.base import (
    EndpointConfig, MetricConfig, MetricDescriptor,
    ModuleResult, ProgressCallback, TestModule, _noop_progress,
)
from utils.logger import logger


class AgenticParams(BaseModel):
    dataset_path: str = Field(
        description="Path to conversation JSON directory dataset (required)."
    )
    concurrency: int = Field(
        default=50, ge=1, le=4096,
        description="Number of concurrent virtual users"
    )
    max_seconds: int = Field(
        default=300, ge=1, le=604800,
        description="Duration of the load test in seconds"
    )


class AgenticModule(TestModule):
    """Agentic multi-turn benchmark (llm-perf-bench engine)."""

    name = "agentic"
    display_name = "Agentic Multi-Turn Bench"
    description = (
        "Replays pre-baked multi-turn conversation datasets under concurrent "
        "virtual-user load using the llm-perf-bench threading engine. "
        "Returns output CPS and TTFT/ITL percentiles. "
        "Default score = output_cps_mean / baseline (uncapped); "
        "latency/uptime are redlines. Admin configures baseline and thresholds."
    )
    ParamsSchema = AgenticParams

    metrics_descriptors = [
        MetricDescriptor("output_cps_mean",   "Output CPS",      "chars/s", "Mean output characters per second (decode phase)", higher_is_better=True),
        MetricDescriptor("uptime",            "Uptime",          "%",       "Fraction of successful requests",                  higher_is_better=True),
        MetricDescriptor("ttft_p99_ms",       "TTFT P99",        "ms",      "Time to first token, 99th percentile",             higher_is_better=False),
        MetricDescriptor("itl_p99_ms",        "ITL P99",         "ms",      "Inter-token latency, 99th percentile",             higher_is_better=False),
        MetricDescriptor("ttft_p50_ms",       "TTFT P50",        "ms",      "Time to first token, 50th percentile",             higher_is_better=False),
        MetricDescriptor("itl_p50_ms",         "ITL P50",         "ms",      "Inter-token latency, 50th percentile",             higher_is_better=False),
        MetricDescriptor("ttft_mean_ms",       "TTFT Mean",       "ms",      "Time to first token, mean",                        higher_is_better=False),
        MetricDescriptor("itl_mean_ms",       "ITL Mean",        "ms",      "Inter-token latency, mean",                        higher_is_better=False),
        MetricDescriptor("total_queries",      "Total Queries",   "",        "Total requests sent during the test window",       higher_is_better=True),
        MetricDescriptor("errored_queries",    "Errored",         "",        "Requests that errored (HTTP/connection errors)",    higher_is_better=False),
        MetricDescriptor("incomplete_queries", "Incomplete",      "",        "Requests that didn't finish before deadline",      higher_is_better=False),
    ]

    default_metric_configs = [
        MetricConfig("output_cps_mean",   role="score",   weight=1.0, formula="ratio", baseline=800.0),
        MetricConfig("uptime",           role="redline", min_val=0.90),
        MetricConfig("ttft_p99_ms",     role="redline", max_val=5000.0),
        MetricConfig("itl_p99_ms",      role="redline", max_val=200.0),
        MetricConfig("ttft_p50_ms",     role="redline", max_val=2500.0),
        MetricConfig("itl_p50_ms",      role="redline", max_val=100.0),
        MetricConfig("ttft_mean_ms",    role="display"),
        MetricConfig("itl_mean_ms",     role="display"),
        MetricConfig("total_queries",   role="display"),
        MetricConfig("errored_queries", role="display"),
        MetricConfig("incomplete_queries", role="display"),
    ]

    @classmethod
    def default_params(cls) -> dict:
        return cls.ParamsSchema.model_construct(dataset_path="").model_dump()

    def run(
        self,
        endpoint: EndpointConfig,
        params: BaseModel,
        output_dir: str,
        progress_cb: ProgressCallback = _noop_progress,
        cancel_event=None,
    ) -> ModuleResult:
        p: AgenticParams = params  # type: ignore[assignment]
        os.makedirs(output_dir, exist_ok=True)

        progress_cb(0.05, f"Starting agentic bench: concurrency={p.concurrency}, "
                         f"duration={p.max_seconds}s")

        # Use param dataset_path only if the path exists; otherwise fall back to
        # AGENTIC_TEST_DATASET_PATH env var (set in docker-compose).
        fallback_path = os.environ.get("AGENTIC_TEST_DATASET_PATH", "")
        effective_dataset = p.dataset_path if (p.dataset_path and os.path.exists(p.dataset_path)) else fallback_path
        if p.dataset_path and effective_dataset != p.dataset_path:
            logger.warning("[agentic] dataset_path %s not found — falling back to %s", p.dataset_path, effective_dataset)

        env_map = {
            "AGENTIC_TEST_DATASET_PATH": effective_dataset,
            "AGENTIC_TEST_CONCURRENCY":  str(p.concurrency),
            "AGENTIC_TEST_MAX_SECONDS":  str(p.max_seconds),
            "AGENTIC_TEST_UPTIME_FLOOR": "0.0",  # pass/fail handled by evaluator
        }
        old_env = {k: os.environ.get(k) for k in env_map}
        for k, v in env_map.items():
            os.environ[k] = v

        try:
            from bench.tests.d_agentic import AgenticBenchTest

            test = AgenticBenchTest(
                api_url=endpoint.api_url,
                model=endpoint.model,
                api_key=endpoint.api_key,
                output_dir=output_dir,
            )
            test.DATASET_PATH = effective_dataset
            test.CONCURRENCY  = p.concurrency
            test.MAX_SECONDS  = p.max_seconds
            test.UPTIME_FLOOR = 0.0  # evaluator handles pass/fail

            progress_cb(0.1, "Running agentic bench (may take several minutes)...")
            result = test.run()
        except Exception:
            from traceback import format_exc
            err = format_exc()
            logger.error("[agentic] test failed: %s", err)
            return ModuleResult(error=err)
        finally:
            for k, old_v in old_env.items():
                if old_v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = old_v

        if result.error:
            return ModuleResult(error=result.error)

        metrics = result.metrics
        output_cps = metrics.get("output_cps_mean", 0.0) or 0.0
        progress_cb(0.99, f"Metrics collected — output_cps={output_cps:.0f}")
        logger.info("[agentic] metrics collected: output_cps=%.1f", output_cps)
        return ModuleResult(metrics=metrics)
