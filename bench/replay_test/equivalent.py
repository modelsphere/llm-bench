"""Equivalent throughput: what a timed replay says about a config's traffic
behind a least-connections load balancer.

A timed replay sends production's requests on their recorded timing, so every
config that keeps up serves the same tokens in the same time: the run's own
TPM says nothing. What differs is each request's latency at the concurrency it
ran at. A least-connections balancer keeps its backends at about the same
number of requests in flight, n, and by Little's law a backend at n completes
n / W(n) requests per second, W(n) being its mean latency at n. Over the time
production spends at each n:

    score = Σ p(n) · n̄(n) · T / W(n)      over n ≥ 2, × 60 → tokens/min

    p(n)   production's share of time at n in flight (the dataset's logged
           send times and latencies, `timing.at_s` / `timing.rt_s`)
    n̄(n)   production's mean in-flight count within that level
    T      production's mean tokens per request (prompt + completion, from the
           capture's usage block), the same for every config
    W(n)   this run's mean latency at n, with the requests' sizes matched to
           production's mix at n (below)

At n ≤ 1 a balancer has no choice to make, so those levels are left out. The
derivation and the checks on gateway logs are in docs/replay-datasets.md
("Equivalent throughput").

**Size matching.** Which requests run at a given n depends on the config: a
slower one pushes more of them up to busy levels. Each request gets a size
from its logged token counts, size = a + b·uncached + c·cached + d·output (per
1k tokens, the load reading's model), fitted once on production's logged
latencies, so it is the same number for every config. Then

    W(n) = s_ref(n) · Σ latency / Σ size     over this run's requests at n

with s_ref(n) the mean size of the requests production ran at n. Without
usable logged token counts every size is 1 and W(n) is the plain mean.

Pure functions, no I/O or clock: the replay hands over plain numbers.
"""
from __future__ import annotations

import bisect
import math
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

# Concurrency levels a request is grouped on, nearest in log terms (the load
# reading's levels).
LEVELS = (1, 2, 3, 4, 5, 6, 8, 10, 12, 16, 20, 24, 32, 40, 48, 64, 80, 96, 128,
          160, 192, 256, 320, 384, 512)
# Requests a level needs before its mean latency is used.
MIN_BUCKET = 20
# Requests a level needs to take part in the size fit.
MIN_FIT_GROUP = 30
# Share of the measured requests that must carry logged token counts for the
# sizes to be used.
MIN_SIZED_SHARE = 0.8


@dataclass
class Logged:
    """Production's side of one request, from the dataset."""
    at_s: float
    rt_s: float
    uncached: Optional[float] = None   # logged token counts; None when the
    cached: Optional[float] = None     # capture recorded no usage
    output: Optional[float] = None

    @property
    def has_tokens(self) -> bool:
        return self.uncached is not None and self.output is not None


@dataclass
class Ran:
    """This run's side of one request. send_s/end_s on the replay's clock;
    None when it was never sent."""
    send_s: Optional[float]
    end_s: Optional[float]
    ok: bool = False
    tokens: float = 0.0                # prompt + completion this run reported


def logged_of(timing: Optional[dict], usage: Optional[dict]) -> Optional[Logged]:
    """A Logged from a timed dataset line's `timing` block and its capture's
    usage block; None without a usable send time and latency."""
    t = timing if isinstance(timing, dict) else {}
    at, rt = _num(t.get("at_s")), _num(t.get("rt_s"))
    if at is None or rt is None or rt <= 0:
        return None
    u = usage if isinstance(usage, dict) else {}
    p, o = _num(u.get("prompt_tokens")), _num(u.get("completion_tokens"))
    if p is None or o is None or p < 0 or o < 0:
        return Logged(at, rt)
    details = u.get("prompt_tokens_details")
    k = _num(details.get("cached_tokens")) if isinstance(details, dict) else None
    k = min(max(k or 0.0, 0.0), p)
    return Logged(at, rt, p - k, k, o)


