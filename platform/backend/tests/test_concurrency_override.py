"""Unit tests for the submission-level concurrency override applied by the
worker (app.queue.jobs). Pure-logic: no DB / Redis / bench modules required —
uses a tiny in-test Pydantic schema so the override math is verified in
isolation. Guards the 'must actually take effect' guarantee."""
from __future__ import annotations

from types import SimpleNamespace

from pydantic import BaseModel, Field

from app.core.config import clamp_concurrency_override, settings
from app.core.module_caps import _report_for_class, resolve_concurrency_override
from app.queue.jobs import _apply_concurrency_override, _clamp_to_field_range
from app.schemas.benchmarks import SubmissionExtraParams
from bench.modules.base import TestModule


# Subclass the REAL TestModule so the tests exercise the actual
# concurrency_param_name / accepts_concurrency_override alias logic (no drift from
# a hand-rolled copy). These are never instantiated — only classmethods are used —
# so leaving the abstract `run` unimplemented is fine.


class _SchemaWithConcurrency(BaseModel):
    concurrency: int = Field(default=5, ge=1, le=128)
    other: str = "keep-me"


class _ModuleWithConcurrency(TestModule):
    ParamsSchema = _SchemaWithConcurrency


class _SchemaHighCap(BaseModel):
    # le above the global MAX_ALLOWED_CONCURRENCY (128) so the global cap, not the
    # module's own range, is the binding constraint.
    concurrency: int = Field(default=5, ge=1, le=1024)


class _ModuleHighCap(TestModule):
    ParamsSchema = _SchemaHighCap


class _SchemaMaxWorkers(BaseModel):
    # opencompass-style: the concurrency-equivalent param is `max_workers`.
    max_workers: int = Field(default=16, ge=1, le=1024)


class _ModuleMaxWorkers(TestModule):
    ParamsSchema = _SchemaMaxWorkers


class _SchemaNoConcurrency(BaseModel):
    foo: int = 1


class _ModuleNoConcurrency(TestModule):
    ParamsSchema = _SchemaNoConcurrency


def _sub(override):
    # Mirrors a Submission row: the override lives in the extra_params JSON bag.
    bag = None if override is None else {"concurrency_override": override}
    return SimpleNamespace(id=1, extra_params=bag)


# --- _clamp_to_field_range ----------------------------------------------------

def test_clamp_respects_le():
    field = _SchemaWithConcurrency.model_fields["concurrency"]
    assert _clamp_to_field_range(field, 1000) == 128


def test_clamp_respects_ge():
    field = _SchemaWithConcurrency.model_fields["concurrency"]
    assert _clamp_to_field_range(field, 0) == 1


def test_clamp_passes_value_in_range():
    field = _SchemaWithConcurrency.model_fields["concurrency"]
    assert _clamp_to_field_range(field, 42) == 42


# --- _apply_concurrency_override ---------------------------------------------

def test_override_takes_effect():
    params = _SchemaWithConcurrency(concurrency=5)
    out = _apply_concurrency_override(_ModuleWithConcurrency, params, _sub(40), "m")
    assert out.concurrency == 40          # the value the module will actually read
    assert out.other == "keep-me"         # other params preserved


def test_override_clamped_to_module_max():
    params = _SchemaWithConcurrency(concurrency=5)
    out = _apply_concurrency_override(_ModuleWithConcurrency, params, _sub(1000), "m")
    assert out.concurrency == 128         # clamped, not failed


def test_override_none_is_noop_same_object():
    params = _SchemaWithConcurrency(concurrency=7)
    out = _apply_concurrency_override(_ModuleWithConcurrency, params, _sub(None), "m")
    assert out is params and out.concurrency == 7


def test_override_ignored_when_module_has_no_concurrency():
    params = _SchemaNoConcurrency(foo=3)
    out = _apply_concurrency_override(_ModuleNoConcurrency, params, _sub(40), "m")
    assert out is params and out.foo == 3  # untouched; no crash


def test_override_targets_max_workers_alias():
    # opencompass-style module: the override must write `max_workers`.
    assert _ModuleMaxWorkers.concurrency_param_name() == "max_workers"
    assert _ModuleMaxWorkers.accepts_concurrency_override() is True
    params = _SchemaMaxWorkers(max_workers=16)
    out = _apply_concurrency_override(_ModuleMaxWorkers, params, _sub(40), "opencompass")
    assert out.max_workers == 40


