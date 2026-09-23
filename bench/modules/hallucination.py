"""
Hallucination Test Module

Replays a single captured request many times (default 1000) at single
concurrency. Under a very large cached prompt the model can occasionally
glitch and emit content that is completely unrelated / incoherent. A separate,
configurable judge LLM grades each reply and we report how many were
hallucinated.

Score and pass/fail are NOT computed here — the platform evaluator uses
per-benchmark MetricConfig rules.

Default evaluation (default_metric_configs):
  Score:   non_hallucination_rate (passthrough, [0, 1])
  Redline: hallucination_rate ≤ 0.05
"""
from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, Field

from bench.modules.base import (
    EndpointConfig, MetricConfig, MetricDescriptor,
    ModuleResult, ProgressCallback, TestModule, _noop_progress,
)
from utils.logger import logger

# Project root = four levels up from bench/modules/hallucination.py
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
# A hand-written grounded question (bench/examples/README.md). Point
# `dataset_path` at a captured request from your own traffic for a real reading.
_DEFAULT_DATASET = str(_PROJECT_ROOT / "bench" / "examples" / "hallucination.json")
HALLUCINATION_DATASET_PATH = os.getenv("HALLUCINATION_DATASET_PATH", _DEFAULT_DATASET)


class HallucinationParams(BaseModel):
    dataset_path: str = Field(
        default="",
        description="Path to the captured-request JSON (proxy-log capture with a "
                    "`req_body` field). Empty → built-in default.",
    )
    num_requests: int = Field(default=1000, ge=1, description="How many times to replay the request")
    concurrency: int = Field(default=1, ge=1, le=4096, description="Concurrent probe workers (default 1 = single concurrency)")
    max_tokens: int = Field(default=256, ge=1, description="max_tokens for the probe request. Kept small for quick testing; 256 gives a thinking model a little room past the reasoning trace so the judged text is less likely to be cut off mid-thought (which the judge can misread as garbled).")
    request_timeout: float = Field(default=120.0, ge=1.0, le=86400.0, description="Per-request HTTP timeout in seconds")
    clean: bool = Field(
        default=False,
        description="Normalize the captured request so a strict chat template "
                    "(Qwen/SGLang) accepts it: merge consecutive leading system "
                    "messages into one and demote any non-leading system message to "
                    "user. Captured gateway/Claude-Code traffic often carries several "
                    "system blocks, which bare engines reject with 'System message must "
                    "be at the beginning.' (4xx). Default off keeps the request "
                    "byte-for-byte; turn on if the probe requests 4xx."
    )
    # Judge LLM — a separate, configurable model that grades each reply.
    judge_api_url: str = Field(default="", description="Judge LLM endpoint URL. Empty → reuse the target endpoint.")
    judge_model: str = Field(default="", description="Judge LLM model name. Empty → reuse the target model.")
    judge_api_key: str = Field(default="", description="Judge LLM API key. Empty → reuse the target key when judge_api_url is also empty.")
    judge_max_tokens: int = Field(default=128, ge=1, description="max_tokens for each judge call. Kept small for quick testing: judge thinking is disabled via chat_template_kwargs so 128 is enough for the verdict JSON. Raise it if your backend ignores the thinking-off switch and the judge truncates mid-reasoning.")
    judge_concurrency: int = Field(default=8, ge=1, le=4096, description="Concurrent judge workers")
    judge_prompt: str = Field(default="", description="Optional judge prompt override. May contain {context}, {instruction} and {response} placeholders. Empty → built-in prompt.")
    judge_disable_thinking: bool = Field(default=True, description="Disable thinking on judge requests (via chat_template_kwargs) so the verdict JSON is emitted as content instead of being consumed by reasoning tokens. Disable only if the judge model has no thinking mode.")
    summarize_context: bool = Field(default=True, description="Before judging, make one summarisation call over the captured conversation and give that summary to the judge as task context. Prevents the judge from flagging the model's grounded references (functions/files/code it saw in the long hidden conversation) as 'invented' hallucinations.")


