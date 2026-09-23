"""Pydantic schemas for the rolling replay-dataset feed (admin API)."""
from __future__ import annotations

import re
from datetime import datetime

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

# Mirrors bench.replay_test.dataset_feed._SAFE_PROFILE. A profile name indexes a
# directory on the shared datasets volume, so it is validated at the API edge as
# well as in the resolver — the two must agree, and the API is where a human
# gets a usable error message.
_SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

SourceType = Literal["bodylog_files", "victorialogs"]


def source_errors(source_type: str, source_url: str, extra_logsql: str | None) -> list[str]:
    """What is wrong with this source, in words an admin can act on. Shared by
    create (all fields at once) and update (checked against the merged row, so
    changing only the type still re-checks the URL)."""
    out: list[str] = []
    url = (source_url or "").strip()
    if source_type == "bodylog_files":
        path = url[len("file://"):] if url.startswith("file://") else url
        if not path.startswith("/"):
            out.append("a bodylog_files source is an absolute directory, e.g. file:///data/bodylog")
        if (extra_logsql or "").strip():
            out.append("extra_logsql is a VictoriaLogs filter; clear it or use the victorialogs source")
    elif source_type == "victorialogs":
        if not url.startswith(("http://", "https://")):
            out.append("a victorialogs source is the store's http(s) base URL")
    return out


