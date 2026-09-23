"""Unit tests for the rolling-dataset collector: query building, windowing,
sampling, publication gates, and the worker-side resolution that pins a build
onto a run. Pure-logic — no log store, no DB, no Redis (the VictoriaLogs client
is driven through a fake that yields rows).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from bench.replay_test import dataset_feed, jsonl_io
from app.datasets.builder import (
    MAX_CARRY_MULTIPLE,
    BuildProfile,
    _Reservoir,
    build_dataset,
    profile_from_dict,
    window_bounds,
)
from app.datasets.victorialogs import LogFilters, build_query, escape_value

REQ_BODY = json.dumps({"model": "glm-5", "messages": [{"role": "user", "content": "hi"}],
                       "max_tokens": 64})


def vl_row(i: int = 0, prompt_tokens: int = 14484, **overrides) -> dict:
    row = {
        "_time": "2026-08-04T05:32:00.084Z",
        "model": "glm-5",
        "status": "200",
        "forwarded_to": "http://198.51.100.20:8052",
        "uri": "/v1/chat/completions",
        "ts": "2026-08-04T13:32:00.084+08:00",
        "request_id": f"req-{i}",
        "req_body": REQ_BODY,
        "req_body_truncated": "false",
        "req_headers.host": "gateway",
        "req_headers.authorization": "Bearer secret",
        "resp_body": "answer",
        "resp_meta.usage.prompt_tokens": str(prompt_tokens),
    }
    row.update(overrides)
    return row


class FakeClient:
    """Stands in for VictoriaLogsClient: records queries, yields canned rows."""

    def __init__(self, rows_per_query=10, prompt_tokens=14484, rows=None):
        self.queries: list[str] = []
        self._rows_per_query = rows_per_query
        self._prompt_tokens = prompt_tokens
        self._rows = rows

    def iter_rows(self, query: str):
        self.queries.append(query)
        if self._rows is not None:
            yield from self._rows
            return
        for i in range(self._rows_per_query):
            yield vl_row(i, self._prompt_tokens)

    def close(self):
        pass


def profile(**overrides) -> BuildProfile:
    base = {
        "name": "daily",
        "models": ["glm-5"],
        "forwarded_to": ["http://198.51.100.20:8052"],
        "window_hours": 3,
        "subwindow_minutes": 60,
        "sample_size": 6,
        "min_records": 1,
        "min_buckets": 1,
        "clean": False,
        "keep_builds": 5,
        "min_retain_hours": 0,
    }
    base.update(overrides)
    return profile_from_dict(base)


# --- query building --------------------------------------------------------------

def test_escape_value_quotes_and_escapes():
    assert escape_value("http://1.2.3.4:8052") == '"http://1.2.3.4:8052"'
    assert escape_value('a"b') == '"a\\"b"'


def test_build_query_uses_exact_field_filters():
    """`_msg:glm-5` is a token match that also hits glm-5.2 (511k vs 85k rows on
    live data). Every emitted filter must be an exact field filter instead."""
    q = build_query(
        LogFilters(models=["glm-5"], forwarded_to=["http://198.51.100.20:8052"]),
        datetime(2026, 8, 4, 0, tzinfo=timezone.utc),
        datetime(2026, 8, 4, 1, tzinfo=timezone.utc),
        limit=30,
    )
    assert '_time:[2026-08-04T00:00:00Z, 2026-08-04T01:00:00Z)' in q
    assert 'model:="glm-5"' in q
    assert 'forwarded_to:="http://198.51.100.20:8052"' in q
    assert 'status:="200"' in q
    assert 'req_body_truncated:="false"' in q
    assert q.endswith("| limit 30")
    assert "_msg:" not in q


def test_build_query_ors_multiple_values_of_one_field():
    q = build_query(LogFilters(models=["a", "b"], statuses=[]),
                    datetime(2026, 8, 4, tzinfo=timezone.utc),
                    datetime(2026, 8, 5, tzinfo=timezone.utc))
    assert '(model:="a" OR model:="b")' in q
    assert "status:=" not in q


def test_build_query_appends_extra_logsql():
    q = build_query(LogFilters(models=[], statuses=[], uris=[], exclude_truncated=False,
                               extra='req_headers.x-app:="cli"'),
                    datetime(2026, 8, 4, tzinfo=timezone.utc),
                    datetime(2026, 8, 5, tzinfo=timezone.utc))
    assert '(req_headers.x-app:="cli")' in q


# --- windowing ---------------------------------------------------------------------

def test_window_bounds_floors_to_subwindow_in_profile_timezone():
    p = profile(window_hours=24, subwindow_minutes=60, window_timezone="Asia/Shanghai")
    now = datetime(2026, 8, 4, 6, 47, 14, tzinfo=timezone.utc)  # 14:47 +08:00
    start, end = window_bounds(p, now)
    assert end.astimezone(p.tzinfo()).hour == 14
    assert end.astimezone(p.tzinfo()).minute == 0
    assert end - start == timedelta(hours=24)


def test_window_bounds_unknown_timezone_falls_back_to_utc():
    p = profile(window_timezone="Mars/Olympus")
    start, end = window_bounds(p, datetime(2026, 8, 4, 6, 30, tzinfo=timezone.utc))
    assert end.utcoffset() == timedelta(0)
    assert end - start == timedelta(hours=3)


# --- reservoir ----------------------------------------------------------------------

def test_reservoir_is_bounded_and_deterministic():
    import random

    def sample(seed):
        r = _Reservoir(5, random.Random(seed), max_bytes=0)
        for i in range(100):
            r.offer(f"line-{i}", i)
        return r.items

    assert len(sample(1)) == 5
    assert sample(1) == sample(1)          # same seed → same sample
    assert sample(1) != sample(2)          # different seed → different sample
    assert _Reservoir(5, random.Random(1), max_bytes=0).seen == 0


def test_reservoir_respects_byte_ceiling():
    import random

    r = _Reservoir(100, random.Random(1), max_bytes=25)
    for i in range(10):
        r.offer("x" * 10, i)
    assert r.bytes <= 25
    assert len(r.items) == 2


# --- build pipeline ------------------------------------------------------------------

def test_build_publishes_a_replayable_dataset(tmp_path):
    client = FakeClient(rows_per_query=10)
    result = build_dataset(client, profile(), feed_root=tmp_path, seed=1)

    assert result.status == "ready", result.error
    assert result.build.records == 6                     # sample_size
    assert len(client.queries) == 3                      # one per sub-window
    resolved = dataset_feed.resolve_latest(tmp_path, "daily")
    assert resolved is not None and resolved.build_id == result.build.build_id

    with jsonl_io.open_text(resolved.path) as handle:
        lines = [json.loads(x) for x in handle if x.strip()]
    assert len(lines) == 6
    assert lines[0]["request_body"] == REQ_BODY
    # Feed strip policy applies: no credentials, no response body on the volume.
    assert "resp_body" not in lines[0]["raw_payload"]
    assert "authorization" not in lines[0]["raw_payload"]["req_headers"]


def test_build_records_bucket_histogram(tmp_path):
    result = build_dataset(
        FakeClient(rows_per_query=10, prompt_tokens=40000),
        profile(), feed_root=tmp_path, seed=1,
    )
    assert result.stats["buckets"]["32K-64K"] == 6


def test_build_summary_describes_what_was_collected(tmp_path):
    """The admin page reads this instead of parsing a multi-hundred-MB dataset,
    so it has to be tallied from the records that were actually KEPT."""
    rows = [
        vl_row(i, prompt_tokens=1000 * (i + 1), **{
            "resp_meta.usage.completion_tokens": str(100 * (i + 1)),
            "resp_meta.usage.prompt_tokens_details.cached_tokens": str(500 * (i + 1)),
        })
        for i in range(6)
    ]
    result = build_dataset(
        FakeClient(rows=rows), profile(sample_size=18, window_hours=1),
        feed_root=tmp_path, seed=1,
    )
    assert result.status == "ready", result.error
    s = result.stats["summary"]

    assert s["models"] == {"glm-5": 6}
    assert s["forwarded_to"] == {"http://198.51.100.20:8052": 6}
    assert s["prompt_tokens"]["count"] == 6
    assert s["prompt_tokens"]["min"] == 1000 and s["prompt_tokens"]["max"] == 6000
    assert s["completion_tokens"]["max"] == 600
    # Token-weighted, matching replay's cache_hit_rate: cached/prompt.
    assert s["total_prompt_tokens"] == sum(1000 * (i + 1) for i in range(6))
    assert s["total_cached_tokens"] == sum(500 * (i + 1) for i in range(6))
    assert s["cache_hit_rate"] == pytest.approx(0.5)


def test_build_summary_survives_missing_usage(tmp_path):
    """Captures without usage (a production 4xx has none) must not break the
    tally — they just don't contribute to the spreads."""
    rows = []
    for i in range(4):
        row = vl_row(i)
        row.pop("resp_meta.usage.prompt_tokens", None)
        rows.append(row)
    result = build_dataset(
        FakeClient(rows=rows), profile(sample_size=12, window_hours=1, min_buckets=0),
        feed_root=tmp_path, seed=1,
    )
    assert result.status == "ready", result.error
    s = result.stats["summary"]
    assert s["prompt_tokens"] is None
    assert s["cache_hit_rate"] is None
    assert s["models"] == {"glm-5": 4}


