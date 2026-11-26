"""How loaded a backend is, read from its gateway logs alone.

Production traffic rises and falls, so a backend has already been observed at
many levels of concurrency. For every logged request we know how many requests
were in flight while it ran (C_req, averaged over its lifetime). Group the
requests by that number and ask how much slower **the same request** is in a
busier group.

"The same request" needs a model, because busy moments do not carry the same
requests as quiet ones (on real traffic they carried smaller ones). Each
request's latency is modelled as

    latency = slowdown(group) × size(request)
    size    = a + b·uncached prompt + c·cached prompt + d·output tokens

and both are fitted together over every request. Between neighbouring groups

    e = 1 − ln(S₂/S₁) / ln(C₂/C₁)

is, by Little's law, the share of the extra concurrency that became
throughput. The knee is the first group where e drops below 0.5; w_log is the
share of requests in the knee group and above, and sorts the backend:

    idle      w_log < 0.10
    moderate  0.10 ≤ w_log < 0.50
    busy      w_log ≥ 0.50

A fit that fails a hard check (R² too low, or the even-day and odd-day halves
of the window giving w_log more than 0.15 apart) is reported as `unclear`,
never as a category. A drift by time of day or a curve that crosses the knee
more than once is reported as a note.

Pure functions, no I/O: the builder feeds it rows, tests feed it synthetic
ones. See docs/replay-datasets.md, "Load reading".
"""
from __future__ import annotations

import bisect
import math
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable

VERSION = 1

IDLE_BELOW = 0.10
BUSY_FROM = 0.50
MIN_GROUP = 300
# The regression is fitted on at most this many requests (an even stride over
# all of them); C_req itself always uses every request.
MAX_FIT = 300_000
# Fit checks. R² on log latency; residual medians per 6-hour UTC block (log
# units, 0.10 ≈ 10 %); the even-day and odd-day halves must agree on the
# category and put the knee at most one group apart.
MIN_R2 = 0.5
MAX_BLOCK_RESIDUAL = 0.10
MAX_HALVES_W_GAP = 0.15
# Checks that withhold a category when they fail. The others are shown as
# notes: worth knowing, but not by themselves a reason to distrust the answer.
HARD_CHECKS = ("r2", "halves_agree")

USAGE = "resp_meta.usage"
TIMING_FIELDS = (
    "_time", "ts", "rt",
    f"{USAGE}.prompt_tokens", f"{USAGE}.completion_tokens",
    f"{USAGE}.prompt_tokens_details.cached_tokens",
    # Some stores also carry the counts at the top level.
    "prompt_tokens", "completion_tokens",
)


# ---------------------------------------------------------------------------
# rows → requests
# ---------------------------------------------------------------------------


def _num(value: Any) -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _ts(value: Any) -> float | None:
    v = _num(value)
    if v is not None:
        return v / 1000 if v > 1e11 else v
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()


def _get(record: dict, dotted: str) -> Any:
    """A nested value, or the same name stored flat (VictoriaLogs rows)."""
    if dotted in record:
        return record[dotted]
    cur: Any = record
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def request_of(record: dict) -> tuple[float, float, float, float, float] | None:
    """(start, latency s, uncached prompt, cached prompt, output tokens), or
    None when the record lacks the timing or the token counts."""
    start, rt = _ts(record.get("ts")), _num(record.get("rt"))
    if start is None:
        start = _ts(record.get("_time"))
    p = _num(_get(record, f"{USAGE}.prompt_tokens"))
    if p is None:
        p = _num(record.get("prompt_tokens"))
    o = _num(_get(record, f"{USAGE}.completion_tokens"))
    if o is None:
        o = _num(record.get("completion_tokens"))
    if start is None or rt is None or rt <= 0 or p is None or o is None or p < 0 or o < 0:
        return None
    k = min(max(_num(_get(record, f"{USAGE}.prompt_tokens_details.cached_tokens")) or 0.0, 0.0), p)
    return (start, rt, p - k, k, o)


# ---------------------------------------------------------------------------
# concurrency
# ---------------------------------------------------------------------------


