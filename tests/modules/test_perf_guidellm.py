"""Unit tests for the perf_guidellm module against mock_server."""
import pytest
from bench.modules import get_module
from bench.modules.base import ModuleResult


@pytest.mark.integration
def test_perf_guidellm_guidellm_backend_runs(mock_endpoint, tmp_path):
    """Regression for the run_guidellm_load refactor (shared with the sweep
    module): the single-level guidellm path must still produce the same flat
    metric contract — canonical keys plus the legacy *_c{N} suffixed keys."""
    cls = get_module("perf_guidellm")
    params = cls.ParamsSchema.model_construct(
        backend="guidellm",
        concurrency=2,
        input_tokens=64,
        output_tokens=32,
        max_seconds=8.0,        # below ge=10 floor — use model_construct for CI
        request_timeout=30.0,
        warmup_seconds=0.0,
        dataset_path=None,
        processor_path="",
    )
    result = cls().run(mock_endpoint, params, str(tmp_path))

    assert isinstance(result, ModuleResult)
    assert result.error is None, f"Module returned error: {result.error}"
    # Canonical keys used by default score/redline configs
    assert result.metrics["output_tps"] > 0
    assert result.metrics["input_tps"] > 0
    assert "ttft_p99_ms" in result.metrics
    # P90 is reported alongside P50/P99 for every latency family
    for stat in ("ttft", "tpot", "itl"):
        for pct in ("p50", "p90", "p99"):
            assert f"{stat}_{pct}_ms" in result.metrics, f"missing {stat}_{pct}_ms"
    assert result.metrics["uptime"] > 0.5
    # Per-request (single-stream) output TPS, matching the sweep module's key
    assert result.metrics["request_output_tps"] == pytest.approx(
        result.metrics["output_tps"] / 2
    )
    # Single-level module keeps its flat legacy per-level keys (no nesting)
    assert "output_tps_mean_c2" in result.metrics
    # ...and also exposes that level under flat, unsuffixed keys, so every stat
    # can be named in a MetricConfig (parity with the sweep's _promote_canonical)
    assert result.metrics["measured_requests"] == result.metrics["measured_requests_c2"]
    assert result.metrics["duration_seconds"] == result.metrics["duration_seconds_c2"]
    assert result.metrics["total_tps_p99"] == result.metrics["total_tps_p99_c2"]
    assert result.metrics["measured_requests"] > 0
    assert result.metrics["reported_concurrency"] == 2
    # No sweep-only artifacts leak into this module
    assert "c2" not in result.metrics
    assert result.extra_display_configs == []


# ---------------------------------------------------------------------------
# request_output_tps: shared with perf_guidellm_sweep
# ---------------------------------------------------------------------------

def test_request_output_tps_prefers_the_reported_level():
    """guidellm's reported level is the divisor, so numerator and denominator
    describe the same run even if it differed from what was requested."""
    from bench.modules.perf_guidellm import _add_flat_request_output_tps

    metrics = {"output_tps": 200.0, "reported_concurrency": 8}
    _add_flat_request_output_tps(metrics, concurrency=4)
    assert metrics["request_output_tps"] == 25.0


def test_request_output_tps_falls_back_to_requested_concurrency():
    """A run that reports no level divides by the requested one."""
    from bench.modules.perf_guidellm import _add_flat_request_output_tps

    metrics = {"output_tps": 200.0}
    _add_flat_request_output_tps(metrics, concurrency=4)
    assert metrics["request_output_tps"] == 50.0


@pytest.mark.parametrize("metrics", [
    {},                                             # backend produced no TPS
    {"output_tps": None},                           # all requests failed
    {"output_tps": 200.0, "reported_concurrency": 0},   # nonsense level
])
def test_request_output_tps_is_omitted_when_it_cannot_be_computed(metrics):
    """A missing metric beats a bogus one — the key is simply absent."""
    from bench.modules.perf_guidellm import _add_flat_request_output_tps

    _add_flat_request_output_tps(metrics, concurrency=0)
    assert "request_output_tps" not in metrics


def test_both_guidellm_modules_declare_request_output_tps_once():
    """The sweep splats the single module's descriptors, so the metric must be
    declared in exactly one place or it ships twice."""
    from bench.modules.perf_guidellm import PerfGuidellmModule
    from bench.modules.perf_guidellm_sweep import PerfGuidellmSweepModule

    for cls in (PerfGuidellmModule, PerfGuidellmSweepModule):
        names = [d.name for d in cls.metrics_descriptors]
        assert names.count("request_output_tps") == 1, cls.__name__
        assert len(names) == len(set(names)), f"{cls.__name__} has duplicate descriptors"
        keys = [c.key for c in cls.default_metric_configs]
        assert keys.count("request_output_tps") == 1, cls.__name__


def test_single_level_promotion_does_not_overwrite_realworld_choices():
    """RealWorldTest already promotes a hardcoded subset; where it has chosen a
    flat value that one wins, and only the stats it skips get filled in."""
    from bench.modules.perf_guidellm import _promote_single_level_keys

    metrics = {
        "ttft_p99_ms": 111.0,        # already promoted by RealWorldTest
        "ttft_p99_ms_c8": 999.0,     # same level, would be the same number in practice
        "measured_requests_c8": 40.0,  # never promoted by RealWorldTest
    }
    _promote_single_level_keys(metrics)
    assert metrics["ttft_p99_ms"] == 111.0
    assert metrics["measured_requests"] == 40.0
    # Suffixed keys survive — they are this module's long-standing contract
    assert metrics["ttft_p99_ms_c8"] == 999.0


def test_single_level_promotion_ignores_keys_without_a_level_suffix():
    from bench.modules.perf_guidellm import _promote_single_level_keys

    metrics = {"uptime": 1.0, "http_status_200": 5.0, "reported_concurrency": 8}
    _promote_single_level_keys(metrics)
    assert metrics == {"uptime": 1.0, "http_status_200": 5.0, "reported_concurrency": 8}


def test_sweep_only_metrics_stay_out_of_the_single_module():
    """The sweep's remaining extras are meaningless for one level — peak_* (the
    only level is the peak), the SLO verdicts and search_* (no such params here).
    Pinned so a future descriptor move doesn't quietly drag them across."""
    from bench.modules.perf_guidellm import PerfGuidellmModule
    from bench.modules.perf_guidellm_sweep import PerfGuidellmSweepModule

    single = {d.name for d in PerfGuidellmModule.metrics_descriptors}
    sweep = {d.name for d in PerfGuidellmSweepModule.metrics_descriptors}
    assert sweep - single == {
        "peak_concurrency", "peak_input_tps", "peak_output_tps", "peak_total_tps",
        "reported_level_meets_slo", "slo_fail_concurrency", "duration_cap_seconds",
        "search_probes", "search_converged", "search_research_rounds",
        "drain_wait_seconds", "pre_level_canary_ms", "started_drained",
    }
    # Everything else the sweep declares, the single module declares too
    assert single - sweep == set()