def test_build_summary_shows_multiple_models_when_filters_are_loose(tmp_path):
    """A profile whose filters let several models through should say so — that
    is exactly the misconfiguration worth seeing on the page."""
    rows = [vl_row(0, model="glm-5"), vl_row(1, model="glm-5"), vl_row(2, model="kimi-k2.6")]
    result = build_dataset(
        FakeClient(rows=rows), profile(sample_size=9, window_hours=1),
        feed_root=tmp_path, seed=1,
    )
    assert result.stats["summary"]["models"] == {"glm-5": 2, "kimi-k2.6": 1}


def test_build_carries_shortfall_forward(tmp_path):
    """A quiet slice must not permanently cost the sample those records — the
    deficit is picked up by later slices at no extra download cost."""
    calls = {"n": 0}

    class Sparse(FakeClient):
        def iter_rows(self, query):
            self.queries.append(query)
            calls["n"] += 1
            count = 0 if calls["n"] == 1 else 10
            for i in range(count):
                yield vl_row(i)

    result = build_dataset(Sparse(), profile(), feed_root=tmp_path, seed=1)
    assert result.status == "ready"
    assert result.build.records == 6


def test_profile_carries_max_carry_multiple():
    assert profile().max_carry_multiple == MAX_CARRY_MULTIPLE  # default preserved
    assert profile(max_carry_multiple=9).max_carry_multiple == 9


