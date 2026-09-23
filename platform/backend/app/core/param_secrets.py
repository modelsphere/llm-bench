"""Redaction of secret values inside module params_json blobs.

Module params are a free-form JSON bag defined by each bench module's
ParamsSchema, and some modules carry credentials in them (e.g. the
opencompass and hallucination modules' ``judge_api_key`` — the API key for
an external LLM-judge endpoint, set by the admin who configures the
benchmark). Those blobs are returned by several APIs whose audience is much
wider than the admin who typed the key:

  * GET /benchmarks and /benchmarks/{slug} — any logged-in user
  * GET /submissions/{id} — any logged-in user for benchmark submissions
    (params_json is snapshotted onto each run)

Secret fields are detected by name (there is no schema-level secret marker),
so any module gains protection automatically as long as its credential
params end in one of the recognised suffixes. Empty values pass through
unredacted — "blank = self-judge" stays visible in the UI.
"""
from __future__ import annotations

import re
from typing import Any

# Suffix match on the param name. Anchored at the end so e.g. `api_key`,
# `judge_api_key`, `probe_api_token` all match but `token_budget` doesn't.
_SECRET_NAME_RE = re.compile(
    r"(api_key|apikey|api_token|access_token|secret|password)$", re.IGNORECASE
)

# Distinctive sentinel: obviously-masked in any UI that displays params, and
# never a plausible real credential.
REDACTED = "••••••••"


def is_secret_param(name: str) -> bool:
    return bool(_SECRET_NAME_RE.search(name))


def redact_params(params: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return a copy of ``params`` with non-empty secret string values masked.

    Shallow on purpose: module params are a flat name→value bag (nested
    structures are dataset selections and the like, never credentials).
    """
    if not params:
        return params
    return {
        k: (REDACTED if is_secret_param(k) and isinstance(v, str) and v else v)
        for k, v in params.items()
    }
