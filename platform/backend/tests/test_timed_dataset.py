"""Timed datasets (app.datasets.timed + the builder's `layout: timed`): one
window of traffic kept whole, in time order, with sessions and send times."""
from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone

import pytest

from app.datasets import timed
from app.datasets.builder import BuildProfile, build_dataset
from bench.replay_test import dataset_feed
from bench.replay_test.log_replay_tool import load_extract_jsonl

T0 = datetime(2026, 11, 12, 0, 0, tzinfo=timezone.utc).timestamp()


# --- choosing the window -----------------------------------------------------------


def test_the_window_is_the_one_at_typical_load():
    rng = random.Random(1)
    spans = []
    t = T0
    # A quiet stretch (one at a time), then a busier one (about three at once).
    for _ in range(300):
        t += 10
        spans.append((t, t + 1.0))
    for _ in range(300):
        t += 1
        spans.append((t, t + 3.0 + rng.uniform(-0.2, 0.2)))
    busy = timed.choose_window(spans, 100, c_target=3.0)
    assert busy["start"] > T0 + 2500 and 2.5 < busy["c_req_median"] < 3.6
    quiet = timed.choose_window(spans, 100, c_target=1.0)
    assert quiet["start"] < T0 + 3000 and quiet["c_req_median"] < 1.2
    whole = timed.choose_window(spans[:50], 100)
    assert whole["requests"] == 50 and whole["start"] == spans[0][0]


# --- sessions ----------------------------------------------------------------------


def _msgs(*turns):
    out = []
    for i, text in enumerate(turns):
        out.append({"role": "user", "content": text})
        if i < len(turns) - 1:
            out.append({"role": "assistant", "content": f"answer to {text}"})
    return out


def test_sessions_by_prefix_and_by_field():
    rows = [
        timed.Row(start=T0, rt=2.0, messages=_msgs("a")),
        timed.Row(start=T0 + 1, rt=1.0, messages=_msgs("x")),                 # unrelated
        timed.Row(start=T0 + 5, rt=2.0, messages=_msgs("a", "b")),            # follows row 0
        timed.Row(start=T0 + 6, rt=1.0, session="s9", messages=_msgs("q")),
        timed.Row(start=T0 + 6.5, rt=1.0, session="s9", messages=_msgs("r")),  # s9's first still running
        timed.Row(start=T0 + 9, rt=1.0, session="s9", messages=_msgs("t")),
    ]
    sources = timed.link_sessions(rows)
    assert sources == {"field": 3, "prefix": 1, "none": 2}
    t = [r.timing for r in rows]
    assert t[2]["after"] == 0 and t[2]["think_s"] == pytest.approx(3.0) and t[2]["turn"] == 2
    assert t[0]["session"] == t[2]["session"] != "" and t[1]["session"] == "" and t[1]["after"] is None
    assert t[3]["session"] == "s9" and t[3]["after"] is None
    assert t[4]["after"] is None and t[4]["turn"] == 2       # sent while turn 1 was running
    assert t[5]["after"] == 4 and t[5]["think_s"] == pytest.approx(1.5)


def test_rows_with_only_hashes_link_as_rows_with_messages():
    # The builder keeps message_hashes, not the messages: a window of long
    # agent requests does not fit in memory as message lists.
    def rows(hashed):
        out = [timed.Row(start=T0 + i, rt=0.5, messages=m) for i, m in enumerate(
            [_msgs("a"), _msgs("x"), _msgs("a", "b"), _msgs("a", "b", "c")])]
        if hashed:
            for r in out:
                r.hashes, r.messages = timed.message_hashes(r.messages), None
        return out
    plain, hashed = rows(False), rows(True)
    assert timed.link_sessions(plain) == timed.link_sessions(hashed) == {"field": 0, "prefix": 2, "none": 2}
    assert [r.timing for r in plain] == [r.timing for r in hashed]


def test_prefix_matching_reaches_past_tool_results():
    first = [{"role": "user", "content": "go"}]
    second = first + [{"role": "assistant", "content": None, "tool_calls": [{"id": "1"}]},
                      {"role": "tool", "content": "r1"}, {"role": "tool", "content": "r2"}]
    rows = [timed.Row(start=T0, rt=1, messages=first), timed.Row(start=T0 + 2, rt=1, messages=second)]
    timed.link_sessions(rows)
    assert rows[1].timing["after"] == 0


# --- idle gaps ---------------------------------------------------------------------


def test_idle_gaps_are_shortened_and_nothing_else_moves():
    rows = [timed.Row(start=T0, rt=2.0), timed.Row(start=T0 + 1, rt=2.0),     # overlap
            timed.Row(start=T0 + 63, rt=1.0),                                  # 60 s idle before
            timed.Row(start=T0 + 64, rt=1.0)]
    timed.link_sessions(rows)
    g = timed.remove_gaps(rows, 5.0)
    assert [r.timing["at_s"] for r in rows] == [0.0, 1.0, 8.0, 9.0]
    assert g == {"window_s": 65.0, "replay_s": 10.0, "removed_s": 55.0, "gaps": 1,
                 "think_removed_s": 0.0}


