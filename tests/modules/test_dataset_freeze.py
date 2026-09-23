"""Freezing a rolling build into a permanent, static dataset.

The point of the whole feature: a rolling build in `<feed>/<profile>/builds/` is
eventually pruned by retention, so pinning a benchmark to a build path is unsafe —
the file rots out from under it. Freezing promotes a build OUT of the rolling
tree into `<frozen>/<name>.jsonl[.gz]`, which nothing in the rolling machinery
touches. The properties under test are:

  - the frozen file survives the source build being deleted (it is a hard link,
    or a real copy when the filesystem refuses to link);
  - it survives `prune()` run at its most aggressive (frozen lives outside
    `builds/`, so retention never sees it);
  - it re-reads through the exact loader the replay runner uses, so a
    `dataset_source=fixed` benchmark pointed at its path replays it identically;
  - freezing is create-once (an immutable dataset must not be silently replaced);
  - the provenance sidecar records where it came from.
"""
from __future__ import annotations

import json

import pytest

from bench.replay_test import dataset_feed, jsonl_io
from bench.replay_test.log_replay_tool import load_extract_jsonl

RECORDS = [
    {
        "source_file": "f", "line_no": i, "level": "INFO", "timestamp": "",
        "request_id": f"r{i}", "channel_id": None, "token_name": None,
        "request_body": json.dumps(
            {"messages": [{"role": "user", "content": f"你好 {i}"}]}, ensure_ascii=False
        ),
        "request_json": None, "raw_payload": {},
    }
    for i in range(5)
]


