"""Card-normalized TPM: throughput scaled to a common card baseline.

Endpoints are served by differently-sized deployments, so raw tokens-per-minute
numbers aren't comparable across submissions. The display-only `*_tpm_card_norm`
metrics rescale TPM "as if" the endpoint ran on `baseline` cards:

    normalized = raw_tpm * baseline / total_card_count

e.g. with the usual baseline of 8, a 4-card endpoint's TPM doubles and a 16-card
endpoint's halves. The baseline is declared per module
(TestModule.card_norm_baseline — modules without one opt out entirely); the card
count comes from the submission's hardware section (cards_per_machine x
machine_count). When the submitter didn't fill that in, the endpoint is assumed
to already be at the baseline (factor 1) so the metric still renders.

Pure functions — the worker (app.queue.jobs) computes the values after a module
run and injects them through ModuleResult.extra_display_configs, so they can
never influence scoring or redlines.
"""
from __future__ import annotations

import math

# normalized key -> (native TPM source key, TPS fallback key scaled x60).
# guidellm/sweep only emit TPS; replay emits TPM natively. `None` means
# the metric family has no source of that kind.
_NORMALIZED_SOURCES: dict[str, tuple[str | None, str | None]] = {
    "input_tpm_card_norm": ("input_tpm", "input_tps"),
    "output_tpm_card_norm": ("output_tpm", "output_tps"),
    "cached_tpm_card_norm": ("cached_tpm", None),
    "total_tpm_card_norm": ("total_tpm", "total_tps_mean"),
}


def total_card_count(cards_per_machine: int | None, machine_count: int | None) -> int | None:
    """Total cards serving the endpoint, or None when the hardware section is
    incomplete (both fields are needed for a meaningful total)."""
    try:
        cards, machines = int(cards_per_machine or 0), int(machine_count or 0)
    except (TypeError, ValueError):
        return None
    total = cards * machines
    return total if total > 0 else None


def _as_finite_float(value: object) -> float | None:
    try:
        f = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def card_normalized_tpm(
    metrics: dict, card_count: int | None, baseline: int
) -> dict[str, float]:
    """Compute the card-normalized TPM metrics available from `metrics`, scaled
    to `baseline` cards (the module's declared card_norm_baseline).

    Returns only the keys whose source metric exists (e.g. cached_tpm_card_norm
    only for modules that report cached tokens). Total falls back to input +
    output when no native total-throughput metric is present. An empty dict
    means the module produced no recognizable throughput metrics."""
    if not isinstance(metrics, dict) or not baseline or baseline <= 0:
        return {}
    cards = card_count if card_count and card_count > 0 else baseline
    factor = baseline / cards

    base: dict[str, float | None] = {}
    for norm_key, (tpm_key, tps_key) in _NORMALIZED_SOURCES.items():
        value = _as_finite_float(metrics.get(tpm_key)) if tpm_key else None
        if value is None and tps_key:
            tps = _as_finite_float(metrics.get(tps_key))
            value = tps * 60.0 if tps is not None else None
        base[norm_key] = value

    input_norm, output_norm = base["input_tpm_card_norm"], base["output_tpm_card_norm"]
    if base["total_tpm_card_norm"] is None and input_norm is not None and output_norm is not None:
        base["total_tpm_card_norm"] = input_norm + output_norm

    return {key: value * factor for key, value in base.items() if value is not None}
