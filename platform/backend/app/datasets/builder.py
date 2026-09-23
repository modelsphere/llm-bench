"""Turn a window of production traffic into a published replay dataset.

The records come from a `LogSource` (app.datasets.sources) — the gateway's
bodylog files, or a VictoriaLogs store they were shipped to.

One build =  windows → fetch → convert → sample → validate → publish → prune.

Design constraints that shaped this, all of them learned from the replay module
itself (see `docs/platform-architecture.md` §8):

- **Memory is bounded by the sample, not the window.** An hour of production
  traffic is gigabytes of request bodies. Rows stream out of the log store and
  only one sub-window's reservoir (a few hundred records) is ever resident.
- **Disk is bounded and checked up front.** The datasets volume is shared with
  every curated dataset the platform already replays; filling it would break
  every benchmark, not just this feature.
- **A bad build must never replace a good one.** Validation happens on the
  staged file, before the pointer moves; a failure leaves the previous build
  serving.
- **The sample is time-stratified.** A flat `| limit N` over a whole day would
  return an arbitrary slab of it. Per-sub-window quotas (with the shortfall
  carried forward) keep the diurnal mix of prompt sizes that makes a replay
  dataset representative.
"""
from __future__ import annotations

import hashlib
import logging
import math
import os
import random
import shutil
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from bench.replay_test import dataset_feed, jsonl_io
from bench.replay_test.bodylog_convert import (
    CONTEXT_HEADROOM_TOKENS,
    TTFT_INPUT_BUCKETS,
    ConvertStats,
    StripPolicy,
    convert_record,
    dumps_record,
)
from bench.replay_test.log_replay_tool import load_extract_jsonl, percentile

from app.datasets.sources import LogSource, as_source
from app.datasets.victorialogs import LogFilters

logger = logging.getLogger(__name__)

# Default cap: a slice may keep at most this multiple of its base quota, however
# large the carried shortfall grows. Without it, a profile whose window starts
# with dead hours would try to make the whole sample up in one slice — the exact
# unbounded-memory shape the per-slice reservoir exists to prevent. Per-profile
# overridable via BuildProfile.max_carry_multiple (admin editor) because for
# traffic concentrated in a few slices this product, not sample_size, is the real
# ceiling on the sample.
MAX_CARRY_MULTIPLE = 4
# Refuse to start a build unless the volume has this multiple of the estimated
# output free. The estimate is refined after the first slice.
DISK_HEADROOM_MULTIPLE = 2.0
# Fallback average record size (bytes) for the pre-flight disk estimate, before
# any real record has been seen. Measured on live traffic: ~60KB-180KB.
ESTIMATED_RECORD_BYTES = 120_000

ProgressFn = Callable[[str], None]
StopFn = Callable[[], bool]


class BuildAborted(RuntimeError):
    """Raised when a build is stopped deliberately (shutdown, cancel)."""


@dataclass
class BuildProfile:
    """Everything a build needs, decoupled from the DB row that stores it.

    Keeping this a plain dataclass means the builder can be driven from a JSON
    file for a dry run on a dev box — no database, no deployment.
    """

    name: str
    filters: LogFilters = field(default_factory=LogFilters)
    window_hours: int = 24
    window_timezone: str = "UTC"
    subwindow_minutes: int = 60
    sample_size: int = 2000
    min_records: int = 100
    min_buckets: int = 1
    max_bytes: int = 8 * 1024 ** 3
    oversample_factor: float = 3.0
    # Per-slice keep cap = ceil(sample_size / n_slices) * max_carry_multiple. The
    # real ceiling on the sample when traffic is concentrated in a few slices —
    # raise it (or subwindow_minutes) to fully capture a bursty model.
    max_carry_multiple: int = MAX_CARRY_MULTIPLE
    clean: bool = True
    max_model_len: int = 262144
    keep_response_body: bool = False
    # Store the dataset gzipped (~3.6x smaller on this kind of JSON, measured
    # on a real build). Readers dispatch on the extension, so this is free to
    # flip per profile; existing uncompressed builds keep working.
    compress: bool = True
    header_denylist: list[str] = field(default_factory=list)
    keep_builds: int = 7
    min_retain_hours: float = 8.0
    # Only used to project how many builds retention will really hold
    # (see `retained_builds`); the schedule itself is the collector's job.
    schedule_interval_hours: float = 24.0

    def tzinfo(self):
        try:
            return ZoneInfo(self.window_timezone)
        except (ZoneInfoNotFoundError, ValueError):
            logger.warning(
                "[feed:%s] unknown timezone %r — falling back to UTC",
                self.name, self.window_timezone,
            )
            return timezone.utc

    def strip_policy(self) -> StripPolicy:
        denylist = (
            frozenset(h.strip().lower() for h in self.header_denylist if h.strip())
            if self.header_denylist else None
        )
        policy = StripPolicy.for_feed(denylist)
        if self.keep_response_body:
            policy = StripPolicy(
                header_denylist=policy.header_denylist, keep_response_body=True
            )
        return policy

    @property
    def budget(self) -> int:
        return self.max_model_len - CONTEXT_HEADROOM_TOKENS


