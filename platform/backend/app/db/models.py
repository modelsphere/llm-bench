"""SQLAlchemy 2.x models for the LLM benchmark platform."""
from __future__ import annotations

import enum
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.core.config import settings

if TYPE_CHECKING:
    pass


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class UserRole(str, enum.Enum):
    # Highest first for readability. Do NOT rely on declaration order for
    # ranking — privilege comparisons use _ROLE_RANK below.
    SUPER_ADMIN = "super_admin"
    ADMIN = "admin"
    # An automation account — another platform (e.g. LLM AutoTune) that submits
    # benchmarks and manages the benchmarks IT created. Created by the deployment
    # (seed.py, SERVICE_USERNAME + SERVICE_API_KEY), never granted from the UI.
    SERVICE = "service"
    USER = "user"


# Privilege ranking: higher number = more privilege. super_admin is strictly
# above admin, which is strictly above service, which is above user. A service
# account is NOT an admin: it may manage only benchmarks it created, and never
# users, card types, or collection profiles.
_ROLE_RANK: dict["UserRole", int] = {
    UserRole.USER: 0,
    UserRole.SERVICE: 1,
    UserRole.ADMIN: 2,
    UserRole.SUPER_ADMIN: 3,
}


def is_admin_or_above(role: UserRole) -> bool:
    """True if `role` has at least admin privilege (admin or super_admin)."""
    return _ROLE_RANK[role] >= _ROLE_RANK[UserRole.ADMIN]


def is_service_or_above(role: UserRole) -> bool:
    """True for a service account or any admin."""
    return _ROLE_RANK[role] >= _ROLE_RANK[UserRole.SERVICE]


class BenchmarkStatus(str, enum.Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    ARCHIVED = "archived"


class SubmissionStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELED = "canceled"


class ModuleRunStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    # Not executed because an earlier module in the chain failed (see
    # BenchmarkModule.skip_if_prev_failed). Distinct from FAILED: nothing ran.
    SKIPPED = "skipped"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    username: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, values_callable=lambda cls: [e.value for e in cls]),
        default=UserRole.USER,
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    # Set to now() whenever the password is reset via the forgot-password flow.
    # JWTs minted before this instant are rejected (see resolve_user_from_token),
    # so a reset logs out any session holding the old password. NULL = never
    # reset → no session check. timezone=True to accept the tz-aware now(UTC).
    password_changed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # reverse relations
    benchmarks: Mapped[list["Benchmark"]] = relationship(back_populates="created_by", lazy="raise")
    submissions: Mapped[list["Submission"]] = relationship(back_populates="user", lazy="raise")


class ApiKey(Base):
    """A long-lived personal API key, used in place of the expiring JWT.

    Only the SHA-256 hash of the key is stored; the plaintext is shown to the
    user exactly once at creation. Presented via `Authorization: Bearer <key>`
    just like a JWT — get_current_user distinguishes them by the key's prefix
    (see app/core/auth.API_KEY_PREFIX). A key grants the full privileges of its
    owner.
    """
    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    # First few chars of the key (incl. prefix), shown so users can tell keys
    # apart in the UI. Not secret.
    key_prefix: Mapped[str] = mapped_column(String(16))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)  # sha256 hex
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    # timezone=True to match the timestamptz DB column (migration 006) AND to
    # accept the tz-aware datetime.now(UTC) we write on each use — a naive
    # DateTime column makes asyncpg reject the tz-aware value (DataError).
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CardType(Base):
    """An admin-managed GPU card type (e.g. A100, H100), offered as a choice in
    the submission "Hardware" section.

    A tiny, admin-editable lookup list — mutated only via the admin card-types
    CRUD API, read by any authenticated user to populate the submit-form
    dropdown. `display_order` gives admins stable control over the dropdown
    order; ties break by name.
    """
    __tablename__ = "card_types"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    display_order: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class PasswordResetRequest(Base):
    """A user-initiated forgot-password request, fulfilled by a super_admin.

    Lifecycle (status is derived from the timestamps, no enum column):
      - created           -> pending
      - + approved_at     -> approved (a single-use token now exists)
      - + used_at         -> used (password was reset)
      - + canceled_at     -> canceled (super_admin rejected it)
      - past expires_at   -> expired

    Only the SHA-256 hash of the token is stored (mirrors ApiKey); the plaintext
    is shown to the approving super_admin exactly once. See app/api/auth.py for
    the flow and app/core/auth.generate_reset_token for the token format.
    """
    __tablename__ = "password_reset_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # Set on approval — who approved, when, and when the minted token dies.
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approved_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Single-use / rejected markers.
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Token exists only after approval. sha256 hex, unique for the lookup.
    token_hash: Mapped[str | None] = mapped_column(String(64), unique=True, index=True, nullable=True)
    token_prefix: Mapped[str | None] = mapped_column(String(16), nullable=True)  # display only

    def status(self, now: datetime) -> str:
        """Derive a human status. `now` must be tz-aware (datetime.now(UTC))."""
        if self.used_at is not None:
            return "used"
        if self.canceled_at is not None:
            return "canceled"
        if self.approved_at is None:
            return "pending"
        if self.expires_at is not None and now >= self.expires_at:
            return "expired"
        return "approved"


