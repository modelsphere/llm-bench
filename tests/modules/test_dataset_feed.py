"""Tests for the rolling replay-dataset feed's on-disk contract.

The pointer file is the source of truth for "which build is current", so the
invariants worth pinning down are the ones a benchmark run depends on:

  - a reader never sees a partial dataset or a pointer to one;
  - a corrupt/missing/garbled pointer resolves to None rather than raising or,
    worse, returning a path to something unexpected;
  - retention never deletes the current build, a pinned one, or a young one;
  - profile names cannot climb out of the feed root.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from datetime import datetime, timedelta, timezone

import pytest

from bench.replay_test import dataset_feed, jsonl_io


def _publish(root, profile="daily", build_id="20260804T000000Z", records=3,
             built_at=None, lines=None):
    staged = dataset_feed.prepare_staging(root, profile, build_id)
    payload = lines if lines is not None else [f'{{"n":{i}}}' for i in range(records)]
    # Via jsonl_io: staging is `.jsonl.gz.part` by default, so a raw write_text
    # would put plain text under a .gz name.
    with jsonl_io.open_text(staged, "w") as handle:
        handle.write("\n".join(payload) + "\n")
    return dataset_feed.publish(
        root, profile, staged,
        build_id=build_id, records=len(payload), sha256="deadbeef",
        built_at=built_at or datetime.now(timezone.utc),
        window_start=datetime(2026, 8, 3, tzinfo=timezone.utc),
        window_end=datetime(2026, 8, 4, tzinfo=timezone.utc),
        stats={"buckets": {"<6K": 1}},
    )


# --- publish / resolve ---------------------------------------------------------

def test_publish_then_resolve_roundtrip(tmp_path):
    build = _publish(tmp_path)
    resolved = dataset_feed.resolve_latest(tmp_path, "daily")
    assert resolved is not None
    assert resolved.build_id == build.build_id
    assert resolved.path == build.path
    assert resolved.records == 3
    assert os.path.isfile(resolved.path)
    assert resolved.stats == {"buckets": {"<6K": 1}}


def test_publish_moves_staging_not_copies(tmp_path):
    """The staged file must be renamed into place, not left behind — staging and
    builds share a filesystem precisely so the publish is atomic."""
    staged = dataset_feed.prepare_staging(tmp_path, "daily", "b1")
    with jsonl_io.open_text(staged, "w") as handle:
        handle.write('{"n":1}\n')
    build = dataset_feed.publish(
        tmp_path, "daily", staged, build_id="b1", records=1, sha256="x"
    )
    assert not staged.exists()
    assert Path(build.path).is_file()
    assert Path(build.path).parent == dataset_feed.builds_dir(tmp_path, "daily")


def test_publishing_a_second_build_switches_the_pointer(tmp_path):
    _publish(tmp_path, build_id="b1", records=2)
    _publish(tmp_path, build_id="b2", records=5)
    resolved = dataset_feed.resolve_latest(tmp_path, "daily")
    assert resolved.build_id == "b2" and resolved.records == 5
    # The older dataset is still on disk — a submission may have pinned it.
    assert any(jsonl_io.strip_dataset_suffix(p.name) == "b1"
               for p in dataset_feed.list_build_files(tmp_path, "daily"))


def test_resolve_missing_profile_is_none(tmp_path):
    assert dataset_feed.resolve_latest(tmp_path, "nope") is None


def test_resolve_garbled_pointer_is_none(tmp_path):
    _publish(tmp_path)
    pointer = dataset_feed.profile_dir(tmp_path, "daily") / dataset_feed.POINTER_NAME
    pointer.write_text("{not json", encoding="utf-8")
    assert dataset_feed.resolve_latest(tmp_path, "daily") is None


def test_resolve_pointer_to_deleted_dataset_is_none(tmp_path):
    build = _publish(tmp_path)
    os.unlink(build.path)
    assert dataset_feed.resolve_latest(tmp_path, "daily") is None


def test_resolve_size_mismatch_is_none(tmp_path):
    """A pointer that disagrees with the file on disk means someone truncated or
    replaced the dataset — refuse it instead of replaying an unknown file."""
    build = _publish(tmp_path)
    with open(build.path, "a", encoding="utf-8") as handle:
        handle.write('{"n":99}\n')
    assert dataset_feed.resolve_latest(tmp_path, "daily") is None


def test_pointer_escape_attempt_is_rejected(tmp_path):
    _publish(tmp_path)
    pointer = dataset_feed.profile_dir(tmp_path, "daily") / dataset_feed.POINTER_NAME
    data = json.loads(pointer.read_text(encoding="utf-8"))
    data["file"] = "../../../etc/passwd"
    pointer.write_text(json.dumps(data), encoding="utf-8")
    assert dataset_feed.resolve_latest(tmp_path, "daily") is None


@pytest.mark.parametrize("name", ["../escape", "a/b", "UPPER", "", "x" * 100, ".hidden"])
def test_invalid_profile_names_rejected(tmp_path, name):
    with pytest.raises(ValueError):
        dataset_feed.validate_profile(name)
    # resolve_latest must not raise on a bad name — it is reached from a run.
    assert dataset_feed.resolve_latest(tmp_path, name) is None


def test_age_hours(tmp_path):
    built = datetime.now(timezone.utc) - timedelta(hours=5)
    _publish(tmp_path, built_at=built)
    resolved = dataset_feed.resolve_latest(tmp_path, "daily")
    assert 4.9 < resolved.age_hours() < 5.1


def test_provenance_shape(tmp_path):
    """The provenance block is what gets pinned onto a submission run, so its
    keys are effectively a persisted contract."""
    _publish(tmp_path)
    prov = dataset_feed.resolve_latest(tmp_path, "daily").provenance()
    assert set(prov) == {
        "profile", "build_id", "path", "records", "sha256",
        "built_at", "window_start", "window_end",
    }


# --- retention -----------------------------------------------------------------

def test_prune_keeps_newest_and_current(tmp_path):
    for i in range(5):
        _publish(tmp_path, build_id=f"b{i}")
    # Age every file past the retention floor.
    old = time.time() - 86400
    for path in dataset_feed.list_build_files(tmp_path, "daily"):
        os.utime(path, (old, old))
    removed = dataset_feed.prune(tmp_path, "daily", keep=2, min_age_seconds=3600)
    assert set(removed) == {"b0", "b1", "b2"}
    remaining = {jsonl_io.strip_dataset_suffix(p.name)
                 for p in dataset_feed.list_build_files(tmp_path, "daily")}
    assert "b4" in remaining  # current build, pointed at by latest.json
    assert "b3" in remaining  # within `keep`
    assert dataset_feed.resolve_latest(tmp_path, "daily") is not None


def test_prune_respects_age_floor(tmp_path):
    """A build young enough that a submission may have just resolved it is never
    deleted, even if it is beyond `keep`."""
    for i in range(4):
        _publish(tmp_path, build_id=f"b{i}")
    removed = dataset_feed.prune(tmp_path, "daily", keep=1, min_age_seconds=3600)
    assert removed == []


def test_prune_respects_protected_ids(tmp_path):
    for i in range(4):
        _publish(tmp_path, build_id=f"b{i}")
    old = time.time() - 86400
    for path in dataset_feed.list_build_files(tmp_path, "daily"):
        os.utime(path, (old, old))
    removed = dataset_feed.prune(
        tmp_path, "daily", keep=1, min_age_seconds=60, protected={"b0"}
    )
    assert "b0" not in removed
    assert any(jsonl_io.strip_dataset_suffix(p.name) == "b0"
               for p in dataset_feed.list_build_files(tmp_path, "daily"))


# --- pins ----------------------------------------------------------------------

def test_pin_survives_the_build_being_pruned(tmp_path):
    """The whole point: retention deleting builds/<id>.jsonl must not take the
    dataset away from a run that is still reading it. A hard link keeps the
    inode alive, so the pinned path stays readable."""
    build = _publish(tmp_path, build_id="b0", records=4)
    _publish(tmp_path, build_id="b1")  # move the pointer off b0
    pinned = dataset_feed.pin(tmp_path, "daily", build, "s1-r1")
    assert pinned is not None and pinned.is_file()

    old = time.time() - 86400
    for path in dataset_feed.list_build_files(tmp_path, "daily"):
        os.utime(path, (old, old))
    assert "b0" in dataset_feed.prune(tmp_path, "daily", keep=1, min_age_seconds=60)

    assert not os.path.exists(build.path)          # the builds/ name is gone
    assert pinned.is_file()                        # the bytes are not
    with jsonl_io.open_text(pinned) as handle:
        assert len([line for line in handle if line.strip()]) == 4


def test_pin_is_a_link_not_a_copy(tmp_path):
    """A copy would multiply disk use by the number of concurrent replays and
    add a multi-GB read to every run's startup."""
    build = _publish(tmp_path)
    pinned = dataset_feed.pin(tmp_path, "daily", build, "s1-r1")
    assert os.stat(pinned).st_ino == os.stat(build.path).st_ino
    assert os.stat(pinned).st_nlink == 2


