"""Equivalent throughput (bench/replay_test/equivalent.py), on a simulated
engine: processor sharing, where n requests in flight share a total service
rate r(n). A config with 1.5 × r(n) is 1.5 × faster at every n, so behind a
least-connections balancer it serves 1.5 × the traffic at the same n."""
from __future__ import annotations

import json
import random

import pytest

from bench.replay_test import equivalent as eq


def ps_sim(arrivals, works, rate):
    """End time of each request on a processor-sharing server whose total
    service rate with n in flight is rate(n)."""
    order = sorted(range(len(arrivals)), key=lambda i: arrivals[i])
    end = [0.0] * len(arrivals)
    active: dict[int, float] = {}
    t, k = 0.0, 0
    while k < len(order) or active:
        ta = arrivals[order[k]] if k < len(order) else float("inf")
        if not active:
            t = ta
            active[order[k]] = works[order[k]]
            k += 1
            continue
        per = rate(len(active)) / len(active)
        first = min(active, key=active.get)
        tc = t + active[first] / per
        step = min(ta, tc) - t
        for i in active:
            active[i] -= per * step
        t += step
        if tc <= ta:
            del active[first]
            end[first] = t
        else:
            active[order[k]] = works[order[k]]
            k += 1
    return end


def traffic(n=3000, lam=0.6, seed=7):
    rnd = random.Random(seed)
    t, at, u, o = 0.0, [], [], []
    for _ in range(n):
        t += rnd.expovariate(lam)
        at.append(t)
        u.append(rnd.uniform(500, 8000))
        o.append(rnd.uniform(50, 800))
    works = [0.2 + 0.5 * a / 1000 + 2.0 * b / 1000 for a, b in zip(u, o)]
    return at, u, o, works


def base_rate(n):
    return 3.0 * n / (n + 4)


def fast_rate(n):
    return 1.5 * base_rate(n)


def logged_from(at, end, u, o, tokens=True):
    return [eq.Logged(a, e - a, *((x, 0.0, y) if tokens else (None, None, None)))
            for a, e, x, y in zip(at, end, u, o)]


def ran_from(at, end, u, o):
    return [eq.Ran(send_s=a, end_s=e, ok=True, tokens=x + y) for a, e, x, y in zip(at, end, u, o)]


@pytest.fixture(scope="module")
def sim():
    at, u, o, works = traffic()
    prod = ps_sim(at, works, base_rate)
    fast = ps_sim(at, works, fast_rate)
    return at, u, o, prod, fast


def test_production_replayed_as_logged_scores_its_logged_value(sim):
    at, u, o, prod, _ = sim
    out = eq.equivalent_throughput(logged_from(at, prod, u, o), ran_from(at, prod, u, o))
    assert out["equivalent_tpm"] is not None
    assert out["equivalent_tpm"] == pytest.approx(out["equivalent_tpm_logged"], rel=1e-9)
    assert out["equivalent_size_matched"] == 1
    assert out["equivalent_size_r2"] > 0.8
    assert out["equivalent_coverage"] > 0.9
    assert 0.5 < out["equivalent_busy_share"] <= 1.0
    assert abs(out["equivalent_little_error"]) < 0.05


def test_a_config_1_5x_faster_at_every_n_scores_1_5x(sim):
    at, u, o, prod, fast = sim
    logged = logged_from(at, prod, u, o)
    base = eq.equivalent_throughput(logged, ran_from(at, prod, u, o))
    quick = eq.equivalent_throughput(logged, ran_from(at, fast, u, o))
    assert quick["equivalent_tpm"] / base["equivalent_tpm"] == pytest.approx(1.5, rel=0.06)
    # the faster config holds fewer requests in flight on the same schedule,
    # but the weights are production's, so both runs are scored alike
    assert quick["equivalent_tpm_logged"] == base["equivalent_tpm_logged"]


