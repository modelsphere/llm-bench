"""
Academic Benchmark Evaluation (OpenCompass-style)

Evaluates the model against up to 8 academic benchmarks:
  aime2025, gpqa_diamond, ifeval, mmlu_pro, hle, livecodebench_v6,
  simpleqa, longbench_v2

Score and pass/fail are NOT computed here — the platform evaluator uses
per-benchmark MetricConfig rules (stored in BenchmarkModule.metric_configs_json).

Default evaluation (default_metric_configs):
  Score: equal-weight average accuracy across all 6 benchmarks (passthrough).
  Metrics not run (None) are skipped by the evaluator naturally.
  Pass: score > 0 (at least some correct output).
"""
from __future__ import annotations

import os
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from bench.modules.base import (
    EndpointConfig, MetricConfig, MetricDescriptor,
    ModuleResult, ProgressCallback, TestModule, _noop_progress,
    redact_secret_params,
)
from utils.logger import logger


_ALL_BENCHMARKS = [
    "aime2025", "gpqa_diamond", "ifeval", "mmlu_pro", "hle", "livecodebench_v6",
    "simpleqa", "longbench_v2",
]


class OpenCompassParams(BaseModel):
    dataset_dir: Optional[str] = Field(
        default=None,
        description="Path to academic benchmark data root. "
                    "Defaults to dataset/opencompass/data relative to repo root."
    )
    selected_benchmarks: List[str] = Field(
        default=_ALL_BENCHMARKS,
        description="Which benchmarks to run. Subset of: "
                    "aime2025, gpqa_diamond, ifeval, mmlu_pro, hle, livecodebench_v6, "
                    "simpleqa, longbench_v2"
    )
    max_workers: int = Field(default=16, ge=1, le=4096, description="Parallel inference workers per benchmark")
    request_timeout: float = Field(default=120.0, ge=1.0, le=86400.0, description="Per-request timeout in seconds")
    max_seconds: float = Field(
        default=57600.0, ge=0.0, le=604800.0,
        title="Module time cap (seconds)",
        description="Hard wall-clock cap for the whole module (all selected benchmarks "
                    "combined). When hit, the suite stops launching/awaiting more work "
                    "and returns partial results — the run completes normally, not "
                    "canceled. Enforced between benchmarks and inside each benchmark's "
                    "result-wait loop. 0 disables. Default 16h (57600s).",
    )
    max_tokens: int = Field(default=4096, ge=1, description="Max output tokens per inference call")
    sample_cap_aime: int = Field(default=0, ge=0, description="Max AIME samples (0 = all)")
    sample_cap_gpqa: int = Field(default=0, ge=0, description="Max GPQA samples (0 = all)")
    sample_cap_ifeval: int = Field(default=0, ge=0, description="Max IFEval samples (0 = all)")
    sample_cap_mmlu: int = Field(default=0, ge=0, description="Max MMLU-Pro samples (0 = all)")
    sample_cap_hle: int = Field(default=0, ge=0, description="Max HLE samples (0 = all)")
    sample_cap_lcb: int = Field(default=0, ge=0, description="Max LiveCodeBench samples (0 = all)")
    sample_cap_simpleqa: int = Field(default=0, ge=0, description="Max SimpleQA samples (0 = all, 4326 total)")
    sample_cap_longbench: int = Field(default=0, ge=0, description="Max LongBench-v2 samples (0 = all, 503 total)")
    # Repeat sampling: each item is queried k times; the reported score is avg@k
    # (mean accuracy over k samples), and pass@k (≥1 of k correct) is recorded
    # alongside. k>1 cuts variance on reasoning benchmarks (AIME, GPQA). The
    # number of requests per item is max(avg_k, pass_k), so leave pass_k=1 unless
    # you specifically want a pass@k view.
    avg_k_aime: int = Field(default=32, ge=1, le=128, description="AIME samples to average (avg@k)")
    avg_k_gpqa: int = Field(default=8, ge=1, le=128, description="GPQA samples to average (avg@k)")
    avg_k_ifeval: int = Field(default=1, ge=1, le=128, description="IFEval samples to average (avg@k)")
    avg_k_mmlu: int = Field(default=1, ge=1, le=128, description="MMLU-Pro samples to average (avg@k)")
    avg_k_hle: int = Field(default=1, ge=1, le=128, description="HLE samples to average (avg@k)")
    avg_k_lcb: int = Field(default=1, ge=1, le=128, description="LiveCodeBench samples to average (avg@k)")
    avg_k_simpleqa: int = Field(default=1, ge=1, le=128, description="SimpleQA samples to average (avg@k)")
    avg_k_longbench: int = Field(default=1, ge=1, le=128, description="LongBench-v2 samples to average (avg@k)")
    pass_k_aime: int = Field(default=1, ge=1, le=128, description="AIME samples for pass@k")
    pass_k_gpqa: int = Field(default=1, ge=1, le=128, description="GPQA samples for pass@k")
    pass_k_ifeval: int = Field(default=1, ge=1, le=128, description="IFEval samples for pass@k")
    pass_k_mmlu: int = Field(default=1, ge=1, le=128, description="MMLU-Pro samples for pass@k")
    pass_k_hle: int = Field(default=1, ge=1, le=128, description="HLE samples for pass@k")
    pass_k_lcb: int = Field(default=1, ge=1, le=128, description="LiveCodeBench samples for pass@k")
    pass_k_simpleqa: int = Field(default=1, ge=1, le=128, description="SimpleQA samples for pass@k")
    pass_k_longbench: int = Field(default=1, ge=1, le=128, description="LongBench-v2 samples for pass@k")
    sample_temperature: float = Field(
        default=1.0, ge=0.0, le=2.0,
        description="Sampling temperature for benchmarks that answer each question "
                    "multiple times (e.g. AIME avg@32, GPQA avg@8) so the repeated "
                    "samples differ. Benchmarks that answer once (k=1, e.g. MMLU-Pro) "
                    "run greedy at temperature 0 and ignore this — unless 'Force "
                    "temperature' is on.",
    )
    sample_top_p: float = Field(
        default=0.95, ge=0.0, le=1.0,
        description="Nucleus top_p applied alongside the sampling temperature on "
                    "multi-sample benchmarks (k>1). Single-shot (k=1) benchmarks stay "
                    "at 1.0 and ignore this — unless 'Force temperature' is on.",
    )
    force_temperature: bool = Field(
        default=False,
        description="Apply the sampling temperature/top_p to EVERY benchmark "
                    "regardless of k, so even single-shot (k=1) benchmarks sample "
                    "instead of decoding greedily. Off by default — k=1 stays greedy "
                    "and reproducible.",
    )
    stream: bool = Field(
        default=True,
        description="Stream responses (SSE) to measure per-request TTFT/TPOT and "
                    "throughput. Falls back to non-streamed parsing automatically if "
                    "the endpoint ignores stream. Disable only if a server mishandles SSE.",
    )
    # LLM judge, shared by SimpleQA and HLE — both have free-form answers that a
    # grader model must judge. SimpleQA self-judges when blank (the model under
    # test grades itself: convenient but mildly biased); HLE falls back to local
    # exact-match, which under-counts. Point these at a strong external grader
    # for the faithful setup of either.
    judge_api_url: str = Field(
        default="",
        title="LLM judge endpoint",
        description="Grader endpoint (OpenAI-compatible base URL) for SimpleQA and "
                    "HLE. Blank = SimpleQA self-judges with the model under test, "
                    "and HLE falls back to exact-match grading.",
    )
    judge_model: str = Field(
        default="",
        title="LLM judge model",
        description="Grader model name for SimpleQA and HLE. Blank = SimpleQA "
                    "self-judges (target model); HLE uses exact-match.",
    )
    judge_api_key: str = Field(
        default="",
        title="LLM judge API key",
        description="API key for the grader endpoint. Only used when a separate "
                    "judge_api_url is set.",
    )
    judge_max_tokens: int = Field(
        default=256, ge=1,
        title="SimpleQA judge max tokens",
        description="Max tokens for one SimpleQA grade. A non-reasoning grader "
                    "needs only a few; raise it (or use a non-reasoning grader) when "
                    "self-judging with a reasoning model.",
    )
    hle_use_judge: bool = Field(
        default=True,
        title="Grade HLE with the LLM judge",
        description="Grade HLE with the configured LLM judge, as the official HLE "
                    "harness does (it has no exact-match path). Has no effect when "
                    "no judge is configured. Turn off to force local exact-match "
                    "grading — cheaper, but it under-counts free-form answers "
                    "(~76-80% of HLE) and is not comparable to published scores.",
    )
    hle_judge_max_tokens: int = Field(
        default=2048, ge=1,
        title="HLE judge max tokens",
        description="Max tokens for one HLE judgement. Much larger than the SimpleQA "
                    "budget because the HLE judge writes an extracted answer and its "
                    "reasoning before the verdict (upstream allows 4096). Too small "
                    "and every reply is cut off before the verdict, silently falling "
                    "back to exact-match.",
    )
    longbench_max_input_tokens: int = Field(
        default=120000, ge=0,
        title="LongBench-v2 max context (tokens)",
        description="LongBench-v2 context middle-truncation budget in tokens "
                    "(keep head+tail, drop middle), mirroring the official pred.py. "
                    "0 = send the full context untruncated. Lower it to match a "
                    "smaller-context endpoint, or those long requests are rejected. "
                    "Auto-clamped down to fit the server's max_model_len minus the "
                    "output budget when /v1/models exposes it.",
    )
    longbench_max_output_tokens: int = Field(
        default=8192, ge=0,
        title="LongBench-v2 max output tokens",
        description="Output-token cap for LongBench-v2 only, overriding 'Max output "
                    "tokens' for this one benchmark. LongBench-v2 is input-heavy but "
                    "answers with a single multiple-choice line, so a large suite-wide "
                    "max_tokens (sized for reasoning benchmarks) would otherwise steal "
                    "the context window and get long requests rejected with 400. Raise "
                    "it for a reasoning model that needs more room to think; 0 = inherit "
                    "the suite-wide Max output tokens.",
    )

    @field_validator("judge_api_url", "judge_model", "judge_api_key")
    @classmethod
    def _strip_judge_fields(cls, v: str) -> str:
        # Pasted values routinely carry a trailing newline/space; a key with a
        # newline makes `requests` reject the Authorization header outright, and
        # a padded URL/model 404s — all of which the grader would swallow as
        # silent NOT_ATTEMPTED grades.
        return v.strip()

    @model_validator(mode="after")
    def _judge_fields_consistent(self):
        # Validated at benchmark save time too (the admin API runs params_json
        # through this schema), so misconfigurations surface in the editor
        # instead of as a silently-zero SimpleQA score.
        if self.judge_api_url and not self.judge_model:
            raise ValueError(
                "judge_model is required when judge_api_url is set — otherwise the "
                "grader would ask the external judge endpoint for the target model's "
                "name, which it almost certainly doesn't serve."
            )
        if self.judge_api_key and not self.judge_api_url:
            raise ValueError(
                "judge_api_key is set but judge_api_url is blank, so the key would "
                "never be used (self-judge authenticates with the target endpoint's "
                "own key). Set judge_api_url or clear the key."
            )
        return self


