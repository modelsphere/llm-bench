"""Tests for the perf_guidellm_sweep module.

Pure-logic tests cover param parsing/normalisation, peak aggregation, the
per-level display-config generation, and the multi-level metric extraction
contract of RealWorldTest. One integration test drives the real guidellm
multi-rate path against mock_server.
"""
import pytest

from bench.modules import get_module
from bench.modules.base import ModuleResult
from bench.modules.perf_guidellm_sweep import (
    PerfGuidellmSweepParams,
    _add_peak_metrics,
    _add_request_output_tps,
    _nest_level_metrics,
    _per_level_display_configs,
    _promote_canonical,
    _select_reported_level,
    parse_concurrencies,
)


# ---------------------------------------------------------------------------
# parse_concurrencies / params validation
# ---------------------------------------------------------------------------

def test_parse_basic():
    assert parse_concurrencies("1,2,4,8,16") == [1, 2, 4, 8, 16]


def test_parse_accepts_spaces_and_mixed_separators():
    assert parse_concurrencies(" 1, 4  8,16 ") == [1, 4, 8, 16]


@pytest.mark.parametrize("raw", ["", "  ", "1,x,4", "0,4", "4,2000", ",".join(str(i) for i in range(1, 30))])
def test_parse_rejects_invalid(raw):
    with pytest.raises(ValueError):
        parse_concurrencies(raw)


@pytest.mark.parametrize("raw", ["16,8", "1,4,2", "4,4", "1,8,8,16"])
def test_parse_rejects_non_strictly_increasing(raw):
    with pytest.raises(ValueError, match="strictly increasing"):
        parse_concurrencies(raw)


def test_params_normalize_concurrencies():
    p = PerfGuidellmSweepParams(concurrencies="1, 8,  16")
    assert p.concurrencies == "1,8,16"
    assert p.concurrency_levels() == [1, 8, 16]


def test_params_default_valid():
    p = PerfGuidellmSweepParams()
    assert p.concurrency_levels() == [1, 2, 4, 8, 16]


def test_params_fixed_requires_valid_fixed_level():
    with pytest.raises(ValueError, match="requires fixed_level"):
        PerfGuidellmSweepParams(concurrencies="1,8", report_level="fixed")
    with pytest.raises(ValueError, match="not one of the swept"):
        PerfGuidellmSweepParams(concurrencies="1,8", report_level="fixed", fixed_level=4)
    p = PerfGuidellmSweepParams(concurrencies="1,8", report_level="fixed", fixed_level=8)
    assert p.fixed_level == 8


def test_sweep_not_targeted_by_concurrency_override():
    """The submission-level integer override must not apply to a sweep — the
    param is deliberately named `concurrencies` so the alias lookup misses."""
    cls = get_module("perf_guidellm_sweep")
    assert cls.concurrency_param_name() is None
    assert cls.accepts_concurrency_override() is False


# ---------------------------------------------------------------------------
# Aggregation helpers
# ---------------------------------------------------------------------------

def test_nest_level_metrics_groups_flat_suffix_keys():
    metrics = {
        "ttft_p99_ms_c8": 1800.0,
        "output_tps_mean_c8": 210.5,
        "uptime_c16": 0.97,
        "output_tps": 210.5,        # canonical — stays flat
        "reported_concurrency": 8,  # canonical — stays flat
        "http_status_200": 42.0,    # no _c{N} suffix — stays flat
    }
    _nest_level_metrics(metrics)
    assert metrics["c8"] == {"ttft_p99_ms": 1800.0, "output_tps_mean": 210.5}
    assert metrics["c16"] == {"uptime": 0.97}
    assert "ttft_p99_ms_c8" not in metrics
    assert metrics["output_tps"] == 210.5
    assert metrics["http_status_200"] == 42.0


def test_add_peak_metrics_picks_highest_total_tps_level():
    metrics = {
        "c1": {"total_tps_mean": 100.0},
        "c8": {"total_tps_mean": 900.0, "input_tps_mean": 800.0, "output_tps_mean": 100.0},
        "c16": {"total_tps_mean": 700.0},
    }
    _add_peak_metrics(metrics)
    assert metrics["peak_concurrency"] == 8
    assert metrics["peak_total_tps"] == 900.0
    assert metrics["peak_input_tps"] == 800.0
    assert metrics["peak_output_tps"] == 100.0


def test_add_peak_metrics_no_levels_is_noop():
    metrics = {"output_tps": 5.0}
    _add_peak_metrics(metrics)
    assert "peak_concurrency" not in metrics


# Sweep where per-request decode speed degrades and TTFT grows with level:
# c1 = 100 out-tps/req, c8 = 25/req, c32 = 7.5/req (total throughput still rises).
def _sweep_metrics():
    return {
        "c1":  {"total_tps_mean": 600.0,  "input_tps_mean": 500.0,
                "output_tps_mean": 100.0, "ttft_p99_ms": 500.0},
        "c8":  {"total_tps_mean": 1200.0, "input_tps_mean": 1000.0,
                "output_tps_mean": 200.0, "ttft_p99_ms": 2000.0},
        "c32": {"total_tps_mean": 1440.0, "input_tps_mean": 1200.0,
                "output_tps_mean": 240.0, "ttft_p99_ms": 9000.0},
    }


