#!/usr/bin/env python3
"""
Causal-chain reconstruction + precedence-aware scheduling analysis for replay datasets.

The question this answers
-------------------------
The replay fires all recorded requests into a fixed-size thread pool in file
order. Requests that were SEQUENTIAL turns of one agentic session (turn N+1's
prompt = turn N's prompt + one more turn) can then land in the pool at the same
time. Turn N+1 hits the target's prefix cache only if turn N has already run and
warmed it; run them concurrently and that hit is lost. So: how do we dispatch
concurrently while never running two requests of the SAME causal chain at once —
and what does that cost in throughput?

How the chain is reconstructed (correctly)
------------------------------------------
parent(B) = the DEEPEST *complete* earlier request A whose entire message list is
a prefix of B's messages (i.e. B == A + one more turn), tie-broken by timestamp
(the A that most recently completed before B started).

Matching against COMPLETE requests (via a full-message-list hash) rather than
against any prefix of a longer request is what makes this immune to dataset
row-order scrambling: request 225 (turn 6, 28 msgs) is NOT a complete prefix of
request 248 (turn 1, 2 msgs), so 248 can never be mis-parented to 225. Only
248 -> 250 -> ... -> 225, ordered by real time, survives.

  Precedence rule for cache-faithful replay:  start B only after parent(B) done.
  => every chain is fully serial; independent chains run in parallel.
     Max sustainable average concurrency  =  total_work / critical_path,
     where critical_path = the longest (duration-weighted) root->leaf chain.
     Ask for more workers than that and they necessarily idle.

The precedence-aware dispatcher (what a real replay would do)
------------------------------------------------------------
  indeg[r]   = 1 if r has a causal parent else 0
  ready      = every request with indeg 0 (chain heads)
  workers    = pool of size C, each pulls from `ready`
  on completion of A: for each child of A, indeg-=1; if 0 -> push to `ready`
This is a topological / list schedule over the chain forest. A child becomes
runnable only after its parent finishes, so no chain ever runs two turns at once,
while all distinct chains run fully in parallel — maximum concurrency that still
preserves every production cache hit.

Usage
-----
  python bench/replay_test/analyze_causal_schedule.py <dataset.jsonl> \
      [--limit N] [--concurrency 5,16,64,256] [--msg-cap CHARS]

Stdlib only.
"""
from __future__ import annotations

import argparse
import hashlib
import heapq
import json
import statistics
from collections import defaultdict, deque
from datetime import datetime


# --------------------------------------------------------------------------- #
# Loading / message flattening
# --------------------------------------------------------------------------- #
def load(path, limit):
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for i, line in enumerate(f):
            if limit and i >= limit:
                break
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return rows


def body(r):
    b = r.get("request_json") or {}
    if not isinstance(b, dict):
        try:
            b = json.loads(r.get("request_body", "{}"))
        except Exception:
            b = {}
    return b if isinstance(b, dict) else {}


def content_str(m):
    c = m.get("content")
    if isinstance(c, list):
        parts = []
        for p in c:
            if isinstance(p, dict):
                parts.append(p.get("text") or f"<{p.get('type')}>")
        c = " ".join(str(x) for x in parts)
    return c if isinstance(c, str) else str(c)


def flat(m, cap):
    # role + (capped) content + any tool_call names, so an assistant turn that
    # differs only by which tool it called still hashes distinctly.
    s = f"{m.get('role')}\x00{content_str(m)[:cap]}"
    tc = m.get("tool_calls")
    if isinstance(tc, list):
        s += "\x00" + ",".join((t.get("function") or {}).get("name", "") for t in tc if isinstance(t, dict))
    return s


