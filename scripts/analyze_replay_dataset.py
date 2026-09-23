#!/usr/bin/env python3
"""Analyze a replay JSONL dataset (converted bodylog OR raw bodylog).

Reports the things that decide whether a replay run is representative:
  - input  token length  (prompt_tokens)        distribution
  - output token length  (completion_tokens, of which reasoning_tokens) distribution
  - cache hit rate        (cached_tokens / prompt_tokens), token-weighted + per-request
  - wall-clock            capture window span + per-request response time (rt) + TTFT
  - uniformity            first-N vs last-N drift, plus a per-segment table, so you
                          can see whether (e.g.) the cache hit rate of the first 100
                          requests differs from the last 100.

It reads the original response usage recorded at capture time (the live model's
own token accounting), found under `raw_payload.resp_meta.usage` (converted replay
JSONL) or `resp_meta.usage` (raw bodylog) — whichever is present.

Usage:
    python scripts/analyze_replay_dataset.py \
        dataset/replay/bodylog_172-26-3-79_8050_2026-06-25_00h.replay.jsonl \
        [--head 100] [--tail 100] [--segments 10]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import defaultdict
from datetime import datetime
from pathlib import Path


# Input-length buckets for the per-input-length TTFT service level: (human, lo, hi)
# in prompt tokens, "K" = 1024, hi exclusive. Keep in sync with
# bench/tests/functional/replay.py :: TTFT_INPUT_BUCKETS (the replay test emits
# ttft_<bucket>_* metrics on these same edges). ge_256k catches oversize prompts.
TTFT_INPUT_BUCKETS = [
    ("<6K",        0,           6 * 1024),
    ("6K-16K",     6 * 1024,    16 * 1024),
    ("16K-32K",    16 * 1024,   32 * 1024),
    ("32K-64K",    32 * 1024,   64 * 1024),
    ("64K-128K",   64 * 1024,   128 * 1024),
    ("128K-256K",  128 * 1024,  256 * 1024),
    ("≥256K",      256 * 1024,  float("inf")),
]


# --------------------------------------------------------------------------- #
# stats helpers (pure python — no numpy dependency)
# --------------------------------------------------------------------------- #
def percentile(sorted_vals: list[float], ratio: float) -> float:
    """Linear-interpolated percentile of an already-sorted list."""
    if not sorted_vals:
        return float("nan")
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    k = ratio * (len(sorted_vals) - 1)
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return float(sorted_vals[int(k)])
    return sorted_vals[lo] * (hi - k) + sorted_vals[hi] * (k - lo)


def describe(vals: list[float]) -> dict:
    if not vals:
        return {"n": 0}
    s = sorted(vals)
    n = len(s)
    return {
        "n": n,
        "sum": sum(s),
        "mean": sum(s) / n,
        "min": s[0],
        "p50": percentile(s, 0.50),
        "p90": percentile(s, 0.90),
        "p99": percentile(s, 0.99),
        "max": s[-1],
    }


def fmt_dist(label: str, d: dict, unit: str = "") -> str:
    if d.get("n", 0) == 0:
        return f"  {label:<22} (no data)"
    u = f" {unit}" if unit else ""
    return (
        f"  {label:<22} n={d['n']:<5} mean={d['mean']:>10,.1f}{u}  "
        f"min={d['min']:>9,.0f}  p50={d['p50']:>9,.0f}  "
        f"p90={d['p90']:>9,.0f}  p99={d['p99']:>9,.0f}  max={d['max']:>9,.0f}  "
        f"Σ={d['sum']:>13,.0f}"
    )


# --------------------------------------------------------------------------- #
# extraction
# --------------------------------------------------------------------------- #
def _resp_meta(rec: dict) -> dict:
    """Find resp_meta in either the converted (raw_payload.resp_meta) or raw
    (top-level resp_meta) layout."""
    rp = rec.get("raw_payload")
    if isinstance(rp, dict) and isinstance(rp.get("resp_meta"), dict):
        return rp["resp_meta"]
    if isinstance(rec.get("resp_meta"), dict):
        return rec["resp_meta"]
    return {}


def _timing(rec: dict) -> dict:
    """Timing/metadata block (rt, first_chunk_t, ts, status), either layout."""
    rp = rec.get("raw_payload")
    if isinstance(rp, dict) and ("rt" in rp or "ts" in rp):
        return rp
    return rec


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def _msg_text(content) -> str:
    """Flatten a message's content (string or list-of-parts) to plain text."""
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict))
    return content or ""