def _params(**kw):
    return PerfGuidellmSweepParams(concurrencies="1,8,32", **kw)


def test_request_output_tps_is_output_tps_over_level():
    metrics = _sweep_metrics()
    _add_request_output_tps(metrics)
    assert metrics["c1"]["request_output_tps"] == 100.0
    assert metrics["c8"]["request_output_tps"] == 25.0
    assert metrics["c32"]["request_output_tps"] == 7.5


def test_slo_best_picks_highest_tps_level_meeting_slo():
    metrics = _sweep_metrics()
    _add_request_output_tps(metrics)
    # c32 fails the 10 tok/s per-request floor; c8 is the best passing level.
    p = _params(slo_min_request_output_tps=10.0, slo_max_ttft_ms=120000.0)
    assert _select_reported_level(metrics, p) == 8
    # A tight TTFT cap disqualifies c8 too → c1 remains.
    p = _params(slo_min_request_output_tps=10.0, slo_max_ttft_ms=1000.0)
    assert _select_reported_level(metrics, p) == 1


def test_legacy_ttft_field_overrides_new_one_when_set():
    """A benchmark stored before the split keeps scoring against the threshold
    it was configured with, rather than silently adopting the new default."""
    metrics = _sweep_metrics()
    _add_request_output_tps(metrics)
    # New field alone would pass c8; the legacy field is tighter and must win.
    p = _params(slo_max_ttft_ms=120000.0, slo_max_ttft_p99_ms=1000.0)
    assert p.effective_max_ttft_ms() == 1000.0
    assert _select_reported_level(metrics, p) == 1
    # Unset legacy field → the new one applies.
    p = _params(slo_max_ttft_ms=120000.0)
    assert p.effective_max_ttft_ms() == 120000.0
    assert _select_reported_level(metrics, p) == 8


def test_legacy_ttft_field_honours_the_percentile_selector():
    """The legacy name says p99 but it has always applied at whichever
    percentile slo_ttft_percentile selects — keep that behaviour."""
    metrics = {"c4": {"total_tps_mean": 100.0, "output_tps_mean": 80.0,
                      "request_output_tps": 20.0,
                      "ttft_p50_ms": 500.0, "ttft_p99_ms": 9000.0}}
    p = _params(slo_max_ttft_p99_ms=1000.0, slo_ttft_percentile="p50")
    assert _select_reported_level(metrics, p) == 4
    _promote_canonical(metrics, 4, p)
    assert metrics["reported_level_meets_slo"] == 1.0  # judged on p50=500ms


def test_slo_best_falls_back_to_lowest_level_when_none_pass():
    metrics = _sweep_metrics()
    _add_request_output_tps(metrics)
    p = _params(slo_min_request_output_tps=500.0)  # unreachable floor
    assert _select_reported_level(metrics, p) == 1
    _promote_canonical(metrics, 1, p)
    assert metrics["reported_level_meets_slo"] == 0.0


def test_report_level_peak_ignores_slo():
    metrics = _sweep_metrics()
    _add_request_output_tps(metrics)
    p = _params(report_level="peak", slo_min_request_output_tps=500.0)
    assert _select_reported_level(metrics, p) == 32


def test_report_level_fixed_uses_configured_level():
    metrics = _sweep_metrics()
    _add_request_output_tps(metrics)
    p = _params(report_level="fixed", fixed_level=8)
    assert _select_reported_level(metrics, p) == 8


def test_report_level_fixed_falls_back_when_level_missing():
    metrics = _sweep_metrics()
    del metrics["c8"]
    _add_request_output_tps(metrics)
    p = _params(report_level="fixed", fixed_level=8)
    # c8 produced no metrics → slo_best fallback → c1 (c32 fails per-request floor)
    assert _select_reported_level(metrics, p) == 1


def test_promote_canonical_copies_level_and_sets_aliases():
    metrics = _sweep_metrics()
    _add_request_output_tps(metrics)
    p = _params()
    _promote_canonical(metrics, 8, p)
    assert metrics["output_tps"] == 200.0
    assert metrics["input_tps"] == 1000.0
    assert metrics["ttft_p99_ms"] == 2000.0
    assert metrics["request_output_tps"] == 25.0
    assert metrics["reported_concurrency"] == 8
    assert metrics["reported_level_meets_slo"] == 1.0
    # nested dicts untouched
    assert metrics["c8"]["output_tps_mean"] == 200.0


def test_select_returns_none_without_levels():
    assert _select_reported_level({"output_tps": 1.0}, _params()) is None


def test_per_level_display_configs_dotted_keys_ordered_by_level():
    metrics = {
        "c16": {"uptime": 1.0},
        "c2": {"uptime": 1.0, "ttft_p99_ms": 5.0},
        "output_tps": 1.0,          # canonical — no config row
        "reported_concurrency": 2,  # canonical — no config row
    }
    rows = _per_level_display_configs(metrics)
    assert all(r["role"] == "display" for r in rows)
    keys = [r["key"] for r in rows]
    assert keys == ["c2.ttft_p99_ms", "c2.uptime", "c16.uptime"]