class TestModule(Base):
    """
    Module descriptor synced from bench.modules.MODULE_REGISTRY on startup.

    Read-heavy table — written only by the startup sync hook.
    """
    __tablename__ = "test_modules"

    name: Mapped[str] = mapped_column(String(100), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text)
    params_schema_json: Mapped[dict] = mapped_column(JSONB)           # pydantic JSON schema
    default_params_json: Mapped[dict] = mapped_column(JSONB)           # defaults from model
    # MetricDescriptor list + score_formula, synced from module code on startup
    metrics_schema_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    version: Mapped[str] = mapped_column(String(50), default="1.0.0")

    # reverse relation
    benchmark_modules: Mapped[list["BenchmarkModule"]] = relationship(back_populates="module", lazy="raise")


class Benchmark(Base):
    __tablename__ = "benchmarks"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, default="")
    version: Mapped[str] = mapped_column(String(50), default="1")
    status: Mapped[BenchmarkStatus] = mapped_column(
        Enum(BenchmarkStatus, values_callable=lambda cls: [e.value for e in cls]),
        default=BenchmarkStatus.DRAFT,
    )
    config_hash: Mapped[str | None] = mapped_column(String(16), nullable=True)
    is_locked: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    # Display grouping for the benchmarks list. A flat list of tag paths, each
    # "Top/Mid/Low" with 1-3 segments (see app.schemas.benchmarks
    # normalize_group_tags for the format). The backend stores and validates
    # the strings; the tree is built entirely in the frontend. A benchmark may
    # carry several paths and is shown under each. Presentation only — not in
    # the config hash, and editable while the benchmark is locked.
    group_tags: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    created_by_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    created_by: Mapped["User"] = relationship(back_populates="benchmarks", lazy="raise")
    modules: Mapped[list["BenchmarkModule"]] = relationship(
        back_populates="benchmark",
        order_by=lambda: BenchmarkModule.order_index,
        lazy="raise",
        cascade="all, delete-orphan",
    )
    submissions: Mapped[list["Submission"]] = relationship(back_populates="benchmark", lazy="raise")


class BenchmarkModule(Base):
    """
    Join table: a module instance within a benchmark, with locked params, weight,
    and admin-configured metric evaluation rules.
    """
    __tablename__ = "benchmark_modules"

    id: Mapped[int] = mapped_column(primary_key=True)
    benchmark_id: Mapped[int] = mapped_column(ForeignKey("benchmarks.id"), index=True)
    module_name: Mapped[str] = mapped_column(ForeignKey("test_modules.name"), index=True)
    params_json: Mapped[dict] = mapped_column(JSONB)                   # admin-locked params
    metric_configs_json: Mapped[list] = mapped_column(JSONB, default=list)  # MetricConfig rules
    weight: Mapped[Decimal] = mapped_column(Numeric(5, 4))             # e.g. 0.25
    order_index: Mapped[int] = mapped_column(default=0)
    # When true, this module is SKIPPED (not run) if the immediately-preceding
    # module in run order failed, was skipped, or breached a redline. The skip
    # cascades, so chaining the flag down a series of increasingly hard tests
    # short-circuits the whole tail once one fails — saving run time.
    skip_if_prev_failed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), default=False
    )

    benchmark: Mapped["Benchmark"] = relationship(back_populates="modules", lazy="raise")
    module: Mapped["TestModule"] = relationship(back_populates="benchmark_modules", lazy="raise")

    __table_args__ = (
        Index("ix_benchmark_module_benchmark_order", "benchmark_id", "order_index"),
    )


