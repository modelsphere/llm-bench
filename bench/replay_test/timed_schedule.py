"""When to send each request of a timed dataset (1× replay).

A timed dataset (platform/backend/app/datasets/timed.py) gives every request a
`timing` block. Two rules decide its send time:

- a request that follows no earlier one (`after` is null) is sent `at_s`
  seconds after the replay starts;
- a later turn of a session (`after` = the line it follows) is sent `think_s`
  after that request finishes **on the engine under test**. A faster engine
  brings the next turn sooner, as it would in production; a slower one delays
  it.

This module only decides times; it sends nothing and reads no clock, so it is
tested with plain numbers. All times are seconds from the start of the replay.
"""
from __future__ import annotations

import heapq
from collections import defaultdict
from typing import List, Optional, Tuple


class TimingMissing(ValueError):
    """The dataset has requests without a usable `timing` block."""


class TimedSchedule:
    def __init__(self, timings: List[Optional[dict]]):
        n = len(timings)
        missing = [i for i, t in enumerate(timings)
                   if not isinstance(t, dict) or not isinstance(t.get("at_s"), (int, float))]
        if missing:
            raise TimingMissing(
                f"{len(missing)} of {n} requests have no timing (first: line {missing[0]}). "
                "A timed replay needs a timed dataset: build one with layout 'timed'."
            )
        self.at_s = [float(t["at_s"]) for t in timings]
        self.think_s: List[float] = []
        self.children: dict[int, List[int]] = defaultdict(list)
        self._ready: List[Tuple[float, int]] = []
        for i, t in enumerate(timings):
            after = t.get("after")
            think = t.get("think_s")
            self.think_s.append(max(0.0, float(think)) if isinstance(think, (int, float)) else 0.0)
            # Only an earlier line can be waited on; anything else (a dangling or
            # forward reference) is sent on its recorded time instead.
            if isinstance(after, int) and 0 <= after < i:
                self.children[after].append(i)
            else:
                heapq.heappush(self._ready, (self.at_s[i], i))
        self.total = n
        self.released = 0

    def finished(self, idx: int, end_s: float) -> None:
        """Request `idx` finished at `end_s`: its next turns become due."""
        for c in self.children.pop(idx, ()):
            heapq.heappush(self._ready, (end_s + self.think_s[c], c))

    def next_due(self) -> Optional[float]:
        return self._ready[0][0] if self._ready else None

    def pop_due(self, now_s: float, limit: Optional[int] = None) -> List[Tuple[int, float]]:
        """Requests due by `now_s`, earliest first, as (index, due time). At most
        `limit` are released; the rest stay due and keep their due time."""
        out: List[Tuple[int, float]] = []
        while self._ready and self._ready[0][0] <= now_s and (limit is None or len(out) < limit):
            due, i = heapq.heappop(self._ready)
            out.append((i, due))
        self.released += len(out)
        return out

    def waiting(self) -> int:
        """Requests not yet released (due later, or waiting on an earlier turn)."""
        return self.total - self.released


def time_weighted_mean(changes: List[Tuple[float, int]], end_s: float) -> float:
    """Mean of a step function given as (time, new value) changes, over [first change, end_s]."""
    if not changes:
        return 0.0
    area = 0.0
    for (t, v), (t2, _) in zip(changes, changes[1:] + [(end_s, 0)]):
        area += v * max(0.0, t2 - t)
    span = end_s - changes[0][0]
    return area / span if span > 0 else 0.0
