"""Tests for perf_guidellm_sweep's auto mode (SLO-driven concurrency search).

The search driver is exercised with a monkeypatched `_run_probe`, so the
doubling ramp, binary search, confirmation/step-down, retry, and budget logic
run for real while no subprocess is spawned. One integration test drives the
real probe_runner subprocess path against mock_server.
"""
import pytest

from bench.modules import get_module
from bench.modules.base import AllRequestsFailed, EndpointConfig, ModuleResult
import bench.modules.perf_guidellm_sweep as sweep_mod
from bench.modules.perf_guidellm_sweep import (
    PerfGuidellmSweepParams,
    _ProbeOutcome,
    _bracket_granularity,
    _next_bisect_level,
)

_ENDPOINT = EndpointConfig(api_url="http://localhost:9", model="m", api_key="")


def _auto_params(**kw):
    kw.setdefault("search_mode", "auto")
    kw.setdefault("warmup_seconds", 0.0)
    kw.setdefault("probe_seconds", 5.0)
    kw.setdefault("max_seconds", 6.0)   # distinct from probe_seconds — the
    kw.setdefault("search_max_concurrency", 16)  # fakes key off the duration
    # No settle pause or canary drain unless a test asks for them: the fakes
    # never load anything, and the drain would only sleep.
    kw.setdefault("level_settle_seconds", 0.0)
    kw.setdefault("level_drain_max_seconds", 0.0)
    return PerfGuidellmSweepParams(**kw)


# ---------------------------------------------------------------------------
# Param validation
# ---------------------------------------------------------------------------

def test_auto_defaults_valid():
    p = _auto_params()
    assert p.search_mode == "auto"
    assert p.report_level == "slo_best"


def test_auto_ignores_grid_only_params_instead_of_rejecting_them():
    """Auto mode never reads `concurrencies` or `report_level`, so leftover
    values from a grid config must not block switching a benchmark to auto."""
    p = _auto_params(concurrencies="1,2,4", report_level="peak")
    assert p.search_mode == "auto"
    p = _auto_params(report_level="fixed", fixed_level=8)
    assert p.search_mode == "auto"


def test_grid_mode_still_validates_fixed_level():
    with pytest.raises(ValueError, match="requires fixed_level"):
        PerfGuidellmSweepParams(search_mode="grid", concurrencies="1,8",
                                report_level="fixed")


def test_auto_rejects_budget_smaller_than_one_probe_plus_confirm():
    with pytest.raises(ValueError, match="max_total_seconds"):
        _auto_params(probe_seconds=100.0, max_seconds=200.0,
                     warmup_seconds=30.0, max_total_seconds=300.0)


def test_grid_mode_unaffected_by_auto_params():
    p = PerfGuidellmSweepParams(concurrencies="1,2,4", report_level="peak")
    assert p.search_mode == "grid"


# ---------------------------------------------------------------------------
# Pure search helpers
# ---------------------------------------------------------------------------

def test_bracket_granularity_auto_scales_with_lo():
    assert _bracket_granularity(1, 0) == 1
    assert _bracket_granularity(7, 0) == 1
    assert _bracket_granularity(8, 0) == 1
    assert _bracket_granularity(64, 0) == 8
    assert _bracket_granularity(64, 3) == 3   # explicit wins


def test_next_bisect_level():
    assert _next_bisect_level(4, 8, 1) == 6
    assert _next_bisect_level(6, 8, 1) == 7
    assert _next_bisect_level(6, 7, 1) is None          # bracket closed
    assert _next_bisect_level(8, 16, 8) is None         # within granularity
    assert _next_bisect_level(4, 5, 0) is None          # no integer between


# ---------------------------------------------------------------------------
# Search driver (monkeypatched _run_probe — no subprocesses)
# ---------------------------------------------------------------------------

