"""Timed replay (schedule=timed): send times, the think-time rule, latency from
the due time, warm-up, falling behind, the in-flight cap, and refusal of a
dataset with no timing."""
from __future__ import annotations

import json
import time

import pytest

from bench.modules.base import ModuleResult
from bench.modules import get_module
from bench.replay_test.timed_schedule import TimedSchedule, TimingMissing, time_weighted_mean


# ---------------------------------------------------------------------------
# The schedule (no clock)
# ---------------------------------------------------------------------------

def _t(at, after=None, think=None, session=""):
    return {"at_s": at, "after": after, "think_s": think, "session": session}


def test_first_turns_go_at_their_recorded_time():
    s = TimedSchedule([_t(0.0), _t(5.0), _t(2.0)])
    assert s.pop_due(1.0) == [(0, 0.0)]
    assert s.next_due() == 2.0
    assert s.pop_due(10.0) == [(2, 2.0), (1, 5.0)]
    assert s.waiting() == 0


def test_a_later_turn_waits_for_the_one_it_follows_on_this_engine():
    s = TimedSchedule([_t(0.0), _t(9.0, after=0, think=1.5)])
    assert s.pop_due(100.0) == [(0, 0.0)]       # line 1 is not due at its at_s
    assert s.waiting() == 1
    s.finished(0, 3.0)                           # this engine took 3 s
    assert s.next_due() == 4.5                   # 3.0 + think 1.5, not 9.0
    assert s.pop_due(4.4) == []
    assert s.pop_due(4.5) == [(1, 4.5)]


def test_a_dangling_or_forward_reference_is_sent_on_its_recorded_time():
    s = TimedSchedule([_t(1.0, after=5, think=1), _t(2.0, after=1, think=1)])
    assert [i for i, _ in s.pop_due(10.0)] == [0, 1]


def test_negative_think_is_zero_and_the_limit_keeps_the_rest_due():
    s = TimedSchedule([_t(0.0), _t(0.0), _t(0.0), _t(1.0, after=0, think=-2)])
    assert len(s.pop_due(0.0, limit=2)) == 2
    assert s.pop_due(0.0, limit=5) == [(2, 0.0)]
    s.finished(0, 0.7)
    assert s.next_due() == 0.7


def test_missing_timing_is_refused_with_a_reason():
    with pytest.raises(TimingMissing, match="layout 'timed'"):
        TimedSchedule([_t(0.0), None, {"session": "x"}])


def test_time_weighted_mean():
    # 0 in flight for 1 s, 2 for 2 s, 1 for 1 s → (0 + 4 + 1) / 4
    assert time_weighted_mean([(0.0, 0), (1.0, 2), (3.0, 1)], 4.0) == pytest.approx(1.25)


# ---------------------------------------------------------------------------
# The runner, with a fake engine (real clock, short times)
# ---------------------------------------------------------------------------

def _write_timed(tmp_path, timings, name="timed.jsonl", recorded_tokens=None):
    body = json.dumps({"model": "m", "messages": [{"role": "user", "content": "hi"}],
                       "max_tokens": 8, "stream": True})
    lines = []
    for i, t in enumerate(timings):
        payload = {"resp_meta": {"usage": {"completion_tokens": recorded_tokens[i]}}} if recorded_tokens else {}
        rec = {"source_file": "t", "line_no": i + 1, "level": "info", "timestamp": "",
               "request_id": f"r{i}", "channel_id": None, "token_name": None,
               "request_body": body, "request_json": None, "raw_payload": payload}
        if t is not None:
            rec["timing"] = t
        lines.append(json.dumps(rec))
    p = tmp_path / name
    p.write_text("\n".join(lines))
    return p


def _mk(tmp_path, ds, **kw):
    from bench.tests.functional.replay import ReplayTest
    kw.setdefault("flush_cache", False)
    kw.setdefault("warmup_s", 0.0)
    return ReplayTest(api_url="http://127.0.0.1:1/v1", model="m", api_key="",
                      output_dir=str(tmp_path / "out"), dataset_path=str(ds),
                      schedule="timed", **kw)