@dataclass
class BuildResult:
    status: str                                   # "ready" | "failed"
    build: "dataset_feed.FeedBuild | None" = None
    error: "str | None" = None
    stats: dict = field(default_factory=dict)
    pruned: list[str] = field(default_factory=list)
    window_start: "datetime | None" = None
    window_end: "datetime | None" = None
    seed: int = 0


def window_bounds(profile: BuildProfile, now: "datetime | None" = None) -> tuple[datetime, datetime]:
    """`[end - window_hours, end)`, with `end` floored to a sub-window boundary.

    Floored in the profile's own timezone so a "daily" window lines up with a
    local day — the captures carry +08:00 timestamps, and a UTC-aligned day
    would cut every build across two local business days.
    """
    tz = profile.tzinfo()
    now = (now or datetime.now(timezone.utc)).astimezone(tz)
    step = max(1, profile.subwindow_minutes)
    minutes = (now.hour * 60 + now.minute) // step * step
    end = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(minutes=minutes)
    return end - timedelta(hours=max(1, profile.window_hours)), end


def _slices(start: datetime, end: datetime, minutes: int) -> Iterator[tuple[datetime, datetime]]:
    step = timedelta(minutes=max(1, minutes))
    cursor = start
    while cursor < end:
        nxt = min(cursor + step, end)
        yield cursor, nxt
        cursor = nxt


def _free_bytes(path: Path) -> int:
    try:
        return shutil.disk_usage(str(path)).free
    except OSError:
        return 0


def _check_disk(path: Path, needed: int, label: str) -> None:
    free = _free_bytes(path)
    if free and free < needed:
        raise RuntimeError(
            f"insufficient space on the datasets volume ({label}): need "
            f"~{needed / 1e9:.1f}GB free (estimate x{DISK_HEADROOM_MULTIPLE:g} headroom), "
            f"have {free / 1e9:.1f}GB. Refusing to build — filling this volume "
            f"would break every benchmark that reads a dataset from it."
        )


def _record_facts(record: dict) -> tuple:
    """The handful of scalars the build summary is tallied from.

    Pulled while the record is already parsed, so summarising a build costs no
    second pass over a dataset that can be hundreds of MB.

    (model, forwarded_to, prompt_tokens, completion_tokens, cached_tokens)
    """
    payload = record.get("raw_payload") or {}
    usage = (payload.get("resp_meta") or {}).get("usage") or {}
    details = usage.get("prompt_tokens_details") or {}
    return (
        payload.get("model"),
        payload.get("forwarded_to"),
        usage.get("prompt_tokens"),
        usage.get("completion_tokens"),
        details.get("cached_tokens"),
    )


class _Summary:
    """What the collected dataset actually contains.

    Answers the questions an operator asks of a build they didn't watch being
    made: which model and which backend is this traffic from, how long are the
    prompts and the answers, and how much of the input was already cached. The
    admin page reads this straight off the build — without it, the only way to
    know is to go and parse the dataset by hand.
    """

    def __init__(self) -> None:
        self.models: Counter = Counter()
        self.backends: Counter = Counter()
        self.prompt: list[int] = []
        self.completion: list[int] = []
        self.total_prompt = 0
        self.total_completion = 0
        self.total_cached = 0

    def add(self, facts: tuple) -> None:
        model, backend, prompt, completion, cached = facts
        self.models[model if model is not None else "(unknown)"] += 1
        self.backends[backend if backend is not None else "(none)"] += 1
        if isinstance(prompt, int) and not isinstance(prompt, bool):
            self.prompt.append(prompt)
            self.total_prompt += prompt
        if isinstance(completion, int) and not isinstance(completion, bool):
            self.completion.append(completion)
            self.total_completion += completion
        if isinstance(cached, int) and not isinstance(cached, bool):
            self.total_cached += cached

    @staticmethod
    def _spread(values: list[int]) -> "dict | None":
        if not values:
            return None
        return {
            "count": len(values),
            "min": values[0] if len(values) == 1 else min(values),
            "p50": round(percentile(values, 0.50)),
            "p90": round(percentile(values, 0.90)),
            "max": max(values),
            "mean": round(sum(values) / len(values)),
        }

    def as_dict(self) -> dict:
        return {
            "models": dict(self.models.most_common(10)),
            "forwarded_to": dict(self.backends.most_common(10)),
            "prompt_tokens": self._spread(self.prompt),
            "completion_tokens": self._spread(self.completion),
            "total_prompt_tokens": self.total_prompt,
            "total_completion_tokens": self.total_completion,
            "total_cached_tokens": self.total_cached,
            # Token-weighted, the same definition replay's
            # cache_hit_rate metric uses, so the two are comparable.
            "cache_hit_rate": (
                round(self.total_cached / self.total_prompt, 4)
                if self.total_prompt else None
            ),
        }