def test_max_carry_multiple_caps_a_concentrated_slice(tmp_path):
    """When traffic sits in a single slice, base_quota * max_carry_multiple — not
    sample_size — is the ceiling. Raising the multiple lifts that ceiling, which
    is the whole reason it is exposed. 4 slices, sample_size 8 -> base_quota 2;
    all rows arrive in the LAST slice, so the deficit has fully carried forward
    and only the per-slice cap can bind."""
    class Bursty(FakeClient):
        def __init__(self, n_slices):
            super().__init__()
            self._n = n_slices
            self._calls = 0

        def iter_rows(self, query):
            self.queries.append(query)
            self._calls += 1
            if self._calls == self._n:          # only the final slice has traffic
                for i in range(50):
                    yield vl_row(i)

    def run(mcm, seed):
        return build_dataset(
            Bursty(n_slices=4),
            profile(window_hours=4, subwindow_minutes=60, sample_size=8,
                    max_carry_multiple=mcm),
            feed_root=tmp_path / f"m{mcm}", seed=seed,
        )

    tight = run(1, seed=1)
    loose = run(4, seed=2)
    assert tight.status == "ready" and loose.status == "ready"
    # cap = base_quota(2) * multiple: 2 with mcm=1, the full sample (8) with mcm=4.
    assert tight.build.records == 2
    assert loose.build.records == 8
    assert loose.build.records > tight.build.records


def test_build_below_min_records_keeps_previous_build(tmp_path):
    good = build_dataset(FakeClient(rows_per_query=10), profile(), feed_root=tmp_path, seed=1)
    assert good.status == "ready"

    thin = build_dataset(
        FakeClient(rows=[]), profile(min_records=5), feed_root=tmp_path, seed=2,
    )
    assert thin.status == "failed"
    assert "below the profile's minimum" in thin.error
    # The pointer still names the good build — a bad collection never displaces one.
    assert dataset_feed.resolve_latest(tmp_path, "daily").build_id == good.build.build_id


def test_build_below_min_buckets_is_rejected(tmp_path):
    result = build_dataset(
        FakeClient(rows_per_query=10), profile(min_buckets=3), feed_root=tmp_path, seed=1,
    )
    assert result.status == "failed"
    assert "bucket" in result.error


