"""
bench/modules/evaluator.py — Shared metric evaluation engine.

Takes raw metrics from a module run and a list of MetricConfig dicts
(stored in BenchmarkModule.metric_configs_json) and computes:
  - module score (float)
  - passed    (bool)

This is the single source of truth for scoring and redline logic.
Modules themselves must NOT compute score or passed.

Score formula reference
-----------------------
role="score" terms are combined as:
  score = Σ(term_score × weight) / Σ(weight)

Per-term formulas (field: formula):
  ratio                — term = value / baseline              (uncapped)
  ratio_capped         — term = min(value / baseline, 1.0)
  inverse_ratio_capped — term = min(baseline / value, 1.0)    (lower-is-better latencies)
  linear               — term = (value − zero_at)/(one_at − zero_at), clamped [0, 1]
  passthrough          — term = value                          (already-normalised metrics)

clip_low / clip_high optionally clamp the computed term before weighting.

role="redline" terms gate pass/fail:
  min_val — metric must be ≥ min_val
  max_val — metric must be ≤ max_val
  A missing metric (None) is ignored.
"""
from __future__ import annotations

from typing import Any


def resolve_metric(metrics: dict[str, Any], key: str) -> Any:
    """Resolve a MetricConfig key against a metrics dict.

    An exact flat key always wins (so existing modules and stored runs are
    unaffected). Otherwise a dotted key walks nested dicts — e.g.
    "c8.ttft_p99_ms" reads metrics["c8"]["ttft_p99_ms"], the shape
    perf_guidellm_sweep emits per concurrency level. Returns None if absent.
    The frontend mirrors these semantics in SubmissionDetail.resolveMetric —
    keep the two in sync.
    """
    if key in metrics:
        return metrics[key]
    if "." not in key:
        return None
    cur: Any = metrics
    for part in key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def evaluate(
    metrics: dict[str, Any],
    configs: list[dict],
) -> tuple[float, bool]:
    """Return (score, passed) for a module given its raw metrics and eval config."""
    score = compute_score(metrics, configs)
    passed = check_redlines(metrics, configs)
    return score, passed


def compute_score(metrics: dict[str, Any], configs: list[dict]) -> float:
    score_terms = [c for c in configs if c.get("role") == "score"]
    if not score_terms:
        return 0.0

    # passthrough_scaled bypasses the /total_weight normalization: its
    # contribution is weight * value and the module score becomes
    # (normalized sum of regular terms) + sum(passthrough_scaled terms).
    # This lets cost-style metrics (e.g. TPM * $/token) push the overall
    # score above 1 without the normalization diluting them.
    normalized = [c for c in score_terms if c.get("formula") != "passthrough_scaled"]
    scaled = [c for c in score_terms if c.get("formula") == "passthrough_scaled"]

    norm_total = 0.0
    norm_weight = sum(c.get("weight", 1.0) for c in normalized)
    for c in normalized:
        raw = resolve_metric(metrics, c["key"])
        if raw is None or raw == "":
            continue
        try:
            val = float(raw)
        except (TypeError, ValueError):
            continue

        term = _apply_formula(val, c)

        clip_low = c.get("clip_low")
        clip_high = c.get("clip_high")
        if clip_low is not None:
            term = max(term, float(clip_low))
        if clip_high is not None:
            term = min(term, float(clip_high))

        norm_total += term * c.get("weight", 1.0)

    total = (norm_total / norm_weight) if norm_weight else 0.0

    for c in scaled:
        raw = resolve_metric(metrics, c["key"])
        if raw is None or raw == "":
            continue
        try:
            val = float(raw)
        except (TypeError, ValueError):
            continue
        total += c.get("weight", 1.0) * val

    return total


def check_redlines(metrics: dict[str, Any], configs: list[dict]) -> bool:
    for c in configs:
        if c.get("role") != "redline":
            continue
        raw = resolve_metric(metrics, c["key"])
        if raw is None:
            continue
        try:
            val = float(raw)
        except (TypeError, ValueError):
            continue
        min_val = c.get("min_val")
        max_val = c.get("max_val")
        if min_val is not None and val < float(min_val):
            return False
        if max_val is not None and val > float(max_val):
            return False
    return True


def explain_score(
    metrics: dict[str, Any],
    configs: list[dict],
) -> list[dict]:
    """
    Return a list of dicts explaining the score computation, one per score term.
    Used by the frontend to render the score breakdown table.

    Each entry:
      key, formula, value, baseline/zero_at/one_at, term_score, weight, contribution
    """
    score_terms = [c for c in configs if c.get("role") == "score"]
    # Normalization total only spans regular terms — passthrough_scaled
    # contributes weight*value directly and never participates in the
    # /total_weight denominator (see compute_score).
    norm_weight = sum(
        c.get("weight", 1.0) for c in score_terms
        if c.get("formula") != "passthrough_scaled"
    ) or 1.0
    rows = []
    for c in score_terms:
        raw = resolve_metric(metrics, c["key"])
        # Empty strings show up when an upstream test produced no result for a
        # metric; treat them the same as missing so the breakdown view doesn't
        # crash on float('').
        try:
            val = float(raw) if raw is not None and raw != "" else None
        except (TypeError, ValueError):
            val = None
        term = _apply_formula(val, c) if val is not None else None

        is_scaled = c.get("formula") == "passthrough_scaled"
        if term is not None and not is_scaled:
            clip_low = c.get("clip_low")
            clip_high = c.get("clip_high")
            if clip_low is not None:
                term = max(term, float(clip_low))
            if clip_high is not None:
                term = min(term, float(clip_high))

        weight = c.get("weight", 1.0)
        if is_scaled:
            contribution = (weight * val) if val is not None else None
            weight_fraction = None  # not normalized
        else:
            contribution = (term * weight / norm_weight) if term is not None else None
            weight_fraction = weight / norm_weight
        rows.append({
            "key": c["key"],
            "formula": c.get("formula", "ratio"),
            "value": val,
            "baseline": c.get("baseline"),
            "zero_at": c.get("zero_at"),
            "one_at": c.get("one_at"),
            "clip_low": c.get("clip_low"),
            "clip_high": c.get("clip_high"),
            "weight": weight,
            "weight_fraction": weight_fraction,
            "term_score": term,
            "contribution": contribution,
        })
    return rows


def _apply_formula(val: float, c: dict) -> float:
    formula = c.get("formula", "ratio")
    if formula == "ratio":
        baseline = float(c.get("baseline") or 1.0)
        return val / baseline
    if formula == "ratio_capped":
        baseline = float(c.get("baseline") or 1.0)
        return min(val / baseline, 1.0)
    if formula == "inverse_ratio_capped":
        baseline = float(c.get("baseline") or 1.0)
        return min(baseline / val, 1.0) if val != 0 else 0.0
    if formula == "linear":
        zero_at = float(c.get("zero_at") or 0.0)
        one_at = float(c.get("one_at") or 1.0)
        span = one_at - zero_at
        if span == 0:
            return 0.0
        raw = (val - zero_at) / span
        return max(0.0, min(1.0, raw))
    if formula == "passthrough":
        return val
    if formula == "passthrough_scaled":
        # compute_score handles this formula specially (no normalization, no
        # clip). Returning val here just keeps the breakdown view honest if it
        # ends up calling _apply_formula directly on a scaled term.
        return val
    return val
