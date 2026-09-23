"""Unit tests for card-normalized TPM (app.core.card_normalize) and its
worker-side injection (app.queue.jobs._add_card_normalized_tpm). Pure-logic:
no DB / Redis required."""
from __future__ import annotations

from types import SimpleNamespace

from app.core.card_normalize import card_normalized_tpm, total_card_count
from app.queue.jobs import _add_card_normalized_tpm, _merge_extra_display_configs
from bench.modules.base import ModuleResult, TestModule

BASELINE = 8


# --- total_card_count ----------------------------------------------------------

def test_total_card_count_multiplies():
    assert total_card_count(8, 2) == 16
    assert total_card_count(4, 1) == 4


def test_total_card_count_incomplete_hardware_is_none():
    assert total_card_count(None, 2) is None
    assert total_card_count(8, None) is None
    assert total_card_count(None, None) is None
    assert total_card_count(0, 3) is None


# --- card_normalized_tpm: replay-shaped metrics (native TPM keys) --------------

REPLAY_METRICS = {
    "input_tpm": 60_000.0,
    "output_tpm": 6_000.0,
    "cached_tpm": 30_000.0,
    "uncached_input_tpm": 30_000.0,  # deliberately not normalized
}


def test_replay_four_cards_doubles():
    out = card_normalized_tpm(REPLAY_METRICS, 4, BASELINE)
    assert out == {
        "input_tpm_card_norm": 120_000.0,
        "output_tpm_card_norm": 12_000.0,
        "cached_tpm_card_norm": 60_000.0,
        "total_tpm_card_norm": 132_000.0,  # (input + output) doubled
    }


def test_replay_sixteen_cards_halves():
    out = card_normalized_tpm(REPLAY_METRICS, 16, BASELINE)
    assert out["output_tpm_card_norm"] == 3_000.0
    assert out["total_tpm_card_norm"] == 33_000.0


def test_replay_baseline_cards_identity():
    out = card_normalized_tpm(REPLAY_METRICS, BASELINE, BASELINE)
    assert out["input_tpm_card_norm"] == REPLAY_METRICS["input_tpm"]


def test_unknown_card_count_assumes_baseline():
    assert card_normalized_tpm(REPLAY_METRICS, None, BASELINE) == card_normalized_tpm(
        REPLAY_METRICS, BASELINE, BASELINE
    )


def test_non_default_baseline():
    # A module declaring a 16-card baseline doubles a 8-card endpoint's numbers.
    out = card_normalized_tpm(REPLAY_METRICS, 8, 16)
    assert out["input_tpm_card_norm"] == 120_000.0


# --- card_normalized_tpm: guidellm-shaped metrics (TPS keys, x60) --------------

GUIDELLM_METRICS = {"input_tps": 1000.0, "output_tps": 100.0, "total_tps_mean": 1100.0}


def test_guidellm_tps_scaled_to_tpm():
    out = card_normalized_tpm(GUIDELLM_METRICS, 8, BASELINE)
    assert out == {
        "input_tpm_card_norm": 60_000.0,
        "output_tpm_card_norm": 6_000.0,
        "total_tpm_card_norm": 66_000.0,
    }
    assert "cached_tpm_card_norm" not in out  # no cached source for guidellm


def test_guidellm_total_falls_back_to_input_plus_output():
    metrics = {"input_tps": 1000.0, "output_tps": 100.0}  # aio path: no total_tps_mean
    out = card_normalized_tpm(metrics, 4, BASELINE)
    assert out["total_tpm_card_norm"] == (60_000.0 + 6_000.0) * 2


def test_native_tpm_preferred_over_tps_fallback():
    # replay has BOTH input_tpm (aggregate) and per-request input_tps-style
    # rates; the aggregate TPM must win. (input_tps here is a decoy.)
    metrics = {"input_tpm": 60_000.0, "input_tps": 999_999.0, "output_tpm": 6_000.0}
    out = card_normalized_tpm(metrics, 8, BASELINE)
    assert out["input_tpm_card_norm"] == 60_000.0


# --- card_normalized_tpm: degenerate inputs ------------------------------------