def per_request_concurrency(spans: list[tuple[float, float]]) -> list[float]:
    """C_req for each (start, end) span, in input order: the average number of
    requests in flight (itself included) over its own lifetime.

    C(t) is a step function, so its running integral is piecewise linear and
    C_req = (I(end) − I(start)) / (end − start)."""
    events: list[tuple[float, int]] = []
    for s, e in spans:
        events.append((s, 1))
        events.append((max(e, s), -1))
    events.sort(key=lambda x: (x[0], x[1]))      # at a tie, ends first
    times: list[float] = []
    level_after: list[int] = []
    integral: list[float] = []
    cur, acc, prev = 0, 0.0, None
    for t, d in events:
        if prev is not None:
            acc += cur * (t - prev)
        cur += d
        if times and times[-1] == t:
            level_after[-1] = cur
            integral[-1] = acc
        else:
            times.append(t)
            level_after.append(cur)
            integral.append(acc)
        prev = t

    def integral_at(t: float) -> float:
        i = bisect.bisect_right(times, t) - 1
        return 0.0 if i < 0 else integral[i] + level_after[i] * (t - times[i])

    out = []
    for s, e in spans:
        if e > s:
            out.append((integral_at(e) - integral_at(s)) / (e - s))
        else:
            i = bisect.bisect_right(times, s) - 1
            out.append(float(max(1, level_after[i] if i >= 0 else 1)))
    return out


# ---------------------------------------------------------------------------
# the fit
# ---------------------------------------------------------------------------


def _solve(a: list[list[float]], b: list[float]) -> list[float]:
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for i in range(n):
        p = max(range(i, n), key=lambda r: abs(m[r][i]))
        m[i], m[p] = m[p], m[i]
        if abs(m[i][i]) < 1e-300:
            raise ValueError("singular system")
        for r in range(n):
            if r != i:
                f = m[r][i] / m[i][i]
                m[r] = [x - f * y for x, y in zip(m[r], m[i])]
    return [m[i][n] / m[i][i] for i in range(n)]


def _x(q: tuple) -> tuple[float, float, float, float]:
    return (1.0, q[2] / 1000, q[3] / 1000, q[4] / 1000)