def test_pin_is_idempotent(tmp_path):
    build = _publish(tmp_path)
    first = dataset_feed.pin(tmp_path, "daily", build, "s1-r1")
    again = dataset_feed.pin(tmp_path, "daily", build, "s1-r1")
    assert first == again
    assert len(dataset_feed.list_pins(tmp_path, "daily")) == 1


def test_unpin_owner_releases_across_profiles(tmp_path):
    a = _publish(tmp_path, profile="daily")
    b = _publish(tmp_path, profile="weekly")
    dataset_feed.pin(tmp_path, "daily", a, "s1-r1")
    dataset_feed.pin(tmp_path, "weekly", b, "s1-r1")
    dataset_feed.pin(tmp_path, "daily", a, "s2-r9")

    assert dataset_feed.unpin_owner(tmp_path, "s1-r1") == 2
    assert [o for o, _ in dataset_feed.list_pins(tmp_path, "daily")] == ["s2-r9"]
    assert dataset_feed.list_pins(tmp_path, "weekly") == []


def test_unpin_owner_is_safe_when_nothing_pinned(tmp_path):
    assert dataset_feed.unpin_owner(tmp_path, "s1-r1") == 0


def _age_pin(path, seconds: int):
    """Rewrite a pin's name so it reports an older creation time. Its age comes
    from the filename, not the filesystem — see `_pin_age_seconds`."""
    owner, build_id, _ts = path.name[: -len(".jsonl")].rsplit("__", 2)
    aged = path.with_name(f"{owner}__{build_id}__{int(time.time()) - seconds}.jsonl")
    path.rename(aged)
    return aged


