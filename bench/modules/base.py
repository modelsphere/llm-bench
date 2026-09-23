"""
bench/modules/base.py — Core abstraction for modular test units.

Each TestModule wraps an existing bench/tests/* implementation and
exposes a structured ParamsSchema so the web platform can render
configuration forms and lock params at the benchmark level.

Scoring and pass/fail are NOT computed inside modules. Modules only
produce raw metric dicts. The evaluator (bench.modules.evaluator) uses
per-benchmark MetricConfig rules to compute score and passed.
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from threading import Event
from typing import Any, Callable, ClassVar, Dict, List, Optional, Type

from pydantic import BaseModel


@dataclass
class MetricDescriptor:
    """
    Describes one metric key produced by a module's ModuleResult.metrics dict.

    Pure metadata — display hints only. Evaluation rules (redlines, score
    formulas) live in MetricConfig, configured per-benchmark by admins.
    """
    name: str               # key in metrics dict, e.g. "ttft_p99_ms"
    display_name: str       # shown in UI, e.g. "TTFT P99"
    unit: str               # "ms", "tok/s", "%", ""
    description: str = ""
    higher_is_better: bool = True


@dataclass
class MetricConfig:
    """
    Per-benchmark evaluation rule for a single metric.

    role:
      "score"   — contributes to the module score via the chosen formula
      "redline" — gates pass/fail; does not affect score
      "display" — informational only

    Score formulas (role="score"):
      ratio              — value / baseline           (uncapped, may exceed 1.0)
      ratio_capped       — min(value / baseline, 1.0)
      inverse_ratio_capped — min(baseline / value, 1.0)  for lower-is-better latencies
      linear             — (value − zero_at) / (one_at − zero_at), clamped [0, 1]
      passthrough        — raw value used directly (for already-normalised metrics)

    clip_low / clip_high optionally clamp the computed term score before
    it is weighted and summed.

    Redline fields (role="redline"):
      min_val — metric must be ≥ min_val to pass
      max_val — metric must be ≤ max_val to pass
    """
    key: str
    role: str = "display"          # "score" | "redline" | "display"
    # score fields
    weight: float = 1.0
    formula: str = "ratio"         # ratio | ratio_capped | inverse_ratio_capped | linear | passthrough | passthrough_scaled
    baseline: Optional[float] = None
    zero_at: Optional[float] = None
    one_at: Optional[float] = None
    clip_low: Optional[float] = None
    clip_high: Optional[float] = None
    # redline fields
    min_val: Optional[float] = None
    max_val: Optional[float] = None


@dataclass
class EndpointConfig:
    """LLM service endpoint configuration provided by the user at submission time."""
    api_url: str
    model: str
    api_key: str = ""
    processor_path: str = ""


@dataclass
class ModuleResult:
    """
    Raw output of one TestModule execution.

    score and passed are placeholder defaults — the platform worker
    overwrites them using the benchmark's MetricConfig rules via the
    evaluator.  Modules must not compute final score/passed.
    """
    metrics: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    artifacts: List[str] = field(default_factory=list)
    # Display-only MetricConfig rows (as dicts) for metrics whose keys are only
    # known at run time (e.g. perf_guidellm_sweep's per-concurrency-level
    # `*_c{N}` keys — the levels are a module param, so static
    # default_metric_configs can't cover them). The worker appends these to the
    # run's metric_configs snapshot so the frontend renders the metrics; rows
    # whose role is not "display" are dropped there — modules must never
    # influence score/redline evaluation through this field.
    extra_display_configs: List[dict] = field(default_factory=list)
    # placeholder values; overwritten by evaluator in jobs.py
    passed: bool = True
    score: float = 0.0

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "score": self.score,
            "metrics": self.metrics,
            "error": self.error,
            "artifacts": self.artifacts,
        }


ProgressCallback = Callable[[float, str], None]  # (fraction_0_1, message)

# Params whose name ends in one of these carry credentials (e.g. the judge
# endpoint's judge_api_key) and must never reach the worker log verbatim: that
# log is persisted per-submission and downloadable by the SUBMITTER, who is not
# the admin that configured the benchmark. Mirrors the platform-side detection
# in app/core/param_secrets.py.
_SECRET_PARAM_RE = re.compile(
    r"(api_key|apikey|api_token|access_token|secret|password)$", re.IGNORECASE
)


def redact_secret_params(params: Dict[str, Any]) -> Dict[str, Any]:
    """Copy of ``params`` with non-empty secret string values masked — use this
    whenever a module echoes its received params to the logger."""
    return {
        k: ("***" if _SECRET_PARAM_RE.search(k) and isinstance(v, str) and v else v)
        for k, v in params.items()
    }


class BenchmarkCancelled(Exception):
    """Raised when a benchmark run is cancelled by the user."""


class AllRequestsFailed(RuntimeError):
    """Raised when a load run completed but not one request succeeded.

    guidellm reports such a run as a normal completion whose metrics happen to
    be all-zero, which the evaluator then scores 0 — indistinguishable on the
    leaderboard from a genuinely slow model, even when the real cause was that
    no request ever left the platform (e.g. an API key that is illegal in an
    HTTP header). Raising instead lets the worker record the concrete reason as
    the run's error, so the submitter can act on it.

    Deliberately propagated past the broad ``except Exception`` handlers in the
    guidellm run path — those exist to turn crashes into "no metrics", which is
    precisely the silence this replaces.
    """


class BenchmarkTimeout(RuntimeError):
    """Raised when a load run blew its wall-clock budget and had to be abandoned.

    guidellm has been observed to wedge AFTER its request phase completes: the
    benchmark ran its full duration, every request returned 200, and then the
    scheduler deadlocked in teardown with its forked worker processes parked
    forever. Nothing inside guidellm recovers from this — even killing every one
    of its workers only produced a log line, not an abort — so the only bound is
    an external deadline. A run that trips it is abandoned and its orphaned child
    processes killed, freeing the worker slot.

    Like AllRequestsFailed, this is deliberately propagated past the broad
    ``except Exception`` handlers in the guidellm run path: "timed out after N
    seconds" is a far more actionable run error than "produced no metrics".
    """


def _noop_progress(fraction: float, message: str) -> None:
    pass


@dataclass(frozen=True)
class DatasetFeedFields:
    """Which of a module's params drive rolling-dataset selection.

    Declared by the module (see `TestModule.dataset_feed_fields`) so the platform
    worker can resolve "the latest build of profile X" to a concrete file path
    without knowing anything module-specific. Field names rather than values:
    the worker reads and rewrites them on the validated params object.

    source:      param holding "fixed" | "auto"
    profile:     param holding the feed profile name (used when source == auto)
    path:        param the resolved dataset path is written to
    max_age:     param holding the staleness threshold in hours (0 = no check)
    auto_value:  the `source` value that means "use the feed"
    """

    source: str
    profile: str
    path: str
    max_age: str = ""
    auto_value: str = "auto"


class TestModule(ABC):
    """
    Abstract base for all modular test units.

    Subclasses define:
      name                   — registry key (e.g. "perf_guidellm")
      display_name           — shown in admin UI and leaderboard
      description            — one-paragraph explanation shown to users
      ParamsSchema           — pydantic model; fields become the admin config form
      metrics_descriptors    — display metadata for each metric this module emits
      default_metric_configs — default evaluation rules (admin can override per-benchmark)

    The run() method must return a ModuleResult with only metrics filled in.
    Score and passed are computed by the evaluator using the benchmark's
    metric_configs, not by the module itself.
    """

    name: str = "unnamed"
    display_name: str = "Unnamed Module"
    description: str = ""
    ParamsSchema: Type[BaseModel]
    metrics_descriptors: ClassVar[List[MetricDescriptor]] = []
    default_metric_configs: ClassVar[List[MetricConfig]] = []

    @classmethod
    def time_budget_seconds(cls, params) -> "float | None":
        """This module's own effective wall-clock bound, or None if it has none.

        The worker sizes its runaway backstop ABOVE this value, so a module that
        enforces its own budget always trips its own mechanism first — which can
        stop cleanly and keep partial results — and only meets the worker's blunt
        abort if that mechanism itself wedges (which is exactly what guidellm did
        on 2026-07-22).

        The default reads ``params.max_seconds``, which is right for modules whose
        budget is a single duration. Override it where the real bound is derived:
        a sweep runs ``max_seconds`` PER concurrency level, so reading the field
        directly would under-estimate by a factor of len(levels) and the worker
        would abort healthy runs.
        """
        value = getattr(params, "max_seconds", None)
        try:
            value = float(value)
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    # Lifecycle flag. A module slated for removal sets deprecated=True and a
    # short deprecation_note explaining the replacement. The flag flows through
    # descriptor() → GET /modules → the frontend, which renders a "Deprecated"
    # badge/warning. Removing a deprecated module later is just deleting its
    # file + its MODULE_REGISTRY entry — nothing reads these fields structurally.
    deprecated: bool = False
    deprecation_note: str = ""

    @abstractmethod
    def run(
        self,
        endpoint: EndpointConfig,
        params: BaseModel,
        output_dir: str,
        progress_cb: ProgressCallback = _noop_progress,
        cancel_event: Optional[Event] = None,
    ) -> ModuleResult:
        raise NotImplementedError

    @classmethod
    def params_schema_json(cls) -> dict:
        return cls.ParamsSchema.model_json_schema()

    @classmethod
    def default_params(cls) -> dict:
        return cls.ParamsSchema().model_dump()

    # Param names (in priority order) the submission-level concurrency override
    # targets. Modules spell this field differently — most use `concurrency`, but
    # e.g. opencompass uses `max_workers`. To alias another spelling, add it here
    # (one edit); everything downstream — worker, descriptor flag, submit-page tip
    # — follows automatically.
    concurrency_param_aliases: ClassVar[tuple[str, ...]] = ("concurrency", "max_workers")

    # Card baseline for the display-only `*_tpm_card_norm` metrics (throughput
    # rescaled as if the endpoint ran on this many cards). A perf module opts in
    # by setting an integer; None (the default) means the platform worker — which
    # computes these, since the submitted card count is invisible to the bench
    # layer — skips normalization for this module. See
    # platform/backend/app/core/card_normalize.py.
    card_norm_baseline: ClassVar[Optional[int]] = None

    @classmethod
    def concurrency_param_name(cls) -> Optional[str]:
        """Name of this module's concurrency-equivalent param (the field the
        override writes), or None if it has none. Resolved against
        `concurrency_param_aliases`, so a differently-named field (opencompass's
        `max_workers`) is covered without special-casing."""
        fields = cls.ParamsSchema.model_fields
        for alias in cls.concurrency_param_aliases:
            if alias in fields:
                return alias
        return None

    @classmethod
    def accepts_concurrency_override(cls) -> bool:
        """True if this module has a concurrency-equivalent param the
        submission-level override can target. Derived straight from the schema, so
        a new module declaring `concurrency` (or any aliased spelling) is picked up
        automatically — no edits in the worker or the frontend. Single source of
        truth: the worker gates/applies the override on the same resolved param,
        and the descriptor/benchmark APIs surface it so the submit page can list
        which modules the override will affect."""
        return cls.concurrency_param_name() is not None

    # Which params drive "use the rolling dataset feed instead of a fixed file".
    # None (the default) means this module has no dataset that rotates, and the
    # worker's resolution step skips it entirely. A module opts in by returning a
    # DatasetFeedFields — one edit, and the worker, the module API and the admin
    # UI all follow. See bench/replay_test/dataset_feed.py for what a feed is.
    @classmethod
    def dataset_feed_fields(cls) -> "Optional[DatasetFeedFields]":
        return None

    @classmethod
    def accepts_dataset_feed(cls) -> bool:
        return cls.dataset_feed_fields() is not None

    @classmethod
    def descriptor(cls) -> dict:
        """Serialisable summary consumed by GET /modules and the startup sync hook."""
        return {
            "name": cls.name,
            "display_name": cls.display_name,
            "description": cls.description,
            "params_schema": cls.params_schema_json(),
            "default_params": cls.default_params(),
            "metrics_descriptors": [asdict(m) for m in cls.metrics_descriptors],
            "default_metric_configs": [asdict(mc) for mc in cls.default_metric_configs],
            "deprecated": cls.deprecated,
            "deprecation_note": cls.deprecation_note,
            "supports_concurrency_override": cls.accepts_concurrency_override(),
            "supports_dataset_feed": cls.accepts_dataset_feed(),
        }