def _fake_probe(p, max_passing, fail_confirm_at=(), all_failed_levels=(),
                abnormal_levels=()):
    """Build a _run_probe stand-in. A level passes the SLOs iff
    level <= max_passing — except confirmation runs (recognised by
    duration == p.max_seconds) for levels in fail_confirm_at, which produce
    metrics that miss the TTFT cap."""
    calls = []
    caps = []
    seeds = []

    def fake(endpoint, params, level, duration, probe_dir, tick, cancel_event,
             log_tag="", requests_per_concurrency=None, random_seed=None,
             **_phase):
        calls.append((level, duration))
        caps.append(requests_per_concurrency)
        seeds.append(random_seed)
        if level in all_failed_levels:
            return _ProbeOutcome(level=level, duration_seconds=duration,
                                 all_requests_failed=True, error="all failed")
        if level in abnormal_levels:
            return _ProbeOutcome(level=level, duration_seconds=duration,
                                 error="boom")
        is_confirm = duration == p.max_seconds
        passing = level <= max_passing and not (is_confirm and level in fail_confirm_at)
        level_metrics = {
            "output_tps_mean": 50.0 * level,
            "input_tps_mean": 100.0 * level,
            "total_tps_mean": 150.0 * level,
            "ttft_p99_ms": 100.0 if passing else 999999.0,
            "request_output_tps": 50.0,
            "uptime": 1.0,
        }
        return _ProbeOutcome(level=level, duration_seconds=duration,
                             level_metrics=level_metrics,
                             http_status={200: 10.0})

    fake.calls = calls
    fake.caps = caps
    fake.seeds = seeds
    return fake


def _run_auto(monkeypatch, tmp_path, p, fake):
    monkeypatch.setattr(sweep_mod, "_run_probe", fake)
    cls = get_module("perf_guidellm_sweep")
    return cls().run(_ENDPOINT, p, str(tmp_path))


def test_search_converges_on_knee(monkeypatch, tmp_path):
    p = _auto_params(search_granularity=1)
    fake = _fake_probe(p, max_passing=6)
    result = _run_auto(monkeypatch, tmp_path, p, fake)

    assert result.error is None
    # ramp 1,2,4 pass, 8 fails; bisect probes 6 (pass) then 7 (fail); confirm 6.
    assert fake.calls == [(1, 5.0), (2, 5.0), (4, 5.0), (8, 5.0),
                          (6, 5.0), (7, 5.0), (6, 6.0)]
    m = result.metrics
    assert m["reported_concurrency"] == 6
    assert m["reported_level_meets_slo"] == 1.0
    assert m["slo_fail_concurrency"] == 7.0
    assert m["search_converged"] == 1.0
    assert m["search_probes"] == 7.0
    # probes carry the small per-slot count, the confirmation the full one
    assert fake.caps == [5, 5, 5, 5, 5, 5, 20]
    # confirmation overwrote the probe entry for c6
    assert m["c6"]["duration_cap_seconds"] == 6.0
    assert m["c8"]["duration_cap_seconds"] == 5.0
    assert m["c8"]["meets_slo"] == 0.0
    assert m["c6"]["meets_slo"] == 1.0
    # canonical keys come from the confirmed level; bookkeeping stays nested
    assert m["output_tps"] == 300.0
    assert "duration_cap_seconds" not in set(m) - {f"c{n}" for n in (1, 2, 4, 6, 7, 8)}
    # http totals summed across every run
    assert m["http_status_200"] == 70.0
    # per-level display rows include the new bookkeeping keys
    keys = {c["key"] for c in result.extra_display_configs}
    assert "c6.meets_slo" in keys and "c6.duration_cap_seconds" in keys


def test_search_passes_at_cap(monkeypatch, tmp_path):
    p = _auto_params(search_max_concurrency=8)
    fake = _fake_probe(p, max_passing=999)
    result = _run_auto(monkeypatch, tmp_path, p, fake)

    assert fake.calls == [(1, 5.0), (2, 5.0), (4, 5.0), (8, 5.0), (8, 6.0)]
    m = result.metrics
    assert m["reported_concurrency"] == 8
    assert m["search_converged"] == 1.0
    assert "slo_fail_concurrency" not in m


def test_baseline_slo_failure_reports_level_one(monkeypatch, tmp_path):
    p = _auto_params()
    fake = _fake_probe(p, max_passing=0)   # even c=1 misses the SLOs
    result = _run_auto(monkeypatch, tmp_path, p, fake)

    assert fake.calls == [(1, 5.0), (1, 6.0)]   # no ramp; confirm still runs
    m = result.metrics
    assert m["reported_concurrency"] == 1
    assert m["reported_level_meets_slo"] == 0.0
    assert m["slo_fail_concurrency"] == 1.0