# ---------------------------------------------------------------------------
# Multi-level extraction contract (RealWorldTest)
# ---------------------------------------------------------------------------

def _fake_stat(mean: float):
    return {
        "successful": {
            "mean": mean,
            "percentiles": {"p50": mean, "p90": mean * 1.5, "p99": mean * 2},
        }
    }


def _fake_guidellm_results(levels):
    benchmarks = []
    for i, conc in enumerate(levels):
        benchmarks.append({
            "config": {"strategy": {"max_concurrency": conc}},
            "metrics": {
                "prompt_tokens_per_second": _fake_stat(1000.0 * (i + 1)),
                "output_tokens_per_second": _fake_stat(100.0 * (i + 1)),
                "tokens_per_second": _fake_stat(1100.0 * (i + 1)),
                "time_to_first_token_ms": _fake_stat(200.0),
                "time_per_output_token_ms": _fake_stat(20.0),
                "inter_token_latency_ms": _fake_stat(20.0),
                "request_totals": {
                    "total": 10, "successful": 10, "errored": 0, "incomplete": 0,
                },
            },
        })
    return {"benchmarks": benchmarks}


def test_realworld_extract_metrics_emits_per_level_keys():
    from bench.tests.b_realworld import RealWorldTest

    test = RealWorldTest("http://localhost:9", "m", "", "/tmp/out", rates=[2.0, 8.0])
    assert test.RATES == [2.0, 8.0]

    metrics = test._extract_metrics(_fake_guidellm_results([2, 8]))
    for conc in (2, 8):
        assert f"output_tps_mean_c{conc}" in metrics
        for stat in ("ttft", "tpot", "itl"):
            for pct in ("p50", "p90", "p99"):
                assert f"{stat}_{pct}_ms_c{conc}" in metrics
        # P90 sits between P50 and P99 (per _fake_stat's 1x/1.5x/2x spread)
        assert metrics[f"ttft_p90_ms_c{conc}"] == 300.0
        assert metrics[f"uptime_c{conc}"] == 1.0
    assert metrics["output_tps_mean_c8"] == 200.0


def test_realworld_env_rates_accept_comma_separated(monkeypatch):
    from bench.tests.b_realworld import RealWorldTest

    monkeypatch.setenv("REALWORLD_TEST_CONCURRENCY", "4,32")
    test = RealWorldTest("http://localhost:9", "m", "", "/tmp/out")
    assert test.RATES == [4.0, 32.0]


# ---------------------------------------------------------------------------
# Integration: real guidellm multi-rate run against mock_server
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_perf_guidellm_sweep_runs(mock_endpoint, tmp_path):
    cls = get_module("perf_guidellm_sweep")
    # model_construct bypasses the ge=10 floor on max_seconds for CI speed;
    # concurrencies must already be normalised (the validator is skipped too).
    params = cls.ParamsSchema.model_construct(
        concurrencies="1,2",
        input_tokens=64,
        output_tokens=32,
        max_seconds=8.0,
        request_timeout=30.0,
        warmup_seconds=0.0,
        dataset_path=None,
        processor_path="",
    )
    result = cls().run(mock_endpoint, params, str(tmp_path))

    assert isinstance(result, ModuleResult)
    assert result.error is None, f"Module returned error: {result.error}"
    # Nested per-level dicts for both swept levels
    for conc in (1, 2):
        level = result.metrics[f"c{conc}"]
        assert "output_tps_mean" in level
        assert "uptime" in level
        for stat in ("ttft", "tpot", "itl"):
            for pct in ("p50", "p90", "p99"):
                assert f"{stat}_{pct}_ms" in level, f"c{conc} missing {stat}_{pct}_ms"
    # Canonical + aggregate keys stay flat
    assert "output_tps" in result.metrics
    assert result.metrics["reported_concurrency"] in (1, 2)
    assert result.metrics["peak_concurrency"] in (1, 2)
    assert result.metrics["peak_total_tps"] > 0
    # Param-driven SLO selection: the mock server is fast, so the reported
    # level must meet the default SLOs and carry per-request output TPS.
    assert result.metrics["reported_level_meets_slo"] == 1.0
    assert result.metrics["request_output_tps"] > 0
    # The reported level's P90s are promoted to canonical flat keys too
    for stat in ("ttft", "tpot", "itl"):
        assert f"{stat}_p90_ms" in result.metrics
    # Display rows use dotted keys for every per-level value
    display_keys = {c["key"] for c in result.extra_display_configs}
    assert "c1.output_tps_mean" in display_keys
    assert all(c["role"] == "display" for c in result.extra_display_configs)
    # Dotted keys resolve through the evaluator, so they work in configs
    from bench.modules.evaluator import resolve_metric
    assert resolve_metric(result.metrics, "c1.output_tps_mean") == \
        result.metrics["c1"]["output_tps_mean"]
