"""The load reading (app.datasets.load_profile): slowdown by concurrency group
from logs, corrected for request mix, and the idle / moderate / busy verdict.

Two kinds of synthetic traffic:
- requests with a known slowdown per group and a mix that shifts with load,
  to check the regression separates "bigger requests" from "busier engine";
- a simulated server that serves `slots` requests at full speed and shares
  itself beyond that (processor sharing), fed with arrivals at a chosen rate,
  so the overlaps — and the knee at about `slots` — are real.
"""
from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone

import pytest

from app.datasets import load_profile as lp

DAY0 = datetime(2026, 11, 2, tzinfo=timezone.utc).timestamp()


# --- known slowdown, shifting mix --------------------------------------------------

TRUE_S = {1: 1.0, 2: 1.05, 3: 1.2, 4: 1.6}


def _labelled(n_per_group=2000, seed=1):
    """Busier groups carry longer prompts and outputs, so raw latency
    overstates their slowdown."""
    rng = random.Random(seed)
    reqs, labels = [], []
    for g, s in TRUE_S.items():
        for _ in range(n_per_group):
            o = rng.lognormvariate(math.log(150 * (1 + 0.5 * (g - 1))), 0.8)
            u = rng.lognormvariate(math.log(800 * g), 0.7)
            k = rng.uniform(0, 4) * u
            size = 0.3 + 0.2 * u / 1000 + 0.02 * k / 1000 + 4.0 * o / 1000
            reqs.append((DAY0 + rng.uniform(0, 86400), s * size * rng.lognormvariate(0, 0.1), u, k, o))
            labels.append(g)
    return reqs, labels


def test_fit_recovers_the_slowdown_despite_the_mix():
    reqs, labels = _labelled()
    reg = lp.fit(reqs, sorted(TRUE_S), labels)
    for g, s in TRUE_S.items():
        assert reg["slowdown"][g] == pytest.approx(s, rel=0.03), g
    assert reg["size_coefficients_s"]["per_1k_output"] == pytest.approx(4.0, abs=0.3)
    assert reg["r2_log"] > 0.8
    # Raw medians mistake size for waiting.
    raw = {g: sorted(q[1] for q, l in zip(reqs, labels) if l == g)[1000] for g in TRUE_S}
    assert raw[4] / raw[1] > 1.4 * TRUE_S[4]


def test_fit_with_a_constant_prompt_length_is_not_singular():
    reqs = [(DAY0 + i, (0.5 + 0.004 * (50 + i % 200)) * (1 + 0.2 * (i % 2)), 1000.0, 0.0, 50.0 + i % 200)
            for i in range(2000)]
    labels = [1 + i % 2 for i in range(2000)]
    reg = lp.fit(reqs, [1, 2], labels)
    assert reg["slowdown"][2] == pytest.approx(1.2, rel=0.02)


def test_classify():
    share = {1: 1.0, 2: 0.6, 3: 0.3, 4: 0.05}
    c = {1: 1, 2: 2, 3: 3, 4: 4}
    v = lp.classify(lp.elasticities({1: 1.0, 2: 1.02, 3: 1.05, 4: 1.6}, c), share)
    assert v["knee_group"] == 4 and v["category"] == "idle"
    assert lp.classify(lp.elasticities({1: 1.0, 2: 1.05, 3: 1.5, 4: 2.5}, c), share)["category"] == "moderate"
    assert lp.classify(lp.elasticities({1: 1.0, 2: 1.8, 3: 2.7, 4: 3.6}, c), share)["category"] == "busy"
    v = lp.classify(lp.elasticities({1: 1.0, 2: 1.01, 3: 1.02, 4: 1.03}, c), share)
    assert v == {"knee_group": None, "w_log": 0.0, "category": "idle", "crossings": 0}
    # Past the knee from the first step: every request waits.
    v = lp.classify(lp.elasticities({1: 1.0, 2: 1.9, 3: 2.8, 4: 3.7}, c), share)
    assert v["knee_group"] == 1 and v["w_log"] == 1.0


# --- a simulated server ------------------------------------------------------------