def test_failed_build_leaves_no_staged_file(tmp_path):
    build_dataset(FakeClient(rows=[]), profile(min_records=5), feed_root=tmp_path, seed=1)
    staging = dataset_feed.staging_dir(tmp_path, "daily")
    assert list(staging.glob("*.part")) == []


def test_build_stops_when_asked(tmp_path):
    result = build_dataset(
        FakeClient(rows_per_query=10), profile(), feed_root=tmp_path, seed=1,
        should_stop=lambda: True,
    )
    assert result.status == "failed"
    assert dataset_feed.resolve_latest(tmp_path, "daily") is None


def test_store_failure_is_reported_not_raised(tmp_path):
    class Broken(FakeClient):
        def iter_rows(self, query):
            raise RuntimeError("log store exploded")

    result = build_dataset(Broken(), profile(), feed_root=tmp_path, seed=1)
    assert result.status == "failed"
    assert "log store exploded" in result.error


def test_clean_mode_drops_original_4xx(tmp_path):
    rows = [vl_row(i, status="400") for i in range(5)]
    result = build_dataset(
        FakeClient(rows=rows), profile(clean=True, max_model_len=262144, min_records=1),
        feed_root=tmp_path, seed=1,
    )
    assert result.status == "failed"          # everything dropped → nothing to publish
    assert result.stats["drops"]["orig_4xx"] == 15


def test_build_respects_disk_headroom(tmp_path, monkeypatch):
    """Filling the datasets volume would break every benchmark that reads a
    dataset from it, so a build refuses to start without headroom."""
    monkeypatch.setattr("app.datasets.builder._free_bytes", lambda _p: 1024)
    result = build_dataset(FakeClient(rows_per_query=10), profile(), feed_root=tmp_path, seed=1)
    assert result.status == "failed"
    assert "insufficient space" in result.error


def test_manual_only_profiles_are_never_due(monkeypatch):
    """schedule_interval_hours == 0 means "only when the API asks".

    The trap this guards: a profile with no build yet takes the "nothing has
    finished, so it's due" path, so without an explicit check a manual-only
    profile would collect once, unprompted, the moment it was created.
    """
    from app.datasets import service

    rows = [
        SimpleNamespace(id=1, name="manual", enabled=True, schedule_interval_hours=0,
                        schedule_anchor_hour=None, window_timezone="UTC"),
        SimpleNamespace(id=2, name="daily", enabled=True, schedule_interval_hours=24,
                        schedule_anchor_hour=None, window_timezone="UTC"),
    ]

    class _Result:
        def __init__(self, rows): self._rows = rows
        def scalars(self): return self
        def all(self): return self._rows
        def first(self): return None
        def scalar_one_or_none(self): return None

    class _Session:
        def execute(self, stmt):
            # First call returns the profile list; later calls are the
            # per-profile "is one already running / when did one last finish"
            # probes, which must answer "no" for this test.
            text = str(stmt).lower()
            return _Result(rows if "replay_dataset_profiles" in text
                           and "replay_dataset_builds" not in text else [])
        def close(self): pass

    monkeypatch.setattr(service, "get_sync_session", lambda: _Session())
    monkeypatch.setattr(service, "profile_to_build_profile",
                        lambda row: profile(name=row.name))

    due = service.due_profiles()
    assert due == [2], "manual-only profile must not be scheduled; daily must be"


def test_manual_build_still_works_for_a_manual_only_profile():
    """`enabled` and "on a schedule" are different things: interval 0 turns off
    scheduling, `enabled=false` turns the profile off entirely. Only the latter
    may block an explicit build request."""
    from app.datasets.service import profile_to_build_profile

    row = SimpleNamespace(
        name="manual", models=["glm-5"], statuses=["200"], forwarded_to=[],
        uris=["/v1/chat/completions"], exclude_truncated=True, extra_logsql="",
        window_hours=24, window_timezone="UTC", subwindow_minutes=60,
        sample_size=100, min_records=1, min_buckets=0, max_bytes=1 << 30,
        oversample_factor=3.0, max_carry_multiple=4, clean=False, max_model_len=262144,
        keep_response_body=False, compress=True, header_denylist=[],
        keep_builds=3, min_retain_hours=8.0, schedule_interval_hours=0,
    )
    built = profile_to_build_profile(row)
    assert built.schedule_interval_hours == 0
    # A manual-only profile is still a perfectly valid build target.
    assert built.name == "manual" and built.sample_size == 100


