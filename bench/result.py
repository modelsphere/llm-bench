from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class TestResult:
    """Result of a single test (one scenario / one concurrency level)."""
    name: str
    passed: bool
    metrics: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None


@dataclass
class PhaseResult:
    """Aggregated result for one evaluation phase."""
    # "functional" | "redline" | "standard_perf" | "extreme"
    phase: str
    passed: bool
    results: List[TestResult] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "phase": self.phase,
            "passed": self.passed,
            "results": [
                {"name": r.name, "passed": r.passed, "metrics": r.metrics, "error": r.error}
                for r in self.results
            ],
        }


@dataclass
class FinalResult:
    """Top-level result of the full benchmark suite."""
    phases: List[PhaseResult] = field(default_factory=list)
    passed_redline: bool = False
    success: bool = False

    # Component scores (0–1 range each)
    score_throughput: float = 0.0
    score_latency: float = 0.0
    score_stability: float = 0.0
    score_bonus: float = 0.0
    score_total: float = 0.0

    # Raw metrics surfaced for leaderboard display
    input_tps: Optional[float] = None
    output_tps: Optional[float] = None
    total_tps: Optional[float] = None
    ttft_p99_ms: Optional[float] = None
    tpot_p99_ms: Optional[float] = None
    itl_p99_ms: Optional[float] = None
    ttft_p50_ms: Optional[float] = None
    tpot_p50_ms: Optional[float] = None
    itl_p50_ms: Optional[float] = None
    input_tps_p50: Optional[float] = None
    output_tps_p50: Optional[float] = None
    uptime: Optional[float] = None

    # Agentic phase (Type D) flat metrics
    ttft_p50_ms_agent: Optional[float] = None
    ttft_p99_ms_agent: Optional[float] = None
    itl_p50_ms_agent: Optional[float] = None
    itl_p99_ms_agent: Optional[float] = None
    output_cps_mean_agent: Optional[float] = None
    uptime_agent: Optional[float] = None
    total_queries_agent: Optional[int] = None
    failed_queries_agent: Optional[int] = None

    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "passed_redline": self.passed_redline,
            "score_throughput": self.score_throughput,
            "score_latency": self.score_latency,
            "score_stability": self.score_stability,
            "score_bonus": self.score_bonus,
            "score_total": self.score_total,
            "input_tps": self.input_tps,
            "output_tps": self.output_tps,
            "total_tps": self.total_tps,
            "ttft_p99_ms": self.ttft_p99_ms,
            "tpot_p99_ms": self.tpot_p99_ms,
            "itl_p99_ms": self.itl_p99_ms,
            "ttft_p50_ms": self.ttft_p50_ms,
            "tpot_p50_ms": self.tpot_p50_ms,
            "itl_p50_ms": self.itl_p50_ms,
            "input_tps_p50": self.input_tps_p50,
            "output_tps_p50": self.output_tps_p50,
            "uptime": self.uptime,
            "ttft_p50_ms_agent": self.ttft_p50_ms_agent,
            "ttft_p99_ms_agent": self.ttft_p99_ms_agent,
            "itl_p50_ms_agent": self.itl_p50_ms_agent,
            "itl_p99_ms_agent": self.itl_p99_ms_agent,
            "output_cps_mean_agent": self.output_cps_mean_agent,
            "uptime_agent": self.uptime_agent,
            "total_queries_agent": self.total_queries_agent,
            "failed_queries_agent": self.failed_queries_agent,
            "error": self.error,
            "phases": [p.to_dict() for p in self.phases],
        }