def test_max_workers_alias_clamped_to_global_max():
    params = _SchemaMaxWorkers(max_workers=16)
    out = _apply_concurrency_override(_ModuleMaxWorkers, params, _sub(1000), "opencompass")
    assert out.max_workers == settings.MAX_ALLOWED_CONCURRENCY == 128


def test_low_override_not_clamped_up():
    params = _SchemaWithConcurrency(concurrency=100)
    out = _apply_concurrency_override(_ModuleWithConcurrency, params, _sub(2), "m")
    assert out.concurrency == 2


# --- global MAX_ALLOWED_CONCURRENCY clamp ------------------------------------

def test_clamp_helper_bounds_and_passthrough():
    assert clamp_concurrency_override(None) is None
    assert clamp_concurrency_override(-5) == 1
    assert clamp_concurrency_override(0) == 1
    assert clamp_concurrency_override(50) == 50
    assert clamp_concurrency_override(10_000) == settings.MAX_ALLOWED_CONCURRENCY


def test_clamp_helper_reads_settings(monkeypatch):
    monkeypatch.setattr(settings, "MAX_ALLOWED_CONCURRENCY", 16)
    assert clamp_concurrency_override(50) == 16
    assert clamp_concurrency_override(8) == 8


def test_global_max_binds_before_module_cap():
    # Module allows up to 1024, but the global cap (128) is lower, so an override
    # of 500 lands at 128 — proving the global guard runs before the module clamp.
    params = _SchemaHighCap(concurrency=5)
    out = _apply_concurrency_override(_ModuleHighCap, params, _sub(500), "m")
    assert out.concurrency == settings.MAX_ALLOWED_CONCURRENCY == 128


def test_global_max_applied_in_worker_respects_monkeypatched_setting(monkeypatch):
    monkeypatch.setattr(settings, "MAX_ALLOWED_CONCURRENCY", 32)
    params = _SchemaHighCap(concurrency=5)
    out = _apply_concurrency_override(_ModuleHighCap, params, _sub(500), "m")
    assert out.concurrency == 32


# --- concurrency_override_report (submission-detail reporting) ----------------
# _report_for_class is the pure core the API uses to tell the detail page which
# runs were affected and the value they actually used. The `effective` it reports
# must match what _apply_concurrency_override would have produced.

def test_report_none_when_no_override():
    assert _report_for_class(_ModuleWithConcurrency, {"concurrency": 5}, None) is None


def test_report_none_when_module_has_no_concurrency():
    assert _report_for_class(_ModuleNoConcurrency, {"foo": 3}, 40) is None


def test_report_records_original_and_effective():
    rep = _report_for_class(_ModuleWithConcurrency, {"concurrency": 5}, 40)
    assert rep == {"param": "concurrency", "original": 5, "requested": 40, "effective": 40}


def test_report_effective_clamped_to_module_max():
    # module le is 128; requesting 1000 lands at 128 — exactly what the worker runs.
    rep = _report_for_class(_ModuleWithConcurrency, {"concurrency": 5}, 1000)
    assert rep["requested"] == settings.MAX_ALLOWED_CONCURRENCY == 128
    assert rep["effective"] == 128


def test_report_targets_max_workers_alias():
    rep = _report_for_class(_ModuleMaxWorkers, {"max_workers": 16}, 40)
    assert rep == {"param": "max_workers", "original": 16, "requested": 40, "effective": 40}


def test_report_original_none_when_param_absent():
    # params snapshot didn't set the concurrency field explicitly.
    rep = _report_for_class(_ModuleWithConcurrency, {}, 40)
    assert rep["original"] is None and rep["effective"] == 40


def test_report_effective_matches_apply_when_module_cap_binds():
    # Module allows up to 1024, global cap is 128 -> both the applied param and the
    # reported `effective` must agree at 128.
    applied = _apply_concurrency_override(
        _ModuleHighCap, _SchemaHighCap(concurrency=5), _sub(500), "m"
    )
    rep = _report_for_class(_ModuleHighCap, {"concurrency": 5}, 500)
    assert rep["effective"] == applied.concurrency == settings.MAX_ALLOWED_CONCURRENCY


# --- recorded (authoritative) value path -------------------------------------
# When the worker recorded applied_concurrency, that value is authoritative and
# must NOT be re-derived from the module's current cap — the whole point of
# recording it is to survive a later cap/limit change.

def test_report_uses_recorded_value_verbatim():
    rep = _report_for_class(_ModuleWithConcurrency, {"concurrency": 5}, 40, applied=40)
    assert rep == {"param": "concurrency", "original": 5, "requested": 40, "effective": 40}