def test_retained_builds_exposes_the_time_floor_dominating():
    """keep_builds is not the disk cap on a fast schedule: retention needs a
    build to be BOTH beyond the count AND past the time floor."""
    from app.datasets.builder import retained_builds

    daily = profile(keep_builds=7, min_retain_hours=8)
    assert retained_builds(daily, 24.0) == 7        # floor covers <1 build

    hourly = profile(keep_builds=7, min_retain_hours=72)
    assert retained_builds(hourly, 1.0) == 72       # 72 builds, not 7

    assert retained_builds(daily, 0.0) == 7         # no schedule → count only


def test_profile_from_dict_ignores_unknown_keys():
    """A newer DB row must not break an older collector mid-deploy."""
    p = profile_from_dict({"name": "x", "sample_size": 10, "some_future_field": 1})
    assert p.name == "x" and p.sample_size == 10


# --- worker-side resolution ------------------------------------------------------------

def _run_row(params_json=None):
    return SimpleNamespace(id=1, submission_id=7, params_json=params_json or {})


class _Session:
    def __init__(self):
        self.commits = 0

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass


def _publish_one(root, profile_name="daily", built_at=None, build_id="b1", records=1):
    staged = dataset_feed.prepare_staging(root, profile_name, build_id)
    # Through jsonl_io, not write_text: staging is `.jsonl.gz.part` by default,
    # so writing it raw would put plain text under a .gz name.
    jsonl_io.open_text(staged, "w").write("".join(
        '{"source_file":"f","line_no":%d,"level":"INFO","timestamp":"",'
        '"request_id":"r%d","request_body":"{}"}\n' % (i, i)
        for i in range(records)
    ))
    return dataset_feed.publish(
        root, profile_name, staged, build_id=build_id, records=records, sha256="abc",
        built_at=built_at or datetime.now(timezone.utc),
    )


def test_worker_pins_the_resolved_build(tmp_path, monkeypatch):
    monkeypatch.setenv("REPLAY_FEED_ROOT", str(tmp_path))
    from app.queue.jobs import _resolve_dataset_feed
    from bench.modules import get_module

    build = _publish_one(tmp_path)
    cls = get_module("replay")
    params = cls.ParamsSchema(dataset_path="", dataset_source="auto", dataset_profile="daily")
    run = _run_row({"dataset_path": "", "dataset_source": "auto", "dataset_profile": "daily"})
    session = _Session()

    resolved = _resolve_dataset_feed(session, run, cls, params, "replay")

    # The run reads the PINNED path (a hard link), not builds/ — so retention
    # deleting the build mid-run cannot take the dataset away from it.
    pinned = Path(resolved.dataset_path)
    assert pinned.parent == dataset_feed.pins_dir(tmp_path, "daily")
    assert pinned.stat().st_ino == Path(build.path).stat().st_ino

    prov = run.params_json["dataset_resolved"]
    assert prov["build_id"] == "b1"
    assert prov["path"] == build.path          # canonical identity
    assert prov["pinned_path"] == str(pinned)  # what this run actually reads
    assert prov["stale"] is False
    # The snapshot's dataset_path is rewritten too, so the detail page shows the
    # file that actually ran rather than the benchmark's placeholder.
    assert run.params_json["dataset_path"] == str(pinned)
    assert session.commits == 1


def test_a_held_pin_does_not_hold_new_runs_back(tmp_path, monkeypatch):
    """A pin is a retention hold, NOT a lock on which build is current.

    Run A pins build b1 and keeps running. The collector publishes b2. Run B
    starts and must get b2 — the newest build — not be dragged onto b1 just
    because someone else is still reading it. Resolution consults only
    latest.json; nothing in the pin path can influence it.
    """
    monkeypatch.setenv("REPLAY_FEED_ROOT", str(tmp_path))
    from app.queue.jobs import _resolve_dataset_feed
    from bench.modules import get_module

    cls = get_module("replay")

    def start_run(run_id: int):
        params = cls.ParamsSchema(
            dataset_path="", dataset_source="auto", dataset_profile="daily"
        )
        run = SimpleNamespace(id=run_id, submission_id=run_id, params_json={})
        resolved = _resolve_dataset_feed(_Session(), run, cls, params, "replay")
        return run, resolved

    first = _publish_one(tmp_path, build_id="b1", records=1)
    run_a, params_a = start_run(1)
    assert run_a.params_json["dataset_resolved"]["build_id"] == "b1"

    # A is still running (its pin is held) when a newer build lands.
    second = _publish_one(tmp_path, build_id="b2", records=2)
    assert dataset_feed.resolve_latest(tmp_path, "daily").build_id == "b2"

    run_b, params_b = start_run(2)
    assert run_b.params_json["dataset_resolved"]["build_id"] == "b2"

    # Two concurrent replays, two different datasets, each reading its own.
    assert params_a.dataset_path != params_b.dataset_path
    assert Path(params_a.dataset_path).stat().st_ino == Path(first.path).stat().st_ino
    assert Path(params_b.dataset_path).stat().st_ino == Path(second.path).stat().st_ino
    assert {o for o, _ in dataset_feed.list_pins(tmp_path, "daily")} == {"s1-r1", "s2-r2"}

    # And a third run started later still tracks the newest build, not either pin.
    _publish_one(tmp_path, build_id="b3", records=3)
    run_c, _ = start_run(3)
    assert run_c.params_json["dataset_resolved"]["build_id"] == "b3"