def parse_ts(r):
    """Best-effort epoch seconds for a record's start / end. Returns (start, end).
    Uses raw_payload.ts / ts_end (ISO w/ tz) when present, else the top-level
    timestamp. Missing -> None."""
    rp = r.get("raw_payload") or {}
    def one(v):
        if not v or not isinstance(v, str):
            return None
        v = v.strip()
        # ISO 8601 (e.g. 2026-06-29T14:16:24.601+08:00)
        try:
            return datetime.fromisoformat(v).timestamp()
        except ValueError:
            pass
        # oneapi format: 2026/05/12 - 00:08:01
        for fmt in ("%Y/%m/%d - %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
            try:
                return datetime.strptime(v, fmt).timestamp()
            except ValueError:
                continue
        return None
    start = one(rp.get("ts")) or one(r.get("timestamp"))
    end = one(rp.get("ts_end")) or one(rp.get("last_chunk_t"))
    return start, end


# --------------------------------------------------------------------------- #
# Causal parent reconstruction
# --------------------------------------------------------------------------- #
def reconstruct(rows, cap):
    n = len(rows)
    msgs = [body(r).get("messages") for r in rows]
    start = [None] * n
    end = [None] * n
    for i, r in enumerate(rows):
        start[i], end[i] = parse_ts(r)

    # incremental prefix hashes + full-message-list hash for every request
    pref_hashes = [[] for _ in range(n)]
    full_hash = [None] * n
    for i, m in enumerate(msgs):
        if not isinstance(m, list) or not m:
            continue
        h = hashlib.md5()
        ph = []
        for mm in m:
            if not isinstance(mm, dict):
                break
            h.update(flat(mm, cap).encode("utf-8", "replace"))
            ph.append(h.hexdigest())
        pref_hashes[i] = ph
        full_hash[i] = ph[-1] if ph else None

    # map: hash of a COMPLETE request's message list -> indices with that body
    full_by_hash = defaultdict(list)
    for i in range(n):
        if full_hash[i] is not None:
            full_by_hash[full_hash[i]].append(i)

    def sort_key_start(i):
        return (start[i] if start[i] is not None else float("inf"), i)

    parent = [None] * n
    for i in range(n):
        ph = pref_hashes[i]
        L = len(ph)
        if L < 2:
            continue
        # deepest k (< L) whose prefix equals some complete request's full body
        for k in range(L - 1, 0, -1):
            cands = full_by_hash.get(ph[k - 1])
            if not cands:
                continue
            # causal candidates: started before B (prefer completed before B)
            best = None
            for a in cands:
                if a == i:
                    continue
                sa = start[a]
                si = start[i]
                if sa is not None and si is not None and sa >= si:
                    continue  # A started at/after B -> can't be B's warmer
                # prefer the A that most recently COMPLETED before B started
                score = end[a] if end[a] is not None else (start[a] if start[a] is not None else -1)
                if best is None or score > best[0]:
                    best = (score, a)
            if best is not None:
                parent[i] = best[1]
                break
    return {
        "n": n, "msgs": msgs, "start": start, "end": end,
        "parent": parent,
    }


# --------------------------------------------------------------------------- #
# Chain / fork structure
# --------------------------------------------------------------------------- #
def structure(rec):
    n, parent = rec["n"], rec["parent"]
    children = defaultdict(list)
    for i, p in enumerate(parent):
        if p is not None:
            children[p].append(i)
    roots = [i for i in range(n) if parent[i] is None and (children[i] or False)]
    singletons = [i for i in range(n)
                  if parent[i] is None and not children[i]]
    true_forks = sum(1 for i in range(n) if len(children[i]) >= 2)
    fork_edges = sum(len(children[i]) for i in range(n) if len(children[i]) >= 2)

    # component (session) = weakly connected set touched by >=1 edge
    def root_of(i):
        while parent[i] is not None:
            i = parent[i]
        return i
    comp = defaultdict(list)
    for i in range(n):
        if parent[i] is not None or children[i]:
            comp[root_of(i)].append(i)
    sizes = sorted((len(v) for v in comp.values()), reverse=True)

    # depth (turns) of the longest chain in each component
    def chain_depth(i):
        best = 1
        for c in children[i]:
            best = max(best, 1 + chain_depth(c))
        return best
    depths = sorted((chain_depth(r) for r in comp), reverse=True)
    return {
        "children": children, "roots": roots, "singletons": singletons,
        "sessions": len(comp), "sizes": sizes, "depths": depths,
        "true_forks": true_forks, "fork_edges": fork_edges,
        "in_session": sum(sizes),
    }


# --------------------------------------------------------------------------- #
# Durations + critical path + schedule simulation
# --------------------------------------------------------------------------- #
def durations(rec, mode):
    n, start, end = rec["n"], rec["start"], rec["end"]
    raw = []
    for i in range(n):
        if start[i] is not None and end[i] is not None and end[i] > start[i]:
            raw.append(end[i] - start[i])
        else:
            raw.append(None)
    have = [d for d in raw if d is not None]
    if mode == "unit" or len(have) < 0.5 * n:
        return [1.0] * n, "unit (1.0/request; durations absent or unit mode)"
    med = statistics.median(have)
    d = [x if x is not None else med for x in raw]
    return d, f"production ts_end-ts (median {med:.2f}s fills {n - len(have)} gaps)"


def critical_path(rec, d):
    parent = rec["parent"]
    memo = {}
    def cp(i):
        if i in memo:
            return memo[i]
        p = parent[i]
        memo[i] = d[i] + (cp(p) if p is not None else 0.0)
        return memo[i]
    return max((cp(i) for i in range(rec["n"])), default=0.0)


def sim_causal(rec, struct, d, C):
    """Precedence-aware greedy schedule with C workers. Returns makespan."""
    parent, n = rec["parent"], rec["n"]
    children = struct["children"]
    indeg = [1 if parent[i] is not None else 0 for i in range(n)]
    start = rec["start"]
    ready = deque(sorted((i for i in range(n) if indeg[i] == 0),
                         key=lambda i: (start[i] if start[i] is not None else float("inf"), i)))
    running = []  # heap (finish_time, node)
    t = 0.0
    free = C
    started = 0
    while ready or running:
        while free > 0 and ready:
            node = ready.popleft()
            heapq.heappush(running, (t + d[node], node))
            free -= 1
            started += 1
        if not running:
            break
        ft, node = heapq.heappop(running)
        t = ft
        free += 1
        for c in children[node]:
            indeg[c] -= 1
            if indeg[c] == 0:
                ready.append(c)
    return t


def sim_naive(d, C):
    """No precedence: C workers, all requests independent (upper bound on speed)."""
    heap = [0.0] * C
    heapq.heapify(heap)
    for x in sorted(d, reverse=True):  # LPT for a tight greedy bound
        wf = heapq.heappop(heap)
        heapq.heappush(heap, wf + x)
    return max(heap)


# --------------------------------------------------------------------------- #
def hist(xs, buckets=((1, 1), (2, 3), (4, 7), (8, 15), (16, 31), (32, 63), (64, 10 ** 9))):
    out = []
    for lo, hi in buckets:
        c = sum(1 for x in xs if lo <= x <= hi)
        if c:
            label = f"{lo}" if lo == hi else (f"{lo}+" if hi > 10 ** 8 else f"{lo}-{hi}")
            out.append(f"{label}:{c}")
    return " ".join(out)


def analyze(path, limit, concurrencies, cap):
    print(f"\n{'=' * 74}\n{path}\n{'=' * 74}")
    rows = load(path, limit)
    rec = reconstruct(rows, cap)
    st = structure(rec)
    n = rec["n"]

    print(f"records={n}  in-session={st['in_session']} ({st['in_session']/n:.0%})  "
          f"singletons={len(st['singletons'])}")
    print(f"sessions(chains)={st['sessions']}  size hist(reqs/chain): {hist(st['sizes'])}  "
          f"max={st['sizes'][0] if st['sizes'] else 0}")
    print(f"chain depth hist(turns): {hist(st['depths'])}  deepest={st['depths'][0] if st['depths'] else 0}")
    print(f"TRUE fork points={st['true_forks']}  (fork edges={st['fork_edges']})  "
          f"-> {'near-linear chains' if st['fork_edges'] <= 0.05*n else 'some real branching'}")

    for mode in ("unit", "duration"):
        d, desc = durations(rec, mode)
        if mode == "duration" and desc.startswith("unit"):
            continue  # no real durations; unit line already covers it
        cp = critical_path(rec, d)
        work = sum(d)
        ceiling = work / cp if cp > 0 else float("inf")
        unit_lbl = "reqs" if mode == "unit" else "s"
        print(f"\n  [{mode}] durations = {desc}")
        print(f"  total work={work:.1f}{unit_lbl}  critical path(longest chain)={cp:.1f}{unit_lbl}  "
              f"=> concurrency CEILING (C=inf) = {ceiling:.1f}")
        print(f"  {'C':>5} | {'makespan_causal':>16} {'makespan_naive':>15} | "
              f"{'sustained_conc':>14} {'utilization':>11} | cost_vs_naive")
        for C in concurrencies:
            mk = sim_causal(rec, st, d, C)
            nv = sim_naive(d, C)
            sustained = work / mk if mk > 0 else 0.0
            util = sustained / C
            cost = mk / nv if nv > 0 else float("inf")
            print(f"  {C:>5} | {mk:>15.1f}{unit_lbl} {nv:>14.1f}{unit_lbl} | "
                  f"{sustained:>14.1f} {util:>10.0%} | {cost:>6.2f}x")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("datasets", nargs="+", help="replay JSONL dataset path(s)")
    ap.add_argument("--limit", type=int, default=4000, help="max records per dataset (0=all). Default 4000")
    ap.add_argument("--concurrency", default="5,16,64,256",
                    help="comma-separated target concurrency levels. Default 5,16,64,256")
    ap.add_argument("--msg-cap", type=int, default=20000,
                    help="chars per message hashed for prefix identity. Default 20000")
    args = ap.parse_args()
    cons = [int(x) for x in args.concurrency.split(",") if x.strip()]
    for p in args.datasets:
        try:
            analyze(p, args.limit, cons, args.msg_cap)
        except Exception:
            import traceback
            traceback.print_exc()


if __name__ == "__main__":
    main()
