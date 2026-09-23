"""Collector-side freeze: locating a build file and promoting it to a frozen
dataset. Pure-logic — the Collector object is constructed but never started, so
no threads, no DB, no log store are touched.
"""
from __future__ import annotations

import json

import pytest

from bench.replay_test import dataset_feed, jsonl_io
from app.datasets import service


def _make_build(tmp_path, profile="daily", build_id="20260804T071349Z", compress=True):
    path = dataset_feed.builds_dir(tmp_path / "auto", profile) / \
        f"{build_id}{jsonl_io.dataset_suffix(compress)}"
    path.parent.mkdir(parents=True, exist_ok=True)
    with jsonl_io.open_text(path, "w") as handle:
        for i in range(3):
            handle.write(json.dumps({
                "source_file": "f", "line_no": i, "level": "INFO",
                "timestamp": "2026-08-04T07:13:49Z", "request_id": f"r{i}",
                "request_body": json.dumps({"messages": []}),
                "request_json": None, "raw_payload": {},
            }) + "\n")
    return path


@pytest.fixture
def roots(tmp_path, monkeypatch):
    monkeypatch.setenv("REPLAY_FEED_ROOT", str(tmp_path / "auto"))
    monkeypatch.setenv("REPLAY_FROZEN_ROOT", str(tmp_path / "frozen"))
    return tmp_path


def test_find_build_file_locates_and_misses(roots):
    source = _make_build(roots, compress=True)
    assert service.find_build_file("daily", "20260804T071349Z") == source
    assert service.find_build_file("daily", "does-not-exist") is None
    assert service.find_build_file("other-profile", "20260804T071349Z") is None


def test_collector_freeze_end_to_end(roots):
    _make_build(roots)
    out = service.Collector().freeze(
        name="acceptance-q3", profile="daily", build_id="20260804T071349Z",
        records=3, sha256="abc123", frozen_by="admin",
    )
    assert out["name"] == "acceptance-q3"
    assert out["path"].endswith("acceptance-q3.jsonl.gz")
    assert out["records"] == 3
    assert out["source_profile"] == "daily"
    assert out["source_build_id"] == "20260804T071349Z"
    assert out["frozen_by"] == "admin"
    # The file really landed under the frozen root and is readable.
    from bench.replay_test.log_replay_tool import load_extract_jsonl
    assert len(list(load_extract_jsonl(out["path"]))) == 3


def test_collector_freeze_missing_build_raises(roots):
    with pytest.raises(FileNotFoundError):
        service.Collector().freeze(name="x", profile="daily", build_id="gone")


def test_collector_freeze_duplicate_raises(roots):
    _make_build(roots)
    c = service.Collector()
    c.freeze(name="dup", profile="daily", build_id="20260804T071349Z")
    with pytest.raises(FileExistsError):
        c.freeze(name="dup", profile="daily", build_id="20260804T071349Z")


def test_collector_freeze_bad_name_raises(roots):
    _make_build(roots)
    with pytest.raises(ValueError):
        service.Collector().freeze(name="Bad/Name", profile="daily",
                                   build_id="20260804T071349Z")
