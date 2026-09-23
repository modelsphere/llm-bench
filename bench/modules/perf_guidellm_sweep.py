"""
GuideLLM Concurrency Sweep Benchmark

Two search modes (`search_mode` param):

  grid (default): runs guidellm's concurrent profile at the admin-specified
    `concurrencies` levels in a single in-process run (each level executes
    sequentially), mirroring
    `guidellm benchmark --profile concurrent --rate 1,2,4,8,16`.

Level sampling is count-based by default (`requests_per_concurrency`,
`probe_requests_per_concurrency`): level c stops after c × the per-slot
target, keeping sample size per slot constant across levels while wall-clock
adapts to the service's throughput (a saturated service takes ∝ c longer per
level — the effect a fixed duration approximates badly in both directions).
`max_seconds`/`probe_seconds` remain hard per-level caps; clearing the count
params restores pure duration-limited levels.

  auto: discovers the maximum concurrency that meets the slo_* params —
    nobody has to guess a grid. Probes c=1, doubles until the SLOs first
    fail (or search_max_concurrency), binary-searches the pass/fail bracket
    down to search_granularity, then re-runs the winner for the full
    `max_seconds` as a confirmation run whose metrics become the canonical
    top-level keys (stepping down one passing level if the long run misses
    the SLOs the short probe met). Each probe executes guidellm in a
    DISPOSABLE SUBPROCESS (bench/probe_runner.py) killed by process group
    on deadline/cancel — guidellm's teardown can wedge after a clean run
    and its singleton locks and non-daemon worker forks leak, so nothing
    of one probe may survive into the next. `reported_concurrency` is the
    answer; probe levels appear as c{N} dicts like grid levels do (with
    `duration_seconds` — the measurement window — distinguishing
    short probes from the full confirmation run, so peak_* aggregates
    compare mixed sample sizes in auto mode).

    Two things keep consecutive runs from contaminating each other, both
    learned from a search whose c=32 probe passed with a 2s TTFT and whose
    c=32 confirmation then failed at 30s:
      - Drain between levels. A level ends client-side, abandoning its last
        wave; a server that does not abort on disconnect keeps generating
        them under the next level. So every level after the first waits
        `level_settle_seconds`, then sends tiny canary requests until one
        answers within tolerance of the idle-endpoint latency measured before
        the search (or `level_drain_max_seconds` runs out — the level runs
        regardless, with started_drained=0 recorded). A canary is the only
        load signal a black-box OpenAI endpoint offers: it queues behind
        whatever is still running.
      - A fresh synthetic-prompt seed per run (base 42 + run index). With one
        fixed seed every run replays the same prompts, and a server-side
        prefix cache warmed by the previous level answers the next level's
        prefills faster than genuinely new traffic would.
    Each level records drain_wait_seconds, pre_level_canary_ms and
    started_drained so a level that looks worse than its neighbours can be
    read back against the state of the endpoint it started on.

Metrics:
  Per level N:  input/output/total TPS, TTFT/TPOT/ITL percentiles, uptime —
                grouped in a nested dict per level, e.g.
                metrics["c8"] = {"output_tps_mean": ..., "ttft_p99_ms": ...}.
                MetricConfig rules address them with dotted keys
                ("c8.ttft_p99_ms") — the evaluator (resolve_metric) and the
                frontend both walk nested dicts, so per-level values can be
                used as score/redline/display terms like any flat metric.
  Canonical:    flat top-level keys, taken from the level selected by the
                `report_level` param:
                  slo_best (default) — highest-total-TPS level whose
                    per-request output TPS (output_tps / concurrency) and
                    TTFT P99 meet the configurable slo_* params; falls back
                    to the lowest level (with reported_level_meets_slo=0)
                    when no level qualifies.
                  peak  — highest-total-TPS level, SLOs ignored.
                  fixed — the level given in fixed_level.
                `reported_concurrency` records the chosen level. The selection
                is fully param-driven — the module declares no global redlines.
  Peaks:        peak_{input,output,total}_tps and peak_concurrency — the
                highest-throughput level regardless of SLOs.
  Search:       (auto mode) search_probes, search_converged and
                slo_fail_concurrency describe how the answer was found. Every
                level dict also carries meets_slo, plus how the level actually
                ran versus how it was configured: duration_seconds (the
                measurement window, warmup/cooldown excluded) and
                measured_requests, against duration_cap_seconds. A level whose
                measured_requests falls well short of concurrency x the
                per-slot count was stopped by its duration cap before
                finishing, and its percentiles will vary between runs — the
                module logs a warning naming the level when this happens.

Score and pass/fail are NOT computed here — the platform evaluator uses
per-benchmark MetricConfig rules. Default score/redlines operate on the
canonical (best-level) keys, so they work regardless of which levels are
configured. Per-level keys can't have static default_metric_configs (levels
are a param), so this module returns display-only config rows (dotted keys)
via ModuleResult.extra_display_configs; the worker appends those to the run's
metric-config snapshot so every level shows up in the UI.

Backend: guidellm.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from bench.modules.base import (
    AllRequestsFailed, BenchmarkCancelled, EndpointConfig, MetricConfig,
    MetricDescriptor, ModuleResult, ProgressCallback, TestModule, _noop_progress,
)
from bench.modules.perf_guidellm import (
    PerfGuidellmModule,
    _GuidellmBudgetMixin,
    run_guidellm_load,
)
from utils.logger import logger

_MAX_LEVELS = 16
_LEVEL_MIN, _LEVEL_MAX = 1, 1024

# Matches the per-level suffix RealWorldTest._extract_metrics appends.
_LEVEL_KEY_RE = re.compile(r"_c(\d+)$")

_DEFAULT_CONCURRENCIES = "1,2,4,8,16"

# --- auto-search subprocess management --------------------------------------
# Repo root (bench/modules/x.py → three levels up): the probe subprocess runs
# `python -m bench.probe_runner` with this as cwd so imports resolve.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# Parent-side probe deadline = warmup + duration + request_timeout (in-flight
# drain) + this grace for child startup (imports, tokenizer, dataset load) and
# teardown/report writing. The child's own watchdog fires 30s later as backup.
_PROBE_STARTUP_GRACE_SECONDS = 300.0
_PROBE_POLL_SECONDS = 0.5
_PROBE_TICK_SECONDS = 30.0
# Progress band for the auto search (mirrors perf_guidellm's elapsed thread:
# between the 0.05 "starting" tick and the 0.9 "aggregating" tick).
_PROGRESS_FLOOR = 0.10
_PROGRESS_CEILING = 0.85
# How many times the search may re-open the bracket after a confirmation run
# contradicts the probe that passed its level. Each round costs a bisect plus
# another confirmation, but with probes long enough to be trustworthy a
# contradiction is rare — so allow several rather than settling for a level
# well below the knee. The budget check gates them anyway.
_MAX_CONFIRM_ROUNDS = 4
# Drain-between-levels canary (see _wait_until_drained). The endpoint counts
# as drained when a canary answers within this multiple of its idle-reference
# latency plus the fixed slack — loose enough to ignore jitter on a sub-second
# reference, tight enough that a canary queued behind leftover generations
# (seconds to tens of seconds) never passes.
_DRAIN_CANARY_TOLERANCE = 2.0
_DRAIN_CANARY_SLACK_SECONDS = 0.5
_DRAIN_CANARY_TIMEOUT_SECONDS = 60.0
_DRAIN_POLL_SECONDS = 5.0
_DRAIN_REFERENCE_SAMPLES = 3
# Base seed for guidellm's synthetic prompts. Each run of the auto search
# gets base + its run index: with one fixed seed every run replays the same
# prompt sequence, and a server-side prefix cache warmed by the previous level
# makes the next level's TTFT look better than a fresh prompt would.
_BASE_RANDOM_SEED = 42


def parse_concurrencies(raw: str) -> List[int]:
    """Parse a comma/space-separated concurrency list into a list of ints.

    Levels must be STRICTLY INCREASING — out-of-order or duplicate levels are
    rejected rather than silently reordered/deduped, so a benchmark config
    always says exactly what will run (levels execute in the order given).
    Raises ValueError with a user-facing message on any invalid input — this
    surfaces in the admin form via pydantic validation.
    """
    tokens = [t for t in re.split(r"[,\s]+", raw.strip()) if t]
    if not tokens:
        raise ValueError("concurrencies must contain at least one level, e.g. '1,2,4,8,16'")
    levels: List[int] = []
    for t in tokens:
        try:
            n = int(t)
        except ValueError:
            raise ValueError(f"invalid concurrency level {t!r} — must be an integer")
        if not (_LEVEL_MIN <= n <= _LEVEL_MAX):
            raise ValueError(f"concurrency level {n} out of range [{_LEVEL_MIN}, {_LEVEL_MAX}]")
        if levels and n <= levels[-1]:
            raise ValueError(
                f"concurrency levels must be strictly increasing: "
                f"{n} follows {levels[-1]} in {raw!r}"
            )
        levels.append(n)
    if len(levels) > _MAX_LEVELS:
        raise ValueError(f"too many concurrency levels ({len(levels)}) — max {_MAX_LEVELS}")
    return levels


# Visibility rules consumed by the admin param form (ModuleParamForm reads
# "x-visible-when": every listed param must currently hold one of the listed
# values for the field to render). Declared here rather than in the frontend so
# the module stays the single source of truth for what its params mean.
_GRID_ONLY = {"x-visible-when": {"search_mode": ["grid"]}}
_AUTO_ONLY = {"x-visible-when": {"search_mode": ["auto"]}}


class PerfGuidellmSweepParams(BaseModel):
    # First field: it decides which of the others apply, so it renders first.
    search_mode: Literal["grid", "auto"] = Field(
        default="grid",
        description=(
            "'grid' = run the admin-specified concurrencies list (original "
            "behavior). 'auto' = discover the maximum concurrency meeting the "
            "slo_* params: doubling ramp + binary search over short probes, "
            "then a full-length confirmation run at the winner. Auto ignores "
            "`concurrencies` and always reports the confirmed level."
        ),
    )
    concurrencies: str = Field(
        default=_DEFAULT_CONCURRENCIES,
        json_schema_extra=_GRID_ONLY,
        description=(
            "[grid] Comma-separated concurrency levels to sweep, strictly "
            "increasing, e.g. '1,2,4,8,16'. Each level runs sequentially, so "
            "total runtime ≈ levels × (warmup_seconds + level duration). "
            "Ignored when search_mode='auto', which picks its own levels."
        ),
    )
    input_tokens: int = Field(default=50000, ge=1, le=1_000_000_000, description="Input tokens per request")
    output_tokens: int = Field(default=1500, ge=1, description="Max output tokens per request")
    max_seconds: float = Field(
        default=300.0, ge=1.0, le=604800.0,
        description=(
            "[grid] Test duration PER concurrency level, in seconds. "
            "[auto] Duration of the final confirmation run at the discovered level."
        ),
    )
    request_timeout: float = Field(default=120.0, ge=1.0, le=86400.0, description="Per-request timeout in seconds")
    warmup_seconds: float = Field(default=30.0, ge=0.0, le=86400.0, description="Warmup duration per concurrency level in seconds")
    dataset_path: Optional[str] = Field(
        default=None,
        json_schema_extra={"x-hidden": True},
        description=(
            "DEPRECATED and unused — file-backed datasets (ShareGPT) are no "
            "longer supported; every run uses synthetic random tokens. Kept "
            "only so configs saved earlier still load, and hidden in the admin "
            "form. A run whose config still carries a path fails with a "
            "message telling you to clear it."
        ),
    )
    processor_path: Optional[str] = Field(
        default=None,
        description=(
            "Tokenizer/processor directory for accurate token counts. "
            "Relative paths resolve against the project root (e.g. "
            "'/models/Qwen3-8B' or a HF repo id). Leave blank to use the built-in "
            "default or the PROCESSOR_PATH env var."
        ),
    )
    report_level: Literal["slo_best", "peak", "fixed"] = Field(
        default="slo_best",
        json_schema_extra=_GRID_ONLY,
        description=(
            "Which swept level provides the canonical top-level metrics: "
            "'slo_best' = highest-throughput level meeting the slo_* params below; "
            "'peak' = highest-throughput level regardless of SLOs; "
            "'fixed' = the level given in fixed_level."
        ),
    )
    fixed_level: Optional[int] = Field(
        default=None, ge=_LEVEL_MIN, le=_LEVEL_MAX,
        json_schema_extra={"x-visible-when": {"search_mode": ["grid"],
                                              "report_level": ["fixed"]}},
        description=(
            "Concurrency level to report when report_level='fixed'. Must be one "
            "of the swept concurrencies. Ignored for other report_level values."
        ),
    )
    slo_min_request_output_tps: float = Field(
        default=10.0, ge=0.0,
        description=(
            "[slo_best/auto] Minimum per-request output TPS "
            "(aggregate output_tps ÷ concurrency) for a level to qualify."
        ),
    )
    slo_max_ttft_ms: float = Field(
        default=120000.0, gt=0.0,
        description=(
            "[slo_best/auto] Maximum TTFT in ms for a level to qualify, applied "
            "to the statistic chosen by slo_ttft_percentile."
        ),
    )
    slo_ttft_percentile: Literal["mean", "p50", "p90", "p99"] = Field(
        default="p99",
        description=(
            "[slo_best/auto] Which TTFT statistic the slo_max_ttft_ms cap "
            "applies to."
        ),
    )
    slo_max_ttft_p99_ms: Optional[float] = Field(
        default=None, gt=0.0,
        json_schema_extra={"x-visible-when-set": True},
        description=(
            "DEPRECATED — use slo_max_ttft_ms. Kept so benchmarks configured "
            "before the split keep their threshold: when set, it overrides "
            "slo_max_ttft_ms. Despite the name it was never fixed to P99; it "
            "has always applied at slo_ttft_percentile. Clear it to switch to "
            "slo_max_ttft_ms."
        ),
    )
    search_max_concurrency: int = Field(
        json_schema_extra=_AUTO_ONLY,
        default=1024, ge=2, le=_LEVEL_MAX,
        description="[auto] Upper bound for the concurrency search.",
    )
    probe_seconds: float = Field(
        json_schema_extra=_AUTO_ONLY,
        default=900.0, ge=1.0, le=86400.0,
        description=(
            "[auto] SAFETY CAP on how long one search probe may run — not the "
            "probe's intended length. What sizes a probe is "
            "probe_requests_per_concurrency (level c stops after c × that many "
            "requests); this cap only stops a level that is far slower than "
            "expected. Keep it comfortably above the expected probe length "
            "(≈ per-slot requests × per-request latency): a probe cut short by "
            "this cap measures a truncated, noisier sample, which shows up as "
            "levels whose duration_seconds sits at the cap and whose "
            "measured_requests falls short of c × the per-slot count."
        ),
    )
    search_granularity: int = Field(
        json_schema_extra=_AUTO_ONLY,
        default=0, ge=0, le=_LEVEL_MAX,
        description=(
            "[auto] Stop the binary search once the pass/fail bracket is at "
            "most this wide. 0 = automatic: 1/8 of the best passing level, "
            "minimum 1."
        ),
    )
    max_total_seconds: float = Field(
        json_schema_extra=_AUTO_ONLY,
        default=7200.0, ge=1.0, le=86400.0,
        description=(
            "[auto] Wall-clock budget for the whole search. Probing stops (and "
            "the confirmation run starts) once the next probe would not fit."
        ),
    )
    requests_per_concurrency: Optional[int] = Field(
        default=20, ge=1, le=100_000,
        description=(
            "[grid + auto confirmation] Target request count per concurrency "
            "slot: level c stops after c × this many requests, or at its "
            "duration cap (max_seconds), whichever comes first. A fixed sample "
            "size per slot keeps statistical power constant across levels and "
            "lets wall-clock adapt to the service's real throughput. Leave "
            "empty for duration-only levels (legacy behavior)."
        ),
    )
    probe_requests_per_concurrency: Optional[int] = Field(
        json_schema_extra=_AUTO_ONLY,
        default=5, ge=1, le=100_000,
        description=(
            "[auto] Same as requests_per_concurrency but for search probes — a "
            "small count gives a quick pass/fail verdict (probe_seconds remains "
            "the cap). Leave empty for full-duration probes."
        ),
    )
    warmup_fraction: float = Field(
        default=0.1, ge=0.0, le=0.9,
        description=(
            "[grid + auto confirmation, count-limited levels] Share of a level's "
            "requests discarded as warmup, taken from the front. All slots fire "
            "at once into an idle server, so the first wave (c requests) sees no "
            "queue and the climb toward steady state lasts another wave or two: "
            "with N requests per slot, one wave is 1/N of the level. Ignored for "
            "duration-limited levels, which use warmup_seconds."
        ),
    )
    cooldown_fraction: float = Field(
        default=0.05, ge=0.0, le=0.9,
        description=(
            "[grid + auto confirmation, count-limited levels] Share of a level's "
            "requests discarded as cooldown, taken from the ragged tail where "
            "slots finish at different times and concurrency decays."
        ),
    )
    probe_warmup_fraction: float = Field(
        json_schema_extra=_AUTO_ONLY,
        default=0.1, ge=0.0, le=0.9,
        description=(
            "[auto] warmup_fraction for search probes. Probes are short, so the "
            "fresh-start wave is a larger share of them: at 10 requests per slot, "
            "0.2 discards the first two waves."
        ),
    )
    probe_cooldown_fraction: float = Field(
        json_schema_extra=_AUTO_ONLY,
        default=0.05, ge=0.0, le=0.9,
        description="[auto] cooldown_fraction for search probes.",
    )
    level_settle_seconds: float = Field(
        json_schema_extra=_AUTO_ONLY,
        default=15.0, ge=0.0, le=3600.0,
        description=(
            "[auto] Pause before every level after the first, so the endpoint "
            "can finish what the previous level left in flight. A level ends "
            "client-side: its last wave of requests is abandoned, and a server "
            "that does not abort on disconnect keeps generating them under the "
            "next level. 0 disables the pause."
        ),
    )
    level_drain_max_seconds: float = Field(
        json_schema_extra=_AUTO_ONLY,
        default=120.0, ge=0.0, le=3600.0,
        description=(
            "[auto] After the settle pause, keep waiting (at most this long) "
            "until a single tiny canary request answers about as fast as it did "
            "on the idle endpoint before the search began — the closest a "
            "black-box endpoint gets to reporting no requests running. A slow "
            "canary is exactly what leftover load produces (it queues behind "
            "it). The search proceeds either way and records the canary "
            "latency per level; 0 disables the check."
        ),
    )

    @field_validator("concurrencies")
    @classmethod
    def _normalize_concurrencies(cls, v: str) -> str:
        return ",".join(str(n) for n in parse_concurrencies(v))

    @model_validator(mode="after")
    def _check_auto_mode(self):
        for w, c, what in ((self.warmup_fraction, self.cooldown_fraction, ""),
                           (self.probe_warmup_fraction,
                            self.probe_cooldown_fraction, "probe_")):
            if w + c >= 1.0:
                raise ValueError(
                    f"{what}warmup_fraction + {what}cooldown_fraction must "
                    f"leave something to measure (got {w:g} + {c:g})")
        if self.search_mode == "auto":
            # `concurrencies` and `report_level` are simply not consulted in
            # auto mode, so whatever they hold is ignored rather than rejected.
            # Demanding specific values for params the mode never reads makes
            # the admin form fight the operator over settings that change
            # nothing (and blocks switching an existing grid benchmark to auto).
            min_needed = (2 * self.warmup_seconds + self.probe_seconds
                          + self.level_settle_seconds + self.max_seconds)
            if self.max_total_seconds < min_needed:
                raise ValueError(
                    f"max_total_seconds={self.max_total_seconds:.0f} cannot fit "
                    f"even one probe plus the confirmation run — needs at least "
                    f"{min_needed:.0f}s (2×warmup_seconds + probe_seconds + "
                    "level_settle_seconds + max_seconds)"
                )
        return self

    @model_validator(mode="after")
    def _check_fixed_level(self):
        if self.search_mode == "grid" and self.report_level == "fixed":
            if self.fixed_level is None:
                raise ValueError("report_level='fixed' requires fixed_level to be set")
            if self.fixed_level not in self.concurrency_levels():
                raise ValueError(
                    f"fixed_level={self.fixed_level} is not one of the swept "
                    f"concurrencies ({self.concurrencies})"
                )
        return self

    def effective_max_ttft_ms(self) -> float:
        """The TTFT cap actually enforced. The deprecated slo_max_ttft_p99_ms
        wins when set, so a benchmark configured before the split keeps the
        threshold its scores were computed under."""
        if self.slo_max_ttft_p99_ms is not None:
            return self.slo_max_ttft_p99_ms
        return self.slo_max_ttft_ms

    def concurrency_levels(self) -> List[int]:
        return parse_concurrencies(self.concurrencies)


class PerfGuidellmSweepModule(_GuidellmBudgetMixin, TestModule):
    """GuideLLM multi-concurrency sweep benchmark."""

    @classmethod
    def _budget_rates(cls, params) -> list:
        # Every level runs sequentially, so the budget scales with how many there
        # are — reading params.max_seconds alone would under-estimate by len(levels).
        return [float(n) for n in parse_concurrencies(params.concurrencies)]

    @classmethod
    def time_budget_seconds(cls, params) -> "float | None":
        # Auto mode ignores the concurrencies grid, so the mixin's formula would
        # size the worker's backstop far below the search's real wall-clock and
        # kill healthy runs. Bound it off the search budget instead: probing +
        # one confirmation fit inside max_total_seconds by construction; the
        # extra terms cover a step-down confirmation, one retry of each, and
        # per-probe deadline slack (see _run_probe).
        if getattr(params, "search_mode", "grid") == "auto":
            try:
                # Each level may also wait its full drain allowance first.
                run_slack = (params.request_timeout + _PROBE_STARTUP_GRACE_SECONDS
                             + getattr(params, "level_settle_seconds", 0.0)
                             + getattr(params, "level_drain_max_seconds", 0.0))
                probe_wall = params.warmup_seconds + params.probe_seconds + run_slack
                confirm_wall = params.warmup_seconds + params.max_seconds + run_slack
                return params.max_total_seconds + 2 * probe_wall + 4 * confirm_wall
            except Exception:  # noqa: BLE001 — a missing budget beats a crashed sync
                return None
        return super().time_budget_seconds(params)

    name = "perf_guidellm_sweep"
    display_name = "GuideLLM Concurrency Sweep"
    description = (
        "Sweeps concurrency levels with guidellm. Grid mode (default) runs the "
        "admin-specified levels; each level c samples c × requests_per_concurrency "
        "requests (max_seconds acts as the per-level cap). Auto mode discovers the "
        "maximum concurrency meeting the configurable per-request-output-TPS "
        "and TTFT SLOs via doubling ramp + binary search (isolated subprocess "
        "per probe), then confirms the winner with a full-size run — "
        "reported_concurrency is the answer. Emits per-level TPS/latency/uptime "
        "metrics addressable in metric configs via dotted keys (e.g. "
        "'c8.ttft_p99_ms'), peak-throughput aggregates, and canonical top-level "
        "metrics from the reported level (report_level: slo_best/peak/fixed in "
        "grid mode; the confirmed level in auto mode). Default score = "
        "0.5×(input_tps/baseline) + 0.5×(output_tps/baseline) on the canonical "
        "metrics; latency/uptime are redlines. Admin configures baselines and "
        "thresholds."
    )
    ParamsSchema = PerfGuidellmSweepParams
    # Card baseline for the display-only card-normalized TPM metrics computed by
    # the platform worker (applied to the canonical reported-level throughput).
    card_norm_baseline = 8

    # NOTE: the param is deliberately named `concurrencies` (not `concurrency`)
    # so the submission-level integer concurrency_override does NOT target this
    # module — overriding a sweep with a single number would defeat its purpose.

    metrics_descriptors = [
        *PerfGuidellmModule.metrics_descriptors,
        MetricDescriptor("reported_level_meets_slo", "Meets SLO",    "",      "1 if the reported level meets the configured per-request-TPS/TTFT SLOs, else 0", higher_is_better=True),
        MetricDescriptor("duration_cap_seconds", "Level Duration Cap", "s",   "Duration limit this level ran under: probe_seconds for a search probe, max_seconds for a confirmation run"),
        MetricDescriptor("peak_concurrency",     "Peak Level",       "",      "Concurrency level with the highest total TPS (ignoring SLOs)"),
        MetricDescriptor("peak_input_tps",       "Peak Input TPS",   "tok/s", "Input TPS at the peak-throughput level",  higher_is_better=True),
        MetricDescriptor("peak_output_tps",      "Peak Output TPS",  "tok/s", "Output TPS at the peak-throughput level", higher_is_better=True),
        MetricDescriptor("peak_total_tps",       "Peak Total TPS",   "tok/s", "Highest total TPS across all swept levels", higher_is_better=True),
        MetricDescriptor("search_probes",        "Search Probes",    "",      "[auto] Number of guidellm runs the search executed (probes, retries, confirmations)"),
        MetricDescriptor("search_converged",     "Search Converged", "",      "[auto] 1 if the search closed its pass/fail bracket to the configured granularity (or passed at the concurrency cap); 0 if it stopped early on the max_total_seconds budget", higher_is_better=True),
        MetricDescriptor("slo_fail_concurrency", "First Failing Level", "",   "[auto] Lowest concurrency the search treated as failing — it either missed the SLOs (in a probe or in the longer confirmation run) or produced no usable result. Absent when every level passed"),
        MetricDescriptor("search_research_rounds", "Re-searches", "",          "[auto] Times a full-length confirmation contradicted the shorter probe that passed its level, forcing the bracket to reopen. Above zero means probes are too short to be trustworthy — raise probe_seconds or the per-slot probe request count"),
        MetricDescriptor("drain_wait_seconds",   "Drain Wait",       "s",     "[auto, per level] How long the search waited before this level for the endpoint to finish the previous level's leftovers (settle pause plus canary checks)"),
        MetricDescriptor("pre_level_canary_ms",  "Pre-level Canary", "ms",    "[auto, per level] Latency of the last tiny canary request before this level started. Near the idle reference means the endpoint was quiet; far above it means the level ran on a still-busy endpoint"),
        MetricDescriptor("started_drained",      "Started Drained",  "",      "[auto, per level] 1 if the canary answered near its idle latency before this level began, 0 if the drain wait ran out with the endpoint still busy"),
    ]

    default_metric_configs = [
        MetricConfig("input_tps",   role="score",   weight=0.5, formula="ratio",   baseline=5000.0),
        MetricConfig("output_tps",  role="score",   weight=0.5, formula="ratio",   baseline=200.0),
        MetricConfig("ttft_p99_ms", role="redline", max_val=120000.0),
        MetricConfig("tpot_p99_ms", role="redline", max_val=200.0),
        MetricConfig("itl_p99_ms",  role="redline", max_val=150.0),
        MetricConfig("ttft_p50_ms", role="redline", max_val=60000.0),
        MetricConfig("tpot_p50_ms", role="redline", max_val=100.0),
        MetricConfig("itl_p50_ms",  role="redline", max_val=75.0),
        MetricConfig("uptime",      role="redline", min_val=0.90),
        # See PerfGuidellmModule.default_metric_configs: P90 is display-only so
        # it doesn't add an unchosen third redline to existing benchmarks.
        MetricConfig("ttft_p90_ms", role="display"),
        MetricConfig("tpot_p90_ms", role="display"),
        MetricConfig("itl_p90_ms",  role="display"),
        MetricConfig("ttft_mean_ms", role="display"),
        MetricConfig("tpot_mean_ms", role="display"),
        MetricConfig("itl_mean_ms",  role="display"),
        MetricConfig("total_tps_mean",       role="display"),
        MetricConfig("request_output_tps",   role="display"),
        MetricConfig("reported_concurrency", role="display"),
        MetricConfig("reported_level_meets_slo", role="display"),
        MetricConfig("peak_concurrency",     role="display"),
        MetricConfig("peak_input_tps",       role="display"),
        MetricConfig("peak_output_tps",      role="display"),
        MetricConfig("peak_total_tps",       role="display"),
        MetricConfig("search_probes",        role="display"),
        MetricConfig("search_converged",     role="display"),
        MetricConfig("search_research_rounds", role="display"),
        MetricConfig("slo_fail_concurrency", role="display"),
        *[MetricConfig(f"http_status_{code}", role="display")
          for code, _, _ in PerfGuidellmModule._HTTP_STATUS_DESCRIPTORS],
    ]

    def run(
        self,
        endpoint: EndpointConfig,
        params: BaseModel,
        output_dir: str,
        progress_cb: ProgressCallback = _noop_progress,
        cancel_event=None,
    ) -> ModuleResult:
        p: PerfGuidellmSweepParams = params  # type: ignore[assignment]
        os.makedirs(output_dir, exist_ok=True)

        if p.search_mode == "auto":
            progress_cb(0.05, f"Starting guidellm auto concurrency search: "
                              f"cap={p.search_max_concurrency}, probe={p.probe_seconds:.0f}s, "
                              f"confirm={p.max_seconds:.0f}s, SLO: request_output_tps>="
                              f"{p.slo_min_request_output_tps:g}, {_slo_ttft_key(p)}<="
                              f"{p.effective_max_ttft_ms():.0f}ms")
            logger.info(
                "[%s] auto search starting | SLO: request_output_tps>=%g, %s<=%.0fms "
                "| probes: %s requests/slot capped at %.0fs | confirmation: %s "
                "requests/slot capped at %.0fs | max concurrency %d | budget %.0fs",
                self.name, p.slo_min_request_output_tps, _slo_ttft_key(p),
                p.effective_max_ttft_ms(),
                p.probe_requests_per_concurrency or "unlimited", p.probe_seconds,
                p.requests_per_concurrency or "unlimited", p.max_seconds,
                p.search_max_concurrency, p.max_total_seconds,
            )
            return self._run_auto(endpoint, p, output_dir, progress_cb, cancel_event)

        levels = p.concurrency_levels()
        progress_cb(0.05, f"Starting guidellm sweep: concurrency levels={levels}, "
                          f"input={p.input_tokens}, output={p.output_tokens}, "
                          f"{p.max_seconds}s per level")

        metrics = run_guidellm_load(
            endpoint,
            rates=[float(c) for c in levels],
            input_tokens=p.input_tokens,
            output_tokens=p.output_tokens,
            max_seconds=p.max_seconds,
            request_timeout=p.request_timeout,
            warmup_seconds=p.warmup_seconds,
            dataset_path=p.dataset_path,
            processor_path=p.processor_path,
            output_dir=output_dir,
            progress_cb=progress_cb,
            cancel_event=cancel_event,
            log_tag=self.name,
            requests_per_concurrency=p.requests_per_concurrency,
            warmup_fraction=p.warmup_fraction,
            cooldown_fraction=p.cooldown_fraction,
        )
        if metrics is None:
            return ModuleResult(error="GuideLLM sweep failed to produce metrics")

        _nest_level_metrics(metrics)
        _add_request_output_tps(metrics)
        _add_level_slo_flags(metrics, p)
        _add_peak_metrics(metrics)
        reported = _select_reported_level(metrics, p)
        if reported is not None:
            _promote_canonical(metrics, reported, p)

        progress_cb(0.99, "Metrics collected — evaluator will compute score")
        logger.info(
            "[%s] sweep done: levels=%s report_level=%s reported_concurrency=%s "
            "output_tps=%.1f peak_total_tps=%.1f",
            self.name, levels, p.report_level, metrics.get("reported_concurrency"),
            metrics.get("output_tps", 0.0), metrics.get("peak_total_tps", 0.0),
        )
        return ModuleResult(
            metrics=metrics,
            extra_display_configs=_per_level_display_configs(metrics),
        )

    def _run_auto(
        self,
        endpoint: EndpointConfig,
        p: "PerfGuidellmSweepParams",
        output_dir: str,
        progress_cb: ProgressCallback,
        cancel_event,
    ) -> ModuleResult:
        """SLO-driven concurrency search: c=1 baseline → doubling ramp →
        binary search → full-length confirmation run at the winner (stepping
        down one passing level if the long run misses the SLOs). Every
        guidellm run happens in a disposable probe_runner subprocess — see
        _run_probe. Raises AllRequestsFailed when the baseline can't complete
        a single request (worker records the concrete reason)."""
        t0 = time.monotonic()
        # The confirmation run is preceded by a drain like every other level;
        # reserve at least its settle pause (the canary wait beyond that is
        # bounded but usually zero on a quiet endpoint).
        confirm_cost = p.level_settle_seconds + p.warmup_seconds + p.max_seconds
        runs: Dict[int, _ProbeOutcome] = {}
        http_totals: Dict[int, float] = {}
        probe_walls: List[float] = []
        state = {"spent_runs": 0, "budget_stopped": False, "research_rounds": 0}
        # Idle-reference canary for the drain check between levels, measured
        # before anything has loaded the endpoint. None = check disabled.
        idle_reference: Optional[float] = None
        if p.level_drain_max_seconds > 0:
            progress_cb(_PROGRESS_FLOOR, "auto search: measuring idle-endpoint canary latency")
            idle_reference = _measure_idle_reference(endpoint, self.name)

        def next_probe_cost() -> float:
            """What to reserve for the next probe. Count-limited probes finish
            when their requests do, usually far inside probe_seconds, so
            reserving the cap would strangle the search into stopping early on
            budget. Reserve the slowest probe observed so far instead, and fall
            back to the cap only before the first one has run. Probes grow with
            concurrency, so the slowest so far is the honest estimate."""
            if probe_walls:
                return max(probe_walls)
            return p.warmup_seconds + p.probe_seconds

        def band_progress(msg: str) -> None:
            frac = _PROGRESS_FLOOR + (_PROGRESS_CEILING - _PROGRESS_FLOOR) * min(
                (time.monotonic() - t0) / p.max_total_seconds, 1.0)
            progress_cb(frac, msg)

        def run_level(level: int, duration: float, tag: str) -> _ProbeOutcome:
            # Probes use the small per-slot count for a quick verdict; the
            # confirmation run uses the full one. Duration remains the cap.
            requests_cap = (p.requests_per_concurrency if tag == "confirm"
                            else p.probe_requests_per_concurrency)
            warmup_frac, cooldown_frac = (
                (p.warmup_fraction, p.cooldown_fraction) if tag == "confirm"
                else (p.probe_warmup_fraction, p.probe_cooldown_fraction))
            out: _ProbeOutcome
            for attempt in (1, 2):  # one retry, for abnormal deaths only
                slot_started = time.monotonic()
                drain: Optional[_DrainOutcome] = None
                if state["spent_runs"] > 0:
                    # Something ran before this (a level, or this level's dead
                    # first attempt): let the endpoint finish its leftovers.
                    drain = _wait_until_drained(
                        endpoint, p, idle_reference, cancel_event, self.name,
                        progress=lambda msg: band_progress(f"auto search: {msg}"))
                # A fresh seed per run: the same seed replays the same synthetic
                # prompts, which a prefix cache warmed by the previous level
                # would answer faster than genuinely new traffic.
                seed = _BASE_RANDOM_SEED + state["spent_runs"]
                state["spent_runs"] += 1
                run_started = time.monotonic()
                subdir = f"{tag}_c{level}" + ("" if attempt == 1 else "_retry")
                logger.info("[%s] %s c=%d starting (%s requests/slot, warmup/cooldown "
                            "%g/%g of requests, %.0fs cap, seed %d)",
                            self.name, tag, level, requests_cap or "unlimited",
                            warmup_frac, cooldown_frac, duration, seed)

                def tick(elapsed: float) -> None:
                    band_progress(
                        f"auto search: {tag} c={level} — {int(elapsed)}s/"
                        f"{int(p.warmup_seconds + duration)}s "
                        f"(search {int(time.monotonic() - t0)}s / "
                        f"budget {int(p.max_total_seconds)}s)")

                out = _run_probe(endpoint, p, level, duration,
                                 os.path.join(output_dir, subdir),
                                 tick, cancel_event,
                                 log_tag=f"{self.name}:{tag}_c{level}",
                                 requests_per_concurrency=requests_cap,
                                 random_seed=seed,
                                 warmup_fraction=warmup_frac,
                                 cooldown_fraction=cooldown_frac)
                if out.level_metrics is not None and drain is not None:
                    # Kept per level so a run can be read back later: a long
                    # wait or an undrained start explains a level that looks
                    # worse than its neighbours.
                    out.level_metrics["drain_wait_seconds"] = drain.waited_seconds
                    if drain.canary_seconds is not None:
                        out.level_metrics["pre_level_canary_ms"] = drain.canary_seconds * 1000.0
                    if drain.drained is not None:
                        out.level_metrics["started_drained"] = 1.0 if drain.drained else 0.0
                if tag == "probe":
                    # Includes the drain wait: that is what the next probe
                    # slot costs the budget too.
                    probe_walls.append(time.monotonic() - slot_started)
                for code, n in out.http_status.items():
                    http_totals[code] = http_totals.get(code, 0.0) + n
                if not out.abnormal:
                    break
                logger.warning("[%s] %s c=%d attempt %d died abnormally: %s",
                               self.name, tag, level, attempt, out.error)
            # Never let a metric-less outcome clobber good data for the level
            # (e.g. a crashed confirmation run after a clean probe).
            if level not in runs or out.level_metrics is not None:
                runs[level] = out
            wall = time.monotonic() - run_started
            if out.level_metrics is not None:
                _warn_if_truncated(self.name, tag, level, duration,
                                   requests_cap, out.level_metrics,
                                   wall_seconds=wall,
                                   measured_fraction=1.0 - warmup_frac - cooldown_frac)
                verdict = "pass" if _meets_slo(out.level_metrics, p) else "FAIL"
                # The log is the only record of how a search reached its answer
                # once the run is over, so each level states its verdict, the
                # numbers behind it, and how much data it rests on.
                logger.info(
                    "[%s] %s c=%d -> %s | %s | %s | took %.0fs",
                    self.name, tag, level, verdict,
                    _format_slo_check(out.level_metrics, p),
                    _format_sample(out.level_metrics, level, requests_cap), wall,
                )
                band_progress(
                    f"auto search: {tag} c={level} → SLO {verdict} "
                    f"({_format_slo_check(out.level_metrics, p)})")
            else:
                logger.warning("[%s] %s c=%d -> NO RESULT after %.0fs: %s",
                               self.name, tag, level, wall, out.error)
                band_progress(f"auto search: {tag} c={level} → no result ({out.error})")
            return out

        def slo_ok(out: _ProbeOutcome) -> bool:
            return out.level_metrics is not None and _meets_slo(out.level_metrics, p)

        def budget_allows_probe() -> bool:
            # Reserve room for the confirmation run — it is the deliverable.
            return ((time.monotonic() - t0) + next_probe_cost() + confirm_cost
                    <= p.max_total_seconds)

        # Phase 1 — baseline. A dead endpoint fails here, not ten probes later.
        base = run_level(1, p.probe_seconds, "probe")
        if base.level_metrics is None:
            if base.all_requests_failed:
                raise AllRequestsFailed(
                    f"auto search aborted: every request failed at concurrency 1 — {base.error}")
            return ModuleResult(
                error=f"auto search aborted: baseline probe produced no metrics — {base.error}")

        lo = 1 if slo_ok(base) else 0        # highest passing level (0 = none yet)
        hi: Optional[int] = None if lo else 1  # lowest failing level

        if lo:
            # Phase 2 — doubling ramp until the SLOs first fail or the cap.
            logger.info("[%s] ramp: doubling from c=2 up to c=%d until the SLOs fail",
                        self.name, p.search_max_concurrency)
            c = 2
            while c <= p.search_max_concurrency:
                if not budget_allows_probe():
                    state["budget_stopped"] = True
                    logger.warning(
                        "[%s] ramp stopped at c=%d: the search budget "
                        "(max_total_seconds=%.0fs) cannot fit another probe plus "
                        "the confirmation run", self.name, c, p.max_total_seconds)
                    break
                if slo_ok(run_level(c, p.probe_seconds, "probe")):
                    lo = c
                    c *= 2
                else:
                    hi = c
                    break
            if hi is None and not state["budget_stopped"]:
                logger.info(
                    "[%s] ramp reached the search_max_concurrency cap (c=%d) with "
                    "every level passing — the true limit may be higher",
                    self.name, lo)
            elif hi is not None:
                logger.info("[%s] ramp bracketed the knee: c=%d passes, c=%d fails",
                            self.name, lo, hi)

        def bisect(lo_pass: int, hi_fail: Optional[int]) -> int:
            """Narrow the (passing, failing) bracket with probes; return the
            highest level that passed."""
            granularity = _bracket_granularity(lo_pass, p.search_granularity)
            if hi_fail is not None:
                logger.info(
                    "[%s] bisecting (%d passes, %d fails), stopping once the "
                    "bracket is within %d level(s)",
                    self.name, lo_pass, hi_fail, granularity)
            while hi_fail is not None and not state["budget_stopped"]:
                granularity = _bracket_granularity(lo_pass, p.search_granularity)
                mid = _next_bisect_level(lo_pass, hi_fail, granularity)
                if mid is None:
                    logger.info(
                        "[%s] bracket closed: c=%d passes, c=%d fails (gap %d <= "
                        "granularity %d) — answer is c=%d",
                        self.name, lo_pass, hi_fail, hi_fail - lo_pass,
                        granularity, lo_pass)
                    break
                if not budget_allows_probe():
                    state["budget_stopped"] = True
                    logger.warning(
                        "[%s] bisect stopped with the bracket still open "
                        "(%d passes, %d fails): the search budget cannot fit "
                        "another probe plus the confirmation run",
                        self.name, lo_pass, hi_fail)
                    break
                logger.info("[%s] bracket (%d, %d) -> probing midpoint c=%d",
                            self.name, lo_pass, hi_fail, mid)
                if slo_ok(run_level(mid, p.probe_seconds, "probe")):
                    lo_pass = mid
                else:
                    hi_fail = mid
            return lo_pass

        if lo:
            # Phase 3 — binary search inside the (lo=pass, hi=fail) bracket.
            lo = bisect(lo, hi)
        else:
            logger.warning(
                "[%s] baseline c=1 already misses the SLOs — reporting it as the "
                "floor; there is no passing level to search for", self.name)

        chosen = lo or 1

        # Phase 4 — confirm at full length. A failed confirmation is not just a
        # reason to report something lower: it is new evidence that the level
        # fails, contradicting the shorter probe that passed it. Treat it as
        # such — make the failed level the new upper bound, take the highest
        # level still known to pass as the lower bound, and re-search that
        # bracket before confirming again. Without this the search drops
        # straight to the nearest already-probed passing level and never
        # examines the range between, reporting a needlessly conservative
        # answer (e.g. confirming c=32 fails, reporting c=16, having never
        # measured 17..31).
        for round_index in range(_MAX_CONFIRM_ROUNDS):
            logger.info(
                "[%s] confirming c=%d at full length (%.0fs cap, %s requests/slot) "
                "— its metrics become the reported result",
                self.name, chosen, p.max_seconds,
                p.requests_per_concurrency or "unlimited")
            out = run_level(chosen, p.max_seconds, "confirm")
            if slo_ok(out) or chosen <= 1:
                if slo_ok(out):
                    logger.info("[%s] confirmed: c=%d meets the SLOs at full length",
                                self.name, chosen)
                break
            passing_below = [lvl for lvl, o in runs.items()
                             if lvl < chosen and slo_ok(o)]
            if not passing_below:
                break
            fallback = max(passing_below)
            if round_index + 1 >= _MAX_CONFIRM_ROUNDS or state["budget_stopped"] \
                    or not budget_allows_probe():
                logger.warning(
                    "[%s] confirmation at c=%d missed the SLOs — reporting the "
                    "highest level still known to pass, c=%d (no budget/rounds "
                    "left to search %d..%d)",
                    self.name, chosen, fallback, fallback + 1, chosen - 1)
                chosen = fallback
                continue
            logger.warning(
                "[%s] confirmation at c=%d missed the SLOs its probe passed — "
                "re-searching %d..%d before confirming again",
                self.name, chosen, fallback + 1, chosen - 1)
            state["research_rounds"] += 1
            chosen = bisect(fallback, chosen)
            if chosen == fallback:
                # Nothing between them passed; confirm the fallback itself.
                logger.info("[%s] re-search found nothing above c=%d",
                            self.name, fallback)

        progress_cb(0.9, "Auto search complete, aggregating metrics")
        metrics: dict = {}
        for level, out in runs.items():
            if out.level_metrics is not None:
                # duration_seconds/measured_requests come from the guidellm
                # report (what actually ran); duration_cap_seconds records the
                # limit it ran under, so a capped level is recognisable.
                level_metrics = dict(out.level_metrics)
                level_metrics["duration_cap_seconds"] = out.duration_seconds
                metrics[f"c{level}"] = level_metrics
        _add_level_slo_flags(metrics, p)
        _add_peak_metrics(metrics)
        if f"c{chosen}" in metrics:
            _promote_canonical(metrics, chosen, p)
        else:
            logger.warning("[%s] chosen level c=%d has no usable metrics — "
                           "skipping canonical promotion", self.name, chosen)
        metrics["search_probes"] = float(state["spent_runs"])
        metrics["search_converged"] = 0.0 if state["budget_stopped"] else 1.0
        metrics["search_research_rounds"] = float(state["research_rounds"])
        # Lowest level the search treated as failing, derived from the recorded
        # runs rather than from the bracket variable: it then covers a level the
        # bisect narrowed to, one a confirmation run overturned after its probe
        # passed, and one that never produced a usable result (which the search
        # also treats as failing).
        failing = [lvl for lvl, o in runs.items() if not slo_ok(o)]
        if failing:
            metrics["slo_fail_concurrency"] = float(min(failing))
        for code, n in http_totals.items():
            metrics[f"http_status_{code}"] = float(n)

        progress_cb(0.99, "Metrics collected — evaluator will compute score")
        logger.info(
            "[%s] auto search done: reported_concurrency=%s slo_fail=%s runs=%d "
            "converged=%s output_tps=%.1f",
            self.name, metrics.get("reported_concurrency"),
            metrics.get("slo_fail_concurrency"), state["spent_runs"],
            metrics.get("search_converged"), metrics.get("output_tps") or 0.0)
        return ModuleResult(
            metrics=metrics,
            extra_display_configs=_per_level_display_configs(metrics),
        )


def _nest_level_metrics(metrics: dict) -> None:
    """Regroup RealWorldTest's flat per-level keys into nested per-level dicts:
    `ttft_p99_ms_c8` → metrics["c8"]["ttft_p99_ms"]. The flat suffixed keys are
    removed; MetricConfig rules address the nested values with dotted keys
    ("c8.ttft_p99_ms") which the evaluator and frontend both resolve."""
    for key in [k for k in metrics if _LEVEL_KEY_RE.search(k)]:
        m = _LEVEL_KEY_RE.search(key)
        base = key[: m.start()]
        metrics.setdefault(f"c{m.group(1)}", {})[base] = metrics.pop(key)


def _iter_levels(metrics: dict):
    """Yield (level:int, level_metrics:dict) pairs from nested c{N} keys."""
    for key, val in metrics.items():
        m = re.fullmatch(r"c(\d+)", key)
        if m and isinstance(val, dict):
            yield int(m.group(1)), val


def _add_request_output_tps(metrics: dict) -> None:
    """Add per-request output TPS (aggregate output_tps ÷ concurrency) to each
    level dict — the decode speed an individual request experiences at that
    level, and one of the two slo_best selection criteria."""
    for level, level_metrics in _iter_levels(metrics):
        try:
            level_metrics["request_output_tps"] = \
                float(level_metrics["output_tps_mean"]) / level
        except (KeyError, TypeError, ValueError):
            continue


def _level_total_tps(level_metrics: dict) -> float:
    try:
        return float(level_metrics["total_tps_mean"])
    except (KeyError, TypeError, ValueError):
        return -1.0


def _add_peak_metrics(metrics: dict) -> None:
    """Add peak_* aggregates: the level with the highest mean total TPS,
    regardless of SLOs. Reads the nested c{N} dicts produced by
    _nest_level_metrics."""
    peak = max(_iter_levels(metrics), key=lambda lv: _level_total_tps(lv[1]), default=None)
    if peak is None or _level_total_tps(peak[1]) < 0:
        return
    level, level_metrics = peak
    metrics["peak_concurrency"] = level
    metrics["peak_total_tps"] = level_metrics["total_tps_mean"]
    for src, dst in (("input_tps_mean", "peak_input_tps"),
                     ("output_tps_mean", "peak_output_tps")):
        val = level_metrics.get(src)
        if val is not None:
            metrics[dst] = val


def _slo_ttft_key(p: "PerfGuidellmSweepParams") -> str:
    """Level-dict key the TTFT SLO reads, per the slo_ttft_percentile param."""
    sel = p.slo_ttft_percentile
    return "ttft_mean_ms" if sel == "mean" else f"ttft_{sel}_ms"


def _meets_slo(level_metrics: dict, p: "PerfGuidellmSweepParams") -> bool:
    """SLO check for one level: per-request output TPS floor + TTFT cap at the
    configured percentile (default P99). A missing metric fails the check
    (conservative)."""
    try:
        return (
            float(level_metrics["request_output_tps"]) >= p.slo_min_request_output_tps
            and float(level_metrics[_slo_ttft_key(p)]) <= p.effective_max_ttft_ms()
        )
    except (KeyError, TypeError, ValueError):
        return False


def _format_slo_check(level_metrics: dict, p: "PerfGuidellmSweepParams") -> str:
    """Each SLO criterion with its measured value, its threshold and whether it
    held — so a verdict in the log says WHICH criterion decided it, rather than
    leaving the reader to guess from a bare pass/fail."""
    ttft_key = _slo_ttft_key(p)
    checks = [
        ("request_output_tps", level_metrics.get("request_output_tps"),
         ">=", p.slo_min_request_output_tps, "tok/s"),
        (ttft_key, level_metrics.get(ttft_key),
         "<=", p.effective_max_ttft_ms(), "ms"),
    ]
    parts = []
    for name, value, op, threshold, unit in checks:
        if value is None:
            parts.append(f"{name}=MISSING (needs {op}{threshold:g}{unit})")
            continue
        held = value >= threshold if op == ">=" else value <= threshold
        parts.append(f"{name}={value:.1f}{unit} {op}{threshold:g} "
                     f"{'OK' if held else 'FAIL'}")
    return "; ".join(parts)


def _format_sample(level_metrics: dict, level: int,
                   requests_per_concurrency: Optional[int]) -> str:
    """How much data the verdict rests on: requests measured (against the
    budget, when count-limited) and the length of the measurement window."""
    measured = level_metrics.get("measured_requests")
    window = level_metrics.get("duration_seconds")
    measured_str = "?" if measured is None else f"{measured:.0f}"
    if requests_per_concurrency:
        measured_str += f"/{level * requests_per_concurrency} requested"
    # Sub-second windows are real (a short count-limited probe against a fast
    # endpoint), so don't round them away to "0s".
    window_str = ("?" if window is None
                  else f"{window:.1f}s" if window < 10 else f"{window:.0f}s")
    return f"{measured_str} requests in a {window_str} window"


def _add_level_slo_flags(metrics: dict, p: "PerfGuidellmSweepParams") -> None:
    """Stamp meets_slo (1/0) into each level dict so the UI shows the SLO
    verdict per level in both grid and auto modes."""
    for _level, level_metrics in _iter_levels(metrics):
        level_metrics["meets_slo"] = 1.0 if _meets_slo(level_metrics, p) else 0.0


def _select_reported_level(metrics: dict, p: "PerfGuidellmSweepParams") -> Optional[int]:
    """Pick the level whose metrics become the canonical top-level keys,
    per the report_level param. Returns None when no level produced metrics."""
    levels = dict(_iter_levels(metrics))
    if not levels:
        return None

    if p.report_level == "fixed":
        if p.fixed_level in levels:
            return p.fixed_level
        # The configured level produced no metrics (e.g. every request at that
        # level errored) — fall through to slo_best rather than report nothing.
        logger.warning(
            "[perf_guidellm_sweep] fixed_level=%s has no metrics — "
            "falling back to slo_best selection", p.fixed_level,
        )

    if p.report_level == "peak":
        return max(levels, key=lambda lvl: _level_total_tps(levels[lvl]))

    # slo_best: highest-throughput level meeting the SLOs; if none qualify,
    # the lowest level (closest to meeting per-request SLOs, since per-request
    # TPS degrades and TTFT grows with concurrency).
    passing = [lvl for lvl, lm in levels.items() if _meets_slo(lm, p)]
    if passing:
        return max(passing, key=lambda lvl: _level_total_tps(levels[lvl]))
    logger.warning(
        "[perf_guidellm_sweep] no level meets SLO (request_output_tps>=%.1f, "
        "%s<=%.0f) — reporting lowest level %s",
        p.slo_min_request_output_tps, _slo_ttft_key(p),
        p.effective_max_ttft_ms(), min(levels),
    )
    return min(levels)


# Level-dict bookkeeping keys that would be misleading as flat canonical
# metrics (meets_slo would shadow reported_level_meets_slo; duration_seconds
# only disambiguates probe vs confirmation runs).
_UNPROMOTED_LEVEL_KEYS = {
    "meets_slo", "duration_seconds", "duration_cap_seconds", "measured_requests",
    "drain_wait_seconds", "pre_level_canary_ms", "started_drained",
}


def _promote_canonical(metrics: dict, level: int, p: "PerfGuidellmSweepParams") -> None:
    """Copy the chosen level's metrics to flat top-level keys (overwriting the
    legacy promotion RealWorldTest did with its hardcoded REDLINES — the
    param-driven selection here is authoritative), plus the input_tps/output_tps
    aliases the default score terms use, the chosen level, and whether it meets
    the configured SLOs."""
    level_metrics = metrics[f"c{level}"]
    for name, val in level_metrics.items():
        if name in _UNPROMOTED_LEVEL_KEYS:
            continue
        metrics[name] = val
    metrics["input_tps"] = level_metrics.get("input_tps_mean")
    metrics["output_tps"] = level_metrics.get("output_tps_mean")
    metrics["reported_concurrency"] = level
    metrics["reported_level_meets_slo"] = 1.0 if _meets_slo(level_metrics, p) else 0.0


def _per_level_display_configs(metrics: dict) -> List[dict]:
    """Display-only MetricConfig rows with dotted keys ("c8.output_tps_mean")
    for every value in the nested c{N} dicts, ordered by level then metric name
    so the UI groups read naturally. Dicts (not dataclasses) because they cross
    the module→worker boundary as plain JSON."""
    return [
        {"key": f"c{level}.{name}", "role": "display"}
        for level, level_metrics in sorted(_iter_levels(metrics))
        for name in sorted(level_metrics)
    ]


# ---------------------------------------------------------------------------
# Auto mode: probe subprocess management (see bench/probe_runner.py)
# ---------------------------------------------------------------------------

# Fraction of a level's requests that survive the default warmup/cooldown
# phases for count-limited runs (10% + 5% excluded); callers with other
# fractions pass their own.
_MEASURED_FRACTION = 0.85
# Below this share of the expected measured requests, a level did not finish
# its request count — its duration cap (or an error) stopped it first.
_TRUNCATION_RATIO = 0.75


def _warn_if_truncated(log_tag: str, tag: str, level: int, cap_seconds: float,
                       requests_per_concurrency: Optional[int],
                       level_metrics: Dict, wall_seconds: float,
                       measured_fraction: float = _MEASURED_FRACTION) -> None:
    """Log when a count-limited level did not finish its requests.

    A truncated level is the main source of run-to-run inconsistency in the
    search: it samples whatever happened to finish before the cap, so its
    latency percentiles move between runs and the SLO verdict can flip.

    Two signals must agree, because neither is sufficient alone:
      - a request shortfall, since `duration_seconds` is the measurement
        window (warmup and cooldown excluded), so a level stopped dead at its
        cap still reports a duration well under it; and
      - the run actually occupying its cap, since at small request counts
        rounding in the warmup/cooldown split alone can look like a shortfall.
    """
    if not requests_per_concurrency:
        return
    measured = level_metrics.get("measured_requests")
    if measured is None:
        return
    requested = level * requests_per_concurrency
    expected = measured_fraction * requested
    if measured >= _TRUNCATION_RATIO * expected:
        return
    if wall_seconds < _TRUNCATION_RATIO * cap_seconds:
        return  # ended well inside its cap — the cap is not what stopped it
    logger.warning(
        "[%s] %s c=%d measured only %.0f requests of the ~%.0f expected from "
        "its %d request budget (%d x %d per slot) — the %.0fs duration cap "
        "stopped it early. Percentiles from a truncated level are noisy and "
        "move between runs: raise the cap (probe_seconds, or max_seconds for a "
        "confirmation run) or lower the per-slot request count.",
        log_tag, tag, level, measured, expected, requested,
        level, requests_per_concurrency, cap_seconds,
    )


def _bracket_granularity(lo: int, explicit: int) -> int:
    """Bracket width at which the binary search stops. 0 → automatic: 1/8 of
    the best passing level (≥1), so precision stays proportional to the
    answer instead of wasting probes on ±1 at c=500."""
    return explicit if explicit > 0 else max(1, lo // 8)


def _next_bisect_level(lo: int, hi: int, granularity: int) -> Optional[int]:
    """Midpoint of the (lo=pass, hi=fail) bracket, or None once the bracket is
    within granularity (or no integer lies strictly between)."""
    if hi - lo <= granularity:
        return None
    mid = (lo + hi) // 2
    return mid if lo < mid < hi else None


@dataclass
class _ProbeOutcome:
    """Result of one probe/confirmation subprocess for a single level."""
    level: int
    duration_seconds: float
    level_metrics: Optional[Dict] = None  # per-level dict incl. request_output_tps
    http_status: Dict[int, float] = field(default_factory=dict)
    all_requests_failed: bool = False
    error: Optional[str] = None

    @property
    def abnormal(self) -> bool:
        """True when the probe died without a verdict (crash, deadline kill,
        missing result) — worth one retry. All-requests-failed is a verdict
        (the endpoint collapsed at this level), not an abnormality."""
        return self.level_metrics is None and not self.all_requests_failed


def _canary_latency(endpoint: EndpointConfig, timeout: float) -> Optional[float]:
    """Seconds one tiny non-streaming chat completion takes end to end, or
    None when it fails or times out. On an idle server this is a few hundred
    milliseconds of prefill; on one still holding leftover generations it is
    the queue wait — so it reads server load without any server cooperation.
    The prompt carries a nonce so a prefix cache cannot shortcut it."""
    import uuid

    import requests

    from utils.api import join_endpoint

    headers = {"Content-Type": "application/json"}
    if endpoint.api_key:
        headers["Authorization"] = f"Bearer {endpoint.api_key}"
    payload = {
        "model": endpoint.model,
        "messages": [{"role": "user", "content": f"ping {uuid.uuid4().hex}"}],
        "max_tokens": 1,
        "temperature": 0,
        "stream": False,
    }
    started = time.monotonic()
    try:
        resp = requests.post(join_endpoint(endpoint.api_url, "chat/completions"),
                             json=payload, headers=headers, timeout=timeout)
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        return None
    return time.monotonic() - started


def _measure_idle_reference(endpoint: EndpointConfig, log_tag: str) -> Optional[float]:
    """Canary latency on the endpoint before any load: the best of a few
    samples, so connection setup and a cold first request do not inflate the
    bar every later drain check is measured against."""
    samples = [_canary_latency(endpoint, _DRAIN_CANARY_TIMEOUT_SECONDS)
               for _ in range(_DRAIN_REFERENCE_SAMPLES)]
    good = [s for s in samples if s is not None]
    if not good:
        logger.warning(
            "[%s] drain check disabled: the idle-reference canary failed %d/%d "
            "times before the search started", log_tag,
            len(samples), len(samples))
        return None
    ref = min(good)
    logger.info("[%s] idle-reference canary: %.2fs (best of %d) — a level starts "
                "once a canary answers within %.2fs", log_tag, ref, len(good),
                ref * _DRAIN_CANARY_TOLERANCE + _DRAIN_CANARY_SLACK_SECONDS)
    return ref


def _sleep_cancellable(seconds: float, cancel_event) -> None:
    end = time.monotonic() + seconds
    while True:
        if cancel_event is not None and cancel_event.is_set():
            raise BenchmarkCancelled("Benchmark was cancelled")
        remaining = end - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(remaining, _PROBE_POLL_SECONDS))


@dataclass
class _DrainOutcome:
    waited_seconds: float
    canary_seconds: Optional[float] = None  # last canary latency, None = never answered
    drained: Optional[bool] = None          # None = canary check disabled


def _wait_until_drained(
    endpoint: EndpointConfig,
    p: "PerfGuidellmSweepParams",
    idle_reference: Optional[float],
    cancel_event,
    log_tag: str,
    progress: Callable[[str], None] = lambda _msg: None,
) -> _DrainOutcome:
    """Give the endpoint time to finish the previous level's leftovers: a
    fixed settle pause, then canaries until one answers close to the idle
    reference or level_drain_max_seconds runs out. Never blocks the search
    for good — an endpoint that stays loaded (outside traffic, a request
    that never ends) is logged and recorded, and the level runs anyway."""
    started = time.monotonic()
    if p.level_settle_seconds > 0:
        progress(f"settling {p.level_settle_seconds:.0f}s before the next level")
        _sleep_cancellable(p.level_settle_seconds, cancel_event)
    if p.level_drain_max_seconds <= 0 or idle_reference is None:
        return _DrainOutcome(waited_seconds=time.monotonic() - started)

    threshold = idle_reference * _DRAIN_CANARY_TOLERANCE + _DRAIN_CANARY_SLACK_SECONDS
    deadline = started + p.level_settle_seconds + p.level_drain_max_seconds
    last: Optional[float] = None
    while True:
        if cancel_event is not None and cancel_event.is_set():
            raise BenchmarkCancelled("Benchmark was cancelled")
        remaining = deadline - time.monotonic()
        last = _canary_latency(endpoint, min(_DRAIN_CANARY_TIMEOUT_SECONDS,
                                             max(remaining, 1.0)))
        waited = time.monotonic() - started
        if last is not None and last <= threshold:
            logger.info("[%s] endpoint drained after %.0fs (canary %.2fs, idle "
                        "reference %.2fs)", log_tag, waited, last, idle_reference)
            return _DrainOutcome(waited_seconds=waited, canary_seconds=last, drained=True)
        if time.monotonic() >= deadline:
            logger.warning(
                "[%s] endpoint NOT drained after %.0fs: canary %s vs idle reference "
                "%.2fs — the next level starts on an endpoint that is still busy "
                "(leftover generations, or load from outside this benchmark); its "
                "metrics will understate what the endpoint can do when idle",
                log_tag, waited,
                "failed/timed out" if last is None else f"{last:.2f}s",
                idle_reference)
            return _DrainOutcome(waited_seconds=waited, canary_seconds=last, drained=False)
        progress(f"waiting for the endpoint to drain — canary "
                 f"{'failed' if last is None else f'{last:.1f}s'} vs idle "
                 f"{idle_reference:.1f}s ({int(waited)}s/"
                 f"{int(p.level_settle_seconds + p.level_drain_max_seconds)}s)")
        _sleep_cancellable(min(_DRAIN_POLL_SECONDS, max(deadline - time.monotonic(), 0.0)),
                           cancel_event)


def _kill_probe_group(proc: "subprocess.Popen") -> None:
    """SIGKILL the probe's whole process group — the child runs in its own
    session (pgid == its pid), so guidellm's forked workers die with it.
    SIGKILL-only: a wedged guidellm teardown ignores anything gentler."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=10)
    except Exception:  # noqa: BLE001 — the reap is best-effort
        pass


