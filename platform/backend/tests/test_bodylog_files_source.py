"""The bodylog-files source: collecting a replay dataset straight from the files
the gateway's bodylog listener writes, with no log store in between.

Laid out exactly as the listener lays them out: `<root>/YYYY-MM-DD/HH.jsonl`
for the live day, and `<root>/YYYY-MM-DD.tar.gz` once a day has rolled. Records
carry the model in `resp_meta.model` (and in the request body), not at the top
level — the one real difference from what VictoriaLogs returns.

Pure filesystem: no database, no network.
"""
from __future__ import annotations

import gzip
import io
import json
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.datasets.builder import build_dataset, profile_from_dict
from app.datasets.errors import LogSourceError
from app.datasets.sources import BodylogFilesSource, matches, source_for
from app.datasets.victorialogs import LogFilters
from app.schemas.replay_datasets import source_errors

UTC = timezone.utc
REQ = {"model": "glm-5", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 64}


def record(i: int, ts: datetime, **overrides) -> dict:
    """One listener line: meta + bodies, as `lua/bodylog.lua` writes it."""
    rec = {
        "ts": ts.isoformat(),
        "request_id": f"r{i}",
        "uri": "/v1/chat/completions",
        "status": 200,
        "peer": "198.51.100.20:8052",
        "req_headers": {"host": "gateway", "authorization": "Bearer secret"},
        "req_body_truncated": False,
        "req_body": json.dumps(REQ),
        "resp_body": "answer",
        "resp_meta": {"model": "glm-5", "usage": {"prompt_tokens": 14484, "completion_tokens": 8}},
    }
    rec.update(overrides)
    return rec


def write_hour(root: Path, hour: datetime, records: list[dict], *, gz: bool = False) -> None:
    day = root / hour.strftime("%Y-%m-%d")
    day.mkdir(parents=True, exist_ok=True)
    body = "".join(json.dumps(r) + "\n" for r in records)
    if gz:
        (day / f"{hour:%H}.jsonl.gz").write_bytes(gzip.compress(body.encode()))
    else:
        (day / f"{hour:%H}.jsonl").write_text(body)


def roll_day(root: Path, day: datetime, hours: dict[int, list[dict]]) -> None:
    """A finished day, archived the way the listener's housekeeping does it."""
    with tarfile.open(root / f"{day:%Y-%m-%d}.tar.gz", "w:gz") as tar:
        for hh, recs in hours.items():
            data = "".join(json.dumps(r) + "\n" for r in recs).encode()
            info = tarfile.TarInfo(f"{day:%Y-%m-%d}/{hh:02d}.jsonl")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))


T0 = datetime(2026, 8, 4, 10, 0, tzinfo=UTC)


# --- which files a window reads ------------------------------------------------


def test_reads_the_hours_the_window_covers_and_trims_by_timestamp(tmp_path):
    write_hour(tmp_path, T0, [record(0, T0 + timedelta(minutes=5)),
                              record(1, T0 + timedelta(minutes=50))])
    write_hour(tmp_path, T0 + timedelta(hours=1), [record(2, T0 + timedelta(minutes=70))])
    write_hour(tmp_path, T0 + timedelta(hours=2), [record(3, T0 + timedelta(minutes=130))])

    src = BodylogFilesSource(tmp_path)
    got = list(src.iter_window(LogFilters(), T0 + timedelta(minutes=30), T0 + timedelta(minutes=90)))
    # The 10:00 file is opened because the window starts inside that hour, and its
    # 10:05 record is then trimmed by its own `ts`. The 12:00 file is never opened.
    assert [r["request_id"] for r in got] == ["r1", "r2"]


def test_reads_gzipped_hour_files(tmp_path):
    write_hour(tmp_path, T0, [record(0, T0 + timedelta(minutes=1))], gz=True)
    got = list(BodylogFilesSource(tmp_path).iter_window(LogFilters(), T0, T0 + timedelta(hours=1)))
    assert len(got) == 1


def test_reads_a_day_that_has_been_rolled_into_an_archive(tmp_path):
    day = T0 - timedelta(days=1)
    roll_day(tmp_path, day, {10: [record(0, day + timedelta(minutes=3))],
                             11: [record(1, day + timedelta(minutes=63))]})
    got = list(BodylogFilesSource(tmp_path).iter_window(
        LogFilters(), day, day + timedelta(hours=2)))
    assert [r["request_id"] for r in got] == ["r0", "r1"]


def test_file_names_follow_the_listeners_timezone(tmp_path):
    """The listener names files in ITS zone. A UTC window of 02:00-03:00 is the
    file `10.jsonl` for a listener running on +08:00."""
    shanghai = timezone(timedelta(hours=8))
    ts = datetime(2026, 8, 4, 10, 30, tzinfo=shanghai)       # 02:30 UTC
    write_hour(tmp_path, ts.replace(minute=0), [record(0, ts)])
    window = (datetime(2026, 8, 4, 2, tzinfo=UTC), datetime(2026, 8, 4, 3, tzinfo=UTC))

    assert list(BodylogFilesSource(tmp_path, "UTC").iter_window(LogFilters(), *window)) == []
    got = list(BodylogFilesSource(tmp_path, "Asia/Shanghai").iter_window(LogFilters(), *window))
    assert len(got) == 1


