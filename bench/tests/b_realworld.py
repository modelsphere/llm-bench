"""
Type B: Real-Request Simulation Test (§5.2)

Purpose : Simulate real-world traffic patterns; detect red-line violations.
Dataset : ShareGPT (local file). Pass the path via the `dataset_path`
          constructor kwarg. The legacy `SHAREGPT_DATASET_PATH` env var still
          works as a fallback for CLI usage but is deprecated for the
          platform path — long-lived worker processes capture it at module
          import time, so mutating os.environ between submissions has no
          effect. Always pass dataset_path explicitly from callers.
Profile : poisson  (inter-arrival times follow a Poisson process)
Rates   : avg concurrency [8, 32]
Duration: 8 min × 2 rounds per concurrency level
Warmup  : 30 s
Outputs : ttft/tpot/itl at p50, p90 and p99, plus uptime  → p99/p50 and uptime
          feed the red-line gate + S_latency / S_stability; p90 is informational
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional

from bench.result import TestResult
from bench.tests.base import GuideLLMBaseTest
from utils.bench_config import BenchConfig
from utils.logger import logger


# Latency/reliability ceilings used ONLY to choose which concurrency level is
# promoted to the flat top-level summary keys when a run sweeps several. They
# judge nothing: a benchmark's pass/fail comes from its own metric_configs
# (app/queue/jobs.py -> bench.modules.evaluator), and every per-level value is
# reported whatever is chosen here. Deliberately loose, so the promotion falls
# back to "best worst-case latency" only when a level is genuinely broken.
_PROMOTION_CEILINGS: dict[str, float] = {
    "ttft_p99_ms": 120000.0,
    "tpot_p99_ms": 200.0,
    "itl_p99_ms": 150.0,
    "uptime": 0.90,          # successful_requests / total_requests
}

# Legacy fallback for CLI usage only. Platform callers MUST pass dataset_path
# via the constructor — see the docstring for why.
_DEFAULT_SHAREGPT_DATASET_PATH = os.getenv("SHAREGPT_DATASET_PATH")

# Input length distribution matching ShareGPT (short 30%, medium 50%, long 20%)
# Expressed as a weighted-average token count passed to GuideLLM's data spec.
# GuideLLM will sample from the file; these values are used as fallback fixed tokens
# if the dataset file is not found.
# Defaults for GuideLLM backend (read at __init__ time to allow CLI args → env var override)
_DEFAULT_PROMPT_TOKENS = 50000
_DEFAULT_OUTPUT_TOKENS = 1500
_DEFAULT_RATE = 50.0
_DEFAULT_MAX_SECONDS = 1200
_DEFAULT_TIMEOUT = 300
_DEFAULT_WARMUP = 30.0
# Count-mode phase sizes as request fractions (see _make_config).
_DEFAULT_WARMUP_FRACTION = 0.1
_DEFAULT_COOLDOWN_FRACTION = 0.05
_DEFAULT_RAMPUP = 30.0
_DEFAULT_ROUNDS = 1
_DEFAULT_PROFILE = "concurrent"

class RealWorldTest(GuideLLMBaseTest):
    """
    §5.2 B. 模拟真实请求测试

    Drives GuideLLM with a Poisson arrival profile and ShareGPT data.
    Red-line metrics (TTFT P99, TPOT P99, ITL P99, uptime) are extracted
    and checked by the BenchmarkRunner after this test returns.
    """

    name = "real_world"

    def __init__(self, *args, dataset_path: Optional[str] = None,
                 processor_path: Optional[str] = None,
                 rates: Optional[list] = None,
                 requests_per_concurrency: Optional[int] = None,
                 random_seed: Optional[int] = None,
                 warmup_fraction: Optional[float] = None,
                 cooldown_fraction: Optional[float] = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.processor_path = processor_path or None
        # Seed for guidellm's synthetic prompts; None = BenchConfig default.
        self.random_seed = random_seed
        # Count-mode warmup/cooldown as request fractions (see _make_config).
        self.warmup_fraction = (_DEFAULT_WARMUP_FRACTION if warmup_fraction is None
                                else float(warmup_fraction))
        self.cooldown_fraction = (_DEFAULT_COOLDOWN_FRACTION if cooldown_fraction is None
                                  else float(cooldown_fraction))
        # Per-slot request target: level c stops after c × this many requests
        # (or at MAX_SECONDS, whichever first). Fixed sample size per slot
        # means wall-clock adapts to the service's real throughput instead of
        # a fixed duration under- or over-sampling it. Explicit kwarg wins
        # (platform path); env var covers CLI use; 0/unset → duration-only.
        if requests_per_concurrency is not None:
            _rpc: Optional[int] = int(requests_per_concurrency)
        else:
            _env_rpc = int(os.getenv("REALWORLD_TEST_REQUESTS_PER_CONCURRENCY", "0"))
            _rpc = _env_rpc if _env_rpc > 0 else None
        self.REQUESTS_PER_CONCURRENCY = _rpc
        # Read env vars here (not at module import) so CLI args set via os.environ take effect
        self.prompt_tokens = int(os.getenv("REALWORLD_TEST_PROMPT_TOKENS", str(_DEFAULT_PROMPT_TOKENS)))
        self.output_tokens = int(os.getenv("REALWORLD_TEST_OUTPUT_TOKENS", str(_DEFAULT_OUTPUT_TOKENS)))
        # Concurrency levels: explicit `rates` kwarg wins (platform path — same
        # rationale as dataset_path above). The env var remains for CLI usage
        # and accepts a comma-separated list, e.g. "1,2,4,8,16"; each level runs
        # sequentially for MAX_SECONDS (guidellm concurrent profile).
        if rates:
            self.RATES = [float(r) for r in rates]
        else:
            _raw = os.getenv("REALWORLD_TEST_CONCURRENCY", str(_DEFAULT_RATE))
            self.RATES = [float(r) for r in _raw.split(",") if r.strip()]
        self.MAX_SECONDS = int(os.getenv("REALWORLD_TEST_MAX_SECONDS", str(_DEFAULT_MAX_SECONDS)))
        self.TIMEOUT = int(os.getenv("REALWORLD_TEST_TIMEOUT", str(_DEFAULT_TIMEOUT)))
        self.WARMUP = float(os.getenv("REALWORLD_TEST_WARMUP", str(_DEFAULT_WARMUP)))
        self.RAMPUP = float(os.getenv("REALWORLD_TEST_RAMPUP", str(_DEFAULT_RAMPUP)))
        self.ROUNDS = int(os.getenv("REALWORLD_TEST_ROUNDS", str(_DEFAULT_ROUNDS)))
        self.PROFILE = os.getenv("REALWORLD_PROFILE", _DEFAULT_PROFILE)
        # Explicit kwarg wins. Fall back to the (deprecated) env var only when
        # the caller didn't pass anything — preserves CLI behavior without
        # leaving the platform path exposed to the env-capture bug.
        self.dataset_path = dataset_path if dataset_path else _DEFAULT_SHAREGPT_DATASET_PATH

    def _make_config(self) -> BenchConfig:
        # Use ShareGPT file as the dataset if it exists; fall back to random tokens
        if self.dataset_path and os.path.exists(self.dataset_path):
            dataset = self.dataset_path
        else:
            logger.warning(
                "ShareGPT dataset not found at %s — falling back to random tokens. "
                "Pass dataset_path to RealWorldTest or set SHAREGPT_DATASET_PATH (CLI only).",
                self.dataset_path,
            )
            dataset = "random"

        # Count-limited runs cap each level at concurrency × per-slot target.
        # A count-limited level lasts only as long as those requests take —
        # often a few seconds — so every DURATION-based phase setting has to be
        # re-expressed against request counts, or it silently swallows the run:
        #
        #   warmup=30s  -> the whole level finishes inside the warmup window,
        #                  so ZERO requests are measured and every metric is 0
        #                  (verified against mock_server: 0 measured requests,
        #                  negative reported durations).
        #   rampup=30s  -> streams start staggered across 30s, so a level that
        #                  ends on its count never reaches target concurrency
        #                  and its measurement window is mostly idle
        #                  (verified: output TPS understated ~12x at c=2).
        #   cooldown    -> computed against a duration the level never reaches;
        #                  in requests mode it trims the intended tail, the
        #                  ragged end where concurrency decays as the last
        #                  slots finish.
        #
        # So in count mode: phases become request fractions and rampup is off.
        # The default 10%/5% split mirrors the duration-mode defaults (30s
        # warmup and 5% cooldown against a 300s level); callers that know
        # their per-slot count set the fractions in waves (one wave = 1/N).
        max_requests_per_level = None
        warmup: "float | dict" = self.WARMUP
        cooldown: "float | dict" = 0.05
        rampup = self.RAMPUP
        if self.REQUESTS_PER_CONCURRENCY:
            max_requests_per_level = [
                max(1, int(round(r)) * self.REQUESTS_PER_CONCURRENCY)
                for r in self.RATES
            ]
            warmup = {"percent": self.warmup_fraction, "mode": "requests"}
            cooldown = {"percent": self.cooldown_fraction, "mode": "requests"}
            rampup = 0.0
            logger.info(
                "[%s] count-limited levels (%d req/slot): warmup/cooldown "
                "switched to request fractions (%g/%g) and rampup disabled "
                "— duration-based phases would consume the whole level "
                "(warmup_seconds=%s, rampup=%s ignored)",
                self.name, self.REQUESTS_PER_CONCURRENCY,
                self.warmup_fraction, self.cooldown_fraction, self.WARMUP, self.RAMPUP,
            )

        return BenchConfig(
            api_url=self.api_url,
            model=self.model,
            api_key=self.api_key,
            dataset_name=dataset,
            prompt_tokens=self.prompt_tokens,
            output_tokens=self.output_tokens,
            rate=self.RATES,
            max_seconds=self.MAX_SECONDS,
            timeout=self.TIMEOUT,
            warmup=warmup,
            cooldown=cooldown,
            rampup=rampup,
            profile=self.PROFILE,
            guidellm_output_dir=self.output_dir,
            max_requests_per_level=max_requests_per_level,
            **({"random_seed": int(self.random_seed)} if self.random_seed is not None else {}),
        )

    def _extract_metrics(self, guidellm_results: Dict[str, Any]) -> Dict[str, Any]:
        """
        Extract P99 latencies and uptime per concurrency level, plus cross-level
        worst-case values used for red-line checking.
        """
        metrics: Dict[str, Any] = {}

        for bench in guidellm_results.get("benchmarks", []):
            conc = bench["config"]["strategy"]["max_concurrency"]
            m = bench["metrics"]
            s = lambda key: m[key]["successful"]  # noqa: E731
            suffix = f"_c{conc}"

            for _per in ('p50', 'p90', 'p99'):
                metrics[f"input_tps_{_per}{suffix}"]  = s("prompt_tokens_per_second")["percentiles"][_per]
                metrics[f"output_tps_{_per}{suffix}"] = s("output_tokens_per_second")["percentiles"][_per]
                metrics[f"total_tps_{_per}{suffix}"] = s("tokens_per_second")["percentiles"][_per]
                metrics[f"ttft_{_per}_ms{suffix}"]    = s("time_to_first_token_ms")["percentiles"][_per]
                metrics[f"tpot_{_per}_ms{suffix}"]    = s("time_per_output_token_ms")["percentiles"][_per]
                metrics[f"itl_{_per}_ms{suffix}"]     = s("inter_token_latency_ms")["percentiles"][_per]

            metrics[f"ttft_mean_ms{suffix}"]    = s("time_to_first_token_ms")['mean']
            metrics[f"tpot_mean_ms{suffix}"]    = s("time_per_output_token_ms")['mean']
            metrics[f"itl_mean_ms{suffix}"]     = s("inter_token_latency_ms")['mean']
            metrics[f"input_tps_mean{suffix}"]  = s("prompt_tokens_per_second")['mean']
            metrics[f"output_tps_mean{suffix}"] = s("output_tokens_per_second")['mean']
            metrics[f"total_tps_mean{suffix}"]  = s("tokens_per_second")['mean']

            # How the level actually ran, as opposed to how it was configured.
            # Both are needed to read a result honestly: a level capped by its
            # duration before reaching its request count produces a different
            # (and noisier) sample than one that ran to completion, and nothing
            # else in the output distinguishes the two.
            if bench.get("duration") is not None:
                metrics[f"duration_seconds{suffix}"] = float(bench["duration"])
            metrics[f"measured_requests{suffix}"] = float(
                m["request_totals"].get("total") or 0
            )

            totals = m["request_totals"]
            total = totals["total"]
            errored = totals["errored"]
            metrics[f"uptime{suffix}"] = (1 - errored / total) if total > 0 else 0.0

        return metrics

    def run(self, cancel_event=None) -> TestResult:
        """Run ROUNDS times and take the worst-case red-line metrics across rounds."""
        all_metrics: list[dict] = []
        last_error = None

        logger.info(f"Running {self.RATES} for {self.MAX_SECONDS} s, {self.prompt_tokens} in {self.output_tokens} out.")

        for _ in range(self.ROUNDS):
            result = super().run(cancel_event=cancel_event)
            if not result.passed:
                last_error = result.error
                continue
            all_metrics.append(result.metrics)

        if not all_metrics:
            return TestResult(name=self.name, passed=False, metrics={}, error=last_error)

        # Worst-case (conservative) for red-line metrics
        merged: dict = {}
        uptime_keys = {"uptime"}

        for key in all_metrics[0]:
            values = [m[key] for m in all_metrics if key in m]
            if key in uptime_keys:
                merged[key] = min(values)   # worst uptime
            else:
                merged[key] = max(values) if any(
                    s in key for s in ("ttft_", "tpot_", "itl_")
                ) else sum(values) / len(values)

        # Aggregate across concurrency levels → top-level summary keys
        # Select the concurrency level with highest total_tps whose latency passes redlines.
        # If only one level exists, use it directly. If none pass redlines, fall back to
        # the one with the best (lowest) worst-case latency.
        conc_levels = sorted({
            int(k.split("_c")[-1])
            for k in merged
            if "_c" in k and k.split("_c")[-1].isdigit()
        })
        if conc_levels:
            def _conc_passes_redlines(c: int) -> bool:
                for metric, threshold in _PROMOTION_CEILINGS.items():
                    if metric == "uptime":
                        val = merged.get(f"uptime_c{c}")
                        if val is None or val < threshold:
                            return False
                    else:
                        val = merged.get(f"{metric}_c{c}")
                        if val is None or val > threshold:
                            return False
                return True

            def _conc_total_tps(c: int) -> float:
                return merged.get(f"total_tps_mean_c{c}") or merged.get(f"total_tps_p50_c{c}") or 0.0

            passing = [c for c in conc_levels if _conc_passes_redlines(c)]

            if len(conc_levels) == 1:
                best = conc_levels[0]
            elif passing:
                best = max(passing, key=_conc_total_tps)
            else:
                # No level passes redlines; pick the one with lowest max p99 latency as fallback
                logger.warning(
                    "No concurrency level passes all redlines; reporting best-latency level as fallback."
                )
                def _worst_p99(c: int) -> float:
                    return max(
                        merged.get(f"ttft_p99_ms_c{c}") or float("inf"),
                        merged.get(f"tpot_p99_ms_c{c}") or float("inf"),
                        merged.get(f"itl_p99_ms_c{c}") or float("inf"),
                    )
                best = min(conc_levels, key=_worst_p99)

            logger.info("Phase B reporting metrics for concurrency level c%d (total_tps=%.1f)", best, _conc_total_tps(best))
            merged["reported_concurrency"] = best

            for stat in ("ttft_p99_ms", "tpot_p99_ms", "itl_p99_ms",
                         "ttft_p90_ms", "tpot_p90_ms", "itl_p90_ms",
                         "ttft_p50_ms", "tpot_p50_ms", "itl_p50_ms",
                         "ttft_mean_ms", "tpot_mean_ms", "itl_mean_ms"):
                val = merged.get(f"{stat}_c{best}")
                if val is not None:
                    merged[stat] = val
            uptime_val = merged.get(f"uptime_c{best}")
            if uptime_val is not None:
                merged["uptime"] = uptime_val
            for stat, raw_key in (("input_tps_p50", "input_tps"), ("output_tps_p50", "output_tps")):
                val = merged.get(f"{raw_key}_p50_c{best}")
                if val is not None:
                    merged[stat] = val
            for stat in ("input_tps_mean", "output_tps_mean", "total_tps_mean"):
                val = merged.get(f"{stat}_c{best}")
                if val is not None:
                    merged[stat] = val

        return TestResult(name=self.name, passed=True, metrics=merged)