def test_levels_a_run_did_not_reach_get_a_lower_bound(sim):
    # A faster config never reaches production's busiest levels and a slower
    # one skips its quietest: both are scored on every level of production's,
    # never above what they showed.
    at, u, o, prod, fast = sim
    logged = logged_from(at, prod, u, o)
    base = eq.equivalent_throughput(logged, ran_from(at, prod, u, o))["equivalent_tpm"]
    quick = eq.equivalent_throughput(logged, ran_from(at, fast, u, o))
    assert quick["equivalent_coverage"] < 0.9
    assert 1.4 < quick["equivalent_tpm"] / base <= 1.5
    at2, u2, o2, works = traffic()
    slow = eq.equivalent_throughput(logged, ran_from(at2, ps_sim(at2, works, lambda n: 0.8 * base_rate(n)), u2, o2))
    assert 0.72 < slow["equivalent_tpm"] / base <= 0.8
    # one that cannot keep up at all queues, and scores far below
    jam = eq.equivalent_throughput(logged, ran_from(at2, ps_sim(at2, works, lambda n: 0.5 * base_rate(n)), u2, o2))
    assert jam["equivalent_tpm"] / base < 0.3 and jam["equivalent_coverage"] < 0.1


def test_without_logged_token_counts_the_plain_means_are_used(sim):
    at, u, o, prod, fast = sim
    logged = logged_from(at, prod, u, o, tokens=False)
    base = eq.equivalent_throughput(logged, ran_from(at, prod, u, o))
    quick = eq.equivalent_throughput(logged, ran_from(at, fast, u, o))
    assert base["equivalent_size_matched"] == 0 and base["equivalent_size_r2"] is None
    # T falls back to this run's own tokens per request
    assert quick["equivalent_tpm"] / base["equivalent_tpm"] == pytest.approx(1.5, rel=0.15)


def test_requests_in_the_warm_up_hold_the_engine_but_are_not_measured(sim):
    at, u, o, prod, _ = sim
    logged = logged_from(at, prod, u, o)
    out = eq.equivalent_throughput(logged, ran_from(at, prod, u, o), warmup_s=at[1500])
    assert out["equivalent_tpm"] == pytest.approx(out["equivalent_tpm_logged"], rel=1e-9)
    assert out["equivalent_tpm"] != eq.equivalent_throughput(logged, ran_from(at, prod, u, o))["equivalent_tpm"]


def test_light_traffic_is_flagged_by_the_busy_share():
    # The score is over the busy time there is, however little: the busy
    # share is what says it carries little.
    at, u, o, works = traffic(n=400, lam=0.01)
    end = ps_sim(at, works, base_rate)
    out = eq.equivalent_throughput(logged_from(at, end, u, o), ran_from(at, end, u, o))
    assert out["equivalent_busy_share"] < 0.02


def test_unsent_and_failed_requests():
    at, u, o, works = traffic(n=1500)
    end = ps_sim(at, works, base_rate)
    ran = ran_from(at, end, u, o)
    for i in range(0, len(ran), 10):
        ran[i] = eq.Ran(send_s=None, end_s=None)
    for i in range(5, len(ran), 10):
        ran[i].ok = False
    out = eq.equivalent_throughput(logged_from(at, end, u, o), ran)
    assert out["equivalent_tpm"] is not None and out["equivalent_coverage"] > 0.5


def test_logged_of_reads_timing_and_usage():
    q = eq.logged_of({"at_s": 1.5, "rt_s": 2.0},
                     {"prompt_tokens": 1000, "completion_tokens": 50,
                      "prompt_tokens_details": {"cached_tokens": 600}})
    assert (q.at_s, q.rt_s, q.uncached, q.cached, q.output) == (1.5, 2.0, 400, 600, 50)
    assert eq.logged_of({"at_s": 1.0, "rt_s": 1.0}, None).has_tokens is False
    assert eq.logged_of({"at_s": 1.0}, None) is None
    assert eq.logged_of(None, {"prompt_tokens": 1}) is None


# ---------------------------------------------------------------------------
# The replay runner: records loaded lean keep the usage, the metrics and the
# curve file come out of a timed run's results
# ---------------------------------------------------------------------------

def _write(tmp_path, at, end, u, o):
    body = json.dumps({"model": "m", "messages": [{"role": "user", "content": "hi"}]})
    lines = []
    for i, (a, e, x, y) in enumerate(zip(at, end, u, o)):
        lines.append(json.dumps({
            "source_file": "t", "line_no": i + 1, "level": "info", "timestamp": "",
            "request_id": f"r{i}", "channel_id": None, "token_name": None,
            "request_body": body, "request_json": None,
            "raw_payload": {"resp_meta": {"usage": {"prompt_tokens": int(x), "completion_tokens": int(y)}}},
            "timing": {"at_s": a, "rt_s": e - a, "after": None, "think_s": None, "session": ""},
        }))
    p = tmp_path / "timed.jsonl"
    p.write_text("\n".join(lines))
    return p