def _fake_engine(test, monkeypatch, service_s):
    """Each request takes `service_s` (a float, or a function of the request id)."""
    def send(record, url):
        d = service_s(record.request_id) if callable(service_s) else service_s
        t = time.monotonic()
        time.sleep(d)
        return {"request_id": record.request_id, "success": True,
                "ttft_ms": 10.0, "total_ms": (time.monotonic() - t) * 1000.0,
                "completion_tokens": 11, "prompt_tokens": 5, "status_code": 200}
    monkeypatch.setattr(test, "_send_request", send)
    monkeypatch.setattr(test, "_preflight_tool_check", lambda make_iter: None)


def test_runner_sends_on_schedule_and_measures_from_the_due_time(tmp_path, monkeypatch):
    ds = _write_timed(tmp_path, [
        _t(0.0, session="a"),
        _t(5.0, after=0, think=0.2, session="a"),   # due when r0 finishes + 0.2
        _t(0.1),
    ])
    test = _mk(tmp_path, ds)
    _fake_engine(test, monkeypatch, 0.15)
    res = test.run()
    assert res.passed, res
    m = res.metrics
    assert m["fell_behind"] == 0 and m["measured_requests"] == 3
    assert m["schedule_lag_p99_s"] < 0.1
    assert m["e2e_p50_ms"] == pytest.approx(150, abs=60)
    # a two-turn session: 0.15 + 0.2 + 0.15
    assert m["session_time_p50_s"] == pytest.approx(0.5, abs=0.1)
    assert m["sessions_measured"] == 1
    # the schedule sets throughput, so the flat TPM keys are left empty
    assert m["output_tpm"] is None and m["input_tpm"] is None
    assert m["tpot_p50_ms"] == pytest.approx((150 - 10) / 10, abs=8)


def test_a_slower_engine_delays_the_next_turn(tmp_path, monkeypatch):
    ds = _write_timed(tmp_path, [_t(0.0, session="a"), _t(0.3, after=0, think=0.1, session="a")])
    test = _mk(tmp_path, ds)
    _fake_engine(test, monkeypatch, lambda rid: 0.4 if rid == "r0" else 0.05)
    results, _ = test._run_timed(test_records := _records(ds), TimedSchedule([r.timing for r in test_records]))
    assert results[1]["due_s"] == pytest.approx(results[0]["end_s"] + 0.1, abs=0.01)
    assert results[1]["due_s"] > 0.45                       # not its recorded 0.3


def _records(ds):
    from bench.replay_test.log_replay_tool import load_extract_jsonl
    return list(load_extract_jsonl(str(ds), lean=True))


def test_warm_up_requests_are_sent_but_not_measured(tmp_path, monkeypatch):
    ds = _write_timed(tmp_path, [_t(0.0), _t(0.05), _t(0.3), _t(0.35)])
    test = _mk(tmp_path, ds, warmup_s=0.2)
    _fake_engine(test, monkeypatch, 0.02)
    m = test.run().metrics
    assert m["warmup_requests"] == 2 and m["measured_requests"] == 2
    assert m["total_requests"] == 2


def test_a_client_that_cannot_keep_up_fell_behind_and_fails(tmp_path, monkeypatch):
    ds = _write_timed(tmp_path, [_t(0.0)] * 4)
    test = _mk(tmp_path, ds, max_in_flight=1, max_lag_s=0.1)
    _fake_engine(test, monkeypatch, 0.2)
    res = test.run()
    m = res.metrics
    assert m["max_in_flight_hit"] == 1 and m["in_flight_max"] == 1
    assert m["fell_behind"] == 1 and not res.passed
    # the wait for a slot counts as latency: the last one waited ~0.6 s
    assert m["e2e_p90_ms"] > 500


def test_untimed_dataset_is_refused_by_the_module(tmp_path, mock_endpoint):
    ds = _write_timed(tmp_path, [None, None])
    cls = get_module("replay")
    params = cls.ParamsSchema(dataset_path=str(ds), schedule="timed")
    result = cls().run(mock_endpoint, params, str(tmp_path))
    assert isinstance(result, ModuleResult)
    assert result.error and "timed" in result.error