def test_baseline_all_requests_failed_raises(monkeypatch, tmp_path):
    p = _auto_params()
    fake = _fake_probe(p, max_passing=8, all_failed_levels={1})
    with pytest.raises(AllRequestsFailed, match="concurrency 1"):
        _run_auto(monkeypatch, tmp_path, p, fake)


def test_failed_confirmation_reopens_the_bracket(monkeypatch, tmp_path):
    """A confirmation that contradicts its probe is new evidence, not merely a
    reason to fall back: the range between the failed level and the next known
    passing level must be searched instead of skipped."""
    p = _auto_params(search_max_concurrency=8, search_granularity=1)
    fake = _fake_probe(p, max_passing=8, fail_confirm_at={8})
    result = _run_auto(monkeypatch, tmp_path, p, fake)

    # ramp 1,2,4,8 pass; confirm 8 fails; re-search 5..7 (probes 6 then 7,
    # both passing since only the c=8 confirmation fails); confirm 7.
    assert fake.calls == [(1, 5.0), (2, 5.0), (4, 5.0), (8, 5.0),
                          (8, 6.0), (6, 5.0), (7, 5.0), (7, 6.0)]
    m = result.metrics
    assert m["reported_concurrency"] == 7   # not 4, as a bare step-down gave
    assert m["reported_level_meets_slo"] == 1.0
    assert m["search_research_rounds"] == 1.0
    # the failed confirmation's metrics replace c8 (it produced real data)
    assert m["c8"]["duration_cap_seconds"] == 6.0
    assert m["c8"]["meets_slo"] == 0.0
    assert m["slo_fail_concurrency"] == 8.0


def test_research_rounds_are_bounded(monkeypatch, tmp_path):
    """Every confirmation failing must still terminate, and report a level that
    actually passed a confirmation attempt or the lowest known-good level."""
    p = _auto_params(search_max_concurrency=8, search_granularity=1)
    # Every confirmation fails, however low the search goes.
    fake = _fake_probe(p, max_passing=8, fail_confirm_at={1, 2, 3, 4, 5, 6, 7, 8})
    result = _run_auto(monkeypatch, tmp_path, p, fake)

    assert result.error is None
    confirms = [c for c in fake.calls if c[1] == p.max_seconds]
    assert len(confirms) <= 4          # _MAX_CONFIRM_ROUNDS
    assert result.metrics["reported_concurrency"] >= 1


def test_abnormal_probe_retried_once_then_treated_as_fail(monkeypatch, tmp_path):
    p = _auto_params(search_max_concurrency=8, search_granularity=1)
    fake = _fake_probe(p, max_passing=8, abnormal_levels={4})
    result = _run_auto(monkeypatch, tmp_path, p, fake)

    # c=4 dies twice (retry) and counts as an SLO failure → bisect (2,4) → 3.
    assert fake.calls == [(1, 5.0), (2, 5.0), (4, 5.0), (4, 5.0),
                          (3, 5.0), (3, 6.0)]
    m = result.metrics
    assert m["reported_concurrency"] == 3
    assert m["slo_fail_concurrency"] == 4.0
    assert "c4" not in m   # no usable metrics for the dead level
    assert m["search_probes"] == 6.0


class _FakeTime:
    """Deterministic clock: the fake probe advances it by the run duration."""

    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def test_budget_exhaustion_stops_search_but_still_confirms(monkeypatch, tmp_path):
    clock = _FakeTime()
    monkeypatch.setattr(sweep_mod, "time", clock)
    # probe_cost=5, confirm_cost=6. Budget 30: probes at c=2 (5+11≤30), c=4
    # (10+11≤30), c=8 (15+11≤30) fit; c=16 (20+11>30) does not → budget stop.
    p = _auto_params(search_max_concurrency=16, max_total_seconds=30.0)
    inner = _fake_probe(p, max_passing=999)

    def timed(endpoint, params, level, duration, probe_dir, tick, cancel_event,
              log_tag="", requests_per_concurrency=None, random_seed=None,
             **_phase):
        out = inner(endpoint, params, level, duration, probe_dir, tick,
                    cancel_event, log_tag,
                    requests_per_concurrency=requests_per_concurrency,
                    random_seed=random_seed, **_phase)
        clock.now += duration
        return out

    timed.calls = inner.calls
    result = _run_auto(monkeypatch, tmp_path, p, timed)

    assert timed.calls == [(1, 5.0), (2, 5.0), (4, 5.0), (8, 5.0), (8, 6.0)]
    m = result.metrics
    assert m["reported_concurrency"] == 8
    assert m["search_converged"] == 0.0