def test_no_throughput_metrics_yields_empty():
    assert card_normalized_tpm({"ttft_p99_ms": 100.0}, 4, BASELINE) == {}
    assert card_normalized_tpm({}, 4, BASELINE) == {}
    assert card_normalized_tpm("not-a-dict", 4, BASELINE) == {}  # type: ignore[arg-type]


def test_degenerate_baseline_yields_empty():
    assert card_normalized_tpm(REPLAY_METRICS, 4, 0) == {}
    assert card_normalized_tpm(REPLAY_METRICS, 4, None) == {}  # type: ignore[arg-type]


def test_non_finite_and_garbage_values_skipped():
    out = card_normalized_tpm(
        {"input_tpm": float("nan"), "output_tpm": "oops", "cached_tpm": 100.0}, 8, BASELINE
    )
    assert out == {"cached_tpm_card_norm": 100.0}


# --- worker-side injection -----------------------------------------------------
# Subclass the REAL TestModule so the tests exercise the actual opt-in mechanism
# (card_norm_baseline class attr). Never instantiated — `run` stays abstract.

class _NormalizingModule(TestModule):
    card_norm_baseline = BASELINE


class _PlainModule(TestModule):
    pass  # keeps the TestModule default: no baseline, no normalization


def _submission(cards=None, machines=None):
    return SimpleNamespace(id=1, cards_per_machine=cards, machine_count=machines)


def test_worker_injects_metrics_and_display_configs():
    result = ModuleResult(metrics=dict(REPLAY_METRICS))
    _add_card_normalized_tpm(result, _submission(2, 2), _NormalizingModule, "replay")
    assert result.metrics["input_tpm_card_norm"] == 120_000.0
    keys = {c["key"] for c in result.extra_display_configs}
    assert keys == {
        "input_tpm_card_norm", "output_tpm_card_norm",
        "cached_tpm_card_norm", "total_tpm_card_norm",
    }
    assert all(c["role"] == "display" for c in result.extra_display_configs)
    # And the merge helper accepts them into a run's metric-config snapshot.
    merged = _merge_extra_display_configs([{"key": "uptime", "role": "redline"}], result)
    assert {c["key"] for c in merged} == keys | {"uptime"}


def test_worker_skips_modules_without_baseline():
    result = ModuleResult(metrics=dict(REPLAY_METRICS))
    _add_card_normalized_tpm(result, _submission(4, 1), _PlainModule, "opencompass")
    assert "input_tpm_card_norm" not in result.metrics
    assert result.extra_display_configs == []


def test_worker_skips_failed_results():
    result = ModuleResult(metrics=dict(REPLAY_METRICS), error="boom")
    _add_card_normalized_tpm(result, _submission(4, 1), _NormalizingModule, "replay")
    assert "input_tpm_card_norm" not in result.metrics


def test_worker_no_hardware_assumes_baseline():
    result = ModuleResult(metrics={"input_tps": 1000.0, "output_tps": 100.0})
    _add_card_normalized_tpm(result, _submission(), _NormalizingModule, "perf_guidellm")
    assert result.metrics["input_tpm_card_norm"] == 60_000.0  # factor 1


def test_worker_never_overwrites_existing_keys():
    result = ModuleResult(metrics={"input_tpm": 60_000.0, "input_tpm_card_norm": 1.0})
    _add_card_normalized_tpm(result, _submission(4, 1), _NormalizingModule, "replay")
    assert result.metrics["input_tpm_card_norm"] == 1.0
    assert "input_tpm_card_norm" not in {c["key"] for c in result.extra_display_configs}


def test_real_perf_modules_declare_baseline():
    # The three perf modules the platform promises normalized TPM for.
    from bench.modules.perf_guidellm import PerfGuidellmModule
    from bench.modules.perf_guidellm_sweep import PerfGuidellmSweepModule
    from bench.modules.replay import ReplayModule

    for cls in (PerfGuidellmModule, PerfGuidellmSweepModule, ReplayModule):
        assert cls.card_norm_baseline == 8
    # And their descriptors document the normalized keys for the UI/API.
    descriptor_names = {d.name for d in ReplayModule.metrics_descriptors}
    assert {"input_tpm_card_norm", "output_tpm_card_norm",
            "cached_tpm_card_norm", "total_tpm_card_norm"} <= descriptor_names