class Submission(Base):
    """
    A user submission of either a full benchmark or a single module.

    Exactly one of benchmark_id or module_name is set (enforced by application logic;
    a DB CHECK is added below).
    """
    __tablename__ = "submissions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    benchmark_id: Mapped[int | None] = mapped_column(ForeignKey("benchmarks.id"), nullable=True, index=True)
    module_name: Mapped[str | None] = mapped_column(ForeignKey("test_modules.name"), nullable=True)
    endpoint_url: Mapped[str] = mapped_column(String(500))
    endpoint_model: Mapped[str] = mapped_column(String(255))
    endpoint_api_key_enc: Mapped[str] = mapped_column(Text)           # Fernet-encrypted
    # Display name the submission is listed under, when the submitter wants one
    # that is not their account name (e.g. a team, or the automation that ran it).
    contributor: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[SubmissionStatus] = mapped_column(
        Enum(SubmissionStatus, values_callable=lambda cls: [e.value for e in cls]),
        default=SubmissionStatus.QUEUED,
    )
    score_total: Mapped[float | None] = mapped_column(Float, nullable=True)
    passed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # How many times a worker has actually started this submission. Bounds the
    # total attempts across BOTH retry paths — Dramatiq's own `retries` counter
    # is not enough, because a broker redelivery after a killed worker arrives
    # with retries=0 and so never consumes SUBMISSION_MAX_RETRIES, letting a
    # submission resurrect indefinitely. This lives on the row, so it survives
    # redelivery, worker death and pod replacement. See jobs.run_submission.
    run_attempts: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    benchmark_config_hash: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Optional, free-form bag of submission-level knobs (JSONB so new keys need no
    # migration). NULL/{} = nothing set. Currently holds at most:
    #   {"concurrency_override": <int>}  — override every module's concurrency param.
    # Future submission-time options go here as additional keys.
    extra_params: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Optional hardware the submission was run on. All nullable — omitted on the
    # form => NULL. This is what makes the card-normalized throughput metrics
    # meaningful (app/core/card_normalize.py): without it an endpoint is assumed
    # to be at the baseline card count. card_type is a free string (the value of
    # a CardType.name at submit time), not a FK, so editing the card_types list
    # never rewrites historical submissions.
    cards_per_machine: Mapped[int | None] = mapped_column(Integer, nullable=True)
    machine_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    card_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # Optional submitter-authored description of the served LLM service and the
    # optimizations behind this run. `description_summary` is a one-liner shown
    # on the leaderboard; `description_detail` is longer markdown rendered on
    # the submission detail page. Length caps enforced in the API schema.
    description_summary: Mapped[str | None] = mapped_column(String(100), nullable=True)
    description_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Optional link back to the system that produced this submission — e.g. the
    # LLM AutoTune run page holding the engine config and the search behind
    # these numbers. Opaque to us: validated at submit time as an http(s) URL
    # under 500 chars and otherwise never parsed. Shown on the submission page.
    source_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)
    # Full worker-process log for this submission, gzipped utf-8 text. Written
    # incrementally during the run (timer + per-module) and on completion by the
    # worker; downloadable via GET /submissions/{id}/logs/download. Deferred so
    # list/detail queries never pull the blob.
    worker_log_gz: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True, deferred=True)
    worker_log_truncated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    user: Mapped["User"] = relationship(back_populates="submissions", lazy="raise")
    benchmark: Mapped["Benchmark | None"] = relationship(back_populates="submissions", lazy="raise")
    runs: Mapped[list["SubmissionRun"]] = relationship(
        back_populates="submission",
        order_by=lambda: SubmissionRun.id,
        lazy="raise",
    )

    __table_args__ = (
        CheckConstraint(
            "(benchmark_id IS NOT NULL AND module_name IS NULL) OR "
            "(benchmark_id IS NULL AND module_name IS NOT NULL)",
            name="ck_submission_one_of",
        ),
        # Leaderboard hot path: filter benchmark_id+status, order by score_total
        # (migration 015 creates it; declared here too so models/migrations agree).
        Index("ix_submissions_leaderboard", "benchmark_id", "status", "score_total"),
        # /submissions/me and admin listings sort on created_at.
        Index("ix_submissions_created_at", "created_at"),
    )