def test_schedule_is_validated():
    cls = get_module("replay")
    with pytest.raises(Exception):
        cls.ParamsSchema(schedule="open")
    assert cls.ParamsSchema().schedule == "closed"


# ---------------------------------------------------------------------------
# End to end against mock_server
# ---------------------------------------------------------------------------

def test_timed_replay_end_to_end(mock_endpoint, tmp_path):
    ds = _write_timed(tmp_path, [
        _t(0.0, session="s1"), _t(0.2), _t(1.0, after=0, think=0.3, session="s1"), _t(0.6),
    ])
    cls = get_module("replay")
    params = cls.ParamsSchema(dataset_path=str(ds), schedule="timed", warmup_s=0.0,
                              request_timeout=30.0)
    result = cls().run(mock_endpoint, params, str(tmp_path))
    m = result.metrics
    assert result.error is None, result.error
    assert m["cache_flushed"] == 1                   # mock_server answers /flush_cache
    assert m["successful_requests"] == 4 and m["fell_behind"] == 0
    assert m["e2e_p50_ms"] > 0 and m["ttft_p50_ms"] > 0
    assert m["sessions_measured"] == 1


def test_closed_loop_reports_no_timed_keys(mock_endpoint, tmp_path):
    ds = _write_timed(tmp_path, [_t(0.0), _t(0.1)])
    cls = get_module("replay")
    params = cls.ParamsSchema(dataset_path=str(ds), concurrency=2, request_timeout=30.0)
    m = cls().run(mock_endpoint, params, str(tmp_path)).metrics
    timed_keys = {"e2e_p50_ms", "fell_behind", "schedule_lag_p99_s", "session_time_p50_s",
                  "cache_flushed", "measured_requests", "in_flight_mean",
                  "equivalent_tpm", "equivalent_coverage", "equivalent_busy_share",
                  "equivalent_little_error", "equivalent_tpm_logged"}
    assert not timed_keys & set(m)
    assert m["output_tpm"] is not None


def test_a_timed_replay_knows_each_recorded_length(tmp_path, monkeypatch):
    # The runner loads lean (no raw_payload): the recorded lengths still reach
    # the run's output-length figures.
    ds = _write_timed(tmp_path, [_t(0.0), _t(0.05)], recorded_tokens=[11, 40])
    test = _mk(tmp_path, ds)
    _fake_engine(test, monkeypatch, 0.02)          # answers 11 tokens each
    m = test.run().metrics
    assert m["fixed_output_lengths"] == 0
    assert m["requests_with_recorded_length"] == 2
    assert m["output_length_match_rate"] == pytest.approx(0.5)
    assert m["recorded_length_score_factor"] is not None


def test_a_timed_replay_forces_the_recorded_lengths(tmp_path, monkeypatch):
    from bench.replay_test import log_replay_tool
    ds = _write_timed(tmp_path, [_t(0.0), _t(0.05)], recorded_tokens=[11, 40])
    test = _mk(tmp_path, ds, fixed_output_lengths=True)
    test._preflight_tool_check = lambda make_iter: None
    sent = []
    real = log_replay_tool.build_replay_body

    def spy(record, *a, **kw):
        sent.append(kw.get("fixed_output_tokens"))
        return real(record, *a, **kw)
    import bench.tests.functional.replay as replay_mod
    monkeypatch.setattr(replay_mod, "build_replay_body", spy)
    test.run()
    assert sorted(sent) == [11, 40]


def test_a_retried_request_is_counted(tmp_path, monkeypatch):
    ds = _write_timed(tmp_path, [_t(0.0), _t(0.05), _t(0.1)])
    test = _mk(tmp_path, ds)

    def send(record, url):
        time.sleep(0.02)
        return {"request_id": record.request_id, "success": True, "ttft_ms": 10.0, "total_ms": 20.0,
                "completion_tokens": 11, "prompt_tokens": 5, "status_code": 200,
                "attempts": 3 if record.request_id == "r1" else 1}
    monkeypatch.setattr(test, "_send_request", send)
    monkeypatch.setattr(test, "_preflight_tool_check", lambda make_iter: None)
    m = test.run().metrics
    assert m["retried_requests"] == 1 and m["retry_attempts"] == 2