def test_recorded_value_survives_module_cap_shrinking_after_run():
    # The run executed at 40. The module's cap is LATER tightened to le=8. A
    # reconstruction would wrongly report 8; the recorded value must still say 40.
    class _Shrunk(BaseModel):
        concurrency: int = Field(default=5, ge=1, le=8)

    class _ModuleShrunk(TestModule):
        ParamsSchema = _Shrunk

    reconstructed = _report_for_class(_ModuleShrunk, {"concurrency": 5}, 40)
    assert reconstructed["effective"] == 8          # what live caps would (wrongly) show
    recorded = _report_for_class(_ModuleShrunk, {"concurrency": 5}, 40, applied=40)
    assert recorded["effective"] == 40              # ground truth preserved


def test_recorded_value_survives_global_cap_change(monkeypatch):
    # Global cap lowered to 16 after the run. Reconstruction would clamp to 16;
    # the recorded 40 must be reported unchanged.
    monkeypatch.setattr(settings, "MAX_ALLOWED_CONCURRENCY", 16)
    recorded = _report_for_class(_ModuleWithConcurrency, {"concurrency": 5}, 40, applied=40)
    assert recorded["requested"] == 40 and recorded["effective"] == 40


def test_report_reported_when_module_unregistered_but_recorded():
    # Module since removed from the registry (cls=None), but the run recorded it —
    # history is still reported (best-effort param/original).
    rep = _report_for_class(None, {"concurrency": 5}, 40, applied=40)
    assert rep["effective"] == 40 and rep["requested"] == 40
    assert rep["param"] == "concurrency" and rep["original"] is None


# --- per-module overrides (module_concurrency_overrides) -----------------------
# The per-module map is the mutually-exclusive alternative to the global
# integer. resolve_concurrency_override is the single resolution point shared by
# the worker (apply/record) and the API (per-run report).


def _sub_map(per_module, global_override=None):
    bag = {}
    if global_override is not None:
        bag["concurrency_override"] = global_override
    if per_module is not None:
        bag["module_concurrency_overrides"] = per_module
    return SimpleNamespace(id=1, extra_params=bag or None)


def test_resolve_per_module_entry_wins():
    bag = {"module_concurrency_overrides": {"m1": 7, "m2": 9}}
    assert resolve_concurrency_override(bag, "m1") == 7
    assert resolve_concurrency_override(bag, "m2") == 9


def test_resolve_module_absent_from_map_gets_no_override():
    bag = {"module_concurrency_overrides": {"m1": 7}}
    assert resolve_concurrency_override(bag, "other") is None


def test_resolve_falls_back_to_global():
    assert resolve_concurrency_override({"concurrency_override": 12}, "m") == 12


def test_resolve_none_bags():
    assert resolve_concurrency_override(None, "m") is None
    assert resolve_concurrency_override({}, "m") is None


def test_resolve_garbage_values_are_none():
    assert resolve_concurrency_override({"concurrency_override": "abc"}, "m") is None
    assert resolve_concurrency_override(
        {"module_concurrency_overrides": {"m": "abc"}}, "m") is None


def test_apply_uses_per_module_entry():
    params = _SchemaWithConcurrency(concurrency=5)
    out = _apply_concurrency_override(
        _ModuleWithConcurrency, params, _sub_map({"m": 40}), "m")
    assert out.concurrency == 40


def test_apply_per_module_ignores_other_modules():
    params = _SchemaWithConcurrency(concurrency=5)
    out = _apply_concurrency_override(
        _ModuleWithConcurrency, params, _sub_map({"other_module": 40}), "m")
    assert out is params and out.concurrency == 5


def test_apply_per_module_value_clamped_like_global():
    params = _SchemaWithConcurrency(concurrency=5)
    out = _apply_concurrency_override(
        _ModuleWithConcurrency, params, _sub_map({"m": 10_000}), "m")
    assert out.concurrency == settings.MAX_ALLOWED_CONCURRENCY == 128


def test_extra_params_schema_rejects_both_mechanisms():
    import pytest
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        SubmissionExtraParams(
            concurrency_override=10, module_concurrency_overrides={"m": 5})


def test_extra_params_schema_accepts_each_alone():
    assert SubmissionExtraParams(concurrency_override=10).concurrency_override == 10
    only_map = SubmissionExtraParams(module_concurrency_overrides={"m": 5})
    assert only_map.module_concurrency_overrides == {"m": 5}
    # An EMPTY map is not "using the mechanism" — must not trip the exclusivity guard.
    both_empty = SubmissionExtraParams(
        concurrency_override=10, module_concurrency_overrides={})
    assert both_empty.concurrency_override == 10