class HallucinationModule(TestModule):
    """Single-request hallucination probe with an LLM judge."""

    name = "hallucination"
    display_name = "Hallucination Probe"
    description = (
        "Replays one captured request many times (default 1000) at single "
        "concurrency to surface cases where the model emits completely "
        "unrelated / incoherent content. A separate, configurable judge LLM "
        "grades each reply. Default score = non_hallucination_rate; admin "
        "configures the hallucination_rate redline."
    )
    ParamsSchema = HallucinationParams

    metrics_descriptors = [
        MetricDescriptor("non_hallucination_rate", "Non-Hallucination Rate", "%", "Fraction of judged replies that are on-topic", higher_is_better=True),
        MetricDescriptor("hallucination_rate",     "Hallucination Rate",     "%", "Fraction of judged replies flagged as hallucinated", higher_is_better=False),
        MetricDescriptor("hallucination_count",    "Hallucination Count",    "",  "Number of replies judged hallucinated", higher_is_better=False),
        MetricDescriptor("judged_requests",        "Judged",                 "",  "Replies that were successfully judged", higher_is_better=True),
        MetricDescriptor("judge_error_count",      "Judge Errors",           "",  "Replies the judge failed to grade (parse/transport error)", higher_is_better=False),
        MetricDescriptor("total_requests",         "Total Requests",         "",  "Requests sent to the target", higher_is_better=True),
        MetricDescriptor("completed_requests",     "Completed",              "",  "Requests that returned a response", higher_is_better=True),
        MetricDescriptor("error_requests",         "Errored",                "",  "Requests that errored (HTTP/transport)", higher_is_better=False),
        MetricDescriptor("empty_response_count",   "Empty/Tool-only",        "",  "Responses with neither content nor reasoning (skipped by judge)", higher_is_better=False),
        MetricDescriptor("reasoning_only_count",   "Reasoning-only",         "",  "Judged replies that had only reasoning_content (content empty — raise max_tokens or disable thinking to get a final answer)", higher_is_better=False),
        MetricDescriptor("avg_completion_tokens",  "Avg Completion Tokens",  "",  "Average output tokens per completed request", higher_is_better=True),
    ]

    default_metric_configs = [
        MetricConfig("non_hallucination_rate", role="score",   weight=1.0, formula="passthrough"),
        MetricConfig("hallucination_rate",     role="redline", max_val=0.05),
        MetricConfig("hallucination_count",    role="display"),
        MetricConfig("judged_requests",        role="display"),
        MetricConfig("judge_error_count",      role="display"),
        MetricConfig("total_requests",         role="display"),
        MetricConfig("completed_requests",     role="display"),
        MetricConfig("error_requests",         role="display"),
        MetricConfig("empty_response_count",   role="display"),
        MetricConfig("reasoning_only_count",   role="display"),
        MetricConfig("avg_completion_tokens",  role="display"),
    ]

    def run(
        self,
        endpoint: EndpointConfig,
        params: BaseModel,
        output_dir: str,
        progress_cb: ProgressCallback = _noop_progress,
        cancel_event=None,
    ) -> ModuleResult:
        p: HallucinationParams = params  # type: ignore[assignment]
        os.makedirs(output_dir, exist_ok=True)

        dataset_path = p.dataset_path if (p.dataset_path and os.path.isfile(p.dataset_path)) else HALLUCINATION_DATASET_PATH
        if p.dataset_path and dataset_path != p.dataset_path:
            logger.warning("[hallucination] dataset_path %s not found — falling back to %s", p.dataset_path, dataset_path)
        if not os.path.isfile(dataset_path):
            return ModuleResult(error=f"Hallucination dataset not found: {dataset_path}")

        progress_cb(0.02, f"Loading captured request from {os.path.basename(dataset_path)}")
        try:
            from bench.tests.functional.fixed_request_probe import (
                HallucinationProbe, load_capture_request,
            )
            request = load_capture_request(dataset_path)
            probe = HallucinationProbe(
                api_url=endpoint.api_url,
                model=endpoint.model,
                api_key=endpoint.api_key,
                request=request,
                num_requests=p.num_requests,
                concurrency=p.concurrency,
                max_tokens=p.max_tokens,
                request_timeout=p.request_timeout,
                clean=p.clean,
                judge_api_url=p.judge_api_url,
                judge_model=p.judge_model,
                judge_api_key=p.judge_api_key,
                judge_max_tokens=p.judge_max_tokens,
                judge_concurrency=p.judge_concurrency,
                judge_prompt=p.judge_prompt,
                judge_disable_thinking=p.judge_disable_thinking,
                summarize_context=p.summarize_context,
                progress_cb=progress_cb,
                cancel_event=cancel_event,
            )
            metrics = probe.run()
        except Exception:
            from traceback import format_exc
            err = format_exc()
            logger.error("[hallucination] test failed: %s", err)
            return ModuleResult(error=err)

        progress_cb(0.99, f"Done — hallucination_rate={metrics.get('hallucination_rate'):.3f}")
        logger.info("[hallucination] hallucination_count=%s/%s rate=%s",
                    metrics.get("hallucination_count"), metrics.get("judged_requests"),
                    metrics.get("hallucination_rate"))
        return ModuleResult(metrics=metrics)