def _num(v: Any) -> Optional[float]:
    if isinstance(v, bool) or v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


# ---------------------------------------------------------------------------
# concurrency
# ---------------------------------------------------------------------------


def level_of(c: float) -> int:
    if c <= 1:
        return 1
    lc = math.log(c)
    return min(LEVELS, key=lambda lv: abs(math.log(lv) - lc))


class InFlight:
    """The number of requests in flight over time, from (start, end) spans."""

    def __init__(self, spans: Sequence[Tuple[float, float]]):
        events: List[Tuple[float, int]] = []
        for s, e in spans:
            events.append((s, 1))
            events.append((max(e, s), -1))
        events.sort(key=lambda x: (x[0], x[1]))      # at a tie, ends first
        self.times: List[float] = []
        self.level_after: List[int] = []
        self.integral: List[float] = []
        cur, acc, prev = 0, 0.0, None
        for t, d in events:
            if prev is not None:
                acc += cur * (t - prev)
            cur += d
            if self.times and self.times[-1] == t:
                self.level_after[-1] = cur
                self.integral[-1] = acc
            else:
                self.times.append(t)
                self.level_after.append(cur)
                self.integral.append(acc)
            prev = t

    def _integral_at(self, t: float) -> float:
        i = bisect.bisect_right(self.times, t) - 1
        return 0.0 if i < 0 else self.integral[i] + self.level_after[i] * (t - self.times[i])

    def per_request(self, span: Tuple[float, float]) -> float:
        """The mean number in flight (itself included) over the span."""
        s, e = span
        if e > s:
            return (self._integral_at(e) - self._integral_at(s)) / (e - s)
        i = bisect.bisect_right(self.times, s) - 1
        return float(max(1, self.level_after[i] if i >= 0 else 1))

    def time_at_levels(self, t0: float, t1: float) -> Tuple[Dict[int, float], Dict[int, float]]:
        """Seconds at each level between t0 and t1 (0 = nothing in flight),
        and ∫ n dt within each level."""
        tau: Dict[int, float] = defaultdict(float)
        ntau: Dict[int, float] = defaultdict(float)
        times = self.times
        for i in range(len(times) - 1):
            a, b = max(times[i], t0), min(times[i + 1], t1)
            if b <= a:
                continue
            n = self.level_after[i]
            lv = 0 if n <= 0 else level_of(n)
            tau[lv] += b - a
            ntau[lv] += n * (b - a)
        return dict(tau), dict(ntau)


# ---------------------------------------------------------------------------
# request size
# ---------------------------------------------------------------------------


def _solve(a: List[List[float]], b: List[float]) -> List[float]:
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


def _x(q: Logged) -> Tuple[float, float, float, float]:
    return (1.0, q.uncached / 1000, (q.cached or 0.0) / 1000, q.output / 1000)