class SubmissionRun(Base):
    """
    One module's execution within a submission.

    benchmark_module_id links the run to a specific BenchmarkModule instance,
    allowing a benchmark to contain the same module multiple times with
    different parameters.
    """
    __tablename__ = "submission_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    submission_id: Mapped[int] = mapped_column(ForeignKey("submissions.id"), index=True)
    benchmark_module_id: Mapped[int | None] = mapped_column(
        ForeignKey("benchmark_modules.id"), nullable=True, index=True
    )
    module_name: Mapped[str] = mapped_column(String(100))
    params_json: Mapped[dict] = mapped_column(JSONB)
    status: Mapped[ModuleRunStatus] = mapped_column(
        Enum(ModuleRunStatus, values_callable=lambda cls: [e.value for e in cls]),
        default=ModuleRunStatus.PENDING,
    )
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    passed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    metrics_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    metric_configs_json: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)
    artifact_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # The concurrency this run ACTUALLY executed at when the submission-level
    # concurrency_override applied to it, recorded by the worker at run time.
    # NULL = no override applied (or a run predating this column). Persisting it
    # makes the detail page report the historical value even if the module's cap
    # or the global limit changes later — the value can't be reconstructed then.
    applied_concurrency: Mapped[int | None] = mapped_column(Integer, nullable=True)

    submission: Mapped["Submission"] = relationship(back_populates="runs", lazy="raise")
    benchmark_module: Mapped["BenchmarkModule | None"] = relationship(lazy="raise")


# ---------------------------------------------------------------------------
# Rolling replay datasets ("the feed")
# ---------------------------------------------------------------------------


class DatasetBuildStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    READY = "ready"
    FAILED = "failed"