def _system_shape(messages) -> tuple[int, bool]:
    """(#system messages, needs_normalization) using the replay tool's own rule:
    a strict Qwen/SGLang template rejects >1 leading system OR any non-leading
    (stray) system message — that is exactly when `clean=true` rewrites the request."""
    if not isinstance(messages, list):
        return 0, False
    i = 0
    while i < len(messages) and isinstance(messages[i], dict) and messages[i].get("role") == "system":
        i += 1
    stray = any(isinstance(m, dict) and m.get("role") == "system" for m in messages[i:])
    n_system = sum(1 for m in messages if isinstance(m, dict) and m.get("role") == "system")
    return n_system, (i > 1 or stray)


def _request_json(rec: dict) -> dict:
    """The OpenAI chat request, from request_json (converted) or by parsing the
    raw request_body / req_body string."""
    req = rec.get("request_json")
    if isinstance(req, dict):
        return req
    for key in ("request_body", "req_body"):
        body = rec.get(key)
        if isinstance(body, str):
            try:
                parsed = json.loads(body)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass
    return {}


class Sample:
    __slots__ = ("prompt", "completion", "reasoning", "cached", "has_cache",
                 "rt", "ttft", "last_chunk", "ts", "status",
                 "stream", "n_messages", "n_system", "multi_system", "has_tool",
                 "req_max_tokens", "sys_sig", "pre_sig")

    def __init__(self, rec: dict, want_prefix: bool = False):
        meta = _resp_meta(rec)
        usage = meta.get("usage") or {}
        self.prompt = usage.get("prompt_tokens")
        self.completion = usage.get("completion_tokens")
        self.reasoning = usage.get("reasoning_tokens")
        ptd = usage.get("prompt_tokens_details") or {}
        # kimi omits prompt_tokens_details entirely on a full cache miss, so a
        # missing field means 0 cached — record that distinction explicitly.
        self.has_cache = "cached_tokens" in ptd
        self.cached = ptd.get("cached_tokens", 0) or 0
        t = _timing(rec)
        self.rt = t.get("rt")
        self.ttft = t.get("first_chunk_t")
        self.last_chunk = t.get("last_chunk_t")
        self.ts = _parse_ts(t.get("ts") or rec.get("timestamp"))
        self.status = t.get("status")

        # ---- request shape (drives clean / force_stream / max_generation_tokens) ----
        req = _request_json(rec)
        msgs = req.get("messages")
        self.stream = bool(req.get("stream"))
        self.n_messages = len(msgs) if isinstance(msgs, list) else 0
        self.n_system, self.multi_system = _system_shape(msgs)
        self.has_tool = isinstance(msgs, list) and any(
            isinstance(m, dict) and m.get("role") == "tool" for m in msgs)
        mt = req.get("max_completion_tokens")
        if mt is None:
            mt = req.get("max_tokens")
        self.req_max_tokens = mt if isinstance(mt, int) else None

        # ---- prefix signatures (only when prefix-locality is requested) ----
        self.sys_sig = None
        self.pre_sig = None
        if want_prefix and isinstance(msgs, list):
            sys_text = "".join(
                _msg_text(m.get("content")) for m in msgs
                if isinstance(m, dict) and m.get("role") == "system")
            self.sys_sig = hashlib.md5(sys_text.encode("utf-8")).hexdigest()
            # The cacheable prefix is everything before the final (new) turn; cap each
            # message at 4KB so the signature is cheap but still prefix-discriminating.
            pre = msgs[:-1] if len(msgs) > 1 else msgs
            pre_repr = json.dumps(
                [(m.get("role"), _msg_text(m.get("content"))[:4000]) for m in pre if isinstance(m, dict)],
                ensure_ascii=False)
            self.pre_sig = hashlib.md5(pre_repr.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# segment / uniformity analysis
# --------------------------------------------------------------------------- #
def cache_hit_rate(samples: list[Sample]) -> tuple[float, float, int, int]:
    """Token-weighted hit rate (Σcached/Σprompt) and per-request mean.

    Returns (token_weighted, per_request_mean, n_prompt, n_with_cache_field).
    Missing cached_tokens is treated as 0 (full miss)."""
    tot_prompt = sum(s.prompt for s in samples if s.prompt)
    tot_cached = sum(s.cached for s in samples if s.prompt)
    per_req = [s.cached / s.prompt for s in samples if s.prompt]
    n_field = sum(1 for s in samples if s.has_cache)
    tw = tot_cached / tot_prompt if tot_prompt else float("nan")
    pr = sum(per_req) / len(per_req) if per_req else float("nan")
    return tw, pr, len(per_req), n_field


def segment_row(name: str, seg: list[Sample]) -> str:
    tw, _pr, _n, n_field = cache_hit_rate(seg)
    inp = describe([s.prompt for s in seg if s.prompt is not None])
    out = describe([s.completion for s in seg if s.completion is not None])
    rt = describe([s.rt for s in seg if s.rt is not None])
    return (
        f"  {name:<16} n={len(seg):<4} "
        f"cache_hit={tw*100:>5.1f}%  "
        f"in(mean/p50)={inp.get('mean', 0):>8,.0f}/{inp.get('p50', 0):>8,.0f}  "
        f"out(mean/p50)={out.get('mean', 0):>8,.0f}/{out.get('p50', 0):>8,.0f}  "
        f"rt(mean/p50)={rt.get('mean', 0):>6.1f}/{rt.get('p50', 0):>6.1f}s  "
        f"cache_field={n_field}/{len(seg)}"
    )


def concurrency_profile(samples: list["Sample"], buckets: int = 12) -> dict | None:
    """Reconstruct in-flight concurrency over time via a sweep line.

    `ts` is the request *start* (it is perfectly monotonic in file order, which a
    completion timestamp could not be given rt ranges 0..1379s), so each request
    occupies [ts, ts+rt]. Returns time-weighted concurrency stats over the window
    [first start, last finish], plus an evenly-spaced timeline of bucket averages.
    """
    iv = [(s.ts, s.rt) for s in samples if s.ts is not None and s.rt]
    if not iv:
        return None
    # +1 at start, -1 at finish. At a tie, finishes (-1) sort before starts (+1)
    # so back-to-back requests aren't counted as overlapping.
    events: list[tuple[float, int]] = []
    for start, rt in iv:
        s0 = start.timestamp()
        events.append((s0, 1))
        events.append((s0 + rt, -1))
    events.sort(key=lambda e: (e[0], e[1]))
    t0, t1 = events[0][0], events[-1][0]
    span = t1 - t0
    width = span / buckets if buckets > 0 and span > 0 else 0.0
    bucket_integral = [0.0] * buckets  # ∫concurrency dt within each time bucket

    time_at: dict[int, float] = defaultdict(float)  # seconds spent at each level
    cur = 0
    peak = 0
    prev = t0
    for t, delta in events:
        dur = t - prev
        if dur > 0:
            time_at[cur] += dur
            if width > 0 and cur > 0:
                # distribute this constant-level segment across the time buckets
                lo = min(int((prev - t0) / width), buckets - 1)
                hi = min(int((t - t0) / width), buckets - 1)
                for b in range(lo, hi + 1):
                    a = max(prev, t0 + b * width)
                    z = min(t, t0 + (b + 1) * width)
                    if z > a:
                        bucket_integral[b] += cur * (z - a)
        cur += delta
        peak = max(peak, cur)
        prev = t

    total_t = sum(time_at.values())
    avg = sum(level * dt for level, dt in time_at.items()) / total_t if total_t else 0.0
    # time-weighted percentiles of concurrency
    levels = sorted(time_at.items())
    pct = {0.5: 0, 0.9: 0, 0.99: 0}
    for q in pct:
        target = q * total_t
        c = 0.0
        for level, dt in levels:
            c += dt
            if c >= target:
                pct[q] = level
                break
    idle = time_at.get(0, 0.0)
    timeline = [bi / width if width > 0 else 0.0 for bi in bucket_integral]
    return {
        "avg": avg, "peak": peak, "p50": pct[0.5], "p90": pct[0.9], "p99": pct[0.99],
        "span": span, "idle_frac": idle / span if span > 0 else 0.0,
        "timeline": timeline, "bucket_minutes": width / 60.0,
    }


def prefix_locality(samples: list[Sample]) -> dict | None:
    """Producer→consumer distance for cache hits, plus how reproducible they are.

    A cache hit needs the request that first populated the shared prefix (its
    "producer") to be earlier in the file AND its KV still resident. We match each
    cache-hit request to the most recent earlier request with the same conversation
    prefix (pre_sig), falling back to the same system prompt (sys_sig). The distance
    (in #requests) tells you how long a prefix must survive, and — combined with the
    replay pool size C — which hits are *guaranteed* reproducible (distance ≥ C means
    the producer has finished before the consumer starts; distance < C may overlap)."""
    if not any(s.pre_sig for s in samples):
        return None
    last_pre: dict[str, int] = {}
    last_sys: dict[str, int] = {}
    dist: list[int] = []
    n_hit = 0
    no_producer = 0
    for i, s in enumerate(samples):
        if s.prompt and s.cached > 0:
            n_hit += 1
            if s.pre_sig in last_pre:
                dist.append(i - last_pre[s.pre_sig])
            elif s.sys_sig in last_sys:
                dist.append(i - last_sys[s.sys_sig])
            else:
                no_producer += 1
        if s.pre_sig is not None:
            last_pre[s.pre_sig] = i
        if s.sys_sig is not None:
            last_sys[s.sys_sig] = i
    distinct_sys = len({s.sys_sig for s in samples if s.sys_sig})
    distinct_pre = len({s.pre_sig for s in samples if s.pre_sig})
    return {
        "n_hit": n_hit, "no_producer": no_producer, "dist": dist,
        "distinct_sys": distinct_sys, "distinct_pre": distinct_pre, "n": len(samples),
    }


def estimate_wall_seconds(sum_rt: float, max_rt: float, concurrency: int) -> float:
    """Lower-bound replay wall-clock at a given pool size, assuming the target
    answers each request as fast as the original did: can't beat the slowest single
    request, and can't pack work tighter than Σrt/C."""
    if concurrency <= 0:
        return float("inf")
    return max(max_rt, sum_rt / concurrency)


def analyze(path: Path, head: int, tail: int, segments: int,
            want_prefix: bool = False) -> None:
    samples: list[Sample] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            samples.append(Sample(json.loads(line), want_prefix=want_prefix))

    n = len(samples)
    print(f"\n=== Replay dataset analysis: {path.name} ===")
    print(f"records: {n}")
    if n == 0:
        return

    # ---- token lengths -------------------------------------------------- #
    prompts = [s.prompt for s in samples if s.prompt is not None]
    compls = [s.completion for s in samples if s.completion is not None]
    reasons = [s.reasoning for s in samples if s.reasoning is not None]
    # completion_tokens includes reasoning_tokens; the visible answer is the rest.
    visible = [
        s.completion - (s.reasoning or 0)
        for s in samples
        if s.completion is not None
    ]
    print("\n--- token lengths (from original capture usage) ---")
    print(fmt_dist("input (prompt)", describe(prompts), "tok"))
    print(fmt_dist("output (completion)", describe(compls), "tok"))
    print(fmt_dist("  of which reasoning", describe(reasons), "tok"))
    print(fmt_dist("  visible (compl-reas)", describe(visible), "tok"))

    # ---- cache hit rate ------------------------------------------------- #
    tw, pr, n_prompt, n_field = cache_hit_rate(samples)
    per_req_rates = sorted(s.cached / s.prompt for s in samples if s.prompt)
    print("\n--- cache hit rate (cached_tokens / prompt_tokens) ---")
    print(f"  token-weighted (Σcached/Σprompt): {tw*100:.2f}%   "
          f"(Σcached={sum(s.cached for s in samples):,} / "
          f"Σprompt={sum(prompts):,})")
    print(f"  per-request mean:                 {pr*100:.2f}%")
    print(f"  per-request p50/p90/max:          "
          f"{percentile(per_req_rates,0.5)*100:.1f}% / "
          f"{percentile(per_req_rates,0.9)*100:.1f}% / "
          f"{percentile(per_req_rates,1.0)*100:.1f}%")
    zero = sum(1 for r in per_req_rates if r == 0)
    print(f"  cached_tokens field present: {n_field}/{n}  "
          f"(absent ⇒ treated as full miss); requests with 0% cache: {zero}/{n}")

    # ---- wall clock ----------------------------------------------------- #
    ts_vals = sorted(s.ts for s in samples if s.ts is not None)
    rts = [s.rt for s in samples if s.rt is not None]
    print("\n--- wall clock ---")
    if ts_vals:
        span = (ts_vals[-1] - ts_vals[0]).total_seconds()
        print(f"  capture window: {ts_vals[0].isoformat()}  →  {ts_vals[-1].isoformat()}")
        print(f"  span: {span:,.0f}s ({span/3600:.2f}h) over {len(ts_vals)} timestamped requests")
        if span > 0:
            print(f"  arrival rate: {len(ts_vals)/(span/60):.2f} req/min "
                  f"(avg gap {span/max(len(ts_vals)-1,1):.1f}s)")
    print(fmt_dist("per-request rt", describe(rts), "s"))
    # first_chunk_t is a real TTFT only for streamed requests; for a non-streamed
    # request the whole body arrives in one chunk, so its first_chunk_t ≈ rt and
    # would inflate the TTFT picture. Split them.
    stream_ttfts = [s.ttft for s in samples if s.ttft is not None and s.stream]
    nonstream_ttfts = [s.ttft for s in samples if s.ttft is not None and not s.stream]
    print(fmt_dist("TTFT (streamed reqs)", describe(stream_ttfts), "s"))
    if nonstream_ttfts:
        print(fmt_dist("first_chunk (non-stream)", describe(nonstream_ttfts), "s")
              + "  [≈ full response time]")

    # ---- TTFT by input length (vs the configured service level) -------- #
    # Real TTFT needs streaming (a non-streamed first_chunk_t ≈ rt), so this
    # table is over streamed requests only — the same population the replay
    # test's ttft_<bucket>_* metrics use. Bucketed by prompt_tokens.
    print("\n--- TTFT by input length (streamed reqs, seconds) ---")
    # cache_hit is the per-request mean of cached/prompt within the bracket — the
    # cache-cold buckets are the slow-TTFT ones, so showing both here makes the
    # relationship explicit. Bucket the TTFT and the cache ratio together first,
    # then render (the ALL row needs the totals before it prints).
    bucket_ttfts: dict[str, list[float]] = {h: [] for h, _, _ in TTFT_INPUT_BUCKETS}
    bucket_cache: dict[str, list[float]] = {h: [] for h, _, _ in TTFT_INPUT_BUCKETS}
    all_cache: list[float] = []
    ttft_no_len = 0
    for s in samples:
        if s.ttft is None or not s.stream:
            continue
        if not s.prompt:  # streamed + has TTFT but no usage.prompt_tokens → can't size
            ttft_no_len += 1
            continue
        hit = s.cached / s.prompt
        all_cache.append(hit)
        for human, lo, hi in TTFT_INPUT_BUCKETS:
            if lo <= s.prompt < hi:
                bucket_ttfts[human].append(s.ttft)
                bucket_cache[human].append(hit)
                break

    print(f"  {'input length':<13}{'n':>6}{'avg':>9}{'p50':>9}{'p90':>9}{'p99':>9}{'cache_hit':>11}")

    def _ttft_row(label: str, vals: list[float], cache: list[float]) -> str:
        ch = f"{sum(cache) / len(cache) * 100:.1f}%" if cache else "—"
        if not vals:
            return f"  {label:<13}{0:>6}{'—':>9}{'—':>9}{'—':>9}{'—':>9}{ch:>11}"
        d = describe(vals)
        return (f"  {label:<13}{d['n']:>6}{d['mean']:>8.2f}s{d['p50']:>8.2f}s"
                f"{d['p90']:>8.2f}s{d['p99']:>8.2f}s{ch:>11}")
    print(_ttft_row("ALL", stream_ttfts, all_cache))
    for human, _, _ in TTFT_INPUT_BUCKETS:
        print(_ttft_row(human, bucket_ttfts[human], bucket_cache[human]))
    if ttft_no_len:
        print(f"  ({ttft_no_len} streamed reqs had a TTFT but no usage.prompt_tokens — "
              f"excluded from buckets, included in ALL)")

    # ---- token throughput (Σtokens / active serving window) ------------ #
    # The production-side analog of the replay test's input_tpm/output_tpm/
    # cached_tpm: total tokens divided by the wall-clock the system spent serving
    # this traffic (first request start → last request finish, start+rt). This is
    # throughput AT THE CAPTURED CONCURRENCY (~avg in-flight below), not a max —
    # a replay at matched concurrency should land near these numbers.
    starts = [s.ts.timestamp() for s in samples if s.ts is not None]
    ends = [s.ts.timestamp() + (s.rt or 0.0) for s in samples if s.ts is not None]
    win = (max(ends) - min(starts)) if (starts and ends and max(ends) > min(starts)) else 0.0
    print("\n--- token throughput (Σtokens / active serving window) ---")
    if win <= 0:
        print("  (no timing data)")
    else:
        sum_in = sum(prompts)
        sum_out = sum(compls)
        sum_cached = sum(s.cached for s in samples)
        sum_reason = sum(reasons)
        sum_uncached = sum_in - sum_cached
        print(f"  window: {win:,.0f}s   "
              f"(totals: input={sum_in:,}  output={sum_out:,}  cached={sum_cached:,} tok)")

        def _rate(total: int) -> str:
            return f"{total / win:>9,.0f} tok/s   ({total / win * 60:>11,.0f} tok/min)"
        print(f"  input  (prompt):     {_rate(sum_in)}")
        print(f"    cached:            {_rate(sum_cached)}  (cache-read; subset of input)")
        print(f"    uncached (fresh):  {_rate(sum_uncached)}")
        print(f"  output (completion): {_rate(sum_out)}")
        print(f"    of which reasoning:{_rate(sum_reason)}")

    # ---- per-request token rate (tps) percentiles ---------------------- #
    # Each request's own rate, split by phase: input & cached are ingested during
    # prefill (the TTFT window, so ÷ first_chunk_t — includes any queue wait),
    # output is produced during decode (÷ last_chunk_t − first_chunk_t). Streamed
    # reqs only, positive window. p10 is the SLOW tail (low tok/s), p90 the fast.
    in_tps: list[float] = []
    cached_tps: list[float] = []
    out_tps: list[float] = []
    for s in samples:
        if not s.stream:
            continue
        if s.ttft and s.ttft > 0:
            if s.prompt:
                in_tps.append(s.prompt / s.ttft)
            if s.cached:
                cached_tps.append(s.cached / s.ttft)
        decode = (s.last_chunk - s.ttft) if (s.last_chunk is not None and s.ttft is not None) else None
        if decode and decode > 0 and s.completion:
            out_tps.append(s.completion / decode)
    print("\n--- per-request token rate (tok/s): mean + percentiles ---")
    print(f"  {'rate':<24}{'n':>6}{'mean':>11}{'p10':>11}{'p50':>11}{'p90':>11}")

    def _tps_row(label: str, vals: list[float]) -> str:
        if not vals:
            return f"  {label:<24}{0:>6}{'—':>11}{'—':>11}{'—':>11}{'—':>11}"
        v = sorted(vals)
        mean = sum(v) / len(v)
        return (f"  {label:<24}{len(v):>6}{mean:>11,.0f}{percentile(v, 0.10):>11,.0f}"
                f"{percentile(v, 0.50):>11,.0f}{percentile(v, 0.90):>11,.0f}")
    print(_tps_row("input  / TTFT (prefill)", in_tps))
    print(_tps_row("cached / TTFT", cached_tps))
    print(_tps_row("output / decode", out_tps))

    # ---- concurrency ---------------------------------------------------- #
    cp = concurrency_profile(samples)
    print("\n--- approximate concurrency (in-flight requests, ts=start, dur=rt) ---")
    if cp is None:
        print("  (no timing data)")
    else:
        print(f"  time-weighted average: {cp['avg']:.1f} in-flight   peak: {cp['peak']}   "
              f"p50/p90/p99: {cp['p50']}/{cp['p90']}/{cp['p99']}")
        print(f"  idle (0 in-flight) fraction of window: {cp['idle_frac']*100:.1f}%   "
              f"(Little's law Σrt/span = {sum(rts)/cp['span']:.1f})")
        spark = "".join("▁▂▃▄▅▆▇█"[min(7, int(v / max(cp['peak'], 1) * 7))] for v in cp['timeline'])
        print(f"  timeline ({cp['bucket_minutes']:.1f}min buckets, avg in-flight): {spark}")
        print("    per-bucket avg: " + " ".join(f"{v:.0f}" for v in cp['timeline']))

    # ---- uniformity ----------------------------------------------------- #
    print("\n--- uniformity (in capture/file order) ---")
    print(segment_row(f"first {head}", samples[:head]))
    print(segment_row(f"last {tail}", samples[-tail:]))
    fh = cache_hit_rate(samples[:head])[0]
    lt = cache_hit_rate(samples[-tail:])[0]
    drift = (lt - fh) * 100
    print(f"  → cache-hit drift first→last: {fh*100:.1f}% → {lt*100:.1f}%  "
          f"(Δ {drift:+.1f} pts)")

    print(f"\n  per-segment ({segments} equal chunks):")
    size = max(1, math.ceil(n / segments))
    seg_rates = []
    for i in range(0, n, size):
        seg = samples[i:i + size]
        if not seg:
            continue
        seg_rates.append(cache_hit_rate(seg)[0])
        print(segment_row(f"[{i:>4}:{i+len(seg):>4}]", seg))
    if seg_rates:
        valid = [r for r in seg_rates if not math.isnan(r)]
        if valid:
            spread = (max(valid) - min(valid)) * 100
            print(f"  → cache-hit spread across segments: "
                  f"{min(valid)*100:.1f}% .. {max(valid)*100:.1f}%  (range {spread:.1f} pts)")
            print("  → " + ("LOOKS UNIFORM" if spread < 10 else
                            "NON-UNIFORM — cache hit rate drifts across the file; "
                            "a truncated/sampled run may not be representative"))

    # ---- request shape (replay-param preflight) ------------------------- #
    n_stream = sum(1 for s in samples if s.stream)
    n_multi = sum(1 for s in samples if s.multi_system)
    n_tool = sum(1 for s in samples if s.has_tool)
    req_mt = [s.req_max_tokens for s in samples if s.req_max_tokens is not None]
    print("\n--- request shape (drives clean / force_stream / max_generation_tokens) ---")
    print(f"  stream=true: {n_stream}/{n}   stream=false: {n - n_stream}/{n}  "
          f"→ {'force_stream=true to measure TTFT on all' if n_stream < n else 'all streamed'}")
    print(f"  multi/out-of-order system msgs: {n_multi}/{n}  "
          f"→ {'set clean=true (strict Qwen/SGLang templates 4xx on these)' if n_multi else 'clean not needed'}")
    print(f"  tool-role messages present: {n_tool}/{n}  "
          f"→ target must render tool/tool_calls or these 4xx")
    print(fmt_dist("messages/request", describe([float(s.n_messages) for s in samples]), ""))
    if req_mt:
        print(f"  requests carrying max_tokens: {len(req_mt)}/{n} "
              f"(these already bound generation)")
        print(fmt_dist("  their max_tokens", describe(req_mt), "tok"))

    # ---- prefix locality (cache reproducibility vs concurrency) --------- #
    if want_prefix:
        pl = prefix_locality(samples)
        print("\n--- prefix locality (cache-hit reproducibility) ---")
        if pl is None:
            print("  (no request messages available)")
        else:
            d = sorted(pl["dist"])
            print(f"  cache-hit requests: {pl['n_hit']}   "
                  f"distinct system prompts: {pl['distinct_sys']}   "
                  f"distinct conversation prefixes: {pl['distinct_pre']}")
            print(f"  hits whose prefix-producer is EARLIER in the file: {len(d)}")
            print(f"  hits with NO in-file producer (warmed before capture, "
                  f"NOT reproducible cold): {pl['no_producer']}")
            if d:
                print(f"  producer→consumer distance (#requests): "
                      f"min {d[0]}  p50 {percentile(d,0.5):.0f}  "
                      f"p90 {percentile(d,0.9):.0f}  max {d[-1]}")
                print("  guaranteed-reproducible hits by pool size C "
                      "(distance ≥ C ⇒ producer finished before consumer starts):")
                for c in (1, 4, 8, 16, 25, 32):
                    reliable = sum(1 for x in d if x >= c)
                    print(f"      C={c:<3} {reliable}/{len(d)} reliable "
                          f"({reliable/len(d)*100:.0f}%)   "
                          f"at-risk(<C): {len(d)-reliable}")
                print("  → lower C ⇒ more hits reliably reproduced; the at-risk ones "
                      "may under-count (consumer prefills before producer commits KV)")

    # ---- recommended replay parameters ---------------------------------- #
    print("\n--- suggested replay params (tune to your goal) ---")
    if rts:
        sum_rt, max_rt = sum(rts), max(rts)
        cand = sorted({4, 8, 16, cp["p90"], cp["peak"]}) if cp else [4, 8, 16]
        print("  concurrency vs estimated wall-clock (assumes target ≈ original latency):")
        for c in cand:
            est = estimate_wall_seconds(sum_rt, max_rt, c)
            tag = ""
            if cp:
                if c == cp["peak"]:
                    tag = " ← original peak (most faithful cache pressure)"
                elif c == cp["p90"]:
                    tag = " ← original p90 in-flight"
            over = "  ⚠ exceeds default max_seconds=18000" if est > 18000 else ""
            print(f"      concurrency={c:<3} ≈ {est/3600:.2f}h ({est:,.0f}s){tag}{over}")
        print(f"  max_seconds: ≥ {max_rt:,.0f}s floor (slowest single request); "
              f"size to the concurrency row above + margin")
        # request_timeout is a per-read socket timeout
        st_max = max(stream_ttfts) if stream_ttfts else 0.0
        if n_stream < n:
            print(f"  request_timeout: ≥ {max_rt*1.2:,.0f}s "
                  f"(non-streamed reqs deliver the whole body in one read, up to rt={max_rt:,.0f}s)")
        else:
            print(f"  request_timeout: ≥ {max(st_max*1.5, 60):,.0f}s "
                  f"(covers max streamed TTFT {st_max:,.0f}s under load)")
    if compls:
        c_max = max(compls)
        print(f"  max_generation_tokens: 0 (requests already carry max_tokens) — or "
              f"≥ {c_max:,.0f} if capping, since legit outputs reach {c_max:,.0f} tok; "
              f"a lower cap truncates real responses")
    print("  max_samples: 0 (full) recommended — dataset is order-dependent for cache; "
          "a contiguous head preserves prefix locality but inherits position bias, "
          "while strided sampling fixes bias but breaks producer→consumer pairs")
    print()


def main() -> None:
    ap = argparse.ArgumentParser(description="Analyze a replay JSONL dataset")
    ap.add_argument("input", type=Path, help="Replay JSONL (converted bodylog or raw bodylog)")
    ap.add_argument("--head", type=int, default=100, help="First-N segment size (default 100)")
    ap.add_argument("--tail", type=int, default=100, help="Last-N segment size (default 100)")
    ap.add_argument("--segments", type=int, default=10, help="Equal chunks for the uniformity table (default 10)")
    ap.add_argument("--prefix-locality", action="store_true",
                    help="Analyze cache-hit prefix reproducibility vs concurrency "
                         "(hashes request messages — a bit slower)")
    args = ap.parse_args()
    if not args.input.exists():
        ap.error(f"Input file not found: {args.input}")
    analyze(args.input, args.head, args.tail, args.segments,
            want_prefix=args.prefix_locality)


if __name__ == "__main__":
    main()
