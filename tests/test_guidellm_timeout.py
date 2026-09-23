"""Guards for the guidellm wall-clock timeout.

A real run was observed finishing its request phase normally — full duration,
every request HTTP 200 — and then deadlocking in teardown for 7+ hours, pinning
a worker slot with its 8 forked processes parked forever. Nothing inside
guidellm aborted: killing every one of its workers produced only a
"died unexpectedly" log line, not a failed run.

So the external deadline in utils.bench is the ONLY thing that can bound such a
run. These tests pin both layers of it:
  1. the watchdog cancels the task at the deadline, and
  2. if that fails to unwind, the caller still escapes.
"""
from __future__ import annotations

import asyncio
import threading
import time

import pytest

import utils.bench as B
from bench.modules.base import BenchmarkCancelled, BenchmarkTimeout


def test_normal_completion_still_returns_its_result():
    async def quick():
        return "ok"

    assert B._run_coro_sync(quick()) == "ok"


def test_deadline_raises_timeout_on_a_wedged_coroutine():
    async def forever():
        await asyncio.sleep(3600)

    t0 = time.monotonic()
    with pytest.raises(BenchmarkTimeout):
        B._run_coro_sync(forever(), deadline=time.monotonic() + 1.0)
    # Must give up promptly, not ride the coroutine's own 1h sleep.
    assert time.monotonic() - t0 < 30


def test_cancel_event_reports_cancelled_not_timeout():
    """A user cancel and a blown budget must stay distinguishable."""
    async def forever():
        await asyncio.sleep(3600)

    ev = threading.Event()
    ev.set()
    with pytest.raises(BenchmarkCancelled):
        B._run_coro_sync(forever(), cancel_event=ev, deadline=time.monotonic() + 3600)


def test_drain_of_an_uncancellable_pending_task_is_bounded(monkeypatch):
    """The cleanup drain was a second latent forever-wait; it must be bounded."""
    monkeypatch.setattr(B, "DRAIN_TIMEOUT_SECONDS", 1.0)

    async def spawns_a_stubborn_child():
        async def stubborn():
            while True:
                try:
                    await asyncio.sleep(0.2)
                except asyncio.CancelledError:
                    pass  # ignores cancellation, like a wedged guidellm task
        asyncio.ensure_future(stubborn())
        await asyncio.sleep(0)
        return "done"

    t0 = time.monotonic()
    assert B._run_coro_sync(spawns_a_stubborn_child()) == "done"
    assert time.monotonic() - t0 < 30, "drain must not hang on an uncancellable task"


def test_caller_escapes_even_when_cancellation_does_not_unwind(monkeypatch):
    """Layer 2 — the case that actually bit us.

    guidellm wedged so hard that cancelling didn't unwind it. Because
    run_until_complete cannot be interrupted from outside its own thread, the
    only way the caller escapes is to abandon the (daemon) run thread and reap
    the forked children it left behind.
    """
    async def unkillable():
        while True:
            try:
                await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                pass  # refuses to die

    monkeypatch.setattr(B, "benchmark_generative_text", lambda *a, **k: unkillable())
    monkeypatch.setattr(B, "CANCEL_GRACE_SECONDS", 1.0)
    monkeypatch.setattr(B, "DRAIN_TIMEOUT_SECONDS", 1.0)

    reaped = {"calls": 0}

    def _fake_reap(reason):
        reaped["calls"] += 1
        return 3

    monkeypatch.setattr(B, "_kill_orphaned_children", _fake_reap)

    t0 = time.monotonic()
    with pytest.raises(BenchmarkTimeout) as exc:
        B.benchmark_generative_text_sync(object(), timeout=1.0)
    elapsed = time.monotonic() - t0

    assert elapsed < 30, "caller must escape a wedged run, not block forever"
    assert reaped["calls"] >= 1, "orphaned guidellm children must be reaped"
    assert "budget" in str(exc.value)


def test_default_max_run_seconds_applies_when_caller_passes_no_timeout(monkeypatch):
    """Un-updated call sites can still be given a ceiling via env."""
    async def forever():
        await asyncio.sleep(3600)

    monkeypatch.setattr(B, "benchmark_generative_text", lambda *a, **k: forever())
    monkeypatch.setattr(B, "DEFAULT_MAX_RUN_SECONDS", 1.0)
    monkeypatch.setattr(B, "CANCEL_GRACE_SECONDS", 1.0)
    monkeypatch.setattr(B, "_kill_orphaned_children", lambda reason: 0)

    t0 = time.monotonic()
    with pytest.raises(BenchmarkTimeout):
        B.benchmark_generative_text_sync(object())  # no explicit timeout
    assert time.monotonic() - t0 < 30


def test_child_reaper_is_safe_when_there_is_nothing_to_reap():
    """Best-effort by design: never raises, whatever the platform."""
    assert B._kill_orphaned_children("unit-test") == 0
