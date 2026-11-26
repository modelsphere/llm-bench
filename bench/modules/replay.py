"""
Production-traffic replay.

Replays real captured requests under concurrent load to detect:
  - Service errors (uptime)
  - TTFT / total generation time latency
  - Truncation (finish_reason="length")
  - Post-finish token anomalies
  - Repetitive final tokens
  - Output throughput (tok/s)

Score and pass/fail are NOT computed here — the platform evaluator uses
per-benchmark MetricConfig rules (stored in BenchmarkModule.metric_configs_json).

The dataset is a JSONL file of captured OpenAI-compatible requests — either a
fixed file or the newest build of a rolling collection profile (see
`docs/replay-datasets.md`). Only the request side is replayed; the response is
the endpoint's own, which is what is being measured.

Default evaluation (`default_metric_configs`) — a worked EXAMPLE, meant to be
replaced per benchmark in the admin editor:
  Score = revenue per minute × 1e6 (passthrough_scaled, Σ price × throughput):
         price_in × uncached_input_tpm + price_cached × cached_tpm
         + price_out × output_tpm, where price is USD per 1M tokens and tpm is
         tok/min → (USD/min) × 1e6. Scores uncached_input_tpm (NOT input_tpm)
         so cached tokens aren't double-counted. The prices below are
         illustrative list prices, not a recommendation.
  Redlines: uptime ≥ 0.95, Total Time P99 ≤ 120000 ms, unfinished_rate ≤ 0.05.
         The per-input-length TTFT buckets are reported but judge nothing by
         default: what counts as an acceptable TTFT at 128k input is a property
         of your service level, not of this module. `EXAMPLE_TTFT_REDLINES_MS`
         below shows the shape one such table takes.
"""
from __future__ import annotations

import os

from pydantic import BaseModel, Field, field_validator, model_validator

from bench.modules.base import (
    DatasetFeedFields, EndpointConfig, MetricConfig, MetricDescriptor,
    ModuleResult, ProgressCallback, TestModule, _noop_progress,
)
from bench.replay_test import dataset_feed
# Single source of truth for the per-input-length TTFT buckets — the test emits
# ttft_<label>_{p90,p50,avg}_ms + _count for each, so the module's descriptors
# and display configs are generated from the same list (below) to stay in sync.
from bench.tests.functional.replay import TTFT_INPUT_BUCKETS
from utils.logger import logger

# Repository root: relative dataset paths resolve against it.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ttft_bucket_descriptors() -> list:
    """One descriptor per (bucket × {p90,p50,avg,count}) — TTFT broken out by
    input length, because a 200k-token prompt and a 2k one are not the same
    request and averaging them hides both."""
    out: list = []
    for label, human, _lo, _hi in TTFT_INPUT_BUCKETS:
        out.append(MetricDescriptor(f"ttft_{label}_p90_ms", f"TTFT P90 ({human})", "ms", f"TTFT 90th percentile for requests with input length {human} tokens", higher_is_better=False))
        out.append(MetricDescriptor(f"ttft_{label}_p50_ms", f"TTFT P50 ({human})", "ms", f"TTFT median for input length {human} tokens", higher_is_better=False))
        out.append(MetricDescriptor(f"ttft_{label}_avg_ms", f"TTFT Avg ({human})", "ms", f"TTFT mean for input length {human} tokens", higher_is_better=False))
        out.append(MetricDescriptor(f"ttft_{label}_count",  f"TTFT Count ({human})", "", f"Successful requests in the {human}-token input bucket (read this before trusting the bucket's percentiles)", higher_is_better=True))
    return out


def _ttft_bucket_display_configs() -> list:
    """Display config for every generated per-bucket TTFT metric."""
    out: list = []
    for label, *_ in TTFT_INPUT_BUCKETS:
        for stat in ("p90", "p50", "avg"):
            out.append(MetricConfig(f"ttft_{label}_{stat}_ms", role="display"))
        out.append(MetricConfig(f"ttft_{label}_count", role="display"))
    return out


# An EXAMPLE TTFT service level, in ms, keyed by input-length bucket:
# (p90_max, p50_max, avg_max). Nothing uses this by default — the buckets are
# reported as display metrics, and a benchmark that wants to gate on them sets
# the redlines in the admin editor. It is kept here because the shape of the
# table is the useful part: a ceiling that scales with input length, stated
# alongside an uptime floor (a latency number measured over a failing service
# is meaningless). Bucket labels come from TTFT_INPUT_BUCKETS.
EXAMPLE_TTFT_REDLINES_MS = {
    "lt_6k":     (5000.0,  2000.0,  2000.0),
    "6k_16k":    (5000.0,  2500.0,  4000.0),
    "16k_32k":   (8000.0,  4000.0,  6000.0),
    "32k_64k":   (15000.0, 8000.0,  8000.0),
    "64k_128k":  (35000.0, 15000.0, 15000.0),
    "128k_256k": (70000.0, 30000.0, 30000.0),
}


def example_ttft_bucket_redline_configs() -> list:
    """The EXAMPLE TTFT service level above, as metric configs.

    Not part of any default: call it when building a benchmark that wants to
    gate on per-bucket TTFT. Empty buckets emit None, which check_redlines
    skips — so a dataset with no requests in a given size band never fails
    that band's redline.
    """
    out: list = []
    for label, (p90, p50, avg) in EXAMPLE_TTFT_REDLINES_MS.items():
        out.append(MetricConfig(f"ttft_{label}_p90_ms", role="redline", max_val=p90))
        out.append(MetricConfig(f"ttft_{label}_p50_ms", role="redline", max_val=p50))
        out.append(MetricConfig(f"ttft_{label}_avg_ms", role="redline", max_val=avg))
    return out


