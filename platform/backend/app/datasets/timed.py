"""Timed datasets: a window of production traffic kept whole, in time order,
for a replay on the recorded timing (1× load).

A sampled dataset (the default layout) keeps single requests spread over a day.
A timed dataset instead keeps **every** request of one contiguous window, so the
engine under test sees exactly the load production saw, and records for each
request when to send it and what it waits for:

    "timing": {
      "at_s":    send time from the start of the replay, idle gaps removed
      "session": session id ("" when none is known)
      "turn":    1-based position in its session within the file
      "after":   0-based line of the request this one follows in its session,
                 or null (first turn, or the previous turn was still running)
      "think_s": the time from that request's end to this one's start, on
                 the replay's timeline (idle gaps shortened as for at_s); a
                 replay sends the turn that long after the previous one
                 finishes on the engine under test
      "think_s_logged": the same time as production logged it
      "rt_s":    production's latency
      "ts":      production's start time, ISO 8601
    }

The window is chosen from the timing of every request in the profile's
`window_hours`: the run of `sample_size` consecutive requests whose median
concurrency (C_req, see load_profile) is closest to the typical value, so the
replay shows typical load, not a quiet night.

Idle gaps (nothing in flight for longer than `max_idle_gap_s`) are shortened to
that length. With nothing in flight the engine has nothing to interfere with, so
this changes no request's conditions, and it is what makes a quiet backend's
hour replayable in minutes.

Pure functions; the builder does the I/O.
"""
from __future__ import annotations

import hashlib
import json
import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.datasets import load_profile

PREFIX_DEPTHS = (2, 3, 4, 5, 6)


# ---------------------------------------------------------------------------
# choosing the window
# ---------------------------------------------------------------------------