# ---------------------------------------------------------------------------
# Integration: real probe_runner subprocesses against mock_server
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_auto_search_runs_subprocesses(mock_endpoint, tmp_path):
    cls = get_module("perf_guidellm_sweep")
    params = cls.ParamsSchema(
        search_mode="auto",
        search_max_concurrency=2,
        input_tokens=64,
        output_tokens=32,
        probe_seconds=5.0,
        max_seconds=6.0,
        warmup_seconds=0.0,
        request_timeout=30.0,
        processor_path="",
        level_settle_seconds=0.0,   # keep the test fast; the canary drain
        level_drain_max_seconds=30.0,  # still runs against mock_server
    )
    result = cls().run(mock_endpoint, params, str(tmp_path))

    assert isinstance(result, ModuleResult)
    assert result.error is None, f"Module returned error: {result.error}"
    m = result.metrics
    # mock server is fast → both levels pass, cap reached, confirm at 2
    assert m["reported_concurrency"] == 2
    assert m["reported_level_meets_slo"] == 1.0
    assert m["search_converged"] == 1.0
    assert m["search_probes"] == 3.0
    # duration_seconds is what the level actually ran; the cap is separate.
    assert m["c1"]["duration_cap_seconds"] == 5.0
    assert m["c2"]["duration_cap_seconds"] == 6.0
    assert 0 < m["c1"]["duration_seconds"] <= 5.0
    assert m["c1"]["measured_requests"] > 0
    assert m["c1"]["meets_slo"] == 1.0
    assert m["output_tps"] > 0
    assert m["request_output_tps"] > 0
    # probe artifacts land in per-run subdirectories
    assert (tmp_path / "probe_c1" / "probe_result.json").exists()
    assert (tmp_path / "confirm_c2" / "probe_result.json").exists()
    # the canary drain ran before every level after the first, against the
    # real mock server, and found it idle
    assert "drain_wait_seconds" not in m["c1"]
    assert m["c2"]["started_drained"] == 1.0
    assert m["c2"]["pre_level_canary_ms"] > 0
    # each run got its own synthetic-prompt seed
    import json
    seeds = [json.load(open(tmp_path / d / "probe_config.json"))["random_seed"]
             for d in ("probe_c1", "probe_c2", "confirm_c2")]
    assert len(set(seeds)) == 3


# ---------------------------------------------------------------------------
# Truncation detection (probes cut short by their duration cap)
# ---------------------------------------------------------------------------

def test_warns_when_level_hits_its_duration_cap(caplog):
    """A count-limited level stopped by its duration cap samples whatever
    finished in time — the main source of run-to-run inconsistency — so it has
    to be visible in the log rather than silently folded into the results."""
    import logging
    from bench.modules.perf_guidellm_sweep import _warn_if_truncated

    # 32 x 5 = 160 requested; ~136 would be measured after warmup/cooldown.
    # Detection is by request shortfall: the measurement window excludes the
    # warmup/cooldown phases, so a capped level still reports a short duration.
    truncated = {"duration_seconds": 48.0, "measured_requests": 40.0}
    with caplog.at_level(logging.WARNING):
        _warn_if_truncated("sweep", "probe", 32, 100.0, 5, truncated,
                           wall_seconds=101.0)
    assert "duration cap" in caplog.text
    assert "160" in caplog.text