def _log_tail(path: str, max_bytes: int = 800) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            return f.read().decode("utf-8", "replace").strip()
    except OSError:
        return ""


def _run_probe(
    endpoint: EndpointConfig,
    p: "PerfGuidellmSweepParams",
    level: int,
    duration_seconds: float,
    probe_dir: str,
    tick: Callable[[float], None],
    cancel_event,
    log_tag: str,
    requests_per_concurrency: Optional[int] = None,
    random_seed: Optional[int] = None,
    warmup_fraction: Optional[float] = None,
    cooldown_fraction: Optional[float] = None,
) -> _ProbeOutcome:
    """Run one concurrency level in a bench.probe_runner subprocess.

    The parent enforces a hard deadline (warmup + duration + request_timeout
    in-flight drain + startup grace) by SIGKILLing the child's process group;
    the child's own watchdog fires 30s later as backup, and also hard-exits if
    this parent dies, so nothing can orphan-hammer the endpoint. `tick(elapsed)`
    runs every ~30s while waiting; an exception from it (the platform's
    progress callback raises on cancellation) kills the child before
    propagating, as does cancel_event."""
    os.makedirs(probe_dir, exist_ok=True)
    result_path = os.path.join(probe_dir, "probe_result.json")
    log_path = os.path.join(probe_dir, "probe.log")
    deadline = (p.warmup_seconds + duration_seconds + p.request_timeout
                + _PROBE_STARTUP_GRACE_SECONDS)
    cfg = {
        "api_url": endpoint.api_url,
        "model": endpoint.model,
        "concurrency": level,
        "input_tokens": p.input_tokens,
        "output_tokens": p.output_tokens,
        "max_seconds": duration_seconds,
        "request_timeout": p.request_timeout,
        "warmup_seconds": p.warmup_seconds,
        "dataset_path": os.path.abspath(p.dataset_path) if p.dataset_path else None,
        "processor_path": p.processor_path,
        "requests_per_concurrency": requests_per_concurrency,
        "random_seed": random_seed,
        "warmup_fraction": warmup_fraction,
        "cooldown_fraction": cooldown_fraction,
        "output_dir": probe_dir,
        "result_path": result_path,
        # Child watchdog is the backup — the parent kills first.
        "deadline_seconds": deadline + 30.0,
        "log_tag": log_tag,
    }
    cfg_path = os.path.join(probe_dir, "probe_config.json")
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f)

    env = dict(os.environ)
    # The key travels via env, never on disk — probe_config.json lands in the
    # run's artifact directory.
    env["GUIDELLM_PROBE_API_KEY"] = endpoint.api_key or ""
    with open(log_path, "ab") as log_f:
        proc = subprocess.Popen(
            [sys.executable, "-m", "bench.probe_runner", cfg_path],
            cwd=_PROJECT_ROOT,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,  # own process group → killable as a unit
        )
    t0 = time.monotonic()
    next_tick = _PROBE_TICK_SECONDS
    try:
        while proc.poll() is None:
            elapsed = time.monotonic() - t0
            if cancel_event is not None and cancel_event.is_set():
                raise BenchmarkCancelled("Benchmark was cancelled")
            if elapsed > deadline:
                _kill_probe_group(proc)
                return _ProbeOutcome(
                    level=level, duration_seconds=duration_seconds,
                    error=(f"probe c={level} exceeded its {deadline:.0f}s "
                           "deadline and was killed"),
                )
            if elapsed >= next_tick:
                next_tick += _PROBE_TICK_SECONDS
                tick(elapsed)
            time.sleep(_PROBE_POLL_SECONDS)
    except BaseException:
        _kill_probe_group(proc)
        raise

    try:
        with open(result_path, encoding="utf-8") as f:
            result = json.load(f)
    except (OSError, ValueError):
        tail = _log_tail(log_path)
        return _ProbeOutcome(
            level=level, duration_seconds=duration_seconds,
            error=(f"probe process exited with code {proc.returncode} without "
                   "a result" + (f" — log tail: {tail}" if tail else "")),
        )
    if not result.get("ok"):
        return _ProbeOutcome(
            level=level, duration_seconds=duration_seconds,
            all_requests_failed=bool(result.get("all_requests_failed")),
            error=str(result.get("error") or "probe failed"),
        )

    flat = result.get("metrics") or {}
    level_metrics: Dict = {}
    http_status: Dict[int, float] = {}
    suffix = f"_c{level}"
    for key, val in flat.items():
        if key.endswith(suffix):
            level_metrics[key[: -len(suffix)]] = val
        elif key.startswith("http_status_"):
            try:
                http_status[int(key[len("http_status_"):])] = float(val)
            except (TypeError, ValueError):
                continue
    if not level_metrics:
        return _ProbeOutcome(
            level=level, duration_seconds=duration_seconds, http_status=http_status,
            error=f"probe c={level} returned metrics without per-level keys",
        )
    try:
        level_metrics["request_output_tps"] = \
            float(level_metrics["output_tps_mean"]) / level
    except (KeyError, TypeError, ValueError):
        pass
    return _ProbeOutcome(
        level=level, duration_seconds=duration_seconds,
        level_metrics=level_metrics, http_status=http_status,
    )