def test_dataset_identity_is_stamped_onto_the_metrics(tmp_path, monkeypatch):
    """The identity keys go in `metrics`, not just params, because that is where
    people compare runs — and with a rolling dataset "same score" is only
    meaningful between runs that replayed the same build."""
    monkeypatch.setenv("REPLAY_FEED_ROOT", str(tmp_path))
    from app.queue.jobs import _add_dataset_identity, _resolve_dataset_feed
    from bench.modules import get_module
    from bench.modules.base import ModuleResult

    _publish_one(tmp_path, build_id="20260804T071349Z")
    cls = get_module("replay")
    params = cls.ParamsSchema(dataset_path="", dataset_source="auto", dataset_profile="daily")
    run = _run_row()
    _resolve_dataset_feed(_Session(), run, cls, params, "replay")

    result = ModuleResult(metrics={"uptime": 1.0})
    _add_dataset_identity(result, run, cls, "replay")

    assert result.metrics["dataset_id"] == "20260804T071349Z"
    assert result.metrics["dataset_sha256"] == "abc"      # 16-char prefix of the build sha
    # Declared display-only, so it can never reach scoring or a redline.
    assert {c["key"] for c in result.extra_display_configs} == {"dataset_id", "dataset_sha256"}
    assert all(c["role"] == "display" for c in result.extra_display_configs)


def test_dataset_identity_for_a_fixed_dataset_has_no_hash(tmp_path):
    """Fixed datasets are multi-GB; hashing one per run is real I/O for little
    gain, so the id is the filename and the hash is blank."""
    from app.queue.jobs import _add_dataset_identity
    from bench.modules import get_module
    from bench.modules.base import ModuleResult

    cls = get_module("replay")
    run = _run_row({"dataset_path": "/app/dataset/replay/captured.jsonl"})
    result = ModuleResult(metrics={"uptime": 1.0})
    _add_dataset_identity(result, run, cls, "replay")

    assert result.metrics["dataset_id"] == "captured.jsonl"
    assert result.metrics["dataset_sha256"] == ""


def test_string_metrics_never_reach_scoring(tmp_path):
    """A string in `metrics` must not break evaluation — the identity keys sit
    next to the numeric ones the score is built from."""
    from bench.modules.evaluator import evaluate

    metrics = {"uncached_input_tpm": 1000.0, "uptime": 1.0,
               "dataset_id": "20260804T071349Z", "dataset_sha256": "9e27e75f08727933"}
    configs = [
        {"key": "uncached_input_tpm", "role": "score",
         "formula": "passthrough_scaled", "weight": 3.0},
        {"key": "uptime", "role": "redline", "min_val": 0.95},
        {"key": "dataset_id", "role": "display"},
        {"key": "dataset_sha256", "role": "display"},
    ]
    score, passed = evaluate(metrics, configs)
    assert passed is True
    assert score == pytest.approx(3000.0)


def test_dataset_identity_skips_failed_runs_and_other_modules(tmp_path):
    from app.queue.jobs import _add_dataset_identity
    from bench.modules import get_module
    from bench.modules.base import ModuleResult

    cls = get_module("replay")
    failed = ModuleResult(error="boom", metrics={})
    _add_dataset_identity(failed, _run_row({"dataset_path": "/x.jsonl"}), cls, "replay")
    assert failed.metrics == {}

    other = get_module("perf_guidellm")
    result = ModuleResult(metrics={"uptime": 1.0})
    _add_dataset_identity(result, _run_row({"dataset_path": "/x.jsonl"}), other, "perf_guidellm")
    assert "dataset_id" not in result.metrics