class ReplayDatasetProfile(Base):
    """An admin-configured rolling replay dataset.

    A profile says *which* production traffic to collect (the log-store filters),
    *how much* (window + sample size), *how* to sanitize it (clean/max_model_len),
    and *how often* to rebuild. The collector service turns each build into a
    published dataset file; `replay` resolves a profile by `name` to
    whichever build is current.

    The profile is deliberately NOT the source of truth for "which build is
    current" — that is the pointer file on the datasets volume (see
    `bench/replay_test/dataset_feed.py`), so a benchmark can still run when the
    database is mid-migration or the collector is down.

    Credentials are never stored here: the log store's URL is configuration, its
    password is an environment variable on the collector pod.
    """
    __tablename__ = "replay_dataset_profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Indexes a directory on the datasets volume, so it is validated as a slug
    # (see dataset_feed.validate_profile) before anything touches the filesystem.
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))

    # --- source ---------------------------------------------------------
    # Where records come from — see app.datasets.sources. `bodylog_files` reads
    # the gateway's bodylog listener files (source_url = file:///dir);
    # `victorialogs` queries a VictoriaLogs store (source_url = http://…).
    source_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="bodylog_files",
        server_default=text("'bodylog_files'"),
    )
    source_url: Mapped[str] = mapped_column(String(500))
    models: Mapped[list] = mapped_column(JSONB, default=list)
    statuses: Mapped[list] = mapped_column(JSONB, default=lambda: ["200"])
    forwarded_to: Mapped[list] = mapped_column(JSONB, default=list)
    uris: Mapped[list] = mapped_column(JSONB, default=lambda: ["/v1/chat/completions"])
    exclude_truncated: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    extra_logsql: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- window + sampling ----------------------------------------------
    window_hours: Mapped[int] = mapped_column(Integer, default=24, server_default=text("24"))
    window_timezone: Mapped[str] = mapped_column(String(64), default="UTC",
                                                 server_default="UTC")
    subwindow_minutes: Mapped[int] = mapped_column(Integer, default=60, server_default=text("60"))
    sample_size: Mapped[int] = mapped_column(Integer, default=2000, server_default=text("2000"))
    oversample_factor: Mapped[float] = mapped_column(Float, default=3.0, server_default=text("3.0"))
    # Each sub-window keeps a base quota of ceil(sample_size / n_slices); when
    # earlier slices are quiet the shortfall carries forward, but a single slice
    # keeps at most base_quota * max_carry_multiple. For traffic concentrated in a
    # few slices (a daily burst), THAT product — not sample_size — is the real
    # ceiling on the sample; raise it (or subwindow_minutes) to capture a bursty
    # model fully. Exposed in the admin editor with the derived cap shown live.
    max_carry_multiple: Mapped[int] = mapped_column(Integer, default=4, server_default=text("4"))
    max_bytes: Mapped[int] = mapped_column(BigInteger, default=8 * 1024 ** 3,
                                           server_default=text(str(8 * 1024 ** 3)))

    # --- sanitation ------------------------------------------------------
    clean: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    max_model_len: Mapped[int] = mapped_column(Integer, default=262144, server_default=text("262144"))
    keep_response_body: Mapped[bool] = mapped_column(Boolean, default=False,
                                                     server_default=text("false"))
    # Store builds gzipped. ~3.6x smaller on this kind of JSON (measured on a
    # real build: 9.60MB -> 2.64MB), decompressed transparently by every reader,
    # so this is safe to flip per profile and existing builds keep working.
    compress: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    header_denylist: Mapped[list | None] = mapped_column(JSONB, nullable=True)

    # --- publication gates -----------------------------------------------
    min_records: Mapped[int] = mapped_column(Integer, default=100, server_default=text("100"))
    min_buckets: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))

    # --- schedule + retention ---------------------------------------------
    schedule_interval_hours: Mapped[float] = mapped_column(Float, default=24.0,
                                                          server_default=text("24.0"))
    schedule_anchor_hour: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Freshness SLA. A submission that resolves a build older than this still
    # runs (by design — availability beats a hard stop here), but the worker logs
    # a warning, the run records `stale: true`, and the admin page flags it.
    max_age_hours: Mapped[float] = mapped_column(Float, default=48.0, server_default=text("48.0"))
    keep_builds: Mapped[int] = mapped_column(Integer, default=7, server_default=text("7"))
    # Floor under retention, sized to outlast the longest single run (replay's
    # own cap is 5h) plus slack. It is deliberately NOT a long window: a run
    # holds a hard link to its dataset for as long as it is reading it, so
    # protecting in-use builds is structural and this floor only covers the
    # fallback where the filesystem refused to link. A large value here is
    # actively dangerous on a fast schedule — it overrides keep_builds, so
    # hourly builds with a 72h floor retain 72 builds, not 7.
    min_retain_hours: Mapped[float] = mapped_column(Float, default=8.0, server_default=text("8.0"))

    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, onupdate=func.now()
    )

    builds: Mapped[list["ReplayDatasetBuild"]] = relationship(
        back_populates="profile", lazy="raise", cascade="all, delete-orphan"
    )


