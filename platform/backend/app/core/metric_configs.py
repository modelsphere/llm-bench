"""Helpers for resolving per-run metric_configs.

A benchmark's `metric_configs_json` is snapshotted onto each SubmissionRun
when it completes, so scoring/redline rules stay frozen even if module
defaults change later. The downside: when a module gains new *display-only*
metrics, existing benchmarks (and already-completed runs) don't show them.

`merge_display_defaults` fixes that for `role="display"` rows only — score
and redline rules are left untouched, so historical scoring is preserved.
"""
from __future__ import annotations

from typing import Any


def merge_display_defaults(module_name: str, configs: list | None) -> list:
    """Return a copy of `configs` extended with any module-default display
    configs whose `key` is not already present. Score/redline rules are
    never injected — only display rows.

    Safe to call with missing modules or malformed inputs; returns the
    original list (or []) on any error.
    """
    base = list(configs or [])
    try:
        from bench.modules import MODULE_REGISTRY
        from dataclasses import asdict

        cls = MODULE_REGISTRY.get(module_name)
        if cls is None:
            return base

        existing_display_keys = {
            c.get("key") for c in base
            if isinstance(c, dict) and c.get("role") == "display"
        }
        for mc in cls.default_metric_configs:
            d: dict[str, Any] = asdict(mc)
            if d.get("role") != "display":
                continue
            if d.get("key") in existing_display_keys:
                continue
            base.append(d)
        return base
    except Exception:
        return base