class _Reservoir:
    """Uniform sample of at most `size` items, with a byte ceiling.

    Classic reservoir sampling (Algorithm R): every candidate in the slice has
    the same probability of ending up in the sample, whatever the slice's true
    size, and only `size` items are ever held.
    """

    def __init__(self, size: int, rng: random.Random, max_bytes: int) -> None:
        self.size = max(0, size)
        self.rng = rng
        self.max_bytes = max_bytes
        self.items: list[str] = []
        # One small facts-tuple per kept record (see _record_facts), carried
        # alongside the serialized line so the build summary can be tallied
        # without re-parsing the dataset afterwards.
        self.facts: list[tuple] = []
        self.seen = 0
        self.bytes = 0

    def offer(self, line: str, facts: tuple) -> None:
        self.seen += 1
        if self.size == 0:
            return
        if len(self.items) < self.size:
            if self.max_bytes and self.bytes + len(line) > self.max_bytes:
                return
            self.items.append(line)
            self.facts.append(facts)
            self.bytes += len(line)
            return
        idx = self.rng.randrange(self.seen)
        if idx >= self.size:
            return
        replaced = self.items[idx]
        if self.max_bytes and self.bytes - len(replaced) + len(line) > self.max_bytes:
            return
        self.bytes += len(line) - len(replaced)
        self.items[idx] = line
        self.facts[idx] = facts


