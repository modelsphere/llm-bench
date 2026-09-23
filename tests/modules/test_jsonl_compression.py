"""Gzipped replay datasets must be indistinguishable from plain ones.

Collected datasets are stored `.jsonl.gz` (~3.6x smaller on this kind of JSON,
measured on a real build: 9.60MB → 2.64MB). Every reader dispatches on the
extension, so the property under test is *equivalence*: the same records, the
same count, through the same loader the replay runner uses — including the
sizing pass, whose count MUST equal what the iterator yields or the runner
reports phantom not-started requests.
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
        # Non-ASCII on purpose: these datasets are full of CJK prompts, and a
        # mis-set encoding would only show up on one of the two paths.
        "request_body": json.dumps(
            {"messages": [{"role": "user", "content": f"你好 {i}"}]}, ensure_ascii=False
        ),
        "request_json": None, "raw_payload": {},
    }
    for i in range(5)
]


def write_dataset(path, records=RECORDS, trailing_blank=False):
    body = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records)
    if trailing_blank:
        body += "\n\n"
    # Written through the same codec rule the builder uses, so a staging name
    # (`.jsonl.gz.part`) is handled here exactly as it is in production.
    with jsonl_io.open_text(path, "w") as handle:
        handle.write(body)
    return path


@pytest.fixture(params=[".jsonl", ".jsonl.gz"], ids=["plain", "gzip"])
def dataset(request, tmp_path):
    return write_dataset(tmp_path / f"data{request.param}")


# --- equivalence ----------------------------------------------------------------

def test_loader_reads_both_forms_identically(dataset):
    loaded = list(load_extract_jsonl(str(dataset), lean=True))
    assert len(loaded) == len(RECORDS)
    assert [r.request_id for r in loaded] == [r["request_id"] for r in RECORDS]
    assert "你好 0" in loaded[0].request_body


def test_count_lines_matches_the_loader(dataset):
    """The equality the replay runner's result bookkeeping depends on."""
    assert jsonl_io.count_lines(dataset) == len(list(load_extract_jsonl(str(dataset))))


def test_count_lines_ignores_blank_lines(tmp_path):
    for suffix in (".jsonl", ".jsonl.gz"):
        path = write_dataset(tmp_path / f"blank{suffix}", trailing_blank=True)
        assert jsonl_io.count_lines(path) == len(RECORDS)
        assert len(list(load_extract_jsonl(str(path)))) == len(RECORDS)


def test_staging_name_uses_the_same_codec_as_its_published_name(tmp_path):
    """A build is staged as `<id>.jsonl.gz.part` and published by RENAME, so the
    staging name must resolve to the same codec — otherwise plain text gets
    published under a .gz name and every reader fails on it."""
    assert jsonl_io.is_compressed("b1.jsonl.gz.part") is True
    assert jsonl_io.is_compressed("b1.jsonl.part") is False
    assert jsonl_io.is_compressed("b1.jsonl.gz") is True
    assert jsonl_io.is_compressed("b1.jsonl") is False


def test_gzip_is_actually_smaller(tmp_path):
    plain = write_dataset(tmp_path / "a.jsonl", RECORDS * 200)
    packed = write_dataset(tmp_path / "a.jsonl.gz", RECORDS * 200)
    assert packed.stat().st_size < plain.stat().st_size / 2


# --- suffix handling ---------------------------------------------------------------

def test_strip_dataset_suffix_handles_the_double_extension():
    """Path.stem would leave `b1.jsonl` for a gzipped build — which would break
    every build-id comparison retention makes."""
    assert jsonl_io.strip_dataset_suffix("20260804T071349Z.jsonl.gz") == "20260804T071349Z"
    assert jsonl_io.strip_dataset_suffix("20260804T071349Z.jsonl") == "20260804T071349Z"
    assert jsonl_io.dataset_suffix(True) == ".jsonl.gz"
    assert jsonl_io.dataset_suffix(False) == ".jsonl"


# --- the feed layout ----------------------------------------------------------------

@pytest.mark.parametrize("compress", [True, False])
def test_feed_publishes_and_resolves_either_form(tmp_path, compress):
    staged = dataset_feed.prepare_staging(tmp_path, "daily", "b1", compress=compress)
    assert str(staged).endswith(".jsonl.gz.part" if compress else ".jsonl.part")
    # The staged name already carries its final extension, so writing it is the
    # same code path as writing a published dataset — publish only renames.
    write_dataset(staged)

    build = dataset_feed.publish(
        tmp_path, "daily", staged, build_id="b1", records=len(RECORDS), sha256="x",
    )
    assert build.path.endswith(".jsonl.gz" if compress else ".jsonl")

    resolved = dataset_feed.resolve_latest(tmp_path, "daily")
    assert resolved is not None and resolved.path == build.path
    assert len(list(load_extract_jsonl(resolved.path, lean=True))) == len(RECORDS)


def test_retention_matches_build_ids_on_compressed_builds(tmp_path):
    """`Path.stem` on `b1.jsonl.gz` yields `b1.jsonl`, which would silently make
    every protected-build check miss."""
    for build_id in ("b0", "b1"):
        staged = dataset_feed.prepare_staging(tmp_path, "daily", build_id, compress=True)
        write_dataset(staged)
        dataset_feed.publish(tmp_path, "daily", staged, build_id=build_id,
                             records=len(RECORDS), sha256="x")

    assert [p.name for p in dataset_feed.list_build_files(tmp_path, "daily")] == [
        "b1.jsonl.gz", "b0.jsonl.gz",
    ]
    # b0 is protected by id — it must survive despite being outside `keep`.
    assert dataset_feed.prune(
        tmp_path, "daily", keep=1, min_age_seconds=0, protected={"b0"},
    ) == []


def test_pin_keeps_the_compressed_extension(tmp_path):
    staged = dataset_feed.prepare_staging(tmp_path, "daily", "b1", compress=True)
    write_dataset(staged)
    build = dataset_feed.publish(tmp_path, "daily", staged, build_id="b1",
                                 records=len(RECORDS), sha256="x")

    pinned = dataset_feed.pin(tmp_path, "daily", build, "s1-r1")
    # A pin whose name lost the .gz would be opened as plain text and explode.
    assert pinned.name.endswith(".jsonl.gz")
    assert len(list(load_extract_jsonl(str(pinned), lean=True))) == len(RECORDS)
    assert [o for o, _ in dataset_feed.list_pins(tmp_path, "daily")] == ["s1-r1"]
    assert dataset_feed.unpin_owner(tmp_path, "s1-r1") == 1