def choose_window(spans: list[tuple[float, float]], size: int, c_target: float | None = None
                  ) -> dict[str, Any]:
    """The run of `size` consecutive requests (by start) whose median C_req is
    closest to `c_target` (default: the median over all of them), the latest on
    a tie. `spans` = (start, end) in any order. Returns the window's start (its
    first request's start), end (just after its last request's start), and the
    medians compared."""
    if not spans:
        raise ValueError("no requests to choose a window from")
    order = sorted(range(len(spans)), key=lambda i: spans[i][0])
    starts = [spans[i][0] for i in order]
    creq_all = load_profile.per_request_concurrency(spans)
    creq = [creq_all[i] for i in order]
    target = c_target if c_target and c_target > 0 else statistics.median(creq)
    n = len(starts)
    if n <= size:
        best = 0
        size = n
    else:
        step = max(1, size // 10)
        best, best_score = 0, math.inf
        for i in list(range(0, n - size + 1, step)) + [n - size]:
            med = statistics.median(creq[i:i + size])
            score = abs(math.log(max(med, 1e-9) / target))
            if score <= best_score:
                best, best_score = i, score
    window = creq[best:best + size]
    return {
        "start": starts[best],
        "end": starts[best + size - 1] + 1e-3,
        "requests": size,
        "c_req_median": statistics.median(window),
        "c_target": target,
        "c_req_median_all": statistics.median(creq),
    }


# ---------------------------------------------------------------------------
# sessions
# ---------------------------------------------------------------------------


@dataclass
class Row:
    """One request of the window, as the builder hands it over."""
    start: float
    rt: float
    session: str = ""                 # from the session field, when the logs carry one
    messages: list | None = None      # the request's messages, for prefix matching
    payload: Any = None               # whatever the builder needs back (the record)
    # message_hashes(messages), instead of the messages: a window of long
    # agent requests does not fit in memory as parsed message lists.
    hashes: dict | None = None
    timing: dict = field(default_factory=dict)


def _h(obj: Any) -> str:
    return hashlib.sha1(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def message_hashes(messages: list | None) -> dict[int, str] | None:
    """What linking needs of a message list: the hash of the whole list (key
    0) and of the list without its last k messages, for each k it looks back."""
    if not messages:
        return None
    out = {0: _h(messages)}
    for k in PREFIX_DEPTHS:
        if len(messages) > k:
            out[k] = _h(messages[:-k])
    return out


def link_sessions(rows: list[Row]) -> dict[str, int]:
    """Sort `rows` by start (in place) and fill each one's session, turn, after
    and think_s.

    A row's session is its session field when it has one. Otherwise, a request
    whose message list continues an earlier request's whole message list (the
    earlier one plus up to six more messages: the answer, tool results, the
    next question) belongs to that request's session; with neither it starts a
    session of its own.

    Within a session a request follows the latest earlier one that had
    finished when it started (`after`), `think_s` later. One whose earlier
    turns were all still running (a client sending in parallel) follows none.
    Returns how many rows got their session from each source."""
    rows.sort(key=lambda r: r.start)
    by_full: dict[str, int] = {}
    sources = {"field": 0, "prefix": 0, "none": 0}
    session_of: list[str] = []
    for i, r in enumerate(rows):
        sid = r.session
        hashes = r.hashes if r.hashes is not None else message_hashes(r.messages)
        if sid:
            sources["field"] += 1
        elif hashes:
            parent = None
            for k in PREFIX_DEPTHS:
                if k in hashes:
                    parent = by_full.get(hashes[k])
                    if parent is not None:
                        break
            if parent is not None:
                sid = session_of[parent]
                sources["prefix"] += 1
        if not sid:
            sid = f"~{i}"                  # a session of its own
            sources["none"] += 1
        session_of.append(sid)
        if hashes:
            by_full[hashes[0]] = i

    members: dict[str, list[int]] = {}
    for i, sid in enumerate(session_of):
        members.setdefault(sid, []).append(i)
    for sid, idx in members.items():
        # A session rebuilt from prefixes is named after its first request; a
        # request alone in its own is in none.
        name = sid if not sid.startswith("~") else (f"prefix-{rows[idx[0]].start:.3f}" if len(idx) > 1 else "")
        for turn, i in enumerate(idx, start=1):
            r = rows[i]
            done = [j for j in idx[:turn - 1] if rows[j].start + rows[j].rt <= r.start]
            after = max(done, key=lambda j: rows[j].start + rows[j].rt) if done else None
            r.timing.update(
                session=name,
                turn=turn,
                after=after,
                think_s=None if after is None else round(r.start - (rows[after].start + rows[after].rt), 3),
            )
    return sources


# ---------------------------------------------------------------------------
# idle gaps
# ---------------------------------------------------------------------------


def remove_gaps(rows: list[Row], max_idle_gap_s: float) -> dict[str, float]:
    """Fill each row's `at_s`: its start from the first row's, less every idle
    stretch (nothing in flight) beyond `max_idle_gap_s` before it. `rows` must
    be sorted by start."""
    if not rows:
        return {"window_s": 0.0, "replay_s": 0.0, "removed_s": 0.0, "gaps": 0}
    t0 = rows[0].start
    busy_until = t0
    removed, gaps = 0.0, 0
    last_end = t0
    for r in rows:
        idle = r.start - busy_until
        if idle > max_idle_gap_s:
            removed += idle - max_idle_gap_s
            gaps += 1
        r.timing["at_s"] = round(r.start - t0 - removed, 3)
        r.timing["rt_s"] = round(r.rt, 3)
        r.timing["ts"] = datetime.fromtimestamp(r.start, timezone.utc).isoformat()
        busy_until = max(busy_until, r.start + r.rt)
        last_end = max(last_end, r.start + r.rt)
    # A later turn is sent `think_s` after the turn it follows ends, so its
    # think time must be measured on the same shortened timeline: left at
    # production's value, every idle stretch cut between two turns of a
    # session comes back in full when the replay waits for it. Nothing is cut
    # while the earlier turn runs (it is in flight), so the shortened gap is
    # this turn's send time less the earlier one's end. Production's own think
    # time stays alongside, for reference.
    removed_think = 0.0
    for r in rows:
        after = r.timing.get("after")
        if after is None:
            continue
        parent = rows[after]
        logged = r.timing.get("think_s")
        short = round(max(0.0, r.timing["at_s"] - (parent.timing["at_s"] + parent.rt)), 3)
        r.timing["think_s_logged"] = logged
        r.timing["think_s"] = short
        if logged is not None:
            removed_think += max(0.0, logged - short)
    window_s = last_end - t0
    return {"window_s": round(window_s, 1), "replay_s": round(window_s - removed, 1),
            "removed_s": round(removed, 1), "gaps": gaps,
            "think_removed_s": round(removed_think, 1)}


def summarize(rows: list[Row], sources: dict[str, int], gaps: dict[str, float]) -> dict[str, Any]:
    sessions: dict[str, int] = {}
    for k, r in enumerate(rows):
        key = r.timing.get("session") or f"~{k}"
        sessions[key] = sessions.get(key, 0) + 1
    turns = sorted(sessions.values())
    linked = sum(1 for r in rows if r.timing.get("after") is not None)
    return {
        "requests": len(rows),
        "sessions": len(sessions),
        "turns_p50": statistics.median(turns) if turns else 0,
        "turns_p90": turns[int(0.9 * (len(turns) - 1))] if turns else 0,
        "turns_max": turns[-1] if turns else 0,
        "following_share": (linked / len(rows)) if rows else 0.0,
        "session_source": sources,
        **gaps,
    }
