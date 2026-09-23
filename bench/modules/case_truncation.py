"""
Correctness under stress: Long Output / Truncation Detection

Sends concurrent streaming requests asking the model to produce maximum-length
output. Checks each response for finish_reason == "stop" and sufficient output
token count.

Score and pass/fail are NOT computed here — the platform evaluator uses
per-benchmark MetricConfig rules (stored in BenchmarkModule.metric_configs_json).

Default evaluation (default_metric_configs):
  Score: completion_ratio (passthrough, [0, 1])
  Redlines: truncation_rate ≤ 0, error_rate ≤ 0
"""
from __future__ import annotations

import os

from pydantic import BaseModel, Field

from bench.modules.base import (
    EndpointConfig, MetricConfig, MetricDescriptor,
    ModuleResult, ProgressCallback, TestModule, _noop_progress,
)
from utils.logger import logger


class CaseTruncationParams(BaseModel):
    concurrency: int = Field(default=5, ge=1, le=4096, description="Number of concurrent streaming requests")
    output_tokens: int = Field(
        default=65536, ge=1,
        description="Target max_tokens per request (model must produce this many to pass)"
    )
    completion_ratio: float = Field(
        default=0.95, ge=0.0, le=1.0,
        description="Fraction of output_tokens the model must reach to count as non-truncated"
    )
    request_timeout: float = Field(
        default=900.0, ge=1.0, le=86400.0,
        description="Per-request timeout in seconds (long outputs take time)"
    )


class CaseTruncationModule(TestModule):
    """Long-output / truncation detection."""

    name = "case_truncation"
    display_name = "Long Output Truncation"
    description = (
        "Detects truncation by sending concurrent requests asking for maximum-length "
        "output (up to 64k tokens). A request is flagged as truncated if "
        "finish_reason != 'stop' or output token count falls short of the target. "
        "Default score = completion_ratio (fraction of non-truncated requests). "
        "Pass requires zero truncations and zero errors."
    )
    ParamsSchema = CaseTruncationParams

    metrics_descriptors = [
        MetricDescriptor("completion_ratio", "Completion Ratio", "%", "Fraction of requests meeting output length target", higher_is_better=True),
        MetricDescriptor("truncation_rate",  "Truncation Rate",  "%", "Fraction of requests truncated",                    higher_is_better=False),
        MetricDescriptor("error_rate",       "Error Rate",       "%", "Fraction of requests that errored",                 higher_is_better=False),
        MetricDescriptor("total_requests",   "Total Requests",   "",  "Total requests sent",                               higher_is_better=True),
    ]

    default_metric_configs = [
        MetricConfig("completion_ratio", role="score",   weight=1.0, formula="passthrough"),
        MetricConfig("truncation_rate",  role="redline", max_val=0.0),
        MetricConfig("error_rate",       role="redline", max_val=0.0),
        MetricConfig("total_requests",   role="display"),
    ]

    def run(
        self,
        endpoint: EndpointConfig,
        params: BaseModel,
        output_dir: str,
        progress_cb: ProgressCallback = _noop_progress,
        cancel_event=None,
    ) -> ModuleResult:
        p: CaseTruncationParams = params  # type: ignore[assignment]
        os.makedirs(output_dir, exist_ok=True)

        progress_cb(0.05, f"Sending {p.concurrency} concurrent requests, "
                         f"max_tokens={p.output_tokens}")

        # All runtime knobs are passed as LongOutputTest constructor args.
        # The env-driven module-level defaults in long_output.py are captured
        # at import time and are useless to mutate from a long-lived worker —
        # see ReplayTest / replay for the same trap.
        try:
            from bench.tests.functional.long_output import LongOutputTest
            test = LongOutputTest(
                api_url=endpoint.api_url,
                model=endpoint.model,
                api_key=endpoint.api_key,
                output_dir=output_dir,
                concurrency=p.concurrency,
                output_tokens=p.output_tokens,
                cancel_event=cancel_event,
                completion_ratio=p.completion_ratio,
                request_timeout=p.request_timeout,
                # Permissive: the platform evaluator handles pass/fail via
                # MetricConfig rules, the underlying test shouldn't gate.
                truncation_rate_threshold=0.0,
            )
            result = test.run()
        except Exception:
            from traceback import format_exc
            err = format_exc()
            logger.error("[case_truncation] test failed: %s", err)
            return ModuleResult(error=err)

        metrics = result.metrics

        # Derive convenience metrics not always produced by the underlying runner
        total = metrics.get("total_requests", 0) or 1
        error_count = metrics.get("error_requests", 0) or 0
        if "error_rate" not in metrics:
            metrics["error_rate"] = error_count / total
        if "completion_ratio" not in metrics:
            tr = metrics.get("truncation_rate")
            truncation_rate = tr if tr is not None else 1.0
            metrics["completion_ratio"] = max(0.0, 1.0 - truncation_rate)

        progress_cb(0.99, f"Metrics collected — truncation_rate={metrics.get('truncation_rate', '?')}")
        logger.info("[case_truncation] truncation_rate=%s completion_ratio=%s",
                    metrics.get("truncation_rate"), metrics.get("completion_ratio"))
        return ModuleResult(metrics=metrics)
