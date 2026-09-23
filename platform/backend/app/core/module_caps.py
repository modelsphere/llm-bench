"""Live module capability lookups.

Read straight from the bench MODULE_REGISTRY (deployed code), NOT the DB row, so
they always reflect what the worker will actually do — even if the startup
module-sync hasn't run or a stale row lingers. The import is deferred because the
bench layer is only on sys.path after main.py inserts BENCH_MODULES_PATH.
"""
from __future__ import annotations


def supports_concurrency_override(module_name: str) -> bool:
    """True if `module_name`'s module accepts the submission-level concurrency
    override (its ParamsSchema declares a `concurrency` field). Single source of
    truth is TestModule.accepts_concurrency_override, the same check the worker
    gates on — so a newly-added module with a `concurrency` param is surfaced on
    the submit page with no other edits. Unknown/orphan names report False."""
    from bench.modules import MODULE_REGISTRY

    cls = MODULE_REGISTRY.get(module_name)
    if cls is None:
        return False
    try:
        return bool(cls.accepts_concurrency_override())
    except Exception:
        return False


def supports_dataset_feed(module_name: str) -> bool:
    """True if `module_name`'s module can replay a rolling dataset profile
    instead of a fixed file. Read live from the registry for the same reason as
    above: it must reflect the code the worker will actually run."""
    from bench.modules import MODULE_REGISTRY

    cls = MODULE_REGISTRY.get(module_name)
    if cls is None:
        return False
    try:
        return bool(cls.accepts_dataset_feed())
    except Exception:
        return False


def resolve_concurrency_override(extra_params: dict | None, module_name: str | None) -> int | None:
    """The concurrency override that applies to `module_name` under a
    submission's extra_params bag: the module's entry in the per-module map when
    present, else the global `concurrency_override`. None = no override applies.

    The two mechanisms are mutually exclusive at submit time (schema-enforced),
    so "map entry wins" is a defensive order, not a merge policy. Single source
    of truth for worker apply/record and the API's per-run report — keep them
    resolving identically."""
    bag = extra_params if isinstance(extra_params, dict) else {}
    per_module = bag.get("module_concurrency_overrides")
    if isinstance(per_module, dict) and module_name in per_module:
        try:
            return int(per_module[module_name])
        except (TypeError, ValueError):
            return None
    value = bag.get("concurrency_override")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _original_from_params(params_json: dict | None, pname: str | None) -> int | None:
    """The module's own configured value for `pname`, read from the run's params
    snapshot (historical, so it survives later benchmark edits). None if absent."""
    if not pname or not isinstance(params_json, dict) or params_json.get(pname) is None:
        return None
    try:
        return int(params_json[pname])
    except (TypeError, ValueError):
        return None


def _report_for_class(
    cls, params_json: dict | None, override: int | None, applied: int | None = None
) -> dict | None:
    """Pure core of `concurrency_override_report`, split out so it can be tested
    against a module class directly (no registry). `cls` may be None (module no
    longer registered). Returns None when the override didn't apply to this run.

    `applied` is the concurrency the worker RECORDED for this run (models.
    SubmissionRun.applied_concurrency). When present it is authoritative for
    `effective` — the value can't be recomputed once the module's cap or the
    global limit moves. Only when it is absent (a run predating that recording, or
    one still in flight) is `effective` reconstructed against the CURRENT caps."""
    pname = cls.concurrency_param_name() if cls is not None else None

    if applied is not None:
        # Ground truth: the value the worker actually ran with. No dependence on
        # the current module cap or global limit.
        effective = int(applied)
        requested = int(override) if override is not None else effective
        return {
            "param": pname or "concurrency",
            "original": _original_from_params(params_json, pname),
            "requested": requested,
            "effective": effective,
        }

    # Fallback reconstruction (no recorded value): mirror the worker's two-stage
    # clamp against CURRENT caps. Best-effort for legacy/in-flight runs.
    if override is None or pname is None:
        return None
    from app.core.config import clamp_concurrency_override
    from app.queue.jobs import _clamp_to_field_range

    requested = clamp_concurrency_override(int(override))
    effective = _clamp_to_field_range(cls.ParamsSchema.model_fields[pname], requested)
    return {
        "param": pname,
        "original": _original_from_params(params_json, pname),
        "requested": requested,
        "effective": effective,
    }


def concurrency_override_report(
    module_name: str, params_json: dict | None, override: int | None,
    applied: int | None = None,
) -> dict | None:
    """Describe how a submission-level `concurrency_override` affected one run, or
    None when it didn't apply to this module.

    Returns ``{"param", "original", "requested", "effective"}``:
      - ``param``:     the concurrency-equivalent field the override wrote (usually
                       ``concurrency``; opencompass uses ``max_workers``)
      - ``original``:  the module's own configured value for that param (from the
                       run's params snapshot), or None if it wasn't set
      - ``requested``: the submission-level override the user asked for
      - ``effective``: the value the worker actually ran with — the recorded
                       ``applied`` when available, else reconstructed against the
                       current caps for legacy/in-flight runs

    `applied` is SubmissionRun.applied_concurrency (recorded at run time). It is
    the authoritative source once the module's cap or the global limit can drift
    from run time. None result means the run wasn't affected by any override."""
    from bench.modules import MODULE_REGISTRY

    cls = MODULE_REGISTRY.get(module_name)
    # A recorded `applied` proves the override ran, so still report it even if the
    # module was since removed from the registry (cls is None handled downstream).
    if cls is None and applied is None:
        return None
    try:
        return _report_for_class(cls, params_json, override, applied)
    except Exception:
        return None
