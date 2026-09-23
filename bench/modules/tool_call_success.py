"""
Tool-Call Success Rate Module

Replays a single captured tool-enabled request N times and checks how often the
model emits a *valid* tool call — i.e. at least one tool call whose function
name is declared in the request's `tools` and whose arguments parse as JSON.

Score and pass/fail are NOT computed here — the platform evaluator uses
per-benchmark MetricConfig rules.

Default evaluation (default_metric_configs):
  Score:   tool_call_success_rate (passthrough, [0, 1] over all requests)
  Redline: tool_call_success_rate ≥ 0.90

max_tokens default = 8192: the captured request enables "thinking", so a
reasoning model may emit a long chain-of-thought before the tool call. Tool
calling stops generation as soon as the call is emitted, so a high cap costs
no extra tokens in the common case — it only adds headroom for the
reasoning-heavy tail. A cap that truncates mid-reasoning (finish_reason=length)
hides the tool call and is scored as a failure; the `truncated_requests` metric
(and a summary WARNING) surfaces how often that happens so the cap can be tuned.
Still well below the captured original (32000).
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

# Project root = four levels up from bench/modules/tool_call_success.py
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
# A hand-written single-tool request (bench/examples/README.md). Point
# `dataset_path` at a captured tool-using request for a real reading.
_DEFAULT_DATASET = str(_PROJECT_ROOT / "bench" / "examples" / "tool_call.json")
TOOL_CALL_DATASET_PATH = os.getenv("TOOL_CALL_DATASET_PATH", _DEFAULT_DATASET)


class ToolCallSuccessParams(BaseModel):
    dataset_path: str = Field(
        default="",
        description="Path to the captured tool-enabled request JSON (proxy-log "
                    "capture with a `req_body` field). Empty → built-in default.",
    )
    num_requests: int = Field(default=100, ge=1, description="How many times to replay the request")
    concurrency: int = Field(default=5, ge=1, le=4096, description="Concurrent probe workers")
    max_tokens: int = Field(default=8192, ge=1, description="max_tokens for the probe request. Tool calling stops as soon as the call is emitted, so a high cap costs no extra tokens in the common case — it only gives reasoning ('thinking') models headroom to finish their chain-of-thought before the tool call. Too low truncates mid-reasoning (finish_reason=length) and counts as a failure; watch 'Truncated (length)' and raise this if it's non-zero.")
    request_timeout: float = Field(default=120.0, ge=1.0, le=86400.0, description="Per-request HTTP timeout in seconds")
    clean: bool = Field(
        default=False,
        description="Normalize the captured request so a strict chat template "
                    "(Qwen/SGLang) accepts it: merge consecutive leading system "
                    "messages into one and demote any non-leading system message to "
                    "user. Captured gateway/Claude-Code traffic often carries several "
                    "system blocks, which bare engines reject with 'System message must "
                    "be at the beginning.' (4xx) — making every probe fail. Default off "
                    "keeps the request byte-for-byte; turn on if all requests 4xx."
    )


class ToolCallSuccessModule(TestModule):
    """Single-request tool-call success-rate probe."""

    name = "tool_call_success"
    display_name = "Tool-Call Success Rate"
    description = (
        "Replays one captured tool-enabled request N times and measures how "
        "often the model emits a valid tool call (declared tool name + "
        "parseable JSON arguments). Default score = tool_call_success_rate "
        "over all requests; admin configures the redline threshold."
    )
    ParamsSchema = ToolCallSuccessParams

    metrics_descriptors = [
        MetricDescriptor("tool_call_success_rate", "Tool-Call Success Rate", "%", "Valid tool calls / total requests", higher_is_better=True),
        MetricDescriptor("valid_tool_call_count",  "Valid Tool Calls",       "",  "Responses whose tool calls were all valid", higher_is_better=True),
        MetricDescriptor("invalid_tool_call_count","Invalid Tool Calls",     "",  "Responses with a tool call but bad name/arguments", higher_is_better=False),
        MetricDescriptor("no_tool_call_count",     "No Tool Call",           "",  "Responses that returned text instead of a tool call", higher_is_better=False),
        MetricDescriptor("tool_call_count",        "Responses w/ Tool Call", "",  "Responses that contained at least one tool call", higher_is_better=True),
        MetricDescriptor("truncated_requests",     "Truncated (length)",     "",  "Completed responses cut off at max_tokens (finish_reason=length) — may hide the tool call", higher_is_better=False),
        MetricDescriptor("total_requests",         "Total Requests",         "",  "Requests sent to the target", higher_is_better=True),
        MetricDescriptor("completed_requests",     "Completed",              "",  "Requests that returned a response", higher_is_better=True),
        MetricDescriptor("error_requests",         "Errored",                "",  "Requests that errored (HTTP/transport)", higher_is_better=False),
        MetricDescriptor("error_rate",             "Error Rate",             "%", "Fraction of requests that errored", higher_is_better=False),
        MetricDescriptor("avg_completion_tokens",  "Avg Completion Tokens",  "",  "Average output tokens per completed request", higher_is_better=False),
    ]

    default_metric_configs = [
        MetricConfig("tool_call_success_rate", role="score",   weight=1.0, formula="passthrough"),
        MetricConfig("tool_call_success_rate", role="redline", min_val=0.90),
        MetricConfig("valid_tool_call_count",  role="display"),
        MetricConfig("invalid_tool_call_count",role="display"),
        MetricConfig("no_tool_call_count",     role="display"),
        MetricConfig("tool_call_count",        role="display"),
        MetricConfig("truncated_requests",     role="display"),
        MetricConfig("total_requests",         role="display"),
        MetricConfig("completed_requests",     role="display"),
        MetricConfig("error_requests",         role="display"),
        MetricConfig("error_rate",             role="display"),
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
        p: ToolCallSuccessParams = params  # type: ignore[assignment]
        os.makedirs(output_dir, exist_ok=True)

        dataset_path = p.dataset_path if (p.dataset_path and os.path.isfile(p.dataset_path)) else TOOL_CALL_DATASET_PATH
        if p.dataset_path and dataset_path != p.dataset_path:
            logger.warning("[tool_call_success] dataset_path %s not found — falling back to %s", p.dataset_path, dataset_path)
        if not os.path.isfile(dataset_path):
            return ModuleResult(error=f"Tool-call dataset not found: {dataset_path}")

        progress_cb(0.02, f"Loading captured request from {os.path.basename(dataset_path)}")
        try:
            from bench.tests.functional.fixed_request_probe import (
                ToolCallProbe, allowed_tool_names, load_capture_request,
            )
            request = load_capture_request(dataset_path)
            tools = allowed_tool_names(request)
            if not tools:
                logger.warning("[tool_call_success] captured request declares no tools — "
                               "any returned tool name will be accepted as valid.")
            probe = ToolCallProbe(
                api_url=endpoint.api_url,
                model=endpoint.model,
                api_key=endpoint.api_key,
                request=request,
                num_requests=p.num_requests,
                concurrency=p.concurrency,
                max_tokens=p.max_tokens,
                request_timeout=p.request_timeout,
                clean=p.clean,
                progress_cb=progress_cb,
                cancel_event=cancel_event,
            )
            metrics = probe.run()
        except Exception:
            from traceback import format_exc
            err = format_exc()
            logger.error("[tool_call_success] test failed: %s", err)
            return ModuleResult(error=err)

        progress_cb(0.99, f"Done — success_rate={metrics.get('tool_call_success_rate'):.3f}")
        logger.info("[tool_call_success] valid=%s/%s success_rate=%s",
                    metrics.get("valid_tool_call_count"), metrics.get("total_requests"),
                    metrics.get("tool_call_success_rate"))
        return ModuleResult(metrics=metrics)
