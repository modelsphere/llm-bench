"""Unit tests for _merge_extra_display_configs (app.queue.jobs). Pure-logic:
no DB / Redis required. Guards the two invariants: modules can only ADD
display-only rows (never score/redline), and admin-configured keys win."""
from __future__ import annotations

from app.queue.jobs import _merge_extra_display_configs
from bench.modules.base import ModuleResult


def test_appends_display_rows_for_new_keys():
    configs = [{"key": "output_tps", "role": "score", "weight": 1.0}]
    result = ModuleResult(
        metrics={},
        extra_display_configs=[
            {"key": "output_tps_mean_c8", "role": "display"},
            {"key": "uptime_c8", "role": "display"},
        ],
    )
    merged = _merge_extra_display_configs(configs, result)
    assert {c["key"] for c in merged} == {"output_tps", "output_tps_mean_c8", "uptime_c8"}
    # original snapshot list is not mutated
    assert len(configs) == 1


def test_drops_non_display_roles():
    """A module must not be able to inject score or redline rows."""
    result = ModuleResult(
        metrics={},
        extra_display_configs=[
            {"key": "sneaky", "role": "score", "weight": 100.0},
            {"key": "sneaky2", "role": "redline", "max_val": 0.0},
            {"key": "ok", "role": "display"},
        ],
    )
    merged = _merge_extra_display_configs([], result)
    assert [c["key"] for c in merged] == ["ok"]


def test_existing_keys_are_not_overridden():
    configs = [{"key": "uptime_c8", "role": "redline", "min_val": 0.9}]
    result = ModuleResult(
        metrics={},
        extra_display_configs=[{"key": "uptime_c8", "role": "display"}],
    )
    merged = _merge_extra_display_configs(configs, result)
    assert merged == configs


def test_no_extras_returns_input_unchanged():
    configs = [{"key": "a", "role": "display"}]
    assert _merge_extra_display_configs(configs, ModuleResult(metrics={})) is configs
    # results lacking the attribute entirely (older ModuleResult-like objects)
    class _Legacy:
        pass
    assert _merge_extra_display_configs(configs, _Legacy()) is configs