def test_a_missing_hour_is_quiet_and_a_missing_root_is_loud(tmp_path):
    assert list(BodylogFilesSource(tmp_path).iter_window(LogFilters(), T0, T0 + timedelta(hours=3))) == []
    with pytest.raises(LogSourceError, match="does not exist"):
        list(BodylogFilesSource(tmp_path / "nope").iter_window(LogFilters(), T0, T0 + timedelta(hours=1)))


def test_unreadable_lines_are_skipped_not_fatal(tmp_path):
    day = tmp_path / f"{T0:%Y-%m-%d}"
    day.mkdir()
    (day / f"{T0:%H}.jsonl").write_text(
        "not json\n\n" + json.dumps(record(0, T0 + timedelta(minutes=1))) + "\n")
    assert len(list(BodylogFilesSource(tmp_path).iter_window(LogFilters(), T0, T0 + timedelta(hours=1)))) == 1


# --- filters: the same meaning VictoriaLogs gives them ----------------------------


def test_model_is_found_where_the_listener_puts_it():
    ts = T0
    from_meta = record(0, ts)
    from_body = record(1, ts, resp_meta={"usage": {}})
    other = record(2, ts, resp_meta={"model": "glm-5.2"}, req_body=json.dumps({**REQ, "model": "glm-5.2"}))
    f = LogFilters(models=["glm-5"])
    assert matches(from_meta, f)
    assert matches(from_body, f)
    assert not matches(other, f)          # an exact match: glm-5 is not glm-5.2


def test_backend_filter_accepts_either_spelling_of_the_same_backend():
    """`forwarded_to` is only recorded behind a router; otherwise it is `peer`,
    and either may or may not carry a scheme."""
    f = LogFilters(forwarded_to=["http://198.51.100.20:8052/"])
    assert matches(record(0, T0), f)                                       # peer only
    assert matches(record(0, T0, forwarded_to="198.51.100.20:8052"), f)
    assert not matches(record(0, T0, peer="198.51.100.21:8052"), f)


def test_status_uri_and_truncation_filters():
    assert not matches(record(0, T0, status=429), LogFilters())
    assert matches(record(0, T0, uri="/v1/chat/completions?x=1"), LogFilters())
    assert not matches(record(0, T0, uri="/v1/embeddings"), LogFilters())
    assert not matches(record(0, T0, req_body_truncated=True), LogFilters())
    assert matches(record(0, T0, req_body_truncated=True), LogFilters(exclude_truncated=False))


def test_extra_logsql_is_refused_rather_than_ignored(tmp_path):
    with pytest.raises(LogSourceError, match="extra_logsql"):
        list(BodylogFilesSource(tmp_path).iter_window(
            LogFilters(extra='req_headers.x-app:="cli"'), T0, T0 + timedelta(hours=1)))


def test_probe_counts_matches_and_shows_a_sample(tmp_path):
    write_hour(tmp_path, T0, [record(i, T0 + timedelta(minutes=i)) for i in range(4)])
    out = BodylogFilesSource(tmp_path).probe(LogFilters(models=["glm-5"]), T0, T0 + timedelta(hours=1))
    assert out["matched"] == 4
    assert out["sample_prompt_tokens"] == 14484
    assert "resp_meta" in out["sample_fields"]


# --- the whole build, from files --------------------------------------------------


def test_a_build_from_listener_files_publishes_a_dataset(tmp_path):
    root, feed = tmp_path / "bodylog", tmp_path / "feed"
    now = datetime(2026, 8, 4, 13, 30, tzinfo=UTC)
    for h in range(3):
        hour = datetime(2026, 8, 4, 10 + h, tzinfo=UTC)
        write_hour(root, hour, [record(h * 10 + i, hour + timedelta(minutes=i)) for i in range(10)])

    profile = profile_from_dict({
        "name": "daily", "models": ["glm-5"], "window_hours": 3, "window_timezone": "UTC",
        "subwindow_minutes": 60, "sample_size": 6, "min_records": 1, "min_buckets": 1,
        "clean": False, "keep_builds": 5, "min_retain_hours": 0,
    })
    result = build_dataset(BodylogFilesSource(root), profile, feed_root=feed, now=now, seed=1)
    assert result.status == "ready", result.error
    assert result.build.records == 6


# --- construction and validation ------------------------------------------------


def test_source_for_builds_each_kind(tmp_path):
    assert isinstance(source_for("bodylog_files", f"file://{tmp_path}"), BodylogFilesSource)
    assert isinstance(source_for("", str(tmp_path)), BodylogFilesSource)      # the default
    with pytest.raises(LogSourceError, match="absolute"):
        source_for("bodylog_files", "relative/dir")
    with pytest.raises(LogSourceError, match="unknown source type"):
        source_for("kafka", "x")


@pytest.mark.parametrize("kind,url,extra,ok", [
    ("bodylog_files", "file:///data/bodylog", None, True),
    ("bodylog_files", "/data/bodylog", None, True),
    ("bodylog_files", "http://vl:9428", None, False),
    ("bodylog_files", "file:///data/bodylog", 'x:="y"', False),
    ("victorialogs", "http://vl:9428", 'x:="y"', True),
    ("victorialogs", "file:///data/bodylog", None, False),
])
def test_source_errors_explain_a_mismatched_source(kind, url, extra, ok):
    assert (source_errors(kind, url, extra) == []) is ok