class OpenCompassModule(TestModule):
    """Self-implemented 6-benchmark academic evaluation."""

    name = "opencompass"
    display_name = "Academic Benchmarks"
    description = (
        "Evaluates model quality against up to 8 academic benchmarks: "
        "AIME 2025 (math), GPQA Diamond (science), IFEval (instruction following), "
        "MMLU-Pro (knowledge), HLE (reasoning), LiveCodeBench V6 (code generation), "
        "SimpleQA (short-form factuality, LLM-graded), LongBench-v2 (long-context MCQ). "
        "Default score = equal-weight average accuracy across selected benchmarks. "
        "Admin can adjust per-benchmark weights."
    )
    ParamsSchema = OpenCompassParams

    metrics_descriptors = [
        MetricDescriptor("aime2025",        "AIME 2025",      "%", "Math olympiad accuracy",          higher_is_better=True),
        MetricDescriptor("gpqa_diamond",    "GPQA Diamond",   "%", "Graduate-level science accuracy", higher_is_better=True),
        MetricDescriptor("ifeval",          "IFEval",         "%", "Instruction-following accuracy",  higher_is_better=True),
        MetricDescriptor("mmlu_pro",        "MMLU-Pro",       "%", "Multi-domain knowledge accuracy", higher_is_better=True),
        MetricDescriptor("hle",             "HLE",            "%", "Humanities & language reasoning", higher_is_better=True),
        MetricDescriptor("livecodebench_v6","LiveCodeBench v6","%","Code generation pass rate",        higher_is_better=True),
        MetricDescriptor("simpleqa",        "SimpleQA",       "%", "Short-form factuality, % correct (LLM-graded; F1 & not-attempted in details)", higher_is_better=True),
        MetricDescriptor("longbench_v2",    "LongBench v2",   "%", "Long-context multiple-choice accuracy (by-difficulty/length in details)",       higher_is_better=True),
        # Generation-performance indicators, aggregated across all benchmark
        # requests (count-weighted means; service_tps = total tokens / wall time).
        # These describe quality-of-service, not correctness — display by default.
        MetricDescriptor("ttft_mean_ms", "TTFT Mean",   "ms",    "Time to first token, mean across requests",   higher_is_better=False),
        MetricDescriptor("tpot_mean_ms", "TPOT Mean",   "ms",    "Time per output token, mean across requests", higher_is_better=False),
        MetricDescriptor("output_tps",   "Output TPS",  "tok/s", "Per-request output tokens/sec, mean",         higher_is_better=True),
        MetricDescriptor("service_tps",  "Service TPS", "tok/s", "Aggregate output tokens / inference wall time", higher_is_better=True),
        MetricDescriptor("gen_tokens",   "Gen Tokens",  "tok",   "Total output tokens generated across the suite", higher_is_better=True),
    ]

    default_metric_configs = [
        # Equal-weight average of all benchmarks (None values skipped by evaluator)
        MetricConfig("aime2025",         role="score", weight=1.0, formula="passthrough"),
        MetricConfig("gpqa_diamond",     role="score", weight=1.0, formula="passthrough"),
        MetricConfig("ifeval",           role="score", weight=1.0, formula="passthrough"),
        MetricConfig("mmlu_pro",         role="score", weight=1.0, formula="passthrough"),
        MetricConfig("hle",              role="score", weight=1.0, formula="passthrough"),
        MetricConfig("livecodebench_v6", role="score", weight=1.0, formula="passthrough"),
        MetricConfig("simpleqa",         role="score", weight=1.0, formula="passthrough"),
        MetricConfig("longbench_v2",     role="score", weight=1.0, formula="passthrough"),
        # Perf metrics are informational here (this is a quality benchmark). An
        # admin can promote any of these to a redline via MetricConfig in the UI.
        MetricConfig("ttft_mean_ms", role="display"),
        MetricConfig("tpot_mean_ms", role="display"),
        MetricConfig("output_tps",   role="display"),
        MetricConfig("service_tps",  role="display"),
        MetricConfig("gen_tokens",   role="display"),
    ]

    def run(
        self,
        endpoint: EndpointConfig,
        params: BaseModel,
        output_dir: str,
        progress_cb: ProgressCallback = _noop_progress,
        cancel_event=None,
    ) -> ModuleResult:
        p: OpenCompassParams = params  # type: ignore[assignment]
        os.makedirs(output_dir, exist_ok=True)

        selected = set(p.selected_benchmarks) & set(_ALL_BENCHMARKS)
        skip_set = set(_ALL_BENCHMARKS) - selected

        # Echo the params the worker actually received so we can confirm that
        # admin-edited BenchmarkModule.params_json reached this layer intact.
        # Secrets are masked: this log is persisted per-submission and
        # downloadable by the submitter, who must not see the admin's judge key.
        logger.info(
            "[opencompass] received params: %s",
            redact_secret_params(p.model_dump()),
        )
        progress_cb(0.05, f"Running {len(selected)} benchmarks: {sorted(selected)}")

        # All runtime knobs are passed as OpenCompassTest constructor args.
        # The env-driven module-level defaults in opencompass.py freeze at
        # import time, so mutating os.environ from this method is useless on
        # long-lived dramatiq workers — see the docstring there + ReplayTest
        # for the same trap and fix template.
        sample_caps = {
            "aime2025":         p.sample_cap_aime,
            "gpqa_diamond":     p.sample_cap_gpqa,
            "ifeval":           p.sample_cap_ifeval,
            "mmlu_pro":         p.sample_cap_mmlu,
            "hle":              p.sample_cap_hle,
            "livecodebench_v6": p.sample_cap_lcb,
            "simpleqa":         p.sample_cap_simpleqa,
            "longbench_v2":     p.sample_cap_longbench,
        }
        avg_k = {
            "aime2025":         p.avg_k_aime,
            "gpqa_diamond":     p.avg_k_gpqa,
            "ifeval":           p.avg_k_ifeval,
            "mmlu_pro":         p.avg_k_mmlu,
            "hle":              p.avg_k_hle,
            "livecodebench_v6": p.avg_k_lcb,
            "simpleqa":         p.avg_k_simpleqa,
            "longbench_v2":     p.avg_k_longbench,
        }
        pass_k = {
            "aime2025":         p.pass_k_aime,
            "gpqa_diamond":     p.pass_k_gpqa,
            "ifeval":           p.pass_k_ifeval,
            "mmlu_pro":         p.pass_k_mmlu,
            "hle":              p.pass_k_hle,
            "livecodebench_v6": p.pass_k_lcb,
            "simpleqa":         p.pass_k_simpleqa,
            "longbench_v2":     p.pass_k_longbench,
        }
        skip_flags = {name: (name in skip_set) for name in _ALL_BENCHMARKS}

        try:
            from bench.tests.functional.opencompass import OpenCompassTest
            # Use param dataset_dir only if the directory exists; otherwise pass None
            # so the test falls back to its module-level ACADEMIC_DATA_DIR default
            # (which is itself env-driven at worker startup).
            effective_dataset_dir = p.dataset_dir if (p.dataset_dir and os.path.isdir(p.dataset_dir)) else None
            if p.dataset_dir and not effective_dataset_dir:
                logger.warning("[opencompass] dataset_dir %s not found — falling back to ACADEMIC_DATA_DIR", p.dataset_dir)
            test = OpenCompassTest(
                api_url=endpoint.api_url,
                model=endpoint.model,
                api_key=endpoint.api_key,
                output_dir=output_dir,
                dataset_dir=effective_dataset_dir,
                cancel_event=cancel_event,
                progress_cb=progress_cb,
                max_workers=p.max_workers,
                request_timeout=p.request_timeout,
                max_seconds=p.max_seconds,
                max_tokens=p.max_tokens,
                sample_caps=sample_caps,
                skip_flags=skip_flags,
                avg_k=avg_k,
                pass_k=pass_k,
                sample_temperature=p.sample_temperature,
                sample_top_p=p.sample_top_p,
                force_temperature=p.force_temperature,
                stream=p.stream,
                judge_api_url=p.judge_api_url,
                judge_model=p.judge_model,
                judge_api_key=p.judge_api_key,
                judge_max_tokens=p.judge_max_tokens,
                hle_use_judge=p.hle_use_judge,
                hle_judge_max_tokens=p.hle_judge_max_tokens,
                longbench_max_input_tokens=p.longbench_max_input_tokens,
                longbench_max_output_tokens=p.longbench_max_output_tokens,
            )
            result = test.run()
        except Exception:
            from traceback import format_exc
            err = format_exc()
            logger.error("[opencompass] test failed: %s", err)
            return ModuleResult(error=err)

        metrics = result.metrics

        # Flatten per-benchmark scores to top level so the evaluator can look them up
        scores_dict: dict = metrics.get("scores", {})
        for bench_name, acc in scores_dict.items():
            if isinstance(acc, (int, float)) and bench_name not in metrics:
                metrics[bench_name] = acc

        # Flatten suite-level generation-perf metrics (ttft/tpot/throughput) to
        # top level too, so the evaluator/UI surfaces them like the score metrics.
        perf_dict: dict = metrics.get("perf", {}) or {}
        for perf_key, perf_val in perf_dict.items():
            if isinstance(perf_val, (int, float)) and perf_key not in metrics:
                metrics[perf_key] = perf_val

        valid_scores = [v for v in scores_dict.values() if isinstance(v, (int, float))]
        avg = sum(valid_scores) / len(valid_scores) if valid_scores else 0.0
        progress_cb(0.99, f"Metrics collected — avg_score={avg:.3f}")
        logger.info("[opencompass] scores=%s avg=%.3f", scores_dict, avg)
        return ModuleResult(metrics=metrics)