def fit_size(reqs: Sequence[Logged], labels: Sequence[int],
             iters: int = 40) -> Tuple[Optional[List[float]], Optional[float]]:
    """latency ≈ S(level) × (a + b·u + c·k + d·o), relative error minimised;
    the load reading's model (platform/backend/app/datasets/load_profile.fit,
    which this package cannot import). Returns the size coefficients and R² on
    log latency, or (None, None) when it cannot be fitted."""
    counts = Counter(labels)
    groups = sorted(g for g, n in counts.items() if n >= MIN_FIT_GROUP)
    if not groups:
        if len(reqs) < MIN_FIT_GROUP:
            return None, None
        labels = [0] * len(reqs)
        groups = [0]
    keep = set(groups)
    data = [(q, g) for q, g in zip(reqs, labels) if g in keep]
    xs = [_x(q) for q, _ in data]
    gram = {g: [[0.0] * 4 for _ in range(4)] for g in groups}
    lin = {g: [0.0] * 4 for g in groups}
    for (q, g), x in zip(data, xs):
        r = q.rt_s
        for i in range(4):
            lin[g][i] += x[i] / r
            for j in range(4):
                gram[g][i][j] += x[i] * x[j] / (r * r)
    s = {g: 1.0 for g in groups}
    beta = [1.0, 0.0, 0.0, 0.01]
    try:
        for _ in range(iters):
            ata = [[sum(s[g] ** 2 * gram[g][i][j] for g in groups) for j in range(4)] for i in range(4)]
            atb = [sum(s[g] * lin[g][i] for g in groups) for i in range(4)]
            # A little ridge: a token count that never varies would otherwise
            # leave the system singular.
            lam = 1e-6 * sum(ata[i][i] for i in range(4))
            beta = _solve([[v + (lam if i == j else 0.0) for j, v in enumerate(row)]
                           for i, row in enumerate(ata)], atb)
            ratios: Dict[int, List[float]] = defaultdict(list)
            for (q, g), x in zip(data, xs):
                size = sum(b * v for b, v in zip(beta, x))
                if size > 0:
                    ratios[g].append(q.rt_s / size)
            new = {g: (statistics.median(ratios[g]) if ratios[g] else s[g]) for g in groups}
            ref = new[groups[0]]
            if ref <= 0:
                return None, None
            beta = [b * ref for b in beta]
            new = {g: v / ref for g, v in new.items()}
            done = max(abs(new[g] - s[g]) for g in groups) < 1e-5
            s = new
            if done:
                break
    except (ValueError, ZeroDivisionError):
        return None, None
    resid, log_r = [], []
    for (q, g), x in zip(data, xs):
        size = sum(b * v for b, v in zip(beta, x))
        if size > 0:
            resid.append(math.log(q.rt_s / (s[g] * size)))
            log_r.append(math.log(q.rt_s))
    if len(log_r) < 2:
        return None, None
    var_r, var_e = statistics.pvariance(log_r), statistics.pvariance(resid)
    return beta, (1 - var_e / var_r) if var_r else None


# ---------------------------------------------------------------------------
# the score
# ---------------------------------------------------------------------------


def _mean_size_by_level(levels: Sequence[int], sizes: Sequence[float]) -> Dict[int, float]:
    acc: Dict[int, List[float]] = defaultdict(list)
    for lv, sz in zip(levels, sizes):
        acc[lv].append(sz)
    full = {lv: statistics.fmean(v) for lv, v in acc.items() if len(v) >= MIN_BUCKET}
    return full or {lv: statistics.fmean(v) for lv, v in acc.items()}


def _nearest(table: Dict[int, float], lv: int) -> Optional[float]:
    if lv in table:
        return table[lv]
    if not table:
        return None
    return table[min(table, key=lambda k: abs(math.log(k) - math.log(lv)))]


def _curve(levels: Sequence[int], latencies: Sequence[float], sizes: Sequence[float],
           s_ref: Dict[int, float]) -> Dict[int, Tuple[float, int]]:
    """W(n) and the request count behind it, for levels with MIN_BUCKET
    requests: s_ref(n) · Σ latency / Σ size."""
    num: Dict[int, float] = defaultdict(float)
    den: Dict[int, float] = defaultdict(float)
    cnt: Counter = Counter()
    for lv, rt, sz in zip(levels, latencies, sizes):
        num[lv] += rt
        den[lv] += sz
        cnt[lv] += 1
    out = {}
    for lv in num:
        ref = _nearest(s_ref, lv)
        if cnt[lv] >= MIN_BUCKET and den[lv] > 0 and ref:
            out[lv] = (ref * num[lv] / den[lv], cnt[lv])
    return out