def build_dataset(
    source: "LogSource",
    profile: BuildProfile,
    *,
    feed_root: "str | os.PathLike[str] | None" = None,
    build_id: "str | None" = None,
    now: "datetime | None" = None,
    seed: "int | None" = None,
    progress: "ProgressFn | None" = None,
    should_stop: "StopFn | None" = None,
    protected_builds: "set[str] | None" = None,
    allow_prune: bool = True,
) -> BuildResult:
    """Collect one dataset for `profile` and publish it. Never raises for an
    ordinary failure — returns a `failed` BuildResult with the reason, leaving
    the previous build serving."""
    say: ProgressFn = progress or (lambda _msg: None)
    stop: StopFn = should_stop or (lambda: False)
    seed = int(seed if seed is not None else random.SystemRandom().randrange(2 ** 31))
    build_id = build_id or dataset_feed.new_build_id(now)
    start, end = window_bounds(profile, now)
    stats = ConvertStats()
    fetch_stats: Counter = Counter()
    summary = _Summary()

    try:
        staged = dataset_feed.prepare_staging(
            feed_root, profile.name, build_id, compress=profile.compress
        )
    except (ValueError, OSError) as exc:
        return BuildResult(status="failed", error=f"cannot prepare staging: {exc}",
                           window_start=start, window_end=end, seed=seed)

    slices = list(_slices(start, end, profile.subwindow_minutes))
    base_quota = max(1, math.ceil(profile.sample_size / max(1, len(slices))))
    est_total = profile.sample_size * ESTIMATED_RECORD_BYTES
    digest = hashlib.sha256()
    raw_bytes = 0
    # Built once: it is applied to every record, and rebuilding the denylist
    # frozenset per record over a whole window is pure waste.
    strip = profile.strip_policy()
    written = 0
    carry = 0

    projected = retained_builds(profile, profile.schedule_interval_hours)
    per_slice_cap = base_quota * max(1, profile.max_carry_multiple)
    say(
        f"build {build_id}: window {start.isoformat()} → {end.isoformat()} "
        f"({len(slices)} x {profile.subwindow_minutes}min), target {profile.sample_size} "
        f"records, base quota {base_quota}/slice, per-slice cap {per_slice_cap} "
        f"(x{max(1, profile.max_carry_multiple)} carry) — traffic concentrated in "
        f"fewer than {profile.sample_size // per_slice_cap if per_slice_cap else 0} "
        f"slices cannot reach the target"
    )
    if projected > profile.keep_builds:
        # keep_builds looks like the cap but isn't when the time floor dominates.
        # Say so out loud rather than letting the volume fill quietly.
        say(
            f"note: retention will hold ~{projected} builds, not keep_builds="
            f"{profile.keep_builds} — min_retain_hours={profile.min_retain_hours:g} "
            f"covers {projected} builds at one every {profile.schedule_interval_hours:g}h"
        )
    source = as_source(source)
    logger.info("[feed:%s] source: %s", profile.name,
                source.describe(profile.filters, start, end, limit=base_quota))

    try:
        _check_disk(staged.parent, int(min(est_total, profile.max_bytes) * DISK_HEADROOM_MULTIPLE),
                    "pre-flight")

        # gzip-aware by extension. Byte accounting below tracks UNCOMPRESSED
        # size on purpose: `out.tell()` through a gzip text wrapper returns an
        # opaque cookie, and sizing the caps by what the records actually weigh
        # keeps `max_bytes` meaning the same thing whether or not a profile
        # compresses. It also errs high for the disk precondition, which is the
        # safe direction.
        with jsonl_io.open_text(staged, "w") as out:
            for index, (slice_start, slice_end) in enumerate(slices):
                if stop():
                    raise BuildAborted("stopped before completion")
                if written >= profile.sample_size:
                    break

                quota = min(base_quota + carry, base_quota * max(1, profile.max_carry_multiple))
                quota = min(quota, profile.sample_size - written)
                if quota <= 0:
                    break
                limit = max(quota, int(quota * max(1.0, profile.oversample_factor)))
                rng = random.Random((seed << 16) ^ index)
                # The reservoir may hold at most what is left of the profile's
                # byte cap, so a slice of unusually large prompts cannot blow
                # past it between the per-slice checks below.
                reservoir = _Reservoir(
                    quota, rng, max_bytes=max(0, profile.max_bytes - raw_bytes),
                )

                for row in source.iter_window(profile.filters, slice_start, slice_end, limit=limit):
                    if stop():
                        raise BuildAborted("stopped mid-slice")
                    fetch_stats["fetched"] += 1
                    stats.total += 1
                    record, reason = convert_record(
                        row,
                        source_file=f"{profile.name}:{build_id}",
                        line_no=stats.total,
                        clean=profile.clean,
                        budget=profile.budget,
                        strip=strip,
                        fixes=stats.fixes,
                    )
                    if record is None:
                        stats.drops[reason] += 1
                        continue
                    reservoir.offer(dumps_record(record), _record_facts(record))

                for line, facts in zip(reservoir.items, reservoir.facts):
                    payload = line + "\n"
                    encoded = payload.encode("utf-8")
                    out.write(payload)
                    digest.update(encoded)
                    raw_bytes += len(encoded)
                    stats.note_written(facts[2])   # prompt_tokens -> length bucket
                    summary.add(facts)
                    written += 1

                carry = max(0, carry + base_quota - len(reservoir.items))
                say(
                    f"slice {index + 1}/{len(slices)} {slice_start:%m-%d %H:%M}: "
                    f"fetched {reservoir.seen}, kept {len(reservoir.items)}, "
                    f"total {written}/{profile.sample_size}"
                )

                out.flush()
                if raw_bytes >= profile.max_bytes:
                    fetch_stats["stopped_on_max_bytes"] = 1
                    say(f"stopping early: {raw_bytes / 1e9:.2f}GB (uncompressed) "
                        f"reached the profile's byte cap")
                    break
                if index == 0 and written:
                    # Re-estimate from reality now that real records exist.
                    est_total = int(raw_bytes / written * profile.sample_size)
                    _check_disk(staged.parent, int(min(est_total, profile.max_bytes) * DISK_HEADROOM_MULTIPLE),
                                "mid-build")
            out.flush()
            os.fsync(out.fileno())

        _validate(staged, profile, stats, written)

        build = dataset_feed.publish(
            feed_root, profile.name, staged,
            build_id=build_id,
            records=written,
            sha256=digest.hexdigest(),
            built_at=datetime.now(timezone.utc),
            window_start=start,
            window_end=end,
            stats={**stats.as_dict(), **dict(fetch_stats),
                   "summary": summary.as_dict(), "seed": seed},
        )
    except BuildAborted as exc:
        _discard(staged)
        return BuildResult(status="failed", error=str(exc), stats=stats.as_dict(),
                           window_start=start, window_end=end, seed=seed)
    except Exception as exc:  # noqa: BLE001 - a build failure is data, not a crash
        logger.exception("[feed:%s] build %s failed", profile.name, build_id)
        _discard(staged)
        return BuildResult(status="failed", error=f"{type(exc).__name__}: {exc}",
                           stats=stats.as_dict(), window_start=start, window_end=end, seed=seed)

    # Skipped when the caller could not determine which builds are in use:
    # retention against an unknown protected set is how you delete a dataset a
    # running benchmark is reading.
    pruned = dataset_feed.prune(
        feed_root, profile.name,
        keep=profile.keep_builds,
        min_age_seconds=profile.min_retain_hours * 3600.0,
        protected=protected_builds,
    ) if allow_prune else []
    if not allow_prune:
        say("skipped retention: could not read the set of in-use builds")
    dataset_feed.sweep_staging(feed_root, profile.name)
    say(
        f"published {written} records ({build.bytes / 1e6:.1f}MB) as {build_id}"
        + (f"; pruned {len(pruned)} old build(s)" if pruned else "")
    )
    return BuildResult(
        status="ready", build=build,
        stats={**stats.as_dict(), **dict(fetch_stats), "summary": summary.as_dict()},
        pruned=pruned, window_start=start, window_end=end, seed=seed,
    )