def test_a_later_turn_thinks_on_the_shortened_timeline():
    # One session: turn 1 ends at 2 s, the user comes back 60 s later. The
    # replay sends turn 2 `think_s` after turn 1 ends on the engine, so the
    # cut must show there too, or the 55 s come back in full.
    rows = [timed.Row(start=T0, rt=2.0, session="a"), timed.Row(start=T0 + 62, rt=1.0, session="a")]
    timed.link_sessions(rows)
    assert rows[1].timing["think_s"] == pytest.approx(60.0)
    g = timed.remove_gaps(rows, 5.0)
    t = rows[1].timing
    assert t["at_s"] == 7.0
    assert t["think_s"] == pytest.approx(5.0) and t["think_s_logged"] == pytest.approx(60.0)
    assert g["think_removed_s"] == pytest.approx(55.0)
    # A short think, under the idle limit, is left as production had it.
    rows = [timed.Row(start=T0, rt=2.0, session="b"), timed.Row(start=T0 + 3, rt=1.0, session="b")]
    timed.link_sessions(rows)
    timed.remove_gaps(rows, 5.0)
    assert rows[1].timing["think_s"] == pytest.approx(1.0) == rows[1].timing["think_s_logged"]


# --- the builder -------------------------------------------------------------------


class _Source:
    """Yields nested bodylog records; timing rows are the same records."""
    timing_is_cheap = True

    def __init__(self, records):
        self.records = records

    def _between(self, start, end):
        for r in self.records:
            ts = datetime.fromisoformat(r["ts"]).timestamp()
            if start.timestamp() <= ts < end.timestamp():
                yield r

    def iter_timing(self, filters, start, end):
        return self._between(start, end)

    def iter_window(self, filters, start, end, *, limit=0):
        return self._between(start, end)

    def describe(self, *a, **k):
        return "fake"

    def close(self):
        pass


def _record(i, t, rt, messages, session=""):
    return {
        "request_id": f"r{i}", "ts": datetime.fromtimestamp(t, timezone.utc).isoformat(), "rt": rt,
        "status": 200, "uri": "/v1/chat/completions", "forwarded_to": "http://192.0.2.1:8000",
        "req_body": json.dumps({"model": "m", "messages": messages, "max_tokens": 64}),
        "req_headers": {"x-session": session} if session else {},
        "resp_meta": {"usage": {"prompt_tokens": 1000 + 10 * i, "completion_tokens": 50}},
    }


def test_a_timed_build_keeps_the_window_whole_in_order(tmp_path):
    now = datetime(2026, 11, 13, 0, 0, tzinfo=timezone.utc)
    start = now.timestamp() - 6 * 3600
    rng = random.Random(4)
    recs, t, i = [], start, 0
    # Sessions of three turns: each turn sent 2 s after the previous ended.
    for s in range(60):
        t += rng.uniform(20, 200)
        msgs, tt = [], t
        for turn in range(3):
            msgs = msgs + ([{"role": "assistant", "content": "ok"}] if msgs else []) + \
                [{"role": "user", "content": f"s{s} t{turn}"}]
            rt = rng.uniform(1, 4)
            recs.append(_record(i, tt, rt, list(msgs), session=f"S{s}" if s % 2 else ""))
            i += 1
            tt += rt + 2.0
    profile = BuildProfile(name="timed", layout="timed", sample_size=120, window_hours=24,
                           min_records=10, min_buckets=0, session_field="req_headers.x-session",
                           max_idle_gap_s=5.0, load_min_group=20)
    result = build_dataset(_Source(recs), profile, feed_root=tmp_path, now=now, seed=1)
    assert result.status == "ready", result.error
    summary = result.build.stats["timed"]
    assert summary["requests"] == result.build.records == 120
    assert summary["session_source"]["field"] > 0 and summary["session_source"]["prefix"] > 0
    assert summary["following_share"] > 0.6
    assert summary["replay_s"] < summary["window_s"] and summary["gaps"] > 0
    assert summary["missing_share"] == 0 and summary["missing"] == {"not_fetched": 0}
    assert 40 <= summary["sessions"] <= 42 and summary["turns_p50"] == 3

    path = dataset_feed.resolve_latest(tmp_path, "timed").path
    loaded = list(load_extract_jsonl(path, lean=True))
    at = [m.timing["at_s"] for m in loaded]
    assert at == sorted(at) and at[0] == 0.0
    for k, m in enumerate(loaded):
        after = m.timing["after"]
        if after is not None:
            assert after < k and m.timing["think_s"] == pytest.approx(2.0, abs=0.01)
            assert m.timing["session"] == loaded[after].timing["session"]