class ProfileBase(BaseModel):
    display_name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    enabled: bool = True

    # source — see app.datasets.sources
    source_type: SourceType = "bodylog_files"
    source_url: str = Field(min_length=1, max_length=500)
    models: list[str] = Field(default_factory=list)
    statuses: list[str] = Field(default_factory=lambda: ["200"])
    forwarded_to: list[str] = Field(default_factory=list)
    uris: list[str] = Field(default_factory=lambda: ["/v1/chat/completions"])
    exclude_truncated: bool = True
    extra_logsql: str | None = Field(default=None, max_length=2000)

    # window + sampling
    window_hours: int = Field(default=24, ge=1, le=24 * 30)
    window_timezone: str = Field(default="UTC", max_length=64)
    subwindow_minutes: int = Field(default=60, ge=1, le=24 * 60)
    sample_size: int = Field(default=2000, ge=1, le=1_000_000)
    oversample_factor: float = Field(default=3.0, ge=1.0, le=100.0)
    # Per-slice keep cap = ceil(sample_size / n_slices) * max_carry_multiple. For
    # traffic concentrated in a few slices this product, not sample_size, is the
    # real ceiling on the sample — raise it for a bursty model.
    max_carry_multiple: int = Field(default=4, ge=1, le=10_000)
    max_bytes: int = Field(default=8 * 1024 ** 3, ge=1024 ** 2)

    # sanitation
    clean: bool = True
    max_model_len: int = Field(default=262144, ge=2048)
    keep_response_body: bool = False
    compress: bool = True
    header_denylist: list[str] | None = None

    # publication gates
    min_records: int = Field(default=100, ge=1)
    min_buckets: int = Field(default=1, ge=0, le=7)

    # schedule + retention
    # 0 = MANUAL ONLY: never collected on a schedule, only when something
    # calls the build endpoint. Distinct from `enabled=false`, which turns the
    # profile off entirely (including manual builds).
    schedule_interval_hours: float = Field(default=24.0, ge=0.0, le=24 * 30)
    schedule_anchor_hour: int | None = Field(default=None, ge=0, le=23)
    max_age_hours: float = Field(default=48.0, ge=0.0)
    keep_builds: int = Field(default=7, ge=1, le=100)
    min_retain_hours: float = Field(default=8.0, ge=0.0)

    @field_validator("models", "forwarded_to", "uris", "statuses", "header_denylist")
    @classmethod
    def _clean_list(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        return [item.strip() for item in v if isinstance(item, str) and item.strip()]

    @field_validator("source_url")
    @classmethod
    def _strip_url(cls, v: str) -> str:
        return v.strip().rstrip("/")

    @model_validator(mode="after")
    def _source_is_usable(self):
        errors = source_errors(self.source_type, self.source_url, self.extra_logsql)
        if errors:
            raise ValueError("; ".join(errors))
        return self


class ProfileCreate(ProfileBase):
    name: str = Field(min_length=1, max_length=64)

    @field_validator("name")
    @classmethod
    def _safe_name(cls, v: str) -> str:
        v = v.strip().lower()
        if not _SAFE_NAME.match(v):
            raise ValueError(
                "name must be a lowercase slug: letters, digits, '-' and '_', "
                "starting with a letter or digit (it names a directory on the "
                "datasets volume)"
            )
        return v


class ProfileUpdate(BaseModel):
    """Every field optional — a PATCH-style update. `name` is immutable: it is
    the directory the published builds already live in, so renaming would orphan
    them and silently break any benchmark referencing the old name."""

    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    enabled: bool | None = None
    source_type: SourceType | None = None
    source_url: str | None = Field(default=None, min_length=1, max_length=500)
    models: list[str] | None = None
    statuses: list[str] | None = None
    forwarded_to: list[str] | None = None
    uris: list[str] | None = None
    exclude_truncated: bool | None = None
    extra_logsql: str | None = Field(default=None, max_length=2000)
    window_hours: int | None = Field(default=None, ge=1, le=24 * 30)
    window_timezone: str | None = Field(default=None, max_length=64)
    subwindow_minutes: int | None = Field(default=None, ge=1, le=24 * 60)
    sample_size: int | None = Field(default=None, ge=1, le=1_000_000)
    oversample_factor: float | None = Field(default=None, ge=1.0, le=100.0)
    max_carry_multiple: int | None = Field(default=None, ge=1, le=10_000)
    max_bytes: int | None = Field(default=None, ge=1024 ** 2)
    clean: bool | None = None
    max_model_len: int | None = Field(default=None, ge=2048)
    keep_response_body: bool | None = None
    compress: bool | None = None
    header_denylist: list[str] | None = None
    min_records: int | None = Field(default=None, ge=1)
    min_buckets: int | None = Field(default=None, ge=0, le=7)
    schedule_interval_hours: float | None = Field(default=None, ge=0.0, le=24 * 30)
    schedule_anchor_hour: int | None = Field(default=None, ge=0, le=23)
    max_age_hours: float | None = Field(default=None, ge=0.0)
    keep_builds: int | None = Field(default=None, ge=1, le=100)
    min_retain_hours: float | None = Field(default=None, ge=0.0)


class BuildResponse(BaseModel):
    id: int
    build_id: str | None = None
    status: str
    trigger: str
    records: int | None = None
    size_bytes: int | None = None
    sha256: str | None = None
    path: str | None = None
    window_start: datetime | None = None
    window_end: datetime | None = None
    stats_json: dict | None = None
    progress: str | None = None
    error: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    # Whether the build's bytes are STILL on the datasets volume. A build row is
    # kept forever; the file it describes is pruned on the profile's retention
    # policy, so `status == ready` alone does not mean it can be downloaded.
    # None = not computed by this endpoint.
    downloadable: bool | None = None


class CurrentBuild(BaseModel):
    """What the resolver on the datasets volume currently points at.

    Read from the pointer file, NOT from the build history, because the pointer
    is what a benchmark will actually replay. A disagreement between the two
    (pointer missing while history says READY) is itself the interesting signal.
    """

    build_id: str
    path: str
    records: int
    bytes: int
    sha256: str
    built_at: datetime
    age_hours: float
    stale: bool
    window_start: datetime | None = None
    window_end: datetime | None = None
    buckets: dict | None = None
    # What the dataset actually contains: models, backends, prompt/completion
    # length spread, cache hit rate. Tallied at build time (see _Summary), so
    # nothing has to re-read a multi-hundred-MB dataset to answer "what is
    # this?". None for builds made before it was recorded.
    summary: dict | None = None


class ProfileResponse(ProfileBase):
    id: int
    name: str
    created_at: datetime
    updated_at: datetime | None = None
    current: CurrentBuild | None = None
    last_build: BuildResponse | None = None


class ProfileListResponse(BaseModel):
    profiles: list[ProfileResponse]
    # Whether the platform can reach a collector at all. False means builds can
    # be scheduled but nothing will run them.
    collector_configured: bool = False


class BuildListResponse(BaseModel):
    builds: list[BuildResponse]


class ProbeRequest(BaseModel):
    """Dry-run a profile's filters over a short window, without building."""

    hours: float = Field(default=1.0, ge=0.1, le=24.0)


class ProbeResponse(BaseModel):
    matched: int
    query: str
    sample_fields: list[str] = Field(default_factory=list)
    sample_prompt_tokens: int | None = None
    error: str | None = None


class TriggerResponse(BaseModel):
    build_row_id: int
    queue_depth: int | None = None


class FeedHealthEntry(BaseModel):
    name: str
    display_name: str
    enabled: bool
    ok: bool
    age_hours: float | None = None
    max_age_hours: float
    records: int | None = None
    build_id: str | None = None
    detail: str | None = None


class FeedHealthResponse(BaseModel):
    profiles: list[FeedHealthEntry]


class FreezeBuildRequest(BaseModel):
    """Promote one build into a permanent, retention-immune dataset.

    `name` becomes a filename on the datasets volume, so it is held to the same
    safe slug as a profile — validated here so a human gets the error in the
    editor, not from the collector."""

    name: str = Field(min_length=1, max_length=64)

    @field_validator("name")
    @classmethod
    def _safe_name(cls, v: str) -> str:
        v = v.strip().lower()
        if not _SAFE_NAME.match(v):
            raise ValueError(
                "name must be a lowercase slug: letters, digits, '-' and '_', "
                "starting with a letter or digit (it names a file on the "
                "datasets volume)"
            )
        return v


class FrozenDatasetResponse(BaseModel):
    """A permanent dataset frozen out of the rolling feed. The `path` is what an
    admin pastes into a `fixed`-source benchmark's dataset_path."""

    name: str
    path: str
    bytes: int
    records: int = 0
    sha256: str = ""
    frozen_at: datetime | None = None
    source_profile: str = ""
    source_build_id: str = ""
    window_start: datetime | None = None
    window_end: datetime | None = None
    frozen_by: str = ""
    # Benchmarks whose dataset_path currently points at this frozen file — the
    # UI warns before deleting one, and delete refuses unless forced.
    used_by: list[str] = Field(default_factory=list)


class FrozenListResponse(BaseModel):
    frozen: list[FrozenDatasetResponse]
    # False when no collector is reachable — freezing/deleting a build needs the
    # collector (it is the writer of the datasets volume); listing does not.
    collector_configured: bool = False