def _score(W: Dict[int, Tuple[float, int]], tau: Dict[int, float], ntau: Dict[int, float],
           T: float) -> Tuple[Optional[float], float]:
    """Σ τ(n)·X(n) / Σ τ(n) over production's busy levels (n ≥ 2), in tokens
    per minute, with X(n) = n̄(n)·T/W(n); and the share of the busy time this
    run measured.

    A faster config holds fewer requests in flight on the same schedule, so it
    may never reach production's busiest levels; a slower one may skip its
    quietest. A level this run did not measure gets the lower bound its
    neighbours give, from two facts: latency does not fall as concurrency
    rises, and neither does throughput. From a level reached below, X is at
    least X there; from one above, W is at most W there, so X ≥ n̄·T/W(above).
    A config is never credited with more than it showed. Leaving those levels
    out instead would score a faster config on its quieter levels only and
    rank it too low."""
    busy = {lv: t for lv, t in tau.items() if lv >= 2 and t > 0}
    X = {lv: (ntau[lv] / tau[lv]) * T / W[lv][0] for lv in busy if lv in W and W[lv][0] > 0}
    total, covered = sum(busy.values()), sum(busy[lv] for lv in X)
    reached = sorted(lv for lv in W if W[lv][0] > 0)
    if not total or not reached:
        return None, 0.0
    acc = 0.0
    for lv, t in busy.items():
        x = X.get(lv)
        if x is None:
            below = [k for k in reached if k < lv]
            above = [k for k in reached if k > lv]
            bounds = []
            if below:
                k = below[-1]
                nbar_k = ntau[k] / tau[k] if tau.get(k) else float(k)
                bounds.append(nbar_k * T / W[k][0])
            if above:
                bounds.append((ntau[lv] / t) * T / W[above[0]][0])
            x = max(bounds) if bounds else 0.0
        acc += t * x
    return acc / total * 60.0, covered / total


def _little_error(ran: Sequence[Ran]) -> Optional[float]:
    """Σ τ(n)·n̄(n)/W(n) over this run's own levels (plain mean latencies)
    against the requests it completed, less one: how well Little's law per
    level accounts for the run. Large values mean the curve is not to be
    trusted."""
    spans = [(r.send_s, r.end_s) for r in ran if r.send_s is not None and r.end_s is not None]
    if len(spans) < MIN_BUCKET:
        return None
    flight = InFlight(spans)
    t0, t1 = min(s for s, _ in spans), max(e for _, e in spans)
    tau, ntau = flight.time_at_levels(t0, t1)
    by: Dict[int, List[float]] = defaultdict(list)
    for s, e in spans:
        by[level_of(flight.per_request((s, e)))].append(e - s)
    predicted, actual = 0.0, 0
    for lv, rts in by.items():
        w = statistics.fmean(rts)
        if lv in tau and w > 0:
            predicted += ntau[lv] / w
            actual += len(rts)
    return predicted / actual - 1 if actual else None