def test_no_truncation_warning_when_level_finished_early(caplog):
    import logging
    from bench.modules.perf_guidellm_sweep import _warn_if_truncated

    finished = {"duration_seconds": 12.0, "measured_requests": 136.0}
    with caplog.at_level(logging.WARNING):
        _warn_if_truncated("sweep", "probe", 32, 100.0, 5, finished,
                           wall_seconds=14.0)
    assert "duration cap" not in caplog.text


def test_no_truncation_warning_without_count_limiting(caplog):
    """Duration-limited levels are supposed to run to their cap."""
    import logging
    from bench.modules.perf_guidellm_sweep import _warn_if_truncated

    at_cap = {"duration_seconds": 100.0, "measured_requests": 5.0}
    with caplog.at_level(logging.WARNING):
        _warn_if_truncated("sweep", "probe", 32, 100.0, None, at_cap,
                           wall_seconds=101.0)
    assert "duration cap" not in caplog.text


def test_no_truncation_warning_for_small_count_rounding(caplog):
    """A level that finished far inside its cap is not truncated, even if
    warmup/cooldown rounding leaves it a request or two short."""
    import logging
    from bench.modules.perf_guidellm_sweep import _warn_if_truncated

    tiny = {"duration_seconds": 0.5, "measured_requests": 3.0}
    with caplog.at_level(logging.WARNING):
        _warn_if_truncated("sweep", "probe", 1, 120.0, 5, tiny, wall_seconds=9.0)
    assert "duration cap" not in caplog.text


# ---------------------------------------------------------------------------
# Drain between levels (canary against a fake endpoint, fake clock) and
# per-run synthetic-prompt seeds
# ---------------------------------------------------------------------------

def _install_canary(monkeypatch, latencies):
    """Make _canary_latency return the given values in order (None = the
    canary failed), then repeat the last one forever."""
    seq = list(latencies)
    seen = []

    def canary(endpoint, timeout):
        val = seq.pop(0) if len(seq) > 1 else seq[0]
        seen.append(val)
        return val

    monkeypatch.setattr(sweep_mod, "_canary_latency", canary)
    return seen


def test_every_run_gets_a_distinct_seed(monkeypatch, tmp_path):
    p = _auto_params(search_max_concurrency=4, search_granularity=1)
    fake = _fake_probe(p, max_passing=999)
    _run_auto(monkeypatch, tmp_path, p, fake)

    assert len(fake.seeds) == len(fake.calls)
    assert len(set(fake.seeds)) == len(fake.seeds)
    assert fake.seeds[0] == sweep_mod._BASE_RANDOM_SEED


def test_drain_waits_for_canary_to_recover(monkeypatch, tmp_path, caplog):
    """The idle reference is the best of the pre-search canaries; the level
    after a probe waits until a canary lands within tolerance of it."""
    clock = _FakeTime()
    monkeypatch.setattr(sweep_mod, "time", clock)
    # reference samples: 0.4, 0.3, 0.5 → idle 0.3s, threshold 0.3*2+0.5=1.1s
    # first drain: 5.0 (busy), 3.0 (busy), 0.9 (drained)
    # later drains: 0.9 immediately
    seen = _install_canary(monkeypatch, [0.4, 0.3, 0.5, 5.0, 3.0, 0.9])
    p = _auto_params(search_max_concurrency=2, level_settle_seconds=10.0,
                     level_drain_max_seconds=60.0)
    fake = _fake_probe(p, max_passing=999)
    with caplog.at_level("INFO"):
        result = _run_auto(monkeypatch, tmp_path, p, fake)

    assert result.error is None
    assert seen[:3] == [0.4, 0.3, 0.5]
    m = result.metrics
    # baseline c=1 ran on a fresh endpoint — nothing to drain
    assert "drain_wait_seconds" not in m["c1"]
    # c=2 probe: settle 10s, then two busy canaries 5s apart, then drained
    drained = [r.message for r in caplog.records if "endpoint drained" in r.message]
    assert len(drained) == 2  # before the c=2 probe and the c=2 confirmation
    assert drained[0].startswith("[perf_guidellm_sweep] endpoint drained after 20s")
    # the level dict carries the latest run at that level (the confirmation),
    # which only needed the settle pause
    assert m["c2"]["started_drained"] == 1.0
    assert m["c2"]["pre_level_canary_ms"] == 900.0
    assert m["c2"]["drain_wait_seconds"] == 10.0
    # the drain keys never leak into the canonical flat metrics
    assert "drain_wait_seconds" not in m
    assert "started_drained" not in m