def retained_builds(profile: BuildProfile, schedule_interval_hours: float) -> int:
    """How many builds the profile will actually hold on disk.

    `keep_builds` is not the answer on its own: retention deletes a build only
    when it is BOTH beyond the keep count AND older than `min_retain_hours`, so
    a fast schedule makes the time floor dominate. Hourly builds behind a 72h
    floor retain 72 of them, not 7 — which is how you fill a shared volume while
    believing you configured a 7-build cap.
    """
    if schedule_interval_hours <= 0:
        return max(1, profile.keep_builds)
    by_time = math.ceil(profile.min_retain_hours / schedule_interval_hours)
    return max(1, profile.keep_builds, by_time)


def _validate(staged: Path, profile: BuildProfile, stats: ConvertStats, written: int) -> None:
    """Gate publication. Every check here is a reason NOT to replace a working
    dataset with this one."""
    if written < profile.min_records:
        raise RuntimeError(
            f"only {written} records collected, below the profile's minimum of "
            f"{profile.min_records} — keeping the previous build"
        )
    non_empty = sum(1 for _h, count in stats.buckets.items() if count)
    if non_empty < profile.min_buckets:
        raise RuntimeError(
            f"records span {non_empty} input-length bucket(s), below the "
            f"profile's minimum of {profile.min_buckets} — the sample is too "
            f"narrow to exercise the per-bucket TTFT thresholds"
        )
    # Full re-read through the real loader: whatever the replay module will do
    # to this file, do it here first. The staged file is sample-sized, so this
    # costs seconds, not the window.
    seen = 0
    for _record in load_extract_jsonl(str(staged), lean=True):
        seen += 1
    if seen != written:
        raise RuntimeError(
            f"staged dataset re-reads as {seen} records but {written} were written"
        )


def _discard(staged: Path) -> None:
    try:
        staged.unlink(missing_ok=True)
    except OSError:  # pragma: no cover - best effort
        logger.warning("[feed] could not remove staged file %s", staged)


def profile_from_dict(data: dict) -> BuildProfile:
    """Build a profile from a plain dict (a DB row's fields, or a JSON file for
    a dry run). Unknown keys are ignored so a newer DB row never breaks an older
    builder mid-deploy."""
    filters = LogFilters(
        models=list(data.get("models") or []),
        statuses=[str(s) for s in (data.get("statuses") or ["200"])],
        forwarded_to=list(data.get("forwarded_to") or []),
        uris=list(data.get("uris") or ["/v1/chat/completions"]),
        exclude_truncated=bool(data.get("exclude_truncated", True)),
        extra=str(data.get("extra_logsql") or ""),
    )
    known = {f for f in BuildProfile.__dataclass_fields__ if f != "filters"}  # noqa: E501
    kwargs = {k: v for k, v in data.items() if k in known}
    return BuildProfile(filters=filters, **kwargs)


def bucket_summary(stats: dict) -> str:
    """`<6K=117  6K-16K=42 …` — the one line worth putting in a log or the UI."""
    buckets = stats.get("buckets") or {}
    parts = [f"{human}={buckets.get(human, 0)}" for human, _lo, _hi in TTFT_INPUT_BUCKETS]
    if stats.get("bucket_unknown"):
        parts.append(f"unknown={stats['bucket_unknown']}")
    return "  ".join(parts)