def test_a_sampled_build_has_no_timing(tmp_path):
    now = datetime(2026, 11, 13, 0, 0, tzinfo=timezone.utc)
    recs = [_record(i, now.timestamp() - 3600 + 10 * i, 1.0, _msgs(f"q{i}")) for i in range(50)]
    profile = BuildProfile(name="s", sample_size=20, window_hours=2, subwindow_minutes=60,
                           min_records=5, min_buckets=0, load_window_hours=0)
    result = build_dataset(_Source(recs), profile, feed_root=tmp_path, now=now, seed=1)
    assert result.status == "ready", result.error
    assert "timed" not in result.build.stats
    path = dataset_feed.resolve_latest(tmp_path, "s").path
    assert all(m.timing is None for m in load_extract_jsonl(path, lean=True))


def test_a_broken_image_is_dropped_when_sampled_and_stood_in_for_when_timed():
    from collections import Counter

    from bench.replay_test.bodylog_convert import PLACEHOLDER_IMAGE_URI, convert_record

    messages = [{"role": "user", "content": [
        {"type": "text", "text": "what is this"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAAcut-short"}}]}]
    rec = _record(0, T0, 1.0, messages)
    record, reason = convert_record(rec, source_file="t", line_no=1, clean=True, budget=100_000)
    assert record is None and reason == "bad_image_data_uri"
    fixes = Counter()
    record, reason = convert_record(rec, source_file="t", line_no=1, clean=True, budget=100_000,
                                    fixes=fixes, bad_images="placeholder")
    assert reason is None and fixes["image_placeholder"] == 1
    sent = json.loads(record["request_body"])["messages"][0]["content"]
    assert sent[1]["image_url"]["url"] == PLACEHOLDER_IMAGE_URI and sent[0]["text"] == "what is this"


def _sessions(start, n=60, seed=4):
    rng = random.Random(seed)
    recs, t, i = [], start, 0
    for s in range(n):
        t += rng.uniform(20, 200)
        msgs, tt = [], t
        for turn in range(3):
            msgs = msgs + ([{"role": "assistant", "content": "ok"}] if msgs else []) + \
                [{"role": "user", "content": f"s{s} t{turn}"}]
            rt = rng.uniform(1, 4)
            recs.append(_record(i, tt, rt, list(msgs)))
            i += 1
            tt += rt + 2.0
    return recs


class _PiecesSource(_Source):
    """Records each window read; can lose all but every `keep`-th record."""

    def __init__(self, records, keep=1):
        super().__init__(records)
        self.reads, self.keep = [], keep

    def iter_window(self, filters, start, end, *, limit=0):
        self.reads.append((start, end))
        return (r for r in self._between(start, end) if int(r["request_id"][1:]) % self.keep == 0)


def test_a_timed_window_is_read_in_short_pieces(tmp_path):
    """The store cuts one long query at its time limit: the window's records
    are read in pieces of at most TIMED_FETCH_SECONDS, end to end."""
    from app.datasets import builder

    now = datetime(2026, 11, 13, 0, 0, tzinfo=timezone.utc)
    source = _PiecesSource(_sessions(now.timestamp() - 6 * 3600))
    profile = BuildProfile(name="timed", layout="timed", sample_size=120, window_hours=24,
                           min_records=10, min_buckets=0, max_idle_gap_s=5.0, load_min_group=20)
    result = build_dataset(source, profile, feed_root=tmp_path, now=now, seed=1)
    assert result.status == "ready", result.error
    assert result.build.records == 120 and result.build.stats["timed"]["missing_share"] == 0
    assert len(source.reads) > 1
    assert all((b - a).total_seconds() <= builder.TIMED_FETCH_SECONDS for a, b in source.reads)
    assert all(source.reads[k][1] == source.reads[k + 1][0] for k in range(len(source.reads) - 1))


def test_a_timed_build_missing_most_of_its_window_is_not_published(tmp_path):
    now = datetime(2026, 11, 13, 0, 0, tzinfo=timezone.utc)
    source = _PiecesSource(_sessions(now.timestamp() - 6 * 3600), keep=4)
    profile = BuildProfile(name="timed", layout="timed", sample_size=120, window_hours=24,
                           min_records=10, min_buckets=0, max_idle_gap_s=5.0, load_min_group=20)
    result = build_dataset(source, profile, feed_root=tmp_path, now=now, seed=1)
    assert result.status != "ready"
    assert "could be read" in (result.error or "") and "keeping the previous build" in result.error


def test_time_pieces_cover_the_span_end_to_end():
    from app.datasets.builder import time_pieces

    t0 = datetime(2026, 11, 13, tzinfo=timezone.utc)
    pieces = time_pieces(t0, t0 + timedelta(seconds=95), 30)
    assert [(a - t0).total_seconds() for a, _ in pieces] == [0, 30, 60, 90]
    assert pieces[-1][1] == t0 + timedelta(seconds=95)
    assert time_pieces(t0, t0, 30) == []