def _serve(arrivals: list[tuple[float, float, float]], slots: int) -> list[tuple]:
    """Processor sharing: n requests in flight each progress at min(1, slots/n)
    work-seconds per second. `arrivals` = (time, work s, output tokens), sorted.
    Returns `request_of` tuples."""
    out = []
    active: dict[int, list[float]] = {}          # id -> [remaining work, start, output]
    t = 0.0
    i = 0
    while i < len(arrivals) or active:
        rate = min(1.0, slots / len(active)) if active else 0.0
        next_arrival = arrivals[i][0] if i < len(arrivals) else math.inf
        if active:
            rid = min(active, key=lambda r: active[r][0])
            next_done = t + active[rid][0] / rate
        else:
            next_done = math.inf
        step_to = min(next_arrival, next_done)
        if active:
            for st in active.values():
                st[0] -= (step_to - t) * rate
        t = step_to
        if next_done <= next_arrival:
            rem, start, o = active.pop(rid)
            out.append((start, t - start, 500.0, 2000.0, o))
        else:
            at, work, o = arrivals[i]
            active[i] = [work, at, o]
            i += 1
    return out


def _traffic(rate_of_hour, days=4.0, seed=3):
    """Poisson arrivals whose rate (req/s) depends on the UTC hour; each needs
    1 s + 20 ms per output token of work (5.2 s on average, so 4 slots serve
    about 0.77 req/s)."""
    rng = random.Random(seed)
    arrivals, t = [], DAY0
    end = DAY0 + days * 86400
    while t < end:
        rate = rate_of_hour(int((t - DAY0) // 3600) % 24)
        t += rng.expovariate(rate)
        o = rng.randint(20, 400)
        arrivals.append((t, 1.0 + 0.02 * o, float(o)))
    return arrivals


def test_a_quiet_server_reads_idle():
    reqs = _serve(_traffic(lambda h: 0.08), slots=4)
    r = lp.analyse(reqs, min_group=200)
    assert r["category"] == "idle", r.get("note") or r["groups"]
    assert r["status"] == "ok"


def test_a_server_past_its_slots_most_of_the_time_reads_busy():
    # 0.7 req/s against ~0.77 of capacity: usually more than 4 in flight.
    reqs = _serve(_traffic(lambda h: 0.7, days=0.5), slots=4)
    r = lp.analyse(reqs, min_group=200)
    assert r["fitted_category"] == "busy", r["groups"]
    assert 3 <= r["knee_group"] <= 6


def test_a_diurnal_server_has_its_knee_near_its_slots():
    reqs = _serve(_traffic(lambda h: 0.7 if 8 <= h < 14 else 0.08, days=2), slots=4)
    r = lp.analyse(reqs, min_group=200)
    assert r["fitted_category"] in ("moderate", "busy")
    assert 3 <= r["knee_group"] <= 6
    groups = {g["group"]: g for g in r["groups"]}
    assert groups[1]["slowdown"] == 1.0
    assert r["c_peak"] > r["c_typ"]
    assert set(r["fit"]["checks"]) == {"r2", "no_drift_by_time_of_day", "halves_agree", "single_crossing"}


def test_almost_no_overlap_is_idle_with_one_group():
    reqs = [(DAY0 + 60 * i, 0.5, 100.0, 0.0, 50.0) for i in range(1000)]
    r = lp.analyse(reqs)
    assert r["category"] == "idle" and r["w_log"] == 0.0
    assert r["overlapped_share"] < 0.10 and "too few to reach any knee" in r["note"]


def test_too_little_traffic_is_unclear():
    r = lp.analyse([(DAY0 + i, 1.0, 1.0, 0.0, 1.0) for i in range(50)])
    assert r["status"] == "too_little_traffic" and r["category"] == "unclear"
    assert lp.analyse([])["status"] == "no_traffic"


def test_a_drift_by_time_of_day_is_a_note_not_a_reason_to_withhold():
    reqs = _serve(_traffic(lambda h: 0.7 if 8 <= h < 14 else 0.08, days=2), slots=4)
    # Something the size formula cannot see makes requests 60 % slower at night.
    drifted = [(q[0], q[1] * (1.6 if datetime.fromtimestamp(q[0], timezone.utc).hour < 6 else 1.0),
                *q[2:]) for q in reqs]
    r = lp.analyse(drifted, min_group=200)
    assert r["fit"]["checks"]["no_drift_by_time_of_day"] is False
    assert r["category"] == r["fitted_category"] != "unclear" and r["status"] == "ok"


def test_latency_that_size_does_not_explain_is_unclear():
    rng = random.Random(5)
    reqs = _serve(_traffic(lambda h: 0.7 if 8 <= h < 14 else 0.08, days=2), slots=4)
    noisy = [(q[0], q[1] * rng.lognormvariate(0, 1.5), *q[2:]) for q in reqs]
    r = lp.analyse(noisy, min_group=200)
    assert r["fit"]["checks"]["r2"] is False
    assert r["category"] == "unclear" and r["status"] == "unclear"


def test_levels_are_log_spaced():
    assert [lp.level_of(c) for c in (0.4, 1.4, 2.6, 7.1, 9.0, 13.9, 14.1, 50.0, 57.0)] == \
        [1, 1, 3, 8, 10, 16, 16, 48, 64]
    assert all(b / a <= 2 for a, b in zip(lp.LEVELS, lp.LEVELS[1:]))


# --- reading records ---------------------------------------------------------------


def test_request_of_reads_nested_flat_and_top_level_counts():
    nested = {"ts": "2026-11-04T10:00:00+08:00", "rt": 2.5,
              "resp_meta": {"usage": {"prompt_tokens": 1000, "completion_tokens": 80,
                                      "prompt_tokens_details": {"cached_tokens": 600}}}}
    assert lp.request_of(nested) == (datetime(2026, 11, 4, 2, tzinfo=timezone.utc).timestamp(),
                                     2.5, 400.0, 600.0, 80.0)
    flat = {"ts": "1762221600.0", "rt": "2.5", "resp_meta.usage.prompt_tokens": "1000",
            "resp_meta.usage.completion_tokens": "80"}
    assert lp.request_of(flat)[2:] == (1000.0, 0.0, 80.0)
    top = {"_time": "2026-11-04T02:00:00Z", "rt": "1", "prompt_tokens": "10", "completion_tokens": "2"}
    assert lp.request_of(top)[0] == datetime(2026, 11, 4, 2, tzinfo=timezone.utc).timestamp()
    assert lp.request_of({"ts": 1762221600000, "rt": 1, "prompt_tokens": 1, "completion_tokens": 1})[0] \
        == 1762221600.0
    assert lp.request_of({"ts": "x", "rt": 1}) is None
    assert lp.request_of({**top, "rt": "0"}) is None
    # Cached tokens never exceed the prompt.
    assert lp.request_of({**top, "resp_meta.usage.prompt_tokens_details.cached_tokens": "99"})[2:4] == (0.0, 10.0)


# --- the builder's call ------------------------------------------------------------


class _Source:
    def __init__(self, rows=(), cheap=True, fail=False):
        self.rows, self.timing_is_cheap, self.fail = list(rows), cheap, fail
        self.asked: list[tuple] = []

    def iter_window(self, *a, **k):
        return iter(())

    def iter_timing(self, filters, start, end):
        self.asked.append((filters, start, end))
        if self.fail:
            raise RuntimeError("store unreachable")
        return iter(self.rows)


def test_measure_load_window_and_failure():
    from app.datasets.builder import BuildProfile, measure_load

    end = datetime(2026, 11, 4, tzinfo=timezone.utc)
    rows = [{"ts": DAY0 + 60 * i, "rt": 0.5, "prompt_tokens": 100, "completion_tokens": 50}
            for i in range(1000)]
    src = _Source(rows)
    r = measure_load(src, BuildProfile(name="p"), end)
    filters, start, stop = src.asked[0]
    assert stop - start == timedelta(hours=168) and not filters.exclude_truncated
    assert r["category"] == "idle" and r["rows"] == 1000 and r["window_hours"] == 168

    files = _Source(rows, cheap=False)
    measure_load(files, BuildProfile(name="p", window_hours=24), end)
    assert files.asked[0][2] - files.asked[0][1] == timedelta(hours=24)

    failed = measure_load(_Source(fail=True), BuildProfile(name="p"), end)
    assert failed["status"] == "failed" and "unreachable" in failed["error"]

    assert measure_load(src, BuildProfile(name="p", load_window_hours=0), end) is None