def test_the_runner_reports_the_score_and_writes_the_curve(tmp_path):
    from bench.replay_test.log_replay_tool import load_extract_jsonl
    from bench.tests.functional.replay import ReplayTest

    at, u, o, works = traffic(n=1500)
    prod = ps_sim(at, works, base_rate)
    ds = _write(tmp_path, at, prod, u, o)
    records = list(load_extract_jsonl(str(ds), lean=True))
    assert records[0].raw_payload == {}
    assert records[0].recorded_usage == {"prompt_tokens": int(u[0]), "completion_tokens": int(o[0])}

    test = ReplayTest(api_url="http://127.0.0.1:1/v1", model="m", api_key="",
                      output_dir=str(tmp_path / "out"), dataset_path=str(ds),
                      schedule="timed", flush_cache=False, warmup_s=0.0)
    results = [{"due_s": a, "lag_s": 0.0, "end_s": e, "success": True,
                "prompt_tokens": int(x), "completion_tokens": int(y)}
               for a, e, x, y in zip(at, prod, u, o)]
    m = test._equivalent_metrics(records, results)
    assert m["equivalent_tpm"] == pytest.approx(m["equivalent_tpm_logged"], rel=1e-6)
    assert m["equivalent_size_matched"] == 1
    curve = m["equivalent_curve"]
    assert {"level", "time_share", "in_flight", "latency_s", "requests", "tpm"} <= set(curve[0])
    assert 0.5 < sum(c["time_share"] for c in curve) <= 1.0 + 1e-6   # the rest is idle
    assert json.loads(json.dumps(m)) == m   # stored as JSON with the result

    # The kept requests and the dataset alone give the score back.
    kept = m["equivalent_requests"]
    assert len(kept["send_s"]) == len(kept["end_s"]) == len(kept["ok"]) == len(kept["tokens"]) == 1500
    assert kept["ok"][0] == 1 and kept["tokens"][0] == int(u[0]) + int(o[0])
    ran = [eq.Ran(s, e, bool(k), float(t))
           for s, e, k, t in zip(kept["send_s"], kept["end_s"], kept["ok"], kept["tokens"])]
    again = eq.equivalent_throughput(
        [eq.logged_of(r.timing, r.recorded_usage) for r in records], ran, 0.0)
    assert again["equivalent_tpm"] == pytest.approx(m["equivalent_tpm"], rel=1e-3)


def test_the_requests_are_kept_when_the_score_fails_and_not_past_the_cap(tmp_path, monkeypatch):
    from bench.replay_test.log_replay_tool import load_extract_jsonl
    from bench.tests.functional import replay as runner

    at, u, o, works = traffic(n=200)
    end = ps_sim(at, works, base_rate)
    ds = _write(tmp_path, at, end, u, o)
    records = list(load_extract_jsonl(str(ds), lean=True))
    test = runner.ReplayTest(api_url="http://127.0.0.1:1/v1", model="m", api_key="",
                             output_dir=str(tmp_path / "out"), dataset_path=str(ds),
                             schedule="timed", flush_cache=False, warmup_s=0.0)
    results = [{"due_s": a, "lag_s": 0.0, "end_s": e, "success": True, "prompt_tokens": 1, "completion_tokens": 1}
               for a, e in zip(at, end)]
    results[3] = {"due_s": at[3], "end_s": None, "success": False, "error_class": "canceled"}

    def broken(*a, **k):
        raise RuntimeError("scoring bug")
    monkeypatch.setattr(runner.equivalent, "equivalent_throughput", broken)
    m = test._equivalent_metrics(records, results)
    assert set(m) == {"equivalent_requests"}
    assert m["equivalent_requests"]["send_s"][3] is None and m["equivalent_requests"]["ok"][3] == 0

    monkeypatch.undo()
    monkeypatch.setattr(runner, "EQUIVALENT_MAX_KEPT_REQUESTS", 100)
    m = test._equivalent_metrics(records, results)
    assert "equivalent_requests" not in m and m["equivalent_tpm"] is not None