def test_drain_gives_up_at_cap_and_records_it(monkeypatch, tmp_path, caplog):
    clock = _FakeTime()
    monkeypatch.setattr(sweep_mod, "time", clock)
    seen = _install_canary(monkeypatch, [0.3, 0.3, 0.3, 8.0])  # never recovers
    p = _auto_params(search_max_concurrency=2, level_settle_seconds=0.0,
                     level_drain_max_seconds=30.0)
    fake = _fake_probe(p, max_passing=999)
    with caplog.at_level("WARNING"):
        result = _run_auto(monkeypatch, tmp_path, p, fake)

    assert result.error is None
    m = result.metrics
    assert m["c2"]["started_drained"] == 0.0
    assert m["c2"]["pre_level_canary_ms"] == 8000.0
    assert m["c2"]["drain_wait_seconds"] >= 30.0
    assert any("NOT drained" in r.message for r in caplog.records)
    # the search still ran every level it would have without the drain
    assert [c for c, _ in fake.calls] == [1, 2, 2]
    assert len(seen) > 3


def test_drain_disabled_when_reference_canary_fails(monkeypatch, tmp_path):
    clock = _FakeTime()
    monkeypatch.setattr(sweep_mod, "time", clock)
    _install_canary(monkeypatch, [None])
    p = _auto_params(search_max_concurrency=2, level_settle_seconds=5.0,
                     level_drain_max_seconds=30.0)
    fake = _fake_probe(p, max_passing=999)
    result = _run_auto(monkeypatch, tmp_path, p, fake)

    m = result.metrics
    # settle pause still applies; no canary verdict is recorded
    assert m["c2"]["drain_wait_seconds"] == 5.0
    assert "started_drained" not in m["c2"]
    assert "pre_level_canary_ms" not in m["c2"]


def test_drain_time_counts_against_the_search_budget(monkeypatch, tmp_path):
    """A probe slot's cost includes the wait before it, so the budget check
    reserves for it instead of starting a probe that cannot finish."""
    clock = _FakeTime()
    monkeypatch.setattr(sweep_mod, "time", clock)
    _install_canary(monkeypatch, [0.3])
    # probe 5s + settle 10s = 15s per probe slot; confirm 6s (+10s settle).
    # Budget 45: c=1 (5) → c=2 needs 15+16 ≤ 45-5 ok → c=4 needs 15+16 ≤ 45-20 fails.
    p = _auto_params(search_max_concurrency=16, max_total_seconds=45.0,
                     level_settle_seconds=10.0, level_drain_max_seconds=30.0)
    inner = _fake_probe(p, max_passing=999)

    def timed(endpoint, params, level, duration, probe_dir, tick, cancel_event,
              log_tag="", requests_per_concurrency=None, random_seed=None,
             **_phase):
        out = inner(endpoint, params, level, duration, probe_dir, tick,
                    cancel_event, log_tag,
                    requests_per_concurrency=requests_per_concurrency,
                    random_seed=random_seed, **_phase)
        clock.now += duration
        return out

    result = _run_auto(monkeypatch, tmp_path, p, timed)
    assert [c for c, _ in inner.calls] == [1, 2, 2]
    assert result.metrics["search_converged"] == 0.0


def test_drain_honours_cancellation(monkeypatch, tmp_path):
    import threading

    from bench.modules.base import BenchmarkCancelled

    clock = _FakeTime()
    monkeypatch.setattr(sweep_mod, "time", clock)
    _install_canary(monkeypatch, [0.3, 0.3, 0.3, 9.0])
    p = _auto_params(search_max_concurrency=2, level_settle_seconds=0.0,
                     level_drain_max_seconds=600.0)
    cancel = threading.Event()
    inner = _fake_probe(p, max_passing=999)

    def cancelling(endpoint, params, level, duration, probe_dir, tick,
                   cancel_event, log_tag="", requests_per_concurrency=None,
                   random_seed=None, **_phase):
        cancel.set()  # cancelled while the next level's drain is waiting
        return inner(endpoint, params, level, duration, probe_dir, tick,
                     cancel_event, log_tag,
                     requests_per_concurrency=requests_per_concurrency,
                     random_seed=random_seed, **_phase)

    monkeypatch.setattr(sweep_mod, "_run_probe", cancelling)
    cls = get_module("perf_guidellm_sweep")
    with pytest.raises(BenchmarkCancelled):
        cls().run(_ENDPOINT, p, str(tmp_path), cancel_event=cancel)