class ReplayParams(BaseModel):
    dataset_path: str = Field(
        default="",
        title="Dataset path",
        description="Path to the JSONL replay dataset to replay. Used only when "
                    "dataset_source is 'fixed'; leave blank to fall back to the "
                    "worker's REPLAY_DATASET_PATH. Ignored under 'auto', where the "
                    "path comes from the rolling profile's current build.",
        # Hidden under 'auto' — the path is resolved from the profile, so an
        # editable box there is a question with no right answer. Hiding is
        # lossless (ModuleParamForm keeps the stored value), so a benchmark can
        # be flipped auto -> fixed and get its old path back.
        json_schema_extra={"x-visible-when": {"dataset_source": ["fixed"]}},
    )
    # --- rolling dataset feed -------------------------------------------
    # "fixed" (the default) is exactly the historical behaviour: replay the file
    # at dataset_path. "auto" instead resolves the newest build published by a
    # collection profile, so the benchmark tracks current production traffic.
    # Defaulting to "fixed" is what keeps every existing benchmark byte-identical
    # — their stored params_json has none of these keys, so they validate to it.
    dataset_source: str = Field(
        default="fixed",
        title="Dataset source",
        description="fixed = replay the file at dataset_path (default). "
                    "auto = replay the newest build of the rolling collection "
                    "profile named below, so the dataset tracks live traffic. "
                    "Scores from different builds are not strictly comparable; "
                    "every run records exactly which build it replayed.",
        json_schema_extra={"enum": ["fixed", "auto"]},
    )
    dataset_profile: str = Field(
        default="",
        title="Rolling dataset profile",
        description="Name of the collection profile to replay (admin → Replay "
                    "Datasets). Required when dataset_source is auto.",
        json_schema_extra={"x-visible-when": {"dataset_source": ["auto"]}},
    )
    dataset_max_age_hours: float = Field(
        default=48.0, ge=0.0,
        title="Warn when the build is older than (hours)",
        description="Freshness expectation for the rolling profile. An older "
                    "build still runs — availability beats a hard stop here — "
                    "but the run logs a warning and records dataset_resolved."
                    "stale=true. 0 disables the check.",
        json_schema_extra={"x-visible-when": {"dataset_source": ["auto"]}},
    )
    # --- schedule ---------------------------------------------------------
    # "closed" (the default) is the historical behaviour: `concurrency` workers
    # send back to back, which measures capacity. "timed" replays a timed
    # dataset (a profile built with layout 'timed') on its recorded timing, so
    # the engine sees production's real load and latency is what users saw.
    schedule: str = Field(
        default="closed",
        title="Schedule",
        description="closed = `concurrency` workers send back to back (default). "
                    "timed = send every request of a timed dataset on its recorded "
                    "timing (1x production load): a session's next turn goes out "
                    "its recorded think time after the previous turn finishes on "
                    "this engine. Needs a dataset built with layout 'timed'.",
        json_schema_extra={"enum": ["closed", "timed"]},
    )
    warmup_s: float = Field(
        default=30.0, ge=0.0,
        title="Warm-up (seconds of schedule)",
        description="Requests due in the first this-many seconds of the schedule "
                    "are sent but not measured.",
        json_schema_extra={"x-visible-when": {"schedule": ["timed"]}},
    )
    max_lag_s: float = Field(
        default=2.0, gt=0.0,
        title="Max send lag (s)",
        description="When the p99 delay between a request's due time and its send "
                    "exceeds this, the run is marked fell_behind (and fails): the "
                    "client could not keep the schedule.",
        json_schema_extra={"x-visible-when": {"schedule": ["timed"]}},
    )
    max_in_flight: int = Field(
        default=512, ge=1, le=4096,
        title="In-flight safety cap",
        description="Most requests open at once. Reaching it makes later requests "
                    "wait (their wait counts as latency) and sets max_in_flight_hit.",
        json_schema_extra={"x-visible-when": {"schedule": ["timed"]}},
    )
    flush_cache: bool = Field(
        default=True,
        title="Flush the prefix cache first",
        description="Ask the engine to empty its prefix cache (SGLang /flush_cache, "
                    "vLLM /reset_prefix_cache) before the timed replay, so every "
                    "config starts from the same state.",
        json_schema_extra={"x-visible-when": {"schedule": ["timed"]}},
    )
    concurrency: int = Field(
        default=5, ge=1, le=4096, description="Concurrent replay workers",
        json_schema_extra={"x-visible-when": {"schedule": ["closed"]}},
    )
    max_samples: int = Field(
        default=0, ge=0,
        description="Max requests to replay (0 = all). Use a small value for faster CI runs. "
                    "Not used by a timed replay, which always sends the whole dataset.",
        json_schema_extra={"x-visible-when": {"schedule": ["closed"]}},
    )
    request_timeout: float = Field(default=600.0, ge=1.0, le=86400.0, description="Per-request HTTP timeout in seconds")
    max_retries: int = Field(default=3, ge=0, description="Retries per failed request")
    max_generation_tokens: int = Field(
        default=0, ge=0,
        description="Cap on generated tokens per request (0 = no cap). Lowers/sets "
                    "max_tokens in each replayed request to bound runaway / "
                    "non-terminating generations; request_timeout is only a "
                    "per-read socket timeout and does NOT cap a response that keeps "
                    "streaming. The client also stops reading after ~2x this value "
                    "as a backstop for servers that overrun the limit."
    )
    fixed_output_lengths: bool = Field(
        default=False,
        description="Force every response to its recorded length: max_tokens and "
                    "min_tokens set to the length production answered with, EOS and "
                    "stop strings ignored. Every engine under test then does the same "
                    "work, so the score measures speed, not how long the sampled "
                    "answers happened to be. Requests whose capture has no usage keep "
                    "their own length (see requests_with_recorded_length). Off by "
                    "default: the replay stays as recorded."
    )
    save_responses: bool = Field(
        default=False,
        description="Debug: save every request + its raw response to "
                    "replay_responses.jsonl under the run's output dir (readable in the "
                    "worker pod via `kubectl exec`, written live as requests complete). "
                    "Off by default — meant for targeted debugging, not normal runs "
                    "(a full run can write a lot of data)."
    )
    save_responses_max_bytes: int = Field(
        default=65536, ge=0,
        description="When save_responses is on, cap the raw response bytes stored per "
                    "request (0 = unlimited / full bodies). Default 65536 (64KB)."
    )
    max_seconds: float = Field(
        default=18000.0, ge=0.0, le=604800.0,
        description="Hard wall-clock cap for the whole batch (seconds). When hit, "
                    "the test stops dispatching new requests and aggregates what "
                    "has been done so far. 0 disables the cap. Default 5h."
    )
    force_stream: bool = Field(
        default=False,
        description="Force stream=true on every request.  Default preserves the original stream flag."
    )
    clean: bool = Field(
        default=False,
        description="Normalize each replayed request so a strict chat template "
                    "(Qwen/SGLang) accepts it: merge consecutive leading system "
                    "messages into one and demote any non-leading system message to "
                    "user. Captured gateway/Claude-Code traffic often carries several "
                    "system blocks, which bare engines reject with 'System message must "
                    "be at the beginning.' (4xx). Default off keeps the replay "
                    "byte-for-byte; turn on when the target rejects multi-system "
                    "requests. See multi_system_requests / system_normalized_count."
    )
    # Opt-in LLM answer-quality judge. Off by default — replay stays a pure
    # perf test unless the admin enables it for more rigorous acceptance runs.
    # A random judge_sample_rate fraction of requests retain their response
    # text; after the perf batch each retained record is mechanically bucketed
    # (error/length/disconnect excluded, tool_calls counted good) and only the
    # genuinely-answered ones are graded by a SEPARATE judge LLM on two axes:
    # quality (good/acceptable/poor) and hallucination (none/suspected/clear).
    judge_enable: bool = Field(
        default=False,
        title="Enable answer-quality judge",
        description="Grade a random sample of replayed answers with a separate "
                    "judge LLM (quality + hallucination). Requires judge_api_url "
                    "and judge_model. Judge calls run pipelined with the replay "
                    "in a small separate thread pool (a slow judge overlaps the "
                    "run instead of extending it); latency/throughput metrics "
                    "are unaffected.",
    )
    judge_sample_rate: float = Field(
        default=0.05, ge=0.0, le=1.0,
        title="Judge sample rate (0–1)",
        description="Fraction (0–1, NOT percent) of replayed requests whose "
                    "answers are retained and judged (independent random draw "
                    "per request). 0.05 = 5%.",
    )
    judge_max_samples: int = Field(
        default=2000, ge=0,
        description="Hard cap on judged samples per run (0 = uncapped). Bounds "
                    "judge cost on full-dataset runs regardless of sample rate. "
                    "NOTE: sampling is deterministic per request; if this cap "
                    "trips, which sampled requests got in first depends on "
                    "completion order — size it above rate x dataset when "
                    "strict run-to-run reproducibility matters.",
    )
    judge_seed: int = Field(
        default=0,
        title="Judge sampling seed",
        description="Seed for the deterministic per-request sampling hash. Same "
                    "seed + dataset = the SAME requests are judged on every run "
                    "(and on every endpoint — A/B runs grade identical prompts). "
                    "Change it to rotate the judged subset.",
    )
    judge_max_retries: int = Field(
        default=5, ge=0,
        description="Retries per judge call on TRANSIENT errors (connection "
                    "failures, timeouts, 429, 5xx) with exponential backoff "
                    "(2s doubling to 30s). Raise for a flaky external judge "
                    "endpoint. Non-retryable 4xx fails immediately; an "
                    "unparseable verdict is re-asked once.",
    )
    judge_api_url: str = Field(
        default="",
        description="Judge LLM endpoint URL (OpenAI-compatible). Required when "
                    "judge_enable is on — replay never self-judges, since the "
                    "target grading its own answers is biased.",
    )
    judge_model: str = Field(
        default="",
        description="Judge LLM model name. Required when judge_enable is on.",
    )
    judge_api_key: str = Field(
        default="",
        description="API key for the judge endpoint. Empty = no Authorization "
                    "header (keyless endpoint).",
    )
    judge_max_tokens: int = Field(
        default=256, ge=1,
        description="max_tokens per judge call. Judge thinking is disabled via "
                    "chat_template_kwargs so 256 comfortably fits the one-line "
                    "verdict JSON; raise it if your judge backend ignores the "
                    "thinking-off switch and truncates mid-reasoning.",
    )
    judge_concurrency: int = Field(
        default=8, ge=1, le=4096,
        description="Concurrent judge workers (judge phase only).",
    )
    judge_prompt: str = Field(
        default="",
        description="Optional judge rubric override (system prompt). The judge "
                    "must still answer with the one-line verdict JSON "
                    '({"task","quality","halluc","reason"}). Empty = built-in '
                    "two-axis rubric.",
    )
    judge_disable_thinking: bool = Field(
        default=True,
        description="Disable thinking on judge requests (via chat_template_kwargs) "
                    "so the verdict JSON lands in content instead of being consumed "
                    "by reasoning tokens. Disable only if the judge model has no "
                    "thinking mode.",
    )

    @field_validator("judge_api_url", "judge_model", "judge_api_key")
    @classmethod
    def _strip_judge_fields(cls, v: str) -> str:
        # Pasted values routinely carry a trailing newline/space; a key with a
        # newline makes `requests` reject the Authorization header outright,
        # and a padded URL/model 404s — which would surface only as a wall of
        # judge_failures at the end of a multi-hour run.
        return v.strip()

    @field_validator("schedule")
    @classmethod
    def _known_schedule(cls, v: str) -> str:
        v = (v or "closed").strip().lower()
        if v not in ("closed", "timed"):
            raise ValueError("schedule must be 'closed' or 'timed'")
        return v

    @field_validator("dataset_source")
    @classmethod
    def _known_dataset_source(cls, v: str) -> str:
        v = (v or "fixed").strip().lower()
        if v not in ("fixed", "auto"):
            raise ValueError("dataset_source must be 'fixed' or 'auto'")
        return v

    @model_validator(mode="after")
    def _dataset_selection_consistent(self):
        # Validated at benchmark save time (the admin API runs params_json
        # through this schema), so "auto with no profile" is caught in the
        # editor rather than at the top of a multi-hour run.
        if self.dataset_source == "auto" and not self.dataset_profile.strip():
            raise ValueError(
                "dataset_source='auto' requires dataset_profile — the name of a "
                "rolling collection profile (admin → Replay Datasets)."
            )
        # Deliberately NOT validating that 'fixed' has a dataset_path: an empty
        # path is a supported legacy configuration (run() then falls back to the
        # worker's REPLAY_DATASET_PATH), and rejecting it here would fail
        # benchmarks that have been running that way.
        return self

    @model_validator(mode="after")
    def _judge_fields_consistent(self):
        # Validated at benchmark save time too (the admin API runs params_json
        # through this schema), so misconfigurations surface in the editor
        # instead of hours into a replay run.
        if self.judge_enable and (not self.judge_api_url or not self.judge_model):
            raise ValueError(
                "judge_enable requires both judge_api_url and judge_model — "
                "replay never self-judges (the target grading its own replayed "
                "answers would be biased)."
            )
        if self.judge_api_url and not self.judge_model:
            raise ValueError(
                "judge_model is required when judge_api_url is set — otherwise "
                "the judge endpoint would be asked for the target model's name, "
                "which it almost certainly doesn't serve."
            )
        if self.judge_api_key and not self.judge_api_url:
            raise ValueError(
                "judge_api_key is set but judge_api_url is blank, so the key "
                "would never be used. Set judge_api_url or clear the key."
            )
        return self