class ReplayDatasetBuild(Base):
    """One collection run of a profile — history, and the collector's claim row.

    `claimed_by` + `heartbeat_at` are the same idiom the benchmark worker uses
    (app/queue/heartbeat.py): a build claimed by a pod that then dies leaves a
    stale heartbeat, and the next collector tick reclaims it. This is used
    instead of a database advisory lock precisely because a lock would mean
    holding a session open for the whole (potentially long) collection — the
    failure mode that blocked a migration and took the site down on 2026-07-23.
    """
    __tablename__ = "replay_dataset_builds"

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("replay_dataset_profiles.id", ondelete="CASCADE"), index=True
    )
    build_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    status: Mapped[DatasetBuildStatus] = mapped_column(
        Enum(DatasetBuildStatus, values_callable=lambda cls: [e.value for e in cls]),
        default=DatasetBuildStatus.PENDING,
        index=True,
    )
    trigger: Mapped[str] = mapped_column(String(20), default="schedule")
    triggered_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )
    claimed_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    window_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    window_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    seed: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    records: Mapped[int | None] = mapped_column(Integer, nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    stats_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    progress: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    profile: Mapped["ReplayDatasetProfile"] = relationship(back_populates="builds", lazy="raise")

    __table_args__ = (
        Index("ix_replay_builds_profile_finished", "profile_id", "finished_at"),
    )


# ---------------------------------------------------------------------------
# Async session factory
# ---------------------------------------------------------------------------

# Keepalive + recycle so pooled connections survive the cluster's L4 idle
# timeout. kube-proxy (IPVS) / conntrack silently drops idle TCP flows
# (commonly after ~900s), leaving connections that Postgres still reports as
# "idle" but that are black-holed in the middle; reusing one then stalls for
# ~13s on TCP retransmit before the stack gives up and reconnects. Two defenses:
#   - server-side TCP keepalives keep the flow's conntrack entry alive so it is
#     never considered idle (probe every 60s, well under the idle timeout);
#   - pool_recycle replaces any connection older than 5 min at checkout, so even
#     if a flow does die we rebuild it (~45ms) instead of hanging on it.
# pool_pre_ping stays as a backstop for connections the server closed cleanly.
_async_engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    pool_pre_ping=True,
    pool_recycle=300,
    connect_args={
        "server_settings": {
            "tcp_keepalives_idle": "60",
            "tcp_keepalives_interval": "30",
            "tcp_keepalives_count": "3",
        }
    },
)
# expire_on_commit=False: after a commit, ORM objects keep their loaded
# attribute values instead of being expired. With the default (True), touching
# any attribute of a previously-committed object triggers a *synchronous* lazy
# reload — illegal under async (raises MissingGreenlet). This bit get_current_user
# when it commits last_used_at and the route then reads user.id. Endpoints that
# need server-side values after an insert already call refresh() explicitly, so
# turning expiry off is safe and is the recommended async setting.
_async_session_factory = async_sessionmaker(
    _async_engine, class_=AsyncSession, expire_on_commit=False
)


async def get_async_session() -> AsyncSession:
    async with _async_session_factory() as session:
        yield session


from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session

# Same idle-flow protection as the async engine, but via psycopg2/libpq's
# client-side keepalive params (the worker's connections idle between jobs).
#
# Small pool, generous overflow: each Dramatiq process runs ONE task at a time
# and a task uses at most ~3 connections concurrently (job handler + the 2s
# cancel-watcher poll + a helper), and releases its main connection during the
# minutes-long module.run() (it commits before running). So pool_size=2 keeps
# idle retention low — important because this multiplies across every worker
# process (N processes x pool_size idle connections vs Postgres max_connections)
# — while max_overflow=5 leaves ample headroom so a task never blocks on
# checkout. Tune Postgres max_connections, not these, when raising process count.
_sync_engine = create_engine(
    settings.DATABASE_URL_SYNC,
    echo=False,
    pool_pre_ping=True,
    pool_recycle=300,
    pool_size=2,
    max_overflow=5,
    connect_args={
        "keepalives": 1,
        "keepalives_idle": 60,
        "keepalives_interval": 30,
        "keepalives_count": 3,
    },
)
# expire_on_commit=False for the SAME reason as the async factory above, and for
# a second one that is specific to the worker.
#
# With the default (True), commit() expires every loaded object, so the next
# attribute access silently issues a full-column refresh SELECT — which OPENS A
# NEW TRANSACTION. In the worker that is not merely wasteful, it is the bug that
# took the site down on 2026-07-23: _run_module_loop commits before the long
# module.run() precisely so the connection is not held (see the pool comment
# above), but the very next `submission.id` in the progress callback re-opened a
# transaction that then stayed open for the WHOLE run — hours. The connection sat
# `idle in transaction`, which blocks DDL on `submissions` (a pending ACCESS
# EXCLUSIVE lock queues every subsequent query behind it) and pins autovacuum.
# Async hit this too and was fixed — but there it fails LOUDLY (MissingGreenlet).
# Sync fails silently, which is why it survived this long. Code that needs
# server-side values after a write calls session.refresh() explicitly, so turning
# expiry off is safe here exactly as it is for async.
#
# Trade-off to keep in mind: attributes read after a commit are now the values
# loaded earlier, NOT live ones. Anything that needs freshness must refresh()
# explicitly or use its own short-lived session (the cancel-watcher already does).
_sync_session_factory = sessionmaker(_sync_engine, expire_on_commit=False)


def get_sync_session() -> Session:
    """Synchronous session for Dramatiq worker (runs in a thread, not an event loop)."""
    return _sync_session_factory()
