"""Pydantic schemas for benchmark and submission endpoints."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

# Length caps for the submitter-authored description fields, counted in
# characters (code points) regardless of script — 100 CJK chars = 100 ASCII
# chars. The summary is a one-liner surfaced on the leaderboard; the detail is
# markdown rendered on the submission detail page. Keep in sync with the
# frontend's maxlength attrs.
DESCRIPTION_SUMMARY_MAX = 100
DESCRIPTION_DETAIL_MAX = 5000

# Cap for the optional `source_url` — a link back to the system that produced
# the submission (e.g. an LLM AutoTune run page). Rendered as a link on the
# submission page, so it is scheme-checked at submit time rather than sanitised
# at every render site.
SOURCE_URL_MAX = 500
_SOURCE_URL_SCHEMES = ("http://", "https://")

# Display grouping for the benchmarks list. Each tag is a "/"-separated path,
# top-down, of 1 to GROUP_TAG_MAX_DEPTH segments: "Top", "Top/Mid" or
# "Top/Mid/Low". A deeper level cannot exist without the ones above it —
# that is what a path encodes, so "/Mid" (empty top) is rejected rather than
# silently repaired. The backend keeps this a flat list of strings; the tree
# is assembled in the frontend. Keep in sync with the frontend's groupTags util.
GROUP_TAG_MAX_DEPTH = 3
GROUP_TAG_SEGMENT_MAX = 60
GROUP_TAG_MAX_COUNT = 20
GROUP_TAG_SEPARATOR = "/"


def normalize_group_tags(tags: object) -> list[str]:
    """Canonicalise a list of group-tag paths, raising ValueError on bad input.

    Whitespace around segments is trimmed, exact duplicates (after trimming)
    are dropped keeping first occurrence, and blank entries are ignored so a
    half-filled row in the editor never fails the whole save. Anything that
    survives must be a well-formed path: 1-3 non-empty segments, none over
    GROUP_TAG_SEGMENT_MAX chars.
    """
    if tags is None:
        return []
    if not isinstance(tags, (list, tuple)):
        raise ValueError("group_tags must be a list of strings")
    out: list[str] = []
    seen: set[str] = set()
    for raw in tags:
        if not isinstance(raw, str):
            raise ValueError("group_tags entries must be strings")
        if not raw.strip():
            continue
        segments = [seg.strip() for seg in raw.split(GROUP_TAG_SEPARATOR)]
        # A trailing separator ("Top/") is a harmless editor artefact; an
        # empty segment anywhere else breaks the top-down rule.
        while segments and segments[-1] == "":
            segments.pop()
        if not segments:
            continue
        if any(seg == "" for seg in segments):
            raise ValueError(
                f"group tag {raw!r}: every level above a used level must be filled "
                f"(no empty segments)"
            )
        if len(segments) > GROUP_TAG_MAX_DEPTH:
            raise ValueError(
                f"group tag {raw!r}: at most {GROUP_TAG_MAX_DEPTH} levels are allowed"
            )
        for seg in segments:
            if len(seg) > GROUP_TAG_SEGMENT_MAX:
                raise ValueError(
                    f"group tag {raw!r}: level {seg[:20]!r}… is longer than "
                    f"{GROUP_TAG_SEGMENT_MAX} characters"
                )
        path = GROUP_TAG_SEPARATOR.join(segments)
        if path in seen:
            continue
        seen.add(path)
        out.append(path)
    if len(out) > GROUP_TAG_MAX_COUNT:
        raise ValueError(f"at most {GROUP_TAG_MAX_COUNT} group tags per benchmark")
    return out


def _normalize_api_key(v: object) -> object:
    """Trim an API key, so whitespace-only input becomes the keyless "".

    A key of " " is truthy, so it survives every ``if api_key`` guard and is
    emitted as the header value ``Bearer  ``. h11 rejects a header value with
    trailing whitespace, so EVERY request fails at construction — before a byte
    leaves the platform — and the run completes with zero successful requests
    and a score of 0. Normalising to "" routes it down the keyless path (no
    Authorization header at all) instead.
    """
    if isinstance(v, str):
        return v.strip()
    return v


class BenchmarkModuleCreate(BaseModel):
    module_name: str
    params_json: dict[str, Any]
    metric_configs: list[dict[str, Any]] = []
    weight: float = Field(ge=0, le=1)
    order_index: int = Field(ge=0)
    # When true, skip this module if the immediately-preceding module in run
    # order failed / was skipped / breached a redline (the skip cascades).
    skip_if_prev_failed: bool = False


class BenchmarkCreate(BaseModel):
    slug: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9-]+$")
    name: str
    description: str = ""
    version: str = "1"
    status: str = "draft"
    # Display grouping — see normalize_group_tags. Flat list of "Top/Mid/Low".
    group_tags: list[str] = []
    modules: list[BenchmarkModuleCreate]

    @field_validator("group_tags", mode="before")
    @classmethod
    def _normalize_group_tags(cls, v: object) -> list[str]:
        return normalize_group_tags(v)


class BenchmarkUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    version: str | None = None
    status: str | None = None  # draft | active | archived
    # None = "leave as-is"; [] clears every group tag.
    group_tags: list[str] | None = None
    modules: list[BenchmarkModuleCreate] | None = None

    @field_validator("group_tags", mode="before")
    @classmethod
    def _normalize_group_tags(cls, v: object) -> list[str] | None:
        if v is None:
            return None
        return normalize_group_tags(v)


class BenchmarkResponse(BaseModel):
    id: int
    slug: str
    name: str
    description: str
    version: str
    status: str
    config_hash: str | None
    is_locked: bool
    group_tags: list[str] = []
    created_by_user_id: int
    created_at: datetime
    modules: list[dict[str, Any]]


class BenchmarkListResponse(BaseModel):
    benchmarks: list[BenchmarkResponse]


class BenchmarkGroupTagAssignment(BaseModel):
    """The full tag list a single benchmark should end up with."""

    benchmark_id: int
    group_tags: list[str] = []

    @field_validator("group_tags", mode="before")
    @classmethod
    def _normalize_group_tags(cls, v: object) -> list[str]:
        return normalize_group_tags(v)


class BenchmarkGroupTagsBulkUpdate(BaseModel):
    """Re-tag several benchmarks at once (admin Groups tab).

    Each assignment REPLACES that benchmark's tags — the client edits a draft
    of the whole grouping and sends only the rows it changed, so a partial
    payload must never be read as "add these". There is no group entity, so
    renaming a group is exactly this: rewrite the path on every benchmark that
    carried it, in one transaction, or not at all.
    """

    assignments: list[BenchmarkGroupTagAssignment] = []

    @model_validator(mode="after")
    def _reject_duplicate_ids(self) -> "BenchmarkGroupTagsBulkUpdate":
        ids = [a.benchmark_id for a in self.assignments]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            # Two assignments for one benchmark would silently resolve to
            # whichever came last; that is a client bug, not an intent.
            raise ValueError(f"duplicate benchmark_id in assignments: {dupes}")
        return self


class BenchmarkGroupTagsBulkResponse(BaseModel):
    updated: int


# ---------------------------------------------------------------------------
# Submission schemas
# ---------------------------------------------------------------------------


class SubmissionExtraParams(BaseModel):
    """Optional, extensible bag of submission-level knobs. Add future
    submission-time options here as new fields — they ride through under
    `extra_params` with no API or DB schema churn for callers."""
    # Override every module's concurrency param with this value (one value for
    # all modules). NULL/omitted = each module keeps its own configured
    # concurrency. Clamped to [1, MAX_ALLOWED_CONCURRENCY] on submit, then to
    # each module's own range when applied by the worker.
    concurrency_override: int | None = None
    # Per-module alternative to the global override: module_name -> concurrency.
    # Modules absent from the map keep their own configured concurrency. Values
    # get the same two-stage clamp as the global override. Mutually exclusive
    # with `concurrency_override` — a submission picks one mechanism or neither.
    module_concurrency_overrides: dict[str, int] | None = None

    @model_validator(mode="after")
    def _one_override_mechanism(self) -> "SubmissionExtraParams":
        if self.concurrency_override is not None and self.module_concurrency_overrides:
            raise ValueError(
                "concurrency_override and module_concurrency_overrides are "
                "mutually exclusive — set one or the other, not both."
            )
        return self


class SubmissionCreate(BaseModel):
    endpoint_url: str
    model: str
    # Optional — raw endpoints without auth are a supported target. Encrypted at
    # rest either way; "" means "send no Authorization header", matching what the
    # benchmark itself does.
    api_key: str = ""
    # Display name to list this submission under, when the submitter wants one
    # that is not their account name.
    contributor: str | None = None
    extra_params: SubmissionExtraParams | None = None
    # Optional "Hardware" section — all nullable, omitted => NULL. Supplying it
    # is what makes the card-normalized throughput metrics comparable across
    # differently-sized deployments (app/core/card_normalize.py).
    cards_per_machine: int | None = Field(default=None, ge=1)
    machine_count: int | None = Field(default=None, ge=1)
    card_type: str | None = None
    # Optional description of the service under test and the optimizations
    # behind this run. Summary: one line, shown on the leaderboard. Detail:
    # markdown, rendered on the submission detail page.
    description_summary: str | None = Field(default=None, max_length=DESCRIPTION_SUMMARY_MAX)
    description_detail: str | None = Field(default=None, max_length=DESCRIPTION_DETAIL_MAX)
    # Optional link back to the system that produced this submission (an LLM
    # AutoTune run, a CI job, a wiki page — whatever the submitter wants).
    # Treated as OPAQUE: never parsed, rewritten, or assumed to have any
    # particular shape, so it stays useful to any submitter.
    source_url: str | None = Field(default=None, max_length=SOURCE_URL_MAX)

    @field_validator("description_summary", "description_detail", "source_url", mode="before")
    @classmethod
    def _blank_description_to_none(cls, v: object) -> object:
        # Trim whitespace and store whitespace-only input as NULL, so the
        # leaderboard/detail views can key display off "is None".
        if isinstance(v, str):
            v = v.strip()
            return v or None
        return v

    @field_validator("source_url", mode="after")
    @classmethod
    def _source_url_scheme(cls, v: str | None) -> str | None:
        # The value ends up rendered as an href, so refuse anything that is not
        # plainly an http(s) URL (`javascript:`, `data:`, a bare path) at the
        # door rather than storing it and hoping every render site sanitises.
        # Beyond the scheme we do not look at it — see the field comment.
        if v is not None and not v.lower().startswith(_SOURCE_URL_SCHEMES):
            raise ValueError("source_url must start with http:// or https://")
        return v

    @field_validator("api_key", mode="before")
    @classmethod
    def _trim_api_key(cls, v: object) -> object:
        return _normalize_api_key(v)


class PreflightRequest(BaseModel):
    """Inputs for a pre-submit endpoint check. Same triple as a submission;
    the api_key is used only for the live probe and is never stored."""
    endpoint_url: str
    model: str
    api_key: str = ""  # optional — some endpoints are keyless

    @field_validator("api_key", mode="before")
    @classmethod
    def _trim_api_key(cls, v: object) -> object:
        # Same normalisation as SubmissionCreate, so the probe tests exactly the
        # key the benchmark would later send.
        return _normalize_api_key(v)


class JudgePreflightRequest(BaseModel):
    """Inputs for a judge-endpoint check (the LLM grader some modules use,
    e.g. opencompass SimpleQA / hallucination). Same live-probe-only contract
    as PreflightRequest: the api_key is never stored."""
    endpoint_url: str
    model: str
    api_key: str = ""  # optional — some endpoints are keyless
    # The module's judge_max_tokens — probing with the real budget is the point
    # (a reasoning judge that can't finish its grade within it must fail here).
    max_tokens: int = Field(default=256, ge=1)

    @field_validator("api_key", mode="before")
    @classmethod
    def _trim_api_key(cls, v: object) -> object:
        return _normalize_api_key(v)


class PreflightCheck(BaseModel):
    name: str
    status: str  # pass | fail | warn | skip
    detail: str


class PreflightResult(BaseModel):
    ok: bool  # True iff no check failed (warnings are advisory)
    latency_ms: float | None = None
    endpoint_tested: str | None = None
    checks: list[PreflightCheck]


class QuotaStatus(BaseModel):
    """Caller's active-submission quota (GET /submissions/quota). The submit
    pages use it to warn and disable the submit button before hitting the 429."""
    limit: int          # configured MAX_ACTIVE_SUBMISSIONS_PER_USER
    active: int         # caller's QUEUED + RUNNING submissions right now
    exempt: bool        # admins (or a disabled cap) — never blocked
    active_ids: list[int]


class SubmissionResponse(BaseModel):
    id: int
    user_id: int
    # Populated only by admin listings (the owner's username) — None elsewhere.
    username: str | None = None
    benchmark_id: int | None
    benchmark_slug: str | None = None
    module_name: str | None
    endpoint_url: str
    endpoint_model: str
    status: str
    score_total: float | None
    passed: bool | None
    error: str | None
    benchmark_config_hash: str | None = None
    contributor: str | None = None
    extra_params: dict | None = None
    cards_per_machine: int | None = None
    machine_count: int | None = None
    card_type: str | None = None
    description_summary: str | None = None
    description_detail: str | None = None
    source_url: str | None = None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class PaginatedSubmissions(BaseModel):
    items: list[SubmissionResponse]
    total: int
    limit: int
    offset: int


class ConcurrencyOverrideInfo(BaseModel):
    """Per-run record of the submission-level concurrency override taking effect.
    Present on a run only when the override applied to that module (the module has
    a concurrency-equivalent param). `effective` is the value the run actually
    used — `requested` clamped to the module's own range."""
    param: str            # the field the override wrote (e.g. "concurrency", "max_workers")
    original: int | None = None  # the module's own configured value, if any
    requested: int        # submission-level override after the global clamp
    effective: int        # value after this module's own range clamp — what ran


class SubmissionRunResponse(BaseModel):
    id: int
    module_name: str
    params_json: dict
    status: str
    score: float | None
    passed: bool | None
    metrics_json: dict | None
    metric_configs_json: list | None
    error: str | None
    started_at: datetime | None
    finished_at: datetime | None
    artifact_path: str | None
    # Which module of the benchmark produced this run. A benchmark may include
    # the same module twice (two sweeps with different shapes); this is the
    # stable way for a client to tell them apart, rather than their order.
    # None for ad-hoc module submissions.
    benchmark_module_id: int | None = None
    # Set only when the submission-level concurrency override affected this run.
    concurrency_override: ConcurrencyOverrideInfo | None = None


class SubmissionDetailResponse(SubmissionResponse):
    runs: list[SubmissionRunResponse]
    benchmark_slug: str | None = None
    benchmark_name: str | None = None