def test_sweep_pins_keeps_live_and_young_owners(tmp_path):
    build = _publish(tmp_path)
    for owner in ("s1-r1", "s2-r1", "s3-r1"):
        dataset_feed.pin(tmp_path, "daily", build, owner)
    for owner, path in dataset_feed.list_pins(tmp_path, "daily"):
        if owner != "s3-r1":
            _age_pin(path, 86400)

    swept = dataset_feed.sweep_pins(
        tmp_path, "daily", is_live=lambda o: o == "s1-r1", min_age_seconds=3600,
    )
    assert swept == 1                                   # only s2 (stale + dead)
    owners = {o for o, _ in dataset_feed.list_pins(tmp_path, "daily")}
    assert owners == {"s1-r1", "s3-r1"}                 # live, and too young


def test_pin_age_is_independent_of_the_dataset_mtime(tmp_path):
    """A hard link shares its inode's timestamps, so a pin taken NOW on a build
    written days ago must still read as young — otherwise the sweeper's age
    floor would happily delete a dataset a run just started using."""
    build = _publish(tmp_path)
    old = time.time() - 30 * 86400
    os.utime(build.path, (old, old))
    dataset_feed.pin(tmp_path, "daily", build, "s1-r1")

    swept = dataset_feed.sweep_pins(
        tmp_path, "daily", is_live=lambda _o: False, min_age_seconds=3600,
    )
    assert swept == 0
    assert len(dataset_feed.list_pins(tmp_path, "daily")) == 1


def test_sweep_pins_keeps_unparseable_names(tmp_path):
    build = _publish(tmp_path)
    pinned = dataset_feed.pin(tmp_path, "daily", build, "s1-r1")
    pinned.rename(pinned.with_name("mystery.jsonl"))
    assert dataset_feed.sweep_pins(
        tmp_path, "daily", is_live=lambda _o: False, min_age_seconds=0.0,
    ) == 0


def test_concurrent_pins_on_one_build_cost_one_copy(tmp_path):
    """Saturating the worker pool with replays of the SAME build must not
    multiply disk use — every pin is another name for one inode."""
    build = _publish(tmp_path)
    for i in range(8):
        dataset_feed.pin(tmp_path, "daily", build, f"s{i}-r1")
    inodes = {os.stat(p).st_ino for _o, p in dataset_feed.list_pins(tmp_path, "daily")}
    assert inodes == {os.stat(build.path).st_ino}
    assert os.stat(build.path).st_nlink == 9        # 8 pins + builds/


