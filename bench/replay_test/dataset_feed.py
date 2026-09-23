"""The "rolling replay dataset" feed — on-disk layout, atomic publish, resolve.

A *feed profile* is a named, continuously-rebuilt replay dataset (e.g. "daily
glm-5 on backend 4.71"). The platform's collector service builds one on a
schedule and publishes it here; the replay module resolves "the latest build of
profile X" to a concrete file path.

Layout under the feed root (a directory on the shared datasets volume):

    <root>/<profile>/latest.json              ← the pointer; atomically replaced
    <root>/<profile>/builds/<build_id>.jsonl  ← immutable dataset files
    <root>/<profile>/tmp/<build_id>.jsonl.part← staging, same filesystem
    <root>/<profile>/pins/<owner>__<build_id>__<epoch>.jsonl
                                              ← hard link held for a run's
                                                lifetime, so retention cannot
                                                delete a dataset mid-replay

**The pointer file is the source of truth**, not the database. That is what lets
this module stay in `bench/` with no platform imports, so exactly one
implementation resolves a profile — the platform worker, the standalone CLI, and
an operator with `cat` all read the same file. The DB mirrors it for history and
the admin UI.

Publishing is atomic in both steps: the staged dataset is fsync'd and
`os.replace`d into `builds/`, then a fresh pointer is fsync'd and `os.replace`d
onto `latest.json`. A reader therefore sees either the old build or the new one
— never a half-written file, and never a pointer to a dataset that isn't fully
on disk.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from bench.replay_test.jsonl_io import (
    DATASET_SUFFIXES,
    GZIP_SUFFIX,
    PLAIN_SUFFIX,
    dataset_suffix,
    is_compressed,
    strip_dataset_suffix,
)

POINTER_NAME = "latest.json"
BUILDS_DIRNAME = "builds"
STAGING_DIRNAME = "tmp"
PINS_DIRNAME = "pins"
POINTER_VERSION = 1
# Sidecar next to a frozen dataset that records where it came from, so a
# permanent copy can always be traced back to the rolling build that produced it.
FROZEN_META_SUFFIX = ".meta.json"

# Profile names index directories, and they arrive from admin-editable DB rows,
# so they are validated rather than trusted: lowercase slug, no dots, no
# separators — nothing that can climb out of the feed root.
_SAFE_PROFILE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

# Default feed root: a sibling of the curated datasets, so one volume mount
# covers both. REPLAY_FEED_ROOT overrides (the platform sets it explicitly).
_DEFAULT_FEED_ROOT = str(
    Path(__file__).resolve().parent.parent.parent / "dataset" / "replay" / "auto"
)
# Frozen datasets live in a sibling of the rolling feed root — deliberately
# OUTSIDE `<feed>/<profile>/builds/`, so retention (`prune`) never sees them and
# a frozen dataset genuinely does not change over time.
_DEFAULT_FROZEN_ROOT = str(
    Path(__file__).resolve().parent.parent.parent / "dataset" / "replay" / "frozen"
)


def feed_root(root: "str | os.PathLike[str] | None" = None) -> Path:
    """The feed root: explicit argument, else $REPLAY_FEED_ROOT, else the
    in-repo default. Read at call time (not import time) so the platform can set
    it per-process without the import-order trap the REPLAY_* knobs have."""
    if root:
        return Path(root)
    return Path(os.getenv("REPLAY_FEED_ROOT") or _DEFAULT_FEED_ROOT)


def frozen_root(root: "str | os.PathLike[str] | None" = None) -> Path:
    """Where frozen (permanent, retention-immune) datasets live.

    Explicit argument, else $REPLAY_FROZEN_ROOT, else a `frozen/` sibling of
    whatever `feed_root()` resolves to — so a custom REPLAY_FEED_ROOT keeps its
    frozen datasets right next to it, and the backend and collector (which share
    the same env) always agree on the location. Read at call time, like
    `feed_root`, for the same reason."""
    if root:
        return Path(root)
    env = os.getenv("REPLAY_FROZEN_ROOT")
    if env:
        return Path(env)
    feed_env = os.getenv("REPLAY_FEED_ROOT")
    if feed_env:
        return Path(feed_env).parent / "frozen"
    return Path(_DEFAULT_FROZEN_ROOT)


def validate_frozen_name(name: str) -> str:
    """A frozen dataset name becomes a filename on the datasets volume, so it is
    held to the same safe slug as a profile — nothing that can climb out of the
    frozen root or collide with the meta sidecar."""
    if not isinstance(name, str) or not _SAFE_PROFILE.match(name):
        raise ValueError(
            f"invalid frozen dataset name {name!r}: expected a lowercase slug "
            "matching [a-z0-9][a-z0-9_-]{0,63}"
        )
    return name


def validate_profile(profile: str) -> str:
    if not isinstance(profile, str) or not _SAFE_PROFILE.match(profile):
        raise ValueError(
            f"invalid feed profile name {profile!r}: expected a lowercase slug "
            "matching [a-z0-9][a-z0-9_-]{0,63}"
        )
    return profile


@dataclass(frozen=True)
class FeedBuild:
    """One published build of a profile, as described by its pointer file."""

    profile: str
    build_id: str
    path: str            # absolute path to the dataset file
    records: int
    bytes: int
    sha256: str
    built_at: datetime   # tz-aware, UTC
    window_start: "datetime | None" = None
    window_end: "datetime | None" = None
    stats: "dict | None" = None

    def age_seconds(self, now: "datetime | None" = None) -> float:
        now = now or datetime.now(timezone.utc)
        return max(0.0, (now - self.built_at).total_seconds())

    def age_hours(self, now: "datetime | None" = None) -> float:
        return self.age_seconds(now) / 3600.0

    def to_pointer(self) -> dict:
        return {
            "version": POINTER_VERSION,
            "profile": self.profile,
            "build_id": self.build_id,
            # Relative so the pointer survives the volume being mounted at a
            # different path (dev box vs pod).
            "file": Path(self.path).name,
            "records": self.records,
            "bytes": self.bytes,
            "sha256": self.sha256,
            "built_at": self.built_at.astimezone(timezone.utc).isoformat(),
            "window_start": _iso_or_none(self.window_start),
            "window_end": _iso_or_none(self.window_end),
            "stats": self.stats or {},
        }

    def provenance(self) -> dict:
        """The compact record pinned onto a submission run, so a result can
        always be traced to the exact dataset that produced it."""
        return {
            "profile": self.profile,
            "build_id": self.build_id,
            "path": self.path,
            "records": self.records,
            "sha256": self.sha256,
            "built_at": self.built_at.astimezone(timezone.utc).isoformat(),
            "window_start": _iso_or_none(self.window_start),
            "window_end": _iso_or_none(self.window_end),
        }


def _iso_or_none(value: "datetime | None") -> "str | None":
    return value.astimezone(timezone.utc).isoformat() if value else None


def _parse_dt(value: Any) -> "datetime | None":
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def profile_dir(root: "str | os.PathLike[str] | None", profile: str) -> Path:
    return feed_root(root) / validate_profile(profile)


def builds_dir(root: "str | os.PathLike[str] | None", profile: str) -> Path:
    return profile_dir(root, profile) / BUILDS_DIRNAME


def staging_dir(root: "str | os.PathLike[str] | None", profile: str) -> Path:
    return profile_dir(root, profile) / STAGING_DIRNAME


def new_build_id(when: "datetime | None" = None) -> str:
    """Sortable, filename-safe build id: 20260804T063000Z."""
    when = (when or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return when.strftime("%Y%m%dT%H%M%SZ")


def prepare_staging(
    root: "str | os.PathLike[str] | None",
    profile: str,
    build_id: str,
    *,
    compress: bool = True,
) -> Path:
    """Create the profile's directories and return the staging path to write to.

    Staging lives on the SAME filesystem as `builds/` so the publish step is a
    rename, not a copy — that is what makes it atomic. The staged name already
    carries its final extension (`.jsonl` or `.jsonl.gz`) for the same reason:
    publishing must never have to re-encode.
    """
    staging_dir(root, profile).mkdir(parents=True, exist_ok=True)
    builds_dir(root, profile).mkdir(parents=True, exist_ok=True)
    return staging_dir(root, profile) / f"{build_id}{dataset_suffix(compress)}.part"


def _fsync_path(path: Path) -> None:
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_dir(path: Path) -> None:
    """Durably record a rename. Best-effort: some filesystems refuse to open a
    directory for fsync, and a missed dir-fsync is not worth failing a build."""
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def publish(
    root: "str | os.PathLike[str] | None",
    profile: str,
    staged: "str | os.PathLike[str]",
    *,
    build_id: str,
    records: int,
    sha256: str,
    built_at: "datetime | None" = None,
    window_start: "datetime | None" = None,
    window_end: "datetime | None" = None,
    stats: "dict | None" = None,
) -> FeedBuild:
    """Promote a staged dataset to `builds/` and point `latest.json` at it.

    Ordering matters and is not negotiable: the dataset lands FIRST, then the
    pointer. The reverse would expose a pointer to a file that does not exist
    yet. Both steps are `os.replace`, which is atomic within a filesystem.
    """
    validate_profile(profile)
    staged_path = Path(staged)
    if not staged_path.is_file():
        raise FileNotFoundError(f"staged dataset not found: {staged_path}")

    # `<build>.jsonl.gz.part` → `<build>.jsonl.gz`: the compression the builder
    # chose travels with the file, so nothing downstream has to be told about it.
    final_name = staged_path.name[: -len(".part")] if staged_path.name.endswith(".part") \
        else staged_path.name
    final_path = builds_dir(root, profile) / final_name
    _fsync_path(staged_path)
    os.replace(staged_path, final_path)
    _fsync_dir(final_path.parent)

    build = FeedBuild(
        profile=profile,
        build_id=build_id,
        path=str(final_path),
        records=records,
        bytes=final_path.stat().st_size,
        sha256=sha256,
        built_at=(built_at or datetime.now(timezone.utc)).astimezone(timezone.utc),
        window_start=window_start,
        window_end=window_end,
        stats=stats,
    )

    pointer = profile_dir(root, profile) / POINTER_NAME
    tmp_pointer = pointer.with_suffix(".json.tmp")
    with tmp_pointer.open("w", encoding="utf-8") as handle:
        json.dump(build.to_pointer(), handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_pointer, pointer)
    _fsync_dir(pointer.parent)
    return build


def resolve_latest(
    root: "str | os.PathLike[str] | None", profile: str
) -> "FeedBuild | None":
    """The profile's newest published build, or None.

    Returns None (never raises) for every "there is nothing usable here" case —
    no pointer yet, unreadable/garbled pointer, or a pointer whose dataset file
    is missing or the wrong size. Callers decide what to do about it; a None is
    always "the feed has not produced anything I can run".
    """
    try:
        validate_profile(profile)
    except ValueError:
        return None
    pointer = profile_dir(root, profile) / POINTER_NAME
    try:
        data = json.loads(pointer.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None

    file_name = data.get("file")
    if not isinstance(file_name, str) or "/" in file_name or file_name in (".", ".."):
        return None
    dataset = builds_dir(root, profile) / file_name
    try:
        size = dataset.stat().st_size
    except OSError:
        return None
    expected = data.get("bytes")
    if isinstance(expected, int) and expected != size:
        # A size mismatch means the pointer and the dataset disagree — treat it
        # as corruption rather than replaying an unknown file.
        return None
    built_at = _parse_dt(data.get("built_at"))
    if built_at is None:
        return None

    return FeedBuild(
        profile=profile,
        build_id=str(data.get("build_id") or Path(file_name).stem),
        path=str(dataset),
        records=int(data.get("records") or 0),
        bytes=size,
        sha256=str(data.get("sha256") or ""),
        built_at=built_at,
        window_start=_parse_dt(data.get("window_start")),
        window_end=_parse_dt(data.get("window_end")),
        stats=data.get("stats") if isinstance(data.get("stats"), dict) else None,
    )


def list_profiles(root: "str | os.PathLike[str] | None" = None) -> list[str]:
    """Profile names that have a directory on disk (published or not)."""
    base = feed_root(root)
    if not base.is_dir():
        return []
    return sorted(
        p.name for p in base.iterdir()
        if p.is_dir() and _SAFE_PROFILE.match(p.name)
    )


def list_build_files(root: "str | os.PathLike[str] | None", profile: str) -> list[Path]:
    """Every dataset file for a profile, newest first (build ids sort by time)."""
    directory = builds_dir(root, profile)
    if not directory.is_dir():
        return []
    files = [p for p in directory.iterdir()
             if p.name.endswith((".jsonl", ".jsonl.gz"))]
    return sorted(files, key=lambda p: p.name, reverse=True)


def pins_dir(root: "str | os.PathLike[str] | None", profile: str) -> Path:
    return profile_dir(root, profile) / PINS_DIRNAME


def pin(
    root: "str | os.PathLike[str] | None", profile: str, build: FeedBuild, owner: str
) -> "Path | None":
    """Hard-link a build into `pins/` so a run cannot lose its dataset mid-flight.

    Replay re-opens its dataset by path several times over a run that can last
    hours (a sizing pass, the preflight scan, and every `make_iter()` in the
    streaming dispatch). Retention deleting `builds/<id>.jsonl` between two of
    those opens would fail an otherwise healthy benchmark. Retention already
    tries hard not to — an age floor, a keep count, and a query for in-flight
    pins — but those are *policy*, and an admin can configure them away.

    A hard link makes it *structural*: unlink only drops one name, and the inode
    (the actual bytes) survives while this link exists. Costs one inode and no
    data movement, which is why this is a link and not a copy — the datasets are
    0.25-3GB each and several replays run concurrently per worker pod, so
    copying would multiply both the volume's read I/O and the pod's disk use for
    no added safety.

    Returns the pinned path, or None if the filesystem refuses to link (the
    caller then falls back to the builds/ path, where the retention guards still
    apply). Never raises.
    """
    try:
        validate_profile(profile)
        directory = pins_dir(root, profile)
        directory.mkdir(parents=True, exist_ok=True)
        token = _safe_owner(owner)
        suffix = ".jsonl.gz" if build.path.endswith(".gz") else ".jsonl"
        existing = sorted(directory.glob(f"{token}__{build.build_id}__*{suffix}"))
        if existing:
            return existing[0]
        target = directory / f"{token}__{build.build_id}__{int(time.time())}{suffix}"
        os.link(build.path, target)
        return target
    except OSError:
        return None
    except ValueError:
        return None


def _safe_owner(owner: str) -> str:
    """Owner tokens become filenames, so keep them to a known-safe alphabet."""
    return re.sub(r"[^A-Za-z0-9_-]", "-", str(owner))[:64] or "unknown"


def _pin_owner(path: Path) -> str:
    return strip_dataset_suffix(path.name).rsplit("__", 2)[0]


def _pin_age_seconds(path: Path, now: float) -> "float | None":
    """Age of the PIN, taken from its filename.

    Not from the filesystem: a hard link shares its inode's timestamps with
    every other link, so `st_mtime` here is when the *dataset* was written and
    `st_ctime` moves whenever any sibling pin is created or dropped. Either
    would make a just-taken pin on an old build look ancient — the exact
    direction that deletes something in use. The creation epoch is therefore
    stamped into the name when the link is made.

    An unparseable name returns None, meaning "unknown age" — the sweeper then
    keeps it unconditionally, whatever the floor. Guessing must never be the
    reason a live dataset is removed.
    """
    tail = strip_dataset_suffix(path.name).rsplit("__", 1)[-1]
    if not tail.isdigit():
        return None
    return max(0.0, now - int(tail))


def unpin_owner(root: "str | os.PathLike[str] | None", owner: str) -> int:
    """Drop every pin held by `owner`, across all profiles. Returns how many.

    Scans profiles rather than taking one, so the caller doesn't have to thread
    the profile name through to its cleanup path — and so a run that somehow
    pinned two profiles is fully released. Never raises: a leaked pin costs one
    extra dataset on disk until the sweeper runs, which is strictly better than
    failing a finished run's cleanup.
    """
    token = _safe_owner(owner)
    removed = 0
    for profile in list_profiles(root):
        directory = pins_dir(root, profile)
        if not directory.is_dir():
            continue
        for path in directory.glob(f"{token}__*"):
            try:
                path.unlink()
                removed += 1
            except OSError:
                continue
    return removed


def list_pins(root: "str | os.PathLike[str] | None", profile: str) -> "list[tuple[str, Path]]":
    directory = pins_dir(root, profile)
    if not directory.is_dir():
        return []
    return [(_pin_owner(p), p) for p in sorted(directory.iterdir())
            if p.name.endswith((".jsonl", ".jsonl.gz"))]


def sweep_pins(
    root: "str | os.PathLike[str] | None",
    profile: str,
    *,
    is_live: "Callable[[str], bool]",
    min_age_seconds: float = 3600.0,
) -> int:
    """Release pins left behind by a worker that died mid-run.

    `is_live(owner)` decides; the age floor keeps a pin that was just created
    (and whose run may not have registered yet) out of reach. Fails SAFE — any
    error means the pin stays, mirroring the output-dir sweeper: leaking disk is
    recoverable, deleting a live run's dataset is not.
    """
    now = time.time()
    removed = 0
    for owner, path in list_pins(root, profile):
        try:
            age = _pin_age_seconds(path, now)
            if age is None or age < min_age_seconds:
                continue
            if is_live(owner):
                continue
            path.unlink()
        except OSError:
            continue
        except Exception:
            continue
        removed += 1
    return removed


def prune(
    root: "str | os.PathLike[str] | None",
    profile: str,
    *,
    keep: int,
    min_age_seconds: float,
    protected: "set[str] | None" = None,
) -> list[str]:
    """Delete old builds, returning the build ids removed.

    Three independent guards, all of which must allow a deletion:
      - the newest `keep` builds always survive;
      - a build younger than `min_age_seconds` always survives (a submission
        that resolved it moments ago may not have opened it yet — replay
        re-opens its dataset several times per run);
      - anything in `protected` always survives (the build `latest.json` points
        at, plus builds pinned by in-flight submissions).
    """
    protected = set(protected or ())
    current = resolve_latest(root, profile)
    if current is not None:
        protected.add(current.build_id)

    removed: list[str] = []
    now = time.time()
    for index, path in enumerate(list_build_files(root, profile)):
        # NOT Path.stem — it strips one suffix, so `b1.jsonl.gz` would yield
        # `b1.jsonl` and never match a protected build id.
        build_id = strip_dataset_suffix(path.name)
        if index < max(0, keep) or build_id in protected:
            continue
        try:
            if now - path.stat().st_mtime < min_age_seconds:
                continue
            path.unlink()
        except OSError:
            continue
        removed.append(build_id)
    return removed


def sweep_staging(
    root: "str | os.PathLike[str] | None", profile: str, *, max_age_seconds: float = 86400.0
) -> int:
    """Delete abandoned `.part` files (a build the pod was killed mid-way).
    Returns how many were removed."""
    directory = staging_dir(root, profile)
    if not directory.is_dir():
        return 0
    now = time.time()
    removed = 0
    for path in directory.glob("*.part"):
        try:
            if now - path.stat().st_mtime < max_age_seconds:
                continue
            path.unlink()
        except OSError:
            continue
        removed += 1
    return removed


# ---------------------------------------------------------------------------
# Frozen datasets — a rolling build promoted to a permanent, static file
# ---------------------------------------------------------------------------
# A rolling build lives in `<feed>/<profile>/builds/` and is subject to
# retention: once `keep_builds` newer builds exist and it is past
# `min_retain_hours`, `prune()` deletes it. That is exactly what makes it *not*
# safe to pin a benchmark to a build path — the file rots out from under it.
#
# Freezing copies (by hard link, so it costs one inode and no data movement) a
# chosen build OUT of `builds/` into `<frozen>/<name>.jsonl[.gz]`, which nothing
# in the rolling machinery ever touches. The frozen file then survives retention,
# profile deletion, and even the whole `auto/` tree being cleaned — an admin
# pastes its path into a `fixed`-source benchmark and the dataset never changes.


@dataclass(frozen=True)
class FrozenDataset:
    """A permanent dataset frozen out of the rolling feed, plus its provenance."""

    name: str
    path: str            # absolute path to the frozen dataset file
    bytes: int
    records: int = 0
    sha256: str = ""     # the SOURCE build's content hash (identity, not a file checksum)
    frozen_at: "datetime | None" = None
    source_profile: str = ""
    source_build_id: str = ""
    window_start: "datetime | None" = None
    window_end: "datetime | None" = None
    frozen_by: str = ""

    def to_meta(self) -> dict:
        return {
            "version": POINTER_VERSION,
            "name": self.name,
            "file": Path(self.path).name,
            "bytes": self.bytes,
            "records": self.records,
            "sha256": self.sha256,
            "frozen_at": _iso_or_none(self.frozen_at),
            "source_profile": self.source_profile,
            "source_build_id": self.source_build_id,
            "window_start": _iso_or_none(self.window_start),
            "window_end": _iso_or_none(self.window_end),
            "frozen_by": self.frozen_by,
        }


def frozen_meta_path(root: "str | os.PathLike[str] | None", name: str) -> Path:
    return frozen_root(root) / f"{name}{FROZEN_META_SUFFIX}"


def _read_frozen_meta(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _frozen_from_path(base: Path, dataset: Path) -> "FrozenDataset | None":
    name = strip_dataset_suffix(dataset.name)
    try:
        size = dataset.stat().st_size
    except OSError:
        return None
    meta = _read_frozen_meta(base / f"{name}{FROZEN_META_SUFFIX}")
    return FrozenDataset(
        name=name,
        path=str(dataset),
        bytes=size,
        records=int(meta.get("records") or 0),
        sha256=str(meta.get("sha256") or ""),
        frozen_at=_parse_dt(meta.get("frozen_at")),
        source_profile=str(meta.get("source_profile") or ""),
        source_build_id=str(meta.get("source_build_id") or ""),
        window_start=_parse_dt(meta.get("window_start")),
        window_end=_parse_dt(meta.get("window_end")),
        frozen_by=str(meta.get("frozen_by") or ""),
    )


def _hardlink_or_copy(source: Path, target: Path, staging: Path) -> None:
    """Materialise `target` from `source` on the same filesystem.

    A hard link is the whole point: the frozen name gets its own directory entry
    pointing at the build's inode, so pruning the build only drops the build's
    name — the bytes live as long as this link does. One inode, zero copy, even
    for a multi-GB dataset. When the filesystem refuses to link (some backends
    do), fall back to a byte-faithful staged copy + fsync + atomic rename, so a
    reader never sees a half-written frozen file. The build's sha256 is a hash of
    the UNCOMPRESSED content (see builder.py), not of the file bytes, so it is
    deliberately NOT used to checksum this copy."""
    try:
        os.link(source, target)
        return
    except OSError:
        pass
    staging.mkdir(parents=True, exist_ok=True)
    tmp = staging / f"{target.name}.{os.getpid()}.part"
    try:
        with open(source, "rb") as src, open(tmp, "wb") as dst:
            while True:
                chunk = src.read(1 << 20)
                if not chunk:
                    break
                dst.write(chunk)
            dst.flush()
            os.fsync(dst.fileno())
        os.replace(tmp, target)
        _fsync_dir(target.parent)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def freeze(
    root: "str | os.PathLike[str] | None",
    name: str,
    source_file: "str | os.PathLike[str]",
    *,
    records: int = 0,
    sha256: str = "",
    source_profile: str = "",
    source_build_id: str = "",
    window_start: "datetime | None" = None,
    window_end: "datetime | None" = None,
    frozen_by: str = "",
    frozen_at: "datetime | None" = None,
) -> FrozenDataset:
    """Promote a build file to a permanent frozen dataset.

    Create-once: a frozen dataset is meant to be immutable, so freezing onto a
    name that already exists raises `FileExistsError` rather than silently
    replacing it. The frozen file keeps the source's codec (`.jsonl` stays
    `.jsonl`, `.jsonl.gz` stays `.jsonl.gz`) because every reader dispatches on
    the extension.
    """
    validate_frozen_name(name)
    source = Path(source_file)
    if not source.is_file():
        raise FileNotFoundError(f"source dataset not found: {source}")

    base = frozen_root(root)
    suffix = GZIP_SUFFIX if is_compressed(source) else PLAIN_SUFFIX
    other_suffix = PLAIN_SUFFIX if suffix == GZIP_SUFFIX else GZIP_SUFFIX
    target = base / f"{name}{suffix}"
    meta_path = base / f"{name}{FROZEN_META_SUFFIX}"
    if target.exists() or (base / f"{name}{other_suffix}").exists() or meta_path.exists():
        raise FileExistsError(f"a frozen dataset named {name!r} already exists")

    base.mkdir(parents=True, exist_ok=True)
    _hardlink_or_copy(source, target, base / STAGING_DIRNAME)

    dataset = FrozenDataset(
        name=name,
        path=str(target),
        bytes=target.stat().st_size,
        records=records,
        sha256=sha256,
        frozen_at=(frozen_at or datetime.now(timezone.utc)).astimezone(timezone.utc),
        source_profile=source_profile,
        source_build_id=source_build_id,
        window_start=window_start,
        window_end=window_end,
        frozen_by=frozen_by,
    )
    tmp_meta = meta_path.parent / f"{meta_path.name}.tmp"
    with tmp_meta.open("w", encoding="utf-8") as handle:
        json.dump(dataset.to_meta(), handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_meta, meta_path)
    _fsync_dir(meta_path.parent)
    return dataset


def list_frozen(root: "str | os.PathLike[str] | None" = None) -> list[FrozenDataset]:
    """Every frozen dataset, newest first. Reads the filesystem, not a DB — the
    frozen file IS the source of truth, and it must survive a DB wipe."""
    base = frozen_root(root)
    if not base.is_dir():
        return []
    out: list[FrozenDataset] = []
    for path in base.iterdir():
        if not path.is_file() or not path.name.endswith(tuple(DATASET_SUFFIXES)):
            continue
        dataset = _frozen_from_path(base, path)
        if dataset is not None:
            out.append(dataset)
    out.sort(
        key=lambda d: (d.frozen_at or datetime.min.replace(tzinfo=timezone.utc), d.name),
        reverse=True,
    )
    return out


def resolve_frozen(
    root: "str | os.PathLike[str] | None", name: str
) -> "FrozenDataset | None":
    """The frozen dataset named `name`, or None. Never raises."""
    try:
        validate_frozen_name(name)
    except ValueError:
        return None
    base = frozen_root(root)
    for suffix in DATASET_SUFFIXES:
        path = base / f"{name}{suffix}"
        if path.is_file():
            return _frozen_from_path(base, path)
    return None


def delete_frozen(root: "str | os.PathLike[str] | None", name: str) -> bool:
    """Remove a frozen dataset and its meta sidecar. Returns whether a dataset
    file was actually removed. Only a missing file is swallowed — a permission or
    I/O error propagates rather than being reported as a no-op delete."""
    validate_frozen_name(name)
    base = frozen_root(root)
    removed = False
    for suffix in DATASET_SUFFIXES:
        path = base / f"{name}{suffix}"
        if path.exists():
            path.unlink()
            removed = True
    meta_path = base / f"{name}{FROZEN_META_SUFFIX}"
    if meta_path.exists():
        meta_path.unlink()
    return removed
