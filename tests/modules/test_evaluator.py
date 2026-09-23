"""Tests for the metric evaluator's dotted-path key resolution.

Nested metrics (perf_guidellm_sweep's per-concurrency c{N} dicts) are
addressed in MetricConfig rules with dotted keys like "c8.ttft_p99_ms".
Exact flat keys must always win so existing modules and stored runs keep
evaluating identically.
"""
from bench.modules.evaluator import (
    check_redlines,
    compute_score,
    explain_score,
    resolve_metric,
)

NESTED = {
    "output_tps": 210.5,
    "c8": {"ttft_p99_ms": 1800.0, "output_tps_mean": 210.5},
    "c16": {"ttft_p99_ms": 4200.0, "uptime": 0.97},
}


def test_resolve_flat_key():
    assert resolve_metric(NESTED, "output_tps") == 210.5


def test_resolve_dotted_key_walks_nested():
    assert resolve_metric(NESTED, "c8.ttft_p99_ms") == 1800.0
    assert resolve_metric(NESTED, "c16.uptime") == 0.97


def test_resolve_exact_match_wins_over_path_walk():
    metrics = {"c8.ttft_p99_ms": 1.0, "c8": {"ttft_p99_ms": 2.0}}
    assert resolve_metric(metrics, "c8.ttft_p99_ms") == 1.0


def test_resolve_missing_returns_none():
    assert resolve_metric(NESTED, "c99.ttft_p99_ms") is None
    assert resolve_metric(NESTED, "c8.nope") is None
    assert resolve_metric(NESTED, "c8.ttft_p99_ms.deeper") is None
    assert resolve_metric(NESTED, "absent") is None


def test_compute_score_with_dotted_key():
    configs = [{"key": "c8.output_tps_mean", "role": "score",
                "formula": "ratio", "baseline": 421.0, "weight": 1.0}]
    assert compute_score(NESTED, configs) == 210.5 / 421.0


def test_check_redlines_with_dotted_keys():
    ok = [{"key": "c8.ttft_p99_ms", "role": "redline", "max_val": 2000.0}]
    assert check_redlines(NESTED, ok) is True
    bad = [{"key": "c16.ttft_p99_ms", "role": "redline", "max_val": 2000.0}]
    assert check_redlines(NESTED, bad) is False
    # missing metric is ignored, as for flat keys
    missing = [{"key": "c99.ttft_p99_ms", "role": "redline", "max_val": 1.0}]
    assert check_redlines(NESTED, missing) is True


def test_key_pointing_at_level_dict_is_skipped():
    """A config key naming a whole c{N} dict is non-numeric — must be skipped
    (score) / ignored (redline), not crash."""
    configs = [{"key": "c8", "role": "score", "formula": "ratio",
                "baseline": 1.0, "weight": 1.0}]
    assert compute_score(NESTED, configs) == 0.0
    assert check_redlines(NESTED, [{"key": "c8", "role": "redline", "max_val": 1.0}]) is True


def test_explain_score_with_dotted_key():
    configs = [{"key": "c8.output_tps_mean", "role": "score",
                "formula": "ratio", "baseline": 421.0, "weight": 1.0}]
    rows = explain_score(NESTED, configs)
    assert rows[0]["value"] == 210.5
    assert rows[0]["term_score"] == 210.5 / 421.0