def _make_build(tmp_path, profile="daily", build_id="20260804T071349Z", compress=True):
    """Write a published-looking build file under `<feed>/<profile>/builds/`."""
    feed = tmp_path / "auto"
    path = dataset_feed.builds_dir(feed, profile) / f"{build_id}{jsonl_io.dataset_suffix(compress)}"
    path.parent.mkdir(parents=True, exist_ok=True)
    with jsonl_io.open_text(path, "w") as handle:
        handle.write("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in RECORDS))
    return feed, path


@pytest.mark.parametrize("compress", [True, False], ids=["gzip", "plain"])
def test_freeze_survives_source_removal_and_reloads(tmp_path, compress):
    _feed, source = _make_build(tmp_path, compress=compress)
    frozen = tmp_path / "frozen"

    ds = dataset_feed.freeze(
        frozen, "acceptance-q3", source,
        records=len(RECORDS), sha256="deadbeef",
        source_profile="daily", source_build_id="20260804T071349Z",
    )
    # The frozen file keeps the source's codec — readers dispatch on extension.
    assert ds.path.endswith(jsonl_io.dataset_suffix(compress))

    # Deleting the source build must not touch the frozen bytes.
    source.unlink()
    resolved = dataset_feed.resolve_frozen(frozen, "acceptance-q3")
    assert resolved is not None and resolved.path == ds.path

    # And it re-reads through the real loader — the guarantee that a
    # dataset_source=fixed benchmark pointed at this path replays it identically.
    loaded = list(load_extract_jsonl(resolved.path))
    assert len(loaded) == len(RECORDS)
    assert jsonl_io.count_lines(resolved.path) == len(RECORDS)


def test_freeze_survives_aggressive_prune(tmp_path):
    """A frozen dataset lives outside `builds/`, so retention never removes it —
    even prune(keep=0, min_age=0), which deletes every prunable build."""
    feed, source = _make_build(tmp_path)
    frozen = tmp_path / "frozen"
    dataset_feed.freeze(frozen, "keeper", source, records=len(RECORDS))

    # Nuke the rolling build history entirely.
    (dataset_feed.profile_dir(feed, "daily") / dataset_feed.POINTER_NAME).unlink(missing_ok=True)
    removed = dataset_feed.prune(feed, "daily", keep=0, min_age_seconds=0.0)
    assert "20260804T071349Z" in removed  # the source build is gone
    assert not source.exists()

    # The frozen copy is untouched and still replayable.
    resolved = dataset_feed.resolve_frozen(frozen, "keeper")
    assert resolved is not None
    assert list(load_extract_jsonl(resolved.path))


def test_freeze_copy_fallback_when_link_refused(tmp_path, monkeypatch):
    """Some filesystems refuse hard links; the fallback is a real byte-faithful
    copy, not a failure."""
    _feed, source = _make_build(tmp_path, compress=True)
    frozen = tmp_path / "frozen"

    def _no_link(*_a, **_k):
        raise OSError("cross-device link not permitted")

    monkeypatch.setattr(dataset_feed.os, "link", _no_link)
    ds = dataset_feed.freeze(frozen, "copied", source, records=len(RECORDS))

    # A genuine independent copy: same content, and removing the source leaves it.
    assert list(load_extract_jsonl(ds.path))
    source.unlink()
    assert list(load_extract_jsonl(ds.path))
    # No staging leftovers.
    assert not list((frozen / dataset_feed.STAGING_DIRNAME).glob("*.part"))


def test_freeze_is_create_once_across_codecs(tmp_path):
    _feed, gz_source = _make_build(tmp_path, build_id="b1", compress=True)
    _feed2, plain_source = _make_build(tmp_path, build_id="b2", compress=False)
    frozen = tmp_path / "frozen"

    dataset_feed.freeze(frozen, "dup", gz_source)
    # A plain-suffix collision on the same name is still a collision.
    with pytest.raises(FileExistsError):
        dataset_feed.freeze(frozen, "dup", plain_source)


def test_freeze_rejects_unsafe_name(tmp_path):
    _feed, source = _make_build(tmp_path)
    frozen = tmp_path / "frozen"
    for bad in ("../escape", "Has/Slash", "UPPER", "a" * 65, ""):
        with pytest.raises(ValueError):
            dataset_feed.freeze(frozen, bad, source)


def test_freeze_writes_provenance_sidecar(tmp_path):
    _feed, source = _make_build(tmp_path)
    frozen = tmp_path / "frozen"
    dataset_feed.freeze(
        frozen, "traced", source,
        records=len(RECORDS), sha256="abc123",
        source_profile="daily", source_build_id="20260804T071349Z",
        frozen_by="admin",
    )
    meta = json.loads((frozen / "traced.meta.json").read_text(encoding="utf-8"))
    assert meta["source_profile"] == "daily"
    assert meta["source_build_id"] == "20260804T071349Z"
    assert meta["sha256"] == "abc123"
    assert meta["records"] == len(RECORDS)
    assert meta["frozen_by"] == "admin"
    assert meta["frozen_at"]  # stamped


def test_list_and_delete_roundtrip(tmp_path):
    _feed, source = _make_build(tmp_path)
    frozen = tmp_path / "frozen"
    dataset_feed.freeze(frozen, "one", source, records=len(RECORDS),
                        source_build_id="20260804T071349Z")

    listed = dataset_feed.list_frozen(frozen)
    assert [d.name for d in listed] == ["one"]
    assert listed[0].records == len(RECORDS)

    assert dataset_feed.delete_frozen(frozen, "one") is True
    assert dataset_feed.resolve_frozen(frozen, "one") is None
    assert not (frozen / "one.meta.json").exists()
    # Deleting a name that isn't there is a no-op, not an error.
    assert dataset_feed.delete_frozen(frozen, "one") is False


def test_frozen_root_resolution(tmp_path, monkeypatch):
    monkeypatch.delenv("REPLAY_FEED_ROOT", raising=False)
    monkeypatch.delenv("REPLAY_FROZEN_ROOT", raising=False)

    # Explicit env wins.
    monkeypatch.setenv("REPLAY_FROZEN_ROOT", str(tmp_path / "explicit"))
    assert dataset_feed.frozen_root() == tmp_path / "explicit"

    # Otherwise it is a `frozen/` sibling of the feed root.
    monkeypatch.delenv("REPLAY_FROZEN_ROOT", raising=False)
    monkeypatch.setenv("REPLAY_FEED_ROOT", str(tmp_path / "d" / "replay" / "auto"))
    assert dataset_feed.frozen_root() == tmp_path / "d" / "replay" / "frozen"