def fit(reqs: list[tuple], groups: list[int], labels: list[int | None], iters: int = 40) -> dict[str, Any]:
    """latency ≈ S(group) × (a + b·u + c·k + d·o), relative error minimised.

    Alternates two steps until S stops moving: least squares for the size
    coefficients with S fixed (each request's error relative to its own
    latency), then S per group = median of latency ÷ size. S is renormalised
    so the least loaded group is 1."""
    data = [(q, g) for q, g in zip(reqs, labels) if g is not None]
    xs = [_x(q) for q, _ in data]
    # Σ x xᵀ / r² and Σ x / r per group: with S fixed the normal equations are
    # Σ_g S_g² G_g β = Σ_g S_g h_g, so the data is read once for them.
    gram: dict[int, list[list[float]]] = {g: [[0.0] * 4 for _ in range(4)] for g in groups}
    lin: dict[int, list[float]] = {g: [0.0] * 4 for g in groups}
    for (q, g), x in zip(data, xs):
        r = q[1]
        G, h = gram[g], lin[g]
        for i in range(4):
            h[i] += x[i] / r
            for j in range(4):
                G[i][j] += x[i] * x[j] / (r * r)
    s = {g: 1.0 for g in groups}
    beta = [1.0, 0.0, 0.0, 0.01]
    for _ in range(iters):
        ata = [[sum(s[g] ** 2 * gram[g][i][j] for g in groups) for j in range(4)] for i in range(4)]
        atb = [sum(s[g] * lin[g][i] for g in groups) for i in range(4)]
        # A little ridge: a size term that never varies (every prompt the same
        # length) would otherwise leave the system singular.
        lam = 1e-6 * sum(ata[i][i] for i in range(4))
        beta = _solve([[v + (lam if i == j else 0.0) for j, v in enumerate(row)]
                       for i, row in enumerate(ata)], atb)
        ratios: dict[int, list[float]] = defaultdict(list)
        for (q, g), x in zip(data, xs):
            size = beta[0] + beta[1] * x[1] + beta[2] * x[2] + beta[3] * x[3]
            if size > 0:
                ratios[g].append(q[1] / size)
        new = {g: (statistics.median(ratios[g]) if ratios[g] else s[g]) for g in groups}
        ref = new[groups[0]]
        beta = [b * ref for b in beta]
        new = {g: v / ref for g, v in new.items()}
        done = max(abs(new[g] - s[g]) for g in groups) < 1e-5
        s = new
        if done:
            break
    resid: list[tuple[float, float]] = []          # (start, log residual)
    log_r: list[float] = []
    for (q, g), x in zip(data, xs):
        size = beta[0] + beta[1] * x[1] + beta[2] * x[2] + beta[3] * x[3]
        if size > 0:
            resid.append((q[0], math.log(q[1] / (s[g] * size))))
            log_r.append(math.log(q[1]))
    var_r = statistics.pvariance(log_r) if len(log_r) > 1 else 0.0
    var_e = statistics.pvariance([e for _, e in resid]) if len(resid) > 1 else 0.0
    by_block: dict[int, list[float]] = defaultdict(list)
    for start, e in resid:
        by_block[datetime.fromtimestamp(start, timezone.utc).hour // 6].append(e)
    return {
        "slowdown": s,
        "size_coefficients_s": dict(zip(
            ("fixed", "per_1k_uncached", "per_1k_cached", "per_1k_output"), beta)),
        "r2_log": (1 - var_e / var_r) if var_r else None,
        "residual_by_utc_block": {f"{b * 6:02d}-{b * 6 + 6:02d}h": statistics.median(v)
                                  for b, v in sorted(by_block.items())},
    }


# ---------------------------------------------------------------------------
# reading the curve
# ---------------------------------------------------------------------------


# Concurrency levels requests are grouped on: nearest in log terms. Unit steps
# at the bottom, then 20–33 % apart, so that every step changes C by enough for
# e = 1 − Δln S / Δln C not to be swamped by noise in S. (One unit at C = 50
# is a 2 % step: a 1 % wobble in S would swing e by ±0.5.)
LEVELS = (1, 2, 3, 4, 5, 6, 8, 10, 12, 16, 20, 24, 32, 40, 48, 64, 80, 96, 128,
          160, 192, 256, 320, 384, 512)


def level_of(c: float) -> int:
    if c <= 1:
        return 1
    lc = math.log(c)
    return min(LEVELS, key=lambda lv: abs(math.log(lv) - lc))


def assign_groups(creq: list[float], min_group: int) -> tuple[list[int], list[int | None]]:
    """Group = the nearest of LEVELS, the sparse top folded into the highest
    group with at least `min_group` requests. Returns (groups kept, label per
    request)."""
    rounded = [level_of(c) for c in creq]
    counts = Counter(rounded)
    kept = [g for g in sorted(counts) if counts[g] >= min_group]
    if not kept:
        return [], [None] * len(creq)
    top = kept[-1]
    keep = set(kept)
    return kept, [g if g in keep else (top if g > top else None) for g in rounded]


def elasticities(s: dict[int, float], c_mean: dict[int, float]) -> list[dict]:
    pts = [(g, s[g]) for g in sorted(s) if s.get(g)]
    out = []
    for (g1, s1), (g2, s2) in zip(pts, pts[1:]):
        c1, c2 = c_mean[g1], c_mean[g2]
        e = 1 - math.log(s2 / s1) / math.log(c2 / c1) if c2 > c1 else None
        out.append({"from": g1, "to": g2, "e": e})
    return out


def category_of(w: float, idle_below: float = IDLE_BELOW, busy_from: float = BUSY_FROM) -> str:
    return "idle" if w < idle_below else "busy" if w >= busy_from else "moderate"


def classify(es: list[dict], share_from: dict[int, float],
             idle_below: float = IDLE_BELOW, busy_from: float = BUSY_FROM) -> dict[str, Any]:
    """Knee = first group entered with e < 0.5; w_log = share of requests in
    it and above (1 when even the first step is past the knee)."""
    knee = next((x["to"] for x in es if x["e"] is not None and x["e"] < 0.5), None)
    w = share_from.get(knee, 0.0) if knee is not None else 0.0
    if es and es[0]["e"] is not None and es[0]["e"] < 0.5:
        # Already past the knee between the two least loaded groups: every
        # request waits, the least loaded included.
        knee, w = es[0]["from"], 1.0
    crossings = sum(1 for a, b in zip(es, es[1:]) if a["e"] is not None and b["e"] is not None
                    and (a["e"] < 0.5) != (b["e"] < 0.5))
    return {"knee_group": knee, "w_log": w, "category": category_of(w, idle_below, busy_from),
            "crossings": crossings}


def _stride(items: list, n: int) -> list:
    if len(items) <= n:
        return items
    step = len(items) / n
    return [items[int(i * step)] for i in range(n)]


def _read(reqs: list[tuple], creq: list[float], min_group: int, idle_below: float,
          busy_from: float, max_fit: int) -> dict[str, Any] | None:
    """Groups, fit and classification for one set of requests; None when fewer
    than two groups can be formed."""
    groups, labels = assign_groups(creq, min_group)
    if len(groups) < 2:
        return None
    idx = _stride([i for i, g in enumerate(labels) if g is not None], max_fit)
    sub_reqs = [reqs[i] for i in idx]
    sub_labels = [labels[i] for i in idx]
    reg = fit(sub_reqs, groups, sub_labels)
    members: dict[int, list[int]] = defaultdict(list)
    for i, g in enumerate(labels):
        if g is not None:
            members[g].append(i)
    n = sum(len(m) for m in members.values())
    c_mean = {g: statistics.fmean(creq[i] for i in members[g]) for g in groups}
    share_from = {g: sum(len(members[h]) for h in groups if h >= g) / n for g in groups}
    es = elasticities(reg["slowdown"], c_mean)
    raw_med = {g: statistics.median(reqs[i][1] for i in members[g]) for g in groups}
    raw = {g: raw_med[g] / raw_med[groups[0]] for g in groups}
    return {"groups": groups, "members": members, "c_mean": c_mean, "share_from": share_from,
            "reg": reg, "e": es, "raw": raw, "verdict": classify(es, share_from, idle_below, busy_from)}


def analyse(
    requests: Iterable[tuple],
    *,
    min_group: int = MIN_GROUP,
    idle_below: float = IDLE_BELOW,
    busy_from: float = BUSY_FROM,
    max_fit: int = MAX_FIT,
) -> dict[str, Any]:
    """The load reading of a set of requests (from `request_of`)."""
    reqs = sorted(requests, key=lambda q: q[0])
    out: dict[str, Any] = {
        "version": VERSION,
        "requests": len(reqs),
        "thresholds": {"idle_below": idle_below, "busy_from": busy_from, "min_group": min_group},
    }
    if not reqs:
        return {**out, "status": "no_traffic", "category": "unclear",
                "note": "no requests with timing and token counts in the window"}
    creq = per_request_concurrency([(q[0], q[0] + q[1]) for q in reqs])
    out.update(_concurrency_summary(reqs, creq))

    full = _read(reqs, creq, min_group, idle_below, busy_from, max_fit)
    if full is None:
        # Not enough requests at a second concurrency level to compare. When
        # almost nothing ever overlapped, that IS the answer: idle.
        overlapped = sum(1 for c in creq if round(c) >= 2) / len(creq)
        if len(reqs) >= min_group and overlapped < idle_below:
            return {**out, "status": "ok", "category": "idle", "w_log": 0.0, "knee_group": None,
                    "groups": [], "overlapped_share": overlapped,
                    "note": f"only {overlapped:.0%} of requests ran with two or more "
                            "in flight: too few to reach any knee"}
        return {**out, "status": "too_little_traffic", "category": "unclear", "groups": [],
                "note": f"fewer than two concurrency levels with {min_group} requests each"}

    reg, verdict = full["reg"], full["verdict"]
    e_into = {x["to"]: x["e"] for x in full["e"]}
    groups_out = []
    for g in full["groups"]:
        m = full["members"][g]
        prompt = sum(reqs[i][2] + reqs[i][3] for i in m)
        groups_out.append({
            "group": g,
            "requests": len(m),
            "c_mean": full["c_mean"][g],
            "share_from": full["share_from"][g],
            "slowdown": reg["slowdown"][g],
            "raw_slowdown": full["raw"][g],
            "e_into": e_into.get(g),
            "latency_median_s": statistics.median(reqs[i][1] for i in m),
            "prompt_mean": prompt / len(m),
            "output_mean": statistics.fmean(reqs[i][4] for i in m),
            "cached_share": (sum(reqs[i][3] for i in m) / prompt) if prompt else None,
        })

    halves = _halves(reqs, creq, min_group, idle_below, busy_from, max_fit)
    checks = _checks(reg, verdict, halves)
    trusted = all(checks[k] is not False for k in HARD_CHECKS)
    return {
        **out,
        "status": "ok" if trusted else "unclear",
        "category": verdict["category"] if trusted else "unclear",
        "fitted_category": verdict["category"],
        "knee_group": verdict["knee_group"],
        "w_log": verdict["w_log"],
        "crossings": verdict["crossings"],
        "groups": groups_out,
        "fit": {
            "r2_log": reg["r2_log"],
            "residual_by_utc_block": reg["residual_by_utc_block"],
            "size_coefficients_s": reg["size_coefficients_s"],
            "halves": halves,
            "checks": checks,
            "hard_checks": list(HARD_CHECKS),
        },
    }


def _concurrency_summary(reqs: list[tuple], creq: list[float]) -> dict[str, Any]:
    hours: Counter = Counter(int(q[0] // 3600) for q in reqs)
    busy = {h for h, _ in hours.most_common(max(1, round(len(hours) * 0.05)))}
    peak = [c for q, c in zip(reqs, creq) if int(q[0] // 3600) in busy]
    top = max(creq)
    levels = [c for c in (1, 2, 3, 4, 6, 8, 12, 16, 24, 32, 48, 64, 96, 128) if c <= top + 1]
    return {
        "window_s": reqs[-1][0] + reqs[-1][1] - reqs[0][0],
        "c_typ": statistics.median(creq),
        "c_peak": statistics.median(peak),
        "c_req_p90": statistics.quantiles(creq, n=10)[-1] if len(creq) >= 10 else max(creq),
        "c_req_max": top,
        "share_at_or_above": {str(c): sum(1 for x in creq if x >= c) / len(creq) for c in levels},
    }


def _halves(reqs, creq, min_group, idle_below, busy_from, max_fit) -> dict[str, Any]:
    """The same reading on even and odd UTC days."""
    out: dict[str, Any] = {}
    for name, parity in (("even_days", 0), ("odd_days", 1)):
        idx = [i for i, q in enumerate(reqs) if (int(q[0] // 86400) % 2) == parity]
        part = _read([reqs[i] for i in idx], [creq[i] for i in idx], min_group,
                     idle_below, busy_from, max_fit) if idx else None
        out[name] = None if part is None else {
            k: part["verdict"][k] for k in ("knee_group", "w_log", "category")}
    return out


def _checks(reg: dict, verdict: dict, halves: dict) -> dict[str, bool | None]:
    """True = passed, False = failed, None = could not be checked. Only a
    failed HARD_CHECKS makes the reading unclear."""
    r2 = reg["r2_log"]
    blocks = reg["residual_by_utc_block"].values()
    even, odd = halves.get("even_days"), halves.get("odd_days")
    # The two halves must agree on the number the decision is made from.
    agree = None if not (even and odd) else abs(even["w_log"] - odd["w_log"]) <= MAX_HALVES_W_GAP
    return {
        "r2": None if r2 is None else r2 >= MIN_R2,
        "no_drift_by_time_of_day": (max(abs(v) for v in blocks) <= MAX_BLOCK_RESIDUAL) if blocks else None,
        "halves_agree": agree,
        "single_crossing": verdict["crossings"] <= 1,
    }