def test_back_to_back_runs_do_not_leak_datasets(tmp_path):
    """Continuous replay load with no idle window.

    Each run pins whatever build is current when it starts, builds keep
    publishing underneath, and retention runs after every build. The invariants:
    a running pin is never broken, and the number of datasets held alive stays
    bounded by the runs that are actually in flight — it does not grow with the
    number of builds.
    """
    live: dict[str, str] = {}                       # owner -> build_id it pinned
    held: list[int] = []

    for i in range(20):
        build = _publish(tmp_path, build_id=f"b{i:02d}")

        # A run starts on this build; the run started 2 builds ago finishes.
        owner = f"s{i}-r1"
        pinned = dataset_feed.pin(tmp_path, "daily", build, owner)
        assert pinned is not None
        live[owner] = build.build_id
        if i >= 2:
            done = f"s{i - 2}-r1"
            dataset_feed.unpin_owner(tmp_path, done)
            live.pop(done)

        # Retention, with nothing protected and no age floor — the most
        # aggressive setting an admin could choose.
        old = time.time() - 86400
        for path in dataset_feed.list_build_files(tmp_path, "daily"):
            os.utime(path, (old, old))
        dataset_feed.prune(tmp_path, "daily", keep=2, min_age_seconds=0)

        # Every in-flight run can still read its dataset, even ones whose
        # builds/ name retention has already removed.
        for held_owner in live:
            matches = [p for o, p in dataset_feed.list_pins(tmp_path, "daily") if o == held_owner]
            assert len(matches) == 1
            assert matches[0].is_file()
            with jsonl_io.open_text(matches[0]) as handle:
                assert any(line.strip() for line in handle)

        held.append(len({os.stat(p).st_ino
                         for _o, p in dataset_feed.list_pins(tmp_path, "daily")}
                        | {os.stat(p).st_ino
                           for p in dataset_feed.list_build_files(tmp_path, "daily")}))

    # Bounded, not growing with the 20 builds: at most `keep` builds plus the
    # datasets the in-flight runs are holding.
    assert max(held) <= 2 + 2
    assert dataset_feed.resolve_latest(tmp_path, "daily").build_id == "b19"


def test_space_is_reclaimed_once_the_last_pin_goes(tmp_path):
    """A pruned-but-pinned dataset is not a leak — it is deferred. The bytes go
    when the last reader releases."""
    build = _publish(tmp_path, build_id="b0")
    _publish(tmp_path, build_id="b1")
    dataset_feed.pin(tmp_path, "daily", build, "s1-r1")
    dataset_feed.pin(tmp_path, "daily", build, "s2-r1")

    old = time.time() - 86400
    for path in dataset_feed.list_build_files(tmp_path, "daily"):
        os.utime(path, (old, old))
    dataset_feed.prune(tmp_path, "daily", keep=1, min_age_seconds=0)
    assert not os.path.exists(build.path)

    dataset_feed.unpin_owner(tmp_path, "s1-r1")
    remaining = dataset_feed.list_pins(tmp_path, "daily")
    assert len(remaining) == 1 and remaining[0][1].is_file()   # s2 still reading

    dataset_feed.unpin_owner(tmp_path, "s2-r1")
    assert dataset_feed.list_pins(tmp_path, "daily") == []     # bytes released


def test_pin_owner_token_is_sanitized(tmp_path):
    """Owner tokens become filenames; a hostile one must not escape pins/."""
    build = _publish(tmp_path)
    pinned = dataset_feed.pin(tmp_path, "daily", build, "../../etc/passwd")
    assert pinned is not None
    assert pinned.parent == dataset_feed.pins_dir(tmp_path, "daily")
    assert "/" not in pinned.name.split("__")[0]


def test_sweep_staging_removes_only_stale_parts(tmp_path):
    fresh = dataset_feed.prepare_staging(tmp_path, "daily", "fresh")
    fresh.write_text("x", encoding="utf-8")
    stale = dataset_feed.prepare_staging(tmp_path, "daily", "stale")
    stale.write_text("x", encoding="utf-8")
    old = time.time() - 200000
    os.utime(stale, (old, old))
    assert dataset_feed.sweep_staging(tmp_path, "daily", max_age_seconds=86400) == 1
    assert fresh.exists() and not stale.exists()


# --- feed root -----------------------------------------------------------------

def test_feed_root_env_override(tmp_path, monkeypatch):
    """Read at call time, not import time: the platform sets this per-process,
    and the REPLAY_* knobs' import-time behaviour has bitten this codebase before."""
    monkeypatch.setenv("REPLAY_FEED_ROOT", str(tmp_path / "elsewhere"))
    assert dataset_feed.feed_root() == tmp_path / "elsewhere"
    assert dataset_feed.feed_root(tmp_path / "explicit") == tmp_path / "explicit"
