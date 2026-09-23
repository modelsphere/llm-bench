from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class BenchConfig:
    api_url: str
    model: str = "llm"
    dataset_name: str = "random"
    api_key: str = ""

    rate: List[float] = field(
        default_factory=lambda: [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 64.0, 128.0, 256.0, 512.0, 1024, 2048]
    )
    max_seconds: int = 300
    timeout: float = 30
    profile: str = "concurrent"

    # Token configuration
    prompt_tokens: int = 256
    output_tokens: int = 128
    prompt_tokens_range: Optional[List[int]] = None  # [min, max] for uniform random
    output_tokens_range: Optional[List[int]] = None   # [min, max] for uniform random

    # GuideLLM native output directory (controls where guidellm writes json/html/csv)
    guidellm_output_dir: Optional[str] = None

    # Limit per-request records in benchmarks.json (None = all)
    sample_requests: Optional[int] = 10

    # Warmup / cooldown. float: fraction (<1.0) or absolute seconds (>=1.0).
    # A dict is passed through to guidellm's TransientPhaseConfig verbatim —
    # e.g. {"percent": 0.05, "mode": "requests"} anchors the phase to request
    # counts instead of wall time (used with max_requests_per_level, where a
    # level may stop well before its duration cap).
    warmup: "float | dict" = 0.1
    cooldown: "float | dict" = 0.05

    # Ramp-up duration in seconds (gradually increase rate to avoid thundering herd)
    rampup: float = 0.0

    max_requests: Optional[int] = None

    # Per-level request caps for multi-level (concurrent-profile) runs: one
    # value per entry in `rate`, applied in order. Becomes a guidellm
    # max_requests constraint whose list is indexed per strategy; each level
    # stops at its count or at max_seconds, whichever comes first. Needs a
    # guidellm at or after the #786/#877 constraints refactor — before it, the
    # per-strategy index never advanced and every level silently used list[0].
    max_requests_per_level: Optional[List[int]] = None

    # Dataset sampling: guidellm's "shuffle" sampler is incompatible with IterableDataset
    # (file-based datasets). Shuffle must be done by pre-processing the dataset file itself.
    data_sampler: Optional[str] = None
    random_seed: int = 42