def test_worker_releases_the_pin_when_the_module_finishes(tmp_path, monkeypatch):
    """Released per MODULE, not per submission: a later module can run for many
    more hours, and holding a multi-GB dataset alive that whole time is the disk
    pressure retention exists to avoid."""
    monkeypatch.setenv("REPLAY_FEED_ROOT", str(tmp_path))
    from app.queue.jobs import _release_dataset_pin, _resolve_dataset_feed
    from bench.modules import get_module

    _publish_one(tmp_path)
    cls = get_module("replay")
    params = cls.ParamsSchema(dataset_path="", dataset_source="auto", dataset_profile="daily")
    run = _run_row()
    _resolve_dataset_feed(_Session(), run, cls, params, "replay")
    assert len(dataset_feed.list_pins(tmp_path, "daily")) == 1

    _release_dataset_pin(7, run)
    assert dataset_feed.list_pins(tmp_path, "daily") == []
    # Releasing twice is harmless — the finally block runs on every path.
    _release_dataset_pin(7, run)


def test_worker_falls_back_to_builds_path_when_linking_is_unsupported(tmp_path, monkeypatch):
    """Some filesystems refuse hard links. That must degrade to the shared path
    (still protected by retention policy), never fail the run."""
    monkeypatch.setenv("REPLAY_FEED_ROOT", str(tmp_path))
    from app.queue.jobs import _resolve_dataset_feed
    from bench.modules import get_module

    build = _publish_one(tmp_path)
    monkeypatch.setattr(dataset_feed, "pin", lambda *a, **k: None)
    cls = get_module("replay")
    params = cls.ParamsSchema(dataset_path="", dataset_source="auto", dataset_profile="daily")
    run = _run_row()

    resolved = _resolve_dataset_feed(_Session(), run, cls, params, "replay")
    assert resolved.dataset_path == build.path
    assert "pinned_path" not in run.params_json["dataset_resolved"]


def test_worker_marks_a_stale_build_but_still_runs(tmp_path, monkeypatch):
    monkeypatch.setenv("REPLAY_FEED_ROOT", str(tmp_path))
    from app.queue.jobs import _resolve_dataset_feed
    from bench.modules import get_module

    _publish_one(tmp_path, built_at=datetime.now(timezone.utc) - timedelta(hours=100))
    cls = get_module("replay")
    params = cls.ParamsSchema(dataset_path="", dataset_source="auto",
                              dataset_profile="daily", dataset_max_age_hours=48)
    run = _run_row()
    resolved = _resolve_dataset_feed(_Session(), run, cls, params, "replay")

    assert Path(resolved.dataset_path).name.endswith(".jsonl.gz")
    assert run.params_json["dataset_resolved"]["build_id"] == "b1"
    assert run.params_json["dataset_resolved"]["stale"] is True
    assert run.params_json["dataset_resolved"]["age_hours"] > 99


def test_worker_fails_when_nothing_published(tmp_path, monkeypatch):
    monkeypatch.setenv("REPLAY_FEED_ROOT", str(tmp_path))
    from app.queue.jobs import DatasetFeedUnavailable, _resolve_dataset_feed
    from bench.modules import get_module

    cls = get_module("replay")
    params = cls.ParamsSchema(dataset_path="/some/fixed.jsonl", dataset_source="auto",
                              dataset_profile="daily")
    with pytest.raises(DatasetFeedUnavailable, match="daily"):
        _resolve_dataset_feed(_Session(), _run_row(), cls, params, "replay")


def test_worker_resolution_is_a_noop_for_fixed_and_other_modules(tmp_path, monkeypatch):
    monkeypatch.setenv("REPLAY_FEED_ROOT", str(tmp_path))
    from app.queue.jobs import _resolve_dataset_feed
    from bench.modules import get_module

    cls = get_module("replay")
    params = cls.ParamsSchema(dataset_path="/fixed.jsonl")
    run = _run_row()
    session = _Session()
    assert _resolve_dataset_feed(session, run, cls, params, "replay") is params
    assert "dataset_resolved" not in run.params_json
    assert session.commits == 0

    other = get_module("perf_guidellm")
    other_params = other.ParamsSchema()
    assert _resolve_dataset_feed(session, run, other, other_params, "perf_guidellm") is other_params