# ---------------------------------------------------------------------------
# Count-mode warmup/cooldown fractions
# ---------------------------------------------------------------------------

def test_probe_and_confirm_use_their_own_phase_fractions(monkeypatch, tmp_path):
    p = _auto_params(search_max_concurrency=2,
                     probe_warmup_fraction=0.2, probe_cooldown_fraction=0.1,
                     warmup_fraction=0.1, cooldown_fraction=0.05)
    seen = []

    def fake(endpoint, params, level, duration, probe_dir, tick, cancel_event,
             log_tag="", requests_per_concurrency=None, random_seed=None,
             warmup_fraction=None, cooldown_fraction=None):
        seen.append((duration, warmup_fraction, cooldown_fraction))
        return _ProbeOutcome(level=level, duration_seconds=duration,
                             level_metrics={"output_tps_mean": 50.0 * level,
                                            "input_tps_mean": 1.0, "total_tps_mean": 1.0,
                                            "ttft_p99_ms": 100.0,
                                            "request_output_tps": 50.0, "uptime": 1.0})

    monkeypatch.setattr(sweep_mod, "_run_probe", fake)
    get_module("perf_guidellm_sweep")().run(_ENDPOINT, p, str(tmp_path))
    assert seen == [(5.0, 0.2, 0.1), (5.0, 0.2, 0.1), (6.0, 0.1, 0.05)]


def test_phase_fractions_must_leave_something_to_measure():
    with pytest.raises(ValueError, match="probe_warmup_fraction"):
        _auto_params(probe_warmup_fraction=0.6, probe_cooldown_fraction=0.4)
    with pytest.raises(ValueError, match="warmup_fraction"):
        PerfGuidellmSweepParams(warmup_fraction=0.9, cooldown_fraction=0.1)


def test_realworld_count_mode_uses_given_fractions(tmp_path):
    from bench.tests.b_realworld import RealWorldTest

    test = RealWorldTest("http://localhost:9", "m", "", str(tmp_path),
                         rates=[4.0], requests_per_concurrency=10,
                         warmup_fraction=0.2, cooldown_fraction=0.1)
    cfg = test._make_config()
    assert cfg.warmup == {"percent": 0.2, "mode": "requests"}
    assert cfg.cooldown == {"percent": 0.1, "mode": "requests"}
    assert cfg.max_requests_per_level == [40]

    default = RealWorldTest("http://localhost:9", "m", "", str(tmp_path),
                            rates=[4.0], requests_per_concurrency=10)._make_config()
    assert default.warmup == {"percent": 0.1, "mode": "requests"}
    assert default.cooldown == {"percent": 0.05, "mode": "requests"}


def test_truncation_check_follows_the_phase_fractions(caplog):
    from bench.modules.perf_guidellm_sweep import _warn_if_truncated

    # 32 x 10 = 320 requested; with 0.2 + 0.1 discarded, ~224 are expected.
    # 200 measured is within 75% of that — not truncated. Under the default
    # 0.85 split it would also pass, so use a value that separates the two.
    metrics = {"measured_requests": 180.0, "duration_seconds": 95.0}
    with caplog.at_level("WARNING"):
        _warn_if_truncated("sweep", "probe", 32, 100.0, 10, metrics,
                           wall_seconds=99.0, measured_fraction=0.7)
    assert not [r for r in caplog.records if "stopped it early" in r.message]
    caplog.clear()
    with caplog.at_level("WARNING"):
        _warn_if_truncated("sweep", "probe", 32, 100.0, 10, metrics,
                           wall_seconds=99.0)  # default 0.85 → expects 272
    assert [r for r in caplog.records if "stopped it early" in r.message]