# Card baseline for the display-only card-normalized TPM metrics
# (`*_tpm_card_norm`, computed by the platform worker): throughput is rescaled
# as if the endpoint ran on this many cards.
CARD_NORM_BASELINE = 8


class ReplayModule(TestModule):
    """Production-traffic replay benchmark."""

    name = "replay"
    display_name = "Production Traffic Replay"
    description = (
        "Replays captured production requests "
        "under concurrent load. Detects errors, TTFT/total-time latency, "
        "truncation (finish_reason=length), post-finish token anomalies, "
        "repetitive final tokens, and output throughput (tok/s). "
        "Full dataset runs take ~10h; use max_samples to limit duration. "
        "Example score = revenue/min × 1e6: price($/Mtok) × (uncached-input + "
        "cached + output) TPM (passthrough_scaled); uptime/latency/TTFT gate "
        "pass/fail via redlines. Set your own prices and thresholds in metric_configs. "
        "Optionally (judge_enable) a separate judge LLM grades a random sample "
        "of answers for quality and hallucination, gated by judge_poor_rate / "
        "judge_halluc_clear_rate redlines."
    )
    ParamsSchema = ReplayParams
    card_norm_baseline = CARD_NORM_BASELINE

    metrics_descriptors = [
        MetricDescriptor("uptime",                       "Uptime",                "%",     "Fraction of successful requests",                 higher_is_better=True),
        MetricDescriptor("ttft_p99_ms",                  "TTFT P99",              "ms",    "Time to first token, 99th percentile",            higher_is_better=False),
        MetricDescriptor("total_time_p99_ms",            "Total Time P99",        "ms",    "End-to-end generation time, 99th percentile",     higher_is_better=False),
        MetricDescriptor("ttft_p50_ms",                  "TTFT P50",              "ms",    "Time to first token, median",                     higher_is_better=False),
        MetricDescriptor("ttft_p90_ms",                  "TTFT P90",              "ms",    "Time to first token, 90th percentile (overall)",  higher_is_better=False),
        MetricDescriptor("ttft_mean_ms",                 "TTFT Mean",             "ms",    "Time to first token, mean",                       higher_is_better=False),
        MetricDescriptor("ttft_input_unknown_count",     "TTFT Input Unknown",    "",      "Successful requests with a TTFT but no reported input length (prompt_tokens) — excluded from the per-bucket TTFT metrics", higher_is_better=False),
        MetricDescriptor("wall_time_s",                  "Wall Time",             "s",     "Total wall-clock duration of the replay batch",   higher_is_better=False),
        # Dataset identity — STRING metrics, filled in by the platform worker
        # (which resolves the rolling profile; the module only ever sees a
        # path). Display-only and absent on standalone CLI runs. They exist so
        # two runs can be told apart at a glance: a rolling dataset changes
        # under the benchmark, and two submissions started minutes apart can
        # legitimately have replayed different traffic. Scores are only
        # comparable between runs that share these.
        MetricDescriptor("dataset_id",                   "Dataset",               "",      "Which dataset this run replayed: the rolling build id (e.g. 20260804T071349Z) or the fixed file's name. Scores are only directly comparable across runs with the same value", higher_is_better=True),
        MetricDescriptor("dataset_sha256",               "Dataset Hash",          "",      "First 16 hex chars of the replayed dataset's SHA-256, taken from the collector's published build. Blank for fixed datasets (never hashed — they can be multi-GB)", higher_is_better=True),
        MetricDescriptor("input_tpm",                    "Input TPM",             "tok/min", "Total input tokens divided by wall-clock minutes",  higher_is_better=True),
        MetricDescriptor("output_tpm",                   "Output TPM",            "tok/min", "Total output tokens divided by wall-clock minutes", higher_is_better=True),
        MetricDescriptor("cached_tpm",                   "Cached TPM",            "tok/min", "Total cached prompt tokens divided by wall-clock minutes (0 if service does not report cache). Subset of Input TPM, not additive", higher_is_better=True),
        MetricDescriptor("uncached_input_tpm",           "Uncached Input TPM",    "tok/min", "Fresh (non-cache-hit) input tokens per wall-clock minute = Input TPM − Cached TPM. Use THIS (not Input TPM) as the full-price-input coefficient in a cost model so cached tokens aren't charged twice", higher_is_better=True),
        # Card-normalized TPM — display-only comparison metrics computed by the
        # platform worker (the card count lives on the submission's hardware
        # section, which modules never see): raw TPM x baseline / card count.
        MetricDescriptor("input_tpm_card_norm",          f"Input TPM ({CARD_NORM_BASELINE}-card norm.)",  "tok/min", f"Input TPM normalized to {CARD_NORM_BASELINE} cards: Input TPM x {CARD_NORM_BASELINE} / total card count (cards per machine x machine count; assumed {CARD_NORM_BASELINE} when hardware info is not provided)",   higher_is_better=True),
        MetricDescriptor("output_tpm_card_norm",         f"Output TPM ({CARD_NORM_BASELINE}-card norm.)", "tok/min", f"Output TPM normalized to {CARD_NORM_BASELINE} cards: Output TPM x {CARD_NORM_BASELINE} / total card count (cards per machine x machine count; assumed {CARD_NORM_BASELINE} when hardware info is not provided)", higher_is_better=True),
        MetricDescriptor("cached_tpm_card_norm",         f"Cached TPM ({CARD_NORM_BASELINE}-card norm.)", "tok/min", f"Cached TPM normalized to {CARD_NORM_BASELINE} cards: Cached TPM x {CARD_NORM_BASELINE} / total card count (cards per machine x machine count; assumed {CARD_NORM_BASELINE} when hardware info is not provided)", higher_is_better=True),
        MetricDescriptor("total_tpm_card_norm",          f"Total TPM ({CARD_NORM_BASELINE}-card norm.)",  "tok/min", f"Total (Input + Output) TPM normalized to {CARD_NORM_BASELINE} cards: (Input TPM + Output TPM) x {CARD_NORM_BASELINE} / total card count (assumed {CARD_NORM_BASELINE} when hardware info is not provided)",      higher_is_better=True),
        MetricDescriptor("total_requests",               "Total Requests",        "",      "Total requests replayed",                         higher_is_better=True),
        MetricDescriptor("attempted_requests",           "Attempted",             "",      "Requests that were actually dispatched (rest were canceled before start)", higher_is_better=True),
        MetricDescriptor("not_started_requests",         "Not Started",           "",      "Requests canceled before they were dispatched (batch ran out of time)", higher_is_better=False),
        MetricDescriptor("http_200_count",               "HTTP 200",              "",      "Attempted requests the service answered with a literal HTTP 200 (sanity check)", higher_is_better=True),
        MetricDescriptor("tool_requests_total",          "Tool Requests",         "",      "Attempted requests whose history carried tool-call messages", higher_is_better=True),
        MetricDescriptor("tool_call_4xx_errors",         "Tool-Call 4xx",         "",      "Tool-carrying requests rejected with 4xx — what the failed request CARRIED, not why it died (an over-context tool request counts here too). For the actual cause, read error_class_counts (tool_schema = real tool-support/schema rejects)", higher_is_better=False),
        MetricDescriptor("image_requests_total",         "Image Requests",        "",      "Attempted requests carrying a multimodal image content part (genuine vision input, not base64 pasted as text)", higher_is_better=True),
        MetricDescriptor("image_4xx_errors",             "Image 4xx",             "",      "Image-carrying requests rejected with 4xx — what the failed request CARRIED, not why it died (an over-context image request counts here too). For the actual cause, read error_class_counts (image_load = real image decode/fetch failures, incl. 5xx)", higher_is_better=False),
        MetricDescriptor("multi_system_requests",        "Multi-System Reqs",     "",      "Attempted requests carrying >1 or out-of-order system messages (the shape strict Qwen/SGLang templates reject). Set clean=true to normalize them", higher_is_better=False),
        MetricDescriptor("multi_system_4xx_errors",      "Multi-System 4xx",      "",      "Multi-system requests rejected with 4xx — what the failed request CARRIED, not why it died (with clean=true the system shape is already normalized, so a nonzero count here usually has another cause). Read error_class_counts (multi_system_template = real template rejects)", higher_is_better=False),
        MetricDescriptor("throughput_resample_p05_change_pct", "Resampled Throughput p5", "%", "Range within this run: the run's requests resampled with replacement; the 5th percentile of the resampled throughput as a change against the run's own. How much the score depends on which requests and response lengths happened; misses what moves a whole run, so it reads smaller than the spread between runs", higher_is_better=True),
        MetricDescriptor("throughput_resample_p95_change_pct", "Resampled Throughput p95", "%", "Range within this run: the 95th percentile of the resampled throughput as a change against the run's own (see the p5 figure)", higher_is_better=True),
        MetricDescriptor("throughput_resample_requests", "Resampled Requests", "", "Requests behind the range within this run", higher_is_better=True),
        MetricDescriptor("recorded_length_score_factor", "Recorded-Length Factor", "x", "Throughput at recorded output lengths over the measured throughput: each request's time modelled as time to first token plus time per output token times its length, the lengths swapped for the recorded ones. Multiply a throughput score by it for the score at recorded output lengths", higher_is_better=True),
        MetricDescriptor("recorded_length_requests", "Recorded-Length Requests", "", "Requests behind the recorded-length factor", higher_is_better=True),
        MetricDescriptor("fixed_output_lengths", "Fixed Output Lengths", "", "1 when every response was forced to its recorded length", higher_is_better=True),
        MetricDescriptor("requests_with_recorded_length", "Requests With Recorded Length", "", "Successful requests whose capture recorded an output length", higher_is_better=True),
        MetricDescriptor("output_length_match_rate", "Output Length Match", "", "Share of those whose response ran to the recorded length (within one token)", higher_is_better=True),
        MetricDescriptor("output_tokens_vs_recorded_ratio", "Output vs Recorded Tokens", "x", "Output tokens over recorded output tokens, over the requests with a recorded length", higher_is_better=True),
        MetricDescriptor("system_normalized_count",      "System Normalized",     "",      "Requests whose system messages the clean toggle actually rewrote (0 when clean=false)", higher_is_better=True),
        MetricDescriptor("error_rate",                   "Error Rate",            "%",     "Fraction of attempted requests that errored",     higher_is_better=False),
        MetricDescriptor("unfinished_requests",          "Unfinished",            "",      "Successful requests with no finish marker",       higher_is_better=False),
        MetricDescriptor("unfinished_rate",              "Unfinished Rate",       "%",     "Fraction of requests lacking a finish marker",    higher_is_better=False),
        MetricDescriptor("avg_prompt_tokens",            "Avg Prompt Tokens",     "",      "Average input tokens per request",                higher_is_better=False),
        MetricDescriptor("avg_completion_tokens",        "Avg Completion Tokens", "",      "Average output tokens per request",               higher_is_better=False),
        MetricDescriptor("output_tps_avg",               "Per-request Output TPS (avg)",  "tok/s", "Output throughput = completion_tokens / total request time (OpenRouter-style; incl. prefill), mean (= output_tps_mean; kept for the legacy redline)", higher_is_better=True),
        MetricDescriptor("output_tps_mean",              "Per-request Output TPS (mean)", "tok/s", "Output throughput = completion_tokens / total request time (OpenRouter-style; incl. prefill), mean across requests", higher_is_better=True),
        MetricDescriptor("output_tps_p10",               "Per-request Output TPS (p10)",  "tok/s", "Output throughput (completion / total time), 10th percentile (slow tail)", higher_is_better=True),
        MetricDescriptor("output_tps_p50",               "Per-request Output TPS (p50)",  "tok/s", "Output throughput (completion / total time), median",                      higher_is_better=True),
        MetricDescriptor("output_tps_p90",               "Per-request Output TPS (p90)",  "tok/s", "Output throughput (completion / total time), 90th percentile",             higher_is_better=True),
        MetricDescriptor("input_tps_mean",               "Per-request Input TPS (mean)",  "tok/s", "Prefill rate = prompt_tokens / TTFT, mean across requests",        higher_is_better=True),
        MetricDescriptor("input_tps_p10",                "Per-request Input TPS (p10)",   "tok/s", "Prefill rate = prompt_tokens / TTFT, 10th percentile (slow tail)",  higher_is_better=True),
        MetricDescriptor("input_tps_p50",                "Per-request Input TPS (p50)",   "tok/s", "Prefill rate = prompt_tokens / TTFT, median",                       higher_is_better=True),
        MetricDescriptor("input_tps_p90",                "Per-request Input TPS (p90)",   "tok/s", "Prefill rate = prompt_tokens / TTFT, 90th percentile",              higher_is_better=True),
        MetricDescriptor("total_input_tokens",           "Total Input Tokens",    "",      "Sum of prompt tokens across successful requests", higher_is_better=True),
        MetricDescriptor("total_output_tokens",          "Total Output Tokens",   "",      "Sum of completion tokens across successful requests", higher_is_better=True),
        MetricDescriptor("total_cached_tokens",          "Total Cached Tokens",   "",      "Sum of cached prompt tokens (0 if service does not report cache). Subset of Total Input Tokens", higher_is_better=True),
        MetricDescriptor("total_uncached_input_tokens",  "Total Uncached Input",  "",      "Total input tokens minus cached (the full-price portion) = Total Input − Total Cached", higher_is_better=True),
        MetricDescriptor("cache_usage_coverage", "Cache usage coverage", "%", "Fraction of successful requests reporting valid input and cached usage"),
        MetricDescriptor("cache_hit_rate",               "Cache Hit Rate",        "%",     "Token-weighted cache hit rate among requests reporting both input and cached usage", higher_is_better=True),
        MetricDescriptor("tokens_after_finish_count",    "Tokens After Finish",   "",      "Tokens received after finish_reason was set",     higher_is_better=False),
        MetricDescriptor("repetitive_token_count",       "Repetitive Tokens",     "",      "Requests with 10 identical final tokens",         higher_is_better=False),
        MetricDescriptor("retried_requests",             "Retried Requests",      "",      "Measured requests the client sent more than once (a connection error, timeout or broken stream, then a retry): each one's latency includes its failed attempts and the waits between them, and the engine saw it more than once", higher_is_better=False),
        MetricDescriptor("retry_attempts",               "Retry Attempts",        "",      "Extra sends over all measured requests (retried_requests counts the requests)", higher_is_better=False),
        MetricDescriptor("generation_capped_count",      "Generation Capped",     "",      "Requests the client stopped because the server overran the injected max_tokens (runaway output)", higher_is_better=False),
        MetricDescriptor("finish_reason_length_count",   "Truncated Count",       "",      "Requests truncated by length limit",              higher_is_better=False),
        MetricDescriptor("finish_reason_stop_count",     "Normal Stop Count",     "",      "Requests that finished normally (stop)",          higher_is_better=True),
        # Opt-in answer-quality judge (all None when judge_enable is off).
        MetricDescriptor("judge_sampled_count",          "Judge Sampled",         "",      "Requests randomly retained for the judge phase", higher_is_better=True),
        MetricDescriptor("judge_answered_count",         "Judge Answered",        "",      "Sampled requests with a genuinely completed text answer (sent to the judge LLM)", higher_is_better=True),
        MetricDescriptor("judge_toolcall_count",         "Judge Tool-Call",       "",      "Sampled requests that ended in tool_calls — a normal agentic turn (empty text is expected), counted good without judging", higher_is_better=True),
        MetricDescriptor("judge_length_count",           "Judge Truncated",       "",      "Sampled requests excluded as length-truncated (finish=length / max_tokens / client cap)", higher_is_better=False),
        MetricDescriptor("judge_disconnect_count",       "Judge Disconnected",    "",      "Sampled requests excluded because the stream ended without a finish marker", higher_is_better=False),
        MetricDescriptor("judge_error_count",            "Judge Errored",         "",      "Sampled requests excluded as HTTP/transport errors", higher_is_better=False),
        MetricDescriptor("judge_kept_count",             "Judge Kept",            "",      "Denominator of the judge rates = successfully judged answers + tool-call turns (excluded/failed-to-judge samples don't count)", higher_is_better=True),
        MetricDescriptor("judge_good_acc_rate",          "Judge Good+Acceptable", "%",     "Fraction of kept samples judged good or acceptable (tool-call turns count good)", higher_is_better=True),
        MetricDescriptor("judge_poor_rate",              "Judge Poor Rate",       "%",     "Fraction of kept samples judged poor (off-topic / empty / self-contradictory / broken format)", higher_is_better=False),
        MetricDescriptor("judge_halluc_clear_rate",      "Judge Halluc (clear)",  "%",     "Fraction of kept samples with a clear hallucination (fabricated facts/citations/IDs beyond the given material)", higher_is_better=False),
        MetricDescriptor("judge_halluc_suspected_count", "Judge Halluc (suspected)", "",   "Kept samples with a suspected (unverifiable) hallucination", higher_is_better=False),
        MetricDescriptor("judge_failures",               "Judge Failures",        "",      "Answered samples the judge failed to grade (transport/parse error) — excluded from the rates; a large value means the judge endpoint is misconfigured", higher_is_better=False),
        # Timed replay only (schedule=timed); absent on closed-loop runs.
        MetricDescriptor("e2e_p50_ms",                   "E2E Latency P50",       "ms",    "Timed replay: request latency from its due time to its last token, median", higher_is_better=False),
        MetricDescriptor("e2e_p90_ms",                   "E2E Latency P90",       "ms",    "Timed replay: request latency from its due time to its last token, 90th percentile", higher_is_better=False),
        MetricDescriptor("e2e_mean_ms",                  "E2E Latency Mean",      "ms",    "Timed replay: request latency from its due time to its last token, mean", higher_is_better=False),
        MetricDescriptor("tpot_p50_ms",                  "TPOT P50",              "ms",    "Time per output token after the first, median", higher_is_better=False),
        MetricDescriptor("session_time_p50_s",           "Session Time P50",      "s",     "Timed replay: first turn due to last turn done, for sessions of two or more measured turns, median", higher_is_better=False),
        MetricDescriptor("session_time_mean_s",          "Session Time Mean",     "s",     "Timed replay: session time, mean", higher_is_better=False),
        MetricDescriptor("sessions_measured",            "Sessions Measured",     "",      "Timed replay: sessions counted in session time", higher_is_better=True),
        MetricDescriptor("measured_requests",            "Measured Requests",     "",      "Timed replay: requests due after the warm-up (the ones measured)", higher_is_better=True),
        MetricDescriptor("warmup_requests",              "Warm-up Requests",      "",      "Timed replay: requests sent in the warm-up and not measured", higher_is_better=True),
        MetricDescriptor("schedule_s",                   "Schedule Length",       "s",     "Timed replay: recorded time of the last request's send, idle gaps removed", higher_is_better=False),
        MetricDescriptor("replay_s",                     "Replay Time",           "s",     "Timed replay: wall clock from the first send to the last request done", higher_is_better=False),
        MetricDescriptor("schedule_lag_p99_s",           "Send Lag P99",          "s",     "Timed replay: delay between a request's due time and its send, 99th percentile (client health)", higher_is_better=False),
        MetricDescriptor("fell_behind",                  "Fell Behind",           "",      "Timed replay: 1 when the send lag p99 exceeded max_lag_s — the client could not keep the schedule and the latencies are not a fair reading", higher_is_better=False),
        MetricDescriptor("max_in_flight_hit",            "In-flight Cap Hit",     "",      "Timed replay: 1 when the in-flight safety cap made requests wait", higher_is_better=False),
        MetricDescriptor("in_flight_mean",               "In Flight (mean)",      "",      "Timed replay: requests open at once, time-weighted mean", higher_is_better=False),
        MetricDescriptor("in_flight_max",                "In Flight (max)",       "",      "Timed replay: most requests open at once", higher_is_better=False),
        MetricDescriptor("cache_flushed",                "Cache Flushed",         "",      "Timed replay: 1 when the engine confirmed a prefix cache flush before the replay", higher_is_better=True),
        # Equivalent throughput (bench/replay_test/equivalent.py). Timed replay
        # only; None when the dataset's timing gives nothing to weight with.
        MetricDescriptor("equivalent_tpm",               "Equivalent TPM",        "tok/min", "Timed replay: total tokens per minute this engine would serve behind a least-connections balancer, at production's concurrency: Σ p(n)·n·T/W(n) over n ≥ 2, with p(n) production's time share at n in flight, T production's tokens per request and W(n) this run's mean latency at n (request sizes matched to production's). Compare runs of the same dataset", higher_is_better=True),
        MetricDescriptor("equivalent_tpm_logged",        "Equivalent TPM (logged)", "tok/min", "Timed replay: the same score from production's logged latencies, for reference. It ran on production's hardware and was timed at the gateway, so it is not a baseline for this engine", higher_is_better=True),
        MetricDescriptor("equivalent_coverage",          "Equivalent Coverage",   "",      "Timed replay: share of production's time at 2+ in flight that the score covers (levels where this run had enough requests). Below about 0.8 the score leaves out busy time", higher_is_better=True),
        MetricDescriptor("equivalent_busy_share",        "Busy Share",            "",      "Timed replay: share of production's time with 2+ requests in flight. Near 0 the traffic is too light for throughput to matter; the score then carries little", higher_is_better=True),
        MetricDescriptor("equivalent_size_matched",      "Size Matched",          "",      "Timed replay: 1 when request sizes from the logged token counts were matched to production's mix at each concurrency; 0 = plain mean latencies (the dataset has no usage counts)", higher_is_better=True),
        MetricDescriptor("equivalent_size_r2",           "Size Model R²",         "",      "Timed replay: how much of the logged latency (log scale) the size model explains", higher_is_better=True),
        MetricDescriptor("equivalent_little_error",      "Little's Law Error",    "",      "Timed replay: requests predicted from this run's time and mean latency at each concurrency over those it completed, less one. Within a few percent the curve accounts for the run", higher_is_better=False),
        MetricDescriptor("equivalent_levels",            "Equivalent Levels",     "",      "Timed replay: concurrency levels (2+) the score is built from", higher_is_better=True),
    ] + _ttft_bucket_descriptors()

    default_metric_configs = [
        # Score = revenue per minute × 1e6. passthrough_scaled means each term
        # contributes weight × raw value (no 0-1 normalization), summed. weight is
        # the price per 1M tokens (USD/Mtok — how token prices are normally
        # quoted) and tpm is tok/min, so:
        #   score = Σ (USD/Mtok) × (tok/min) = (USD/min) × 1e6
        #         = price_in · uncached_input_tpm + price_cacheR · cached_tpm
        #           + price_out · output_tpm                         (all ×1e6)
        # The 1e6 just keeps the score a readable magnitude instead of ~1e-1.
        # Scores uncached_input_tpm — NOT input_tpm — so cached tokens aren't
        # billed at the full input rate (inside input_tpm) AND again via
        # cached_tpm (the double-count). The weights below are ILLUSTRATIVE
        # mid-tier list prices ($3 / $0.30 / $15 per 1M tokens), chosen only to
        # make the example concrete; set your own in the editor.
        # The trio is also display (raw tok/min) for ad-hoc math.
        MetricConfig("uncached_input_tpm",           role="score", formula="passthrough_scaled", weight=3.0),
        MetricConfig("cached_tpm",                   role="score", formula="passthrough_scaled", weight=0.3),
        MetricConfig("output_tpm",                   role="score", formula="passthrough_scaled", weight=15.0),
        # Redlines — service quality still gates pass/fail even though the score
        # is revenue, so a high-revenue-but-broken endpoint fails.
        MetricConfig("uptime",                       role="redline", min_val=0.95),
        MetricConfig("ttft_p99_ms",                  role="redline", max_val=10000.0),
        MetricConfig("total_time_p99_ms",            role="redline", max_val=120000.0),
        MetricConfig("unfinished_rate",              role="redline", max_val=0.05),
        # Informational
        MetricConfig("uptime",                       role="display"),
        MetricConfig("ttft_p99_ms",                  role="display"),
        MetricConfig("total_time_p99_ms",            role="display"),
        MetricConfig("ttft_p50_ms",                  role="display"),
        MetricConfig("ttft_p90_ms",                  role="display"),
        MetricConfig("ttft_mean_ms",                 role="display"),
        MetricConfig("ttft_input_unknown_count",     role="display"),
        MetricConfig("wall_time_s",                  role="display"),
        MetricConfig("dataset_id",                   role="display"),
        MetricConfig("dataset_sha256",               role="display"),
        # The throughput trio is also displayed (not just scored) so the raw
        # tok/min values are available for external cost/revenue math:
        #   cost = p_in·uncached_input_tpm + p_cacheR·cached_tpm + p_out·output_tpm
        # A metric can be both score and display (cf. uptime = redline+display).
        MetricConfig("uncached_input_tpm",           role="display"),
        MetricConfig("cached_tpm",                   role="display"),
        MetricConfig("output_tpm",                   role="display"),
        # input_tpm is display-only: it includes cached, so using it as a cost
        # coefficient double-counts. The score uses uncached_input_tpm instead.
        MetricConfig("input_tpm",                    role="display"),
        MetricConfig("total_requests",               role="display"),
        MetricConfig("attempted_requests",           role="display"),
        MetricConfig("not_started_requests",         role="display"),
        MetricConfig("http_200_count",               role="display"),
        MetricConfig("tool_requests_total",          role="display"),
        MetricConfig("tool_call_4xx_errors",         role="display"),
        MetricConfig("image_requests_total",         role="display"),
        MetricConfig("image_4xx_errors",             role="display"),
        MetricConfig("multi_system_requests",        role="display"),
        MetricConfig("multi_system_4xx_errors",      role="display"),
        MetricConfig("system_normalized_count",      role="display"),
        MetricConfig("error_rate",                   role="display"),
        MetricConfig("unfinished_requests",          role="display"),
        MetricConfig("avg_prompt_tokens",            role="display"),
        MetricConfig("avg_completion_tokens",        role="display"),
        MetricConfig("output_tps_avg",               role="display"),
        MetricConfig("output_tps_mean",              role="display"),
        MetricConfig("output_tps_p10",               role="display"),
        MetricConfig("output_tps_p50",               role="display"),
        MetricConfig("output_tps_p90",               role="display"),
        MetricConfig("input_tps_mean",               role="display"),
        MetricConfig("input_tps_p10",                role="display"),
        MetricConfig("input_tps_p50",                role="display"),
        MetricConfig("input_tps_p90",                role="display"),
        MetricConfig("total_input_tokens",           role="display"),
        MetricConfig("total_output_tokens",          role="display"),
        MetricConfig("total_cached_tokens",          role="display"),
        MetricConfig("total_uncached_input_tokens",  role="display"),
        MetricConfig("cache_usage_coverage",         role="display"),
        MetricConfig("cache_hit_rate",               role="display"),
        MetricConfig("tokens_after_finish_count",    role="display"),
        MetricConfig("repetitive_token_count",       role="display"),
        MetricConfig("generation_capped_count",      role="display"),
        MetricConfig("finish_reason_length_count",   role="display"),
        MetricConfig("finish_reason_stop_count",     role="display"),
        # Opt-in judge: quality gates pass/fail only when the judge ran — these
        # metrics are None when judge_enable is off (or nothing was judged), and
        # check_redlines skips None values, same as empty TTFT buckets. Starting
        # thresholds — tune per-benchmark in the editor.
        MetricConfig("judge_poor_rate",              role="redline", max_val=0.10),
        MetricConfig("judge_halluc_clear_rate",      role="redline", max_val=0.02),
        MetricConfig("judge_sampled_count",          role="display"),
        MetricConfig("judge_answered_count",         role="display"),
        MetricConfig("judge_toolcall_count",         role="display"),
        MetricConfig("judge_length_count",           role="display"),
        MetricConfig("judge_disconnect_count",       role="display"),
        MetricConfig("judge_error_count",            role="display"),
        MetricConfig("judge_kept_count",             role="display"),
        MetricConfig("judge_good_acc_rate",          role="display"),
        MetricConfig("judge_poor_rate",              role="display"),
        MetricConfig("judge_halluc_clear_rate",      role="display"),
        MetricConfig("judge_halluc_suspected_count", role="display"),
        MetricConfig("judge_failures",               role="display"),
        # Timed replay: a run whose client fell behind is not a fair reading.
        # None on closed-loop runs, which check_redlines skips.
        MetricConfig("fell_behind",                  role="redline", max_val=0.0),
    ] + [MetricConfig(k, role="display") for k in (
        "e2e_p50_ms", "e2e_p90_ms", "e2e_mean_ms", "tpot_p50_ms", "session_time_p50_s",
        "session_time_mean_s", "sessions_measured", "measured_requests", "warmup_requests",
        "schedule_s", "replay_s", "schedule_lag_p99_s", "fell_behind", "max_in_flight_hit",
        "in_flight_mean", "in_flight_max", "cache_flushed",
        "equivalent_tpm", "equivalent_tpm_logged", "equivalent_coverage", "equivalent_busy_share",
        "equivalent_size_matched", "equivalent_size_r2", "equivalent_little_error", "equivalent_levels",
    )] + [
    # The per-bucket TTFT metrics are reported, not judged — see
    # EXAMPLE_TTFT_REDLINES_MS for why, and for the shape of a table that does.
    ] + _ttft_bucket_display_configs()

    @classmethod
    def default_params(cls) -> dict:
        return cls.ParamsSchema.model_construct(dataset_path="").model_dump()

    @staticmethod
    def _resolve_feed(profile: str):
        """Newest published build of a rolling profile, or None.

        Thin wrapper over the shared resolver so the platform worker and this
        module cannot disagree about which build is current — the pointer file
        on the datasets volume is the single source of truth (the database only
        mirrors it for the admin UI).
        """
        return dataset_feed.resolve_latest(None, (profile or "").strip())

    @classmethod
    def dataset_feed_fields(cls) -> DatasetFeedFields:
        """This module can replay a rolling collection profile instead of a
        fixed file. The platform worker uses this to resolve the profile ONCE,
        before the run starts, and to pin the concrete build onto the run — see
        `_resolve_dataset_feed` in app/queue/jobs.py."""
        return DatasetFeedFields(
            source="dataset_source",
            profile="dataset_profile",
            path="dataset_path",
            max_age="dataset_max_age_hours",
        )

    def run(
        self,
        endpoint: EndpointConfig,
        params: BaseModel,
        output_dir: str,
        progress_cb: ProgressCallback = _noop_progress,
        cancel_event=None,
    ) -> ModuleResult:
        p: ReplayParams = params  # type: ignore[assignment]
        os.makedirs(output_dir, exist_ok=True)

        if p.schedule == "timed":
            progress_cb(0.05, "Starting timed replay (recorded timing, 1x load)")
        else:
            progress_cb(0.05, f"Starting replay: concurrency={p.concurrency}, "
                             f"max_samples={p.max_samples or 'all'}")

        # All runtime knobs are passed as ReplayTest constructor args. We do NOT
        # set REPLAY_* env vars: the underlying module reads env exactly once at
        # import time, and the dramatiq worker process imports it long before
        # this method runs. Mutating os.environ here has no effect on the
        # module-level constants — every value must travel via the constructor.
        from bench.tests.functional.replay import ReplayTest, REPLAY_DATASET_PATH

        if p.dataset_source == "auto":
            # Rolling profile. In the platform the worker has already resolved
            # this and rewritten dataset_path (so the whole run is pinned to one
            # build — see below); this branch is the standalone/CLI path.
            #
            # It must NOT fall through to REPLAY_DATASET_PATH: silently replaying
            # an unrelated fixed dataset because a feed was empty would produce
            # numbers that look fine and mean nothing.
            resolved = self._resolve_feed(p.dataset_profile)
            if resolved is None:
                msg = (
                    f"rolling dataset profile {p.dataset_profile!r} has no published "
                    f"build to replay (looked under {dataset_feed.feed_root()}). "
                    f"Check the collector on the admin Replay Datasets page."
                )
                logger.error("[replay] %s", msg)
                return ModuleResult(error=msg)
            effective_path = resolved.path
            age_h = resolved.age_hours()
            if p.dataset_max_age_hours and age_h > p.dataset_max_age_hours:
                logger.warning(
                    "[replay] STALE FEED: profile %r build %s is %.1fh old "
                    "(expected <= %.1fh) — replaying it anyway; results reflect "
                    "traffic from %s",
                    p.dataset_profile, resolved.build_id, age_h,
                    p.dataset_max_age_hours, resolved.window_start,
                )
            progress_cb(0.06, (
                f"Rolling dataset {p.dataset_profile}: build {resolved.build_id} "
                f"({resolved.records} records, {age_h:.1f}h old)"
            ))
        else:
            # A blank dataset_path means the worker's default (REPLAY_DATASET_PATH,
            # else the shipped example set). A relative one is relative to the
            # repository root, so one benchmark file works from a checkout and in
            # the image. A path that is SET but missing fails the run: quietly
            # replaying some other file would produce a plausible score that
            # means nothing — the same rule a missing rolling feed follows.
            effective_path = REPLAY_DATASET_PATH
            if p.dataset_path:
                effective_path = p.dataset_path if os.path.isabs(p.dataset_path) \
                    else os.path.join(_PROJECT_ROOT, p.dataset_path)
                if not os.path.isfile(effective_path):
                    return ModuleResult(error=f"replay dataset not found: {p.dataset_path}")

        try:
            test = ReplayTest(
                api_url=endpoint.api_url,
                model=endpoint.model,
                api_key=endpoint.api_key,
                output_dir=output_dir,
                dataset_path=effective_path,
                concurrency=p.concurrency,
                force_stream=p.force_stream,
                clean=p.clean,
                progress_cb=progress_cb,
                cancel_event=cancel_event,
                max_seconds=p.max_seconds,
                max_samples=p.max_samples,
                request_timeout=p.request_timeout,
                max_retries=p.max_retries,
                max_generation_tokens=p.max_generation_tokens,
                fixed_output_lengths=p.fixed_output_lengths,
                save_responses=p.save_responses,
                save_responses_max_bytes=p.save_responses_max_bytes,
                judge_enable=p.judge_enable,
                judge_sample_rate=p.judge_sample_rate,
                judge_max_samples=p.judge_max_samples,
                judge_api_url=p.judge_api_url,
                judge_model=p.judge_model,
                judge_api_key=p.judge_api_key,
                judge_max_tokens=p.judge_max_tokens,
                judge_concurrency=p.judge_concurrency,
                judge_prompt=p.judge_prompt,
                judge_disable_thinking=p.judge_disable_thinking,
                judge_seed=p.judge_seed,
                judge_max_retries=p.judge_max_retries,
                schedule=p.schedule,
                warmup_s=p.warmup_s,
                max_lag_s=p.max_lag_s,
                max_in_flight=p.max_in_flight,
                flush_cache=p.flush_cache,
                # Force-permissive redlines: the platform evaluator handles
                # pass/fail via MetricConfig rules; ReplayTest itself should
                # never gate on its own redlines when invoked from here.
                ttft_p99_redline_ms=999999.0,
                total_time_p99_redline_ms=999999.0,
                uptime_floor=0.0,
            )
            progress_cb(0.1, "Replay in progress (may take a long time)...")
            result = test.run()
        except Exception:
            from traceback import format_exc
            err = format_exc()
            logger.error("[replay] test failed: %s", err)
            return ModuleResult(error=err)

        metrics = result.metrics
        if result.error and not metrics:
            # e.g. a timed replay of a dataset with no timing: refused up front.
            return ModuleResult(error=result.error)
        progress_cb(0.99, f"Metrics collected — uptime={metrics.get('uptime', '?')}")
        logger.info("[replay] uptime=%s ttft_p99=%s total_time_p99=%s",
                    metrics.get("uptime"), metrics.get("ttft_p99_ms"), metrics.get("total_time_p99_ms"))
        return ModuleResult(metrics=metrics)