def equivalent_throughput(logged: Sequence[Optional[Logged]], ran: Sequence[Ran],
                          warmup_s: float = 0.0) -> Dict[str, Any]:
    """The score for one timed replay. `logged[i]` and `ran[i]` are the same
    dataset line; requests due before `warmup_s` count towards concurrency but
    are not measured. Returns the metrics (None where there is nothing to say)
    and the curve under "curve"."""
    out: Dict[str, Any] = {
        "equivalent_tpm": None, "equivalent_tpm_logged": None, "equivalent_coverage": None,
        "equivalent_busy_share": None, "equivalent_size_matched": 0, "equivalent_size_r2": None,
        "equivalent_little_error": None, "equivalent_levels": 0, "curve": [],
    }
    idx = [i for i, q in enumerate(logged) if q is not None]
    if len(idx) < MIN_BUCKET:
        return out

    # Production: its in-flight count over the logged timeline, each
    # request's level, and the time at each level after the warm-up.
    prod_flight = InFlight([(logged[i].at_s, logged[i].at_s + logged[i].rt_s) for i in idx])
    prod_level = {i: level_of(prod_flight.per_request((logged[i].at_s, logged[i].at_s + logged[i].rt_s)))
                  for i in idx}
    t1 = max(logged[i].at_s + logged[i].rt_s for i in idx)
    t0 = min(max(warmup_s, 0.0), t1)
    tau, ntau = prod_flight.time_at_levels(t0, t1)
    span = sum(tau.values())
    out["equivalent_busy_share"] = (sum(t for lv, t in tau.items() if lv >= 2) / span) if span else None
    measured = [i for i in idx if logged[i].at_s >= warmup_s] or idx

    # Sizes, from the logged token counts and latencies.
    sized = [i for i in idx if logged[i].has_tokens]
    sizes: Dict[int, float] = {}
    beta = None
    if len([i for i in measured if logged[i].has_tokens]) >= MIN_SIZED_SHARE * len(measured):
        beta, r2 = fit_size([logged[i] for i in sized], [prod_level[i] for i in sized])
        out["equivalent_size_r2"] = r2
    if beta is not None:
        sizes = {i: sum(b * v for b, v in zip(beta, _x(logged[i]))) for i in sized}
        sizes = {i: s for i, s in sizes.items() if s > 0}
        out["equivalent_size_matched"] = 1
    else:
        sizes = {i: 1.0 for i in idx}

    # T: production's tokens per request, else this run's.
    logged_tokens = [logged[i].uncached + (logged[i].cached or 0.0) + logged[i].output
                     for i in measured if logged[i].has_tokens]
    run_tokens = [ran[i].tokens for i in measured if i < len(ran) and ran[i].ok and ran[i].tokens > 0]
    T = statistics.fmean(logged_tokens) if logged_tokens else (statistics.fmean(run_tokens) if run_tokens else 0.0)
    if T <= 0:
        return out

    ref_idx = [i for i in measured if i in sizes]
    s_ref = _mean_size_by_level([prod_level[i] for i in ref_idx], [sizes[i] for i in ref_idx])

    # Production's own logged curve, for reference.
    W_logged = _curve([prod_level[i] for i in ref_idx], [logged[i].rt_s for i in ref_idx],
                      [sizes[i] for i in ref_idx], s_ref)
    out["equivalent_tpm_logged"], _ = _score(W_logged, tau, ntau, T)

    # This run: its own in-flight count (every request it sent, warm-up and
    # failures included: they held the engine), and the measured successes'
    # latencies at their levels.
    sent = [i for i in range(len(ran)) if ran[i].send_s is not None and ran[i].end_s is not None]
    if len(sent) < MIN_BUCKET:
        return out
    run_flight = InFlight([(ran[i].send_s, ran[i].end_s) for i in sent])
    pts = [i for i in measured if i < len(ran) and ran[i].ok and ran[i].send_s is not None
           and ran[i].end_s is not None and i in sizes]
    run_level = {i: level_of(run_flight.per_request((ran[i].send_s, ran[i].end_s))) for i in pts}
    W = _curve([run_level[i] for i in pts], [ran[i].end_s - ran[i].send_s for i in pts],
               [sizes[i] for i in pts], s_ref)
    out["equivalent_tpm"], out["equivalent_coverage"] = _score(W, tau, ntau, T)
    out["equivalent_levels"] = sum(1 for lv in W if lv >= 2 and tau.get(lv))
    out["equivalent_little_error"] = _little_error(ran)

    for lv in sorted(set(tau) | set(W)):
        if lv == 0:
            continue
        p = tau.get(lv, 0.0) / span if span else 0.0
        nbar = ntau[lv] / tau[lv] if tau.get(lv) else None
        w = W.get(lv)
        wl = W_logged.get(lv)
        out["curve"].append({
            "level": lv,
            "time_share": round(p, 6),
            "in_flight": round(nbar, 3) if nbar is not None else None,
            "latency_s": round(w[0], 4) if w else None,
            "requests": w[1] if w else sum(1 for i in pts if run_level[i] == lv),
            "tpm": round(nbar * T / w[0] * 60.0, 1) if (w and nbar) else None,
            "logged_latency_s": round(wl[0], 4) if wl else None,
        })
    return out
