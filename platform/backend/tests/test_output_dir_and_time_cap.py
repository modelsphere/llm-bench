"""Guards for the worker's scratch-dir lifecycle and the generic module time cap.

Both exist because of the 2026-07-22 incident:

  * Submission output dirs were created with tempfile.mkdtemp and never deleted.
    One replay with save_responses=true left a 1.4GB file, the container has no
    ephemeral-storage limit, and the node's disk was already 78% full — so a
    long-lived worker would eventually push the node into DiskPressure and get
    pods evicted (postgres shares that node).
  * A guidellm sweep wedged for 7h20m on a 660s budget with nothing to stop it.
    Per-module caps now cover the known modules; this cap is the backstop for the
    ones that have none and for anything added later.

These helpers are pure filesystem/threading, so they need no test database.
"""
from __future__ import annotations

import os
import threading
import time

import pytest

from app.queue import jobs


class _Res:
    """Stand-in for ModuleResult (only the two fields the cleanup logic reads)."""

    def __init__(self, passed=True, error=None):
        self.passed = passed
        self.error = error


# --------------------------------------------------------------------------
# scratch-dir cleanup
# --------------------------------------------------------------------------

def _make_dir(tmp_path, name="llmbench_submission_1_abc", size_bytes=16):
    d = tmp_path / name
    d.mkdir()
    (d / "artifact.bin").write_bytes(b"x" * size_bytes)
    return d


def test_clean_run_removes_its_output_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "OUTPUT_DIR_KEEP", "on-failure")
    d = _make_dir(tmp_path)
    jobs._cleanup_output_dir(1, str(d), [_Res(passed=True)])
    assert not d.exists(), "a clean run's artifacts are redundant — DB has the numbers"


def test_failed_run_keeps_its_output_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "OUTPUT_DIR_KEEP", "on-failure")
    d = _make_dir(tmp_path)
    jobs._cleanup_output_dir(1, str(d), [_Res(passed=True), _Res(passed=False, error="boom")])
    assert d.exists(), "a failure's artifacts are the whole diagnosis"


def test_keep_always_and_never_are_honoured(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "OUTPUT_DIR_KEEP", "always")
    d = _make_dir(tmp_path, "llmbench_submission_2_a")
    jobs._cleanup_output_dir(2, str(d), [_Res(passed=True)])
    assert d.exists()

    monkeypatch.setattr(jobs, "OUTPUT_DIR_KEEP", "never")
    d2 = _make_dir(tmp_path, "llmbench_submission_3_b")
    jobs._cleanup_output_dir(3, str(d2), [_Res(passed=False, error="boom")])
    assert not d2.exists(), "'never' must drop even a failed run's dir"


def test_cleanup_never_raises_on_a_missing_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "OUTPUT_DIR_KEEP", "on-failure")
    jobs._cleanup_output_dir(4, str(tmp_path / "gone"), [_Res(passed=True)])


# --------------------------------------------------------------------------
# retention sweep — what stops "keep on failure" from silently becoming a leak
# --------------------------------------------------------------------------

def test_sweep_removes_stale_dirs_but_spares_fresh_ones(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(jobs, "OUTPUT_DIR_RETENTION_HOURS", 24.0)
    monkeypatch.setattr(jobs, "_non_terminal_submission_ids", lambda ids: set())

    stale = _make_dir(tmp_path, "llmbench_submission_10_old")
    fresh = _make_dir(tmp_path, "llmbench_submission_11_new")
    unrelated = tmp_path / "something_else"
    unrelated.mkdir()

    old = time.time() - 48 * 3600
    os.utime(stale, (old, old))

    jobs._sweep_stale_output_dirs()

    assert not stale.exists(), "dirs past the retention window must be swept"
    assert fresh.exists(), "recent dirs must survive — they may still be needed"
    assert unrelated.exists(), "the sweep must only touch its own prefix"


def test_sweep_disabled_by_zero_retention(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(jobs, "OUTPUT_DIR_RETENTION_HOURS", 0.0)
    monkeypatch.setattr(jobs, "_non_terminal_submission_ids", lambda ids: set())
    stale = _make_dir(tmp_path, "llmbench_submission_12_old")
    old = time.time() - 999 * 3600
    os.utime(stale, (old, old))

    jobs._sweep_stale_output_dirs()
    assert stale.exists()


# --------------------------------------------------------------------------
# generic module time cap
# --------------------------------------------------------------------------

def test_time_cap_trips_abort_and_is_distinguishable_from_cancel():
    cancel = threading.Event()
    abort, timed_out, stop = jobs._start_module_deadline(1, "m", cancel, cap_seconds=0.5)
    try:
        assert abort.wait(timeout=15), "the cap must actually reach the module"
        assert timed_out.is_set(), "a blown cap must NOT be reported as a user cancel"
    finally:
        stop()


def test_user_cancel_trips_abort_without_marking_it_a_timeout():
    cancel = threading.Event()
    abort, timed_out, stop = jobs._start_module_deadline(1, "m", cancel, cap_seconds=3600)
    try:
        cancel.set()
        assert abort.wait(timeout=15)
        assert not timed_out.is_set(), "a real cancel must stay labelled 'Canceled'"
    finally:
        stop()


def test_zero_cap_disables_the_deadline():
    cancel = threading.Event()
    abort, timed_out, stop = jobs._start_module_deadline(1, "m", cancel, cap_seconds=0)
    try:
        assert not abort.wait(timeout=2)
        assert not timed_out.is_set()
    finally:
        stop()


def test_stop_retires_the_watchdog_thread():
    """The incident also involved leaked threads; don't add another source."""
    before = threading.active_count()
    cancel = threading.Event()
    _abort, _timed_out, stop = jobs._start_module_deadline(1, "m", cancel, cap_seconds=3600)
    stop()
    time.sleep(0.2)
    assert threading.active_count() <= before, "watchdog must not outlive the module"


# --------------------------------------------------------------------------
# per-module cap table
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "module_name,expected_hours",
    [
        ("functional_acceptance", 1),
        ("case_truncation", 1),
        ("perf_guidellm", 4),
        ("perf_guidellm_sweep", 8),
        ("replay", 24),
        ("opencompass", 48),
    ],
)
def test_each_module_gets_its_configured_cap(module_name, expected_hours):
    from bench.modules import get_module

    cls = get_module(module_name)
    params = cls.ParamsSchema(**cls.default_params())
    assert jobs._module_time_cap(cls, params, module_name) == expected_hours * 3600


def test_a_module_configured_longer_than_its_cap_is_not_aborted():
    """The table is a FLOOR, not a ceiling.

    replay's max_seconds accepts up to 7 days, so an admin can legitimately
    configure a run longer than the table entry. If the backstop stayed at 24h it
    would abort that run before the module's own, cleaner stop could fire.
    """
    from bench.modules import get_module

    cls = get_module("replay")
    d = cls.default_params()
    d["max_seconds"] = 30 * 3600
    params = cls.ParamsSchema(**d)

    cap = jobs._module_time_cap(cls, params, "replay")
    assert cap > 30 * 3600, "the cap must stay above the module's own declared budget"
    assert cap > jobs.MODULE_TIME_CAPS_SECONDS["replay"]


def test_env_override_replaces_the_table_value(monkeypatch):
    from bench.modules import get_module

    monkeypatch.setenv("MODULE_TIME_CAP_CASE_TRUNCATION", "900")
    cls = get_module("case_truncation")
    params = cls.ParamsSchema(**cls.default_params())
    assert jobs._module_time_cap(cls, params, "case_truncation") == 900


def test_unknown_module_falls_back_to_the_default_cap():
    class _Bare:
        @classmethod
        def time_budget_seconds(cls, params):
            return None

    assert (
        jobs._module_time_cap(_Bare, object(), "something_new")
        == jobs.MODULE_TIME_CAP_DEFAULT_SECONDS
    )


def test_a_raising_budget_hook_does_not_break_the_run():
    class _Broken:
        @classmethod
        def time_budget_seconds(cls, params):
            raise RuntimeError("boom")

    assert jobs._module_time_cap(_Broken, object(), "case_truncation") == 3600


def test_a_run_that_produced_no_results_keeps_its_dir(tmp_path, monkeypatch):
    """Empty results = crashed/cancelled before any module finished.

    `any()` over an empty list is False, so without an explicit check this
    deletes the artifacts in exactly the case they are most needed.
    """
    monkeypatch.setattr(jobs, "OUTPUT_DIR_KEEP", "on-failure")
    d = _make_dir(tmp_path, "llmbench_submission_20_crash")
    jobs._cleanup_output_dir(20, str(d), [])
    assert d.exists(), "a run with no results crashed — keep the evidence"


def test_sweep_never_deletes_a_still_running_submissions_dir(tmp_path, monkeypatch):
    """/tmp is shared by every dramatiq fork, and mtime is not a liveness signal:
    one module can run for hours without touching its parent dir. Deleting a live
    run's artifacts out from under it would be far worse than leaking disk."""
    monkeypatch.setattr(jobs.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(jobs, "OUTPUT_DIR_RETENTION_HOURS", 24.0)
    monkeypatch.setattr(jobs, "_non_terminal_submission_ids", lambda ids: {42})

    live = _make_dir(tmp_path, "llmbench_submission_42_running")
    dead = _make_dir(tmp_path, "llmbench_submission_43_finished")
    old = time.time() - 999 * 3600
    os.utime(live, (old, old))
    os.utime(dead, (old, old))

    jobs._sweep_stale_output_dirs()

    assert live.exists(), "a RUNNING submission's dir must survive the sweep"
    assert not dead.exists()


def test_sweep_fails_safe_when_liveness_cannot_be_determined(tmp_path, monkeypatch):
    """No DB, no deletions. Leaking disk is recoverable; deleting evidence isn't."""
    monkeypatch.setattr(jobs.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(jobs, "OUTPUT_DIR_RETENTION_HOURS", 24.0)

    def _boom(ids):
        raise RuntimeError("db down")

    monkeypatch.setattr(jobs, "_non_terminal_submission_ids", _boom)
    d = _make_dir(tmp_path, "llmbench_submission_44_old")
    old = time.time() - 999 * 3600
    os.utime(d, (old, old))

    jobs._sweep_stale_output_dirs()
    assert d.exists(), "sweep must not delete anything when it cannot verify liveness"


def test_malformed_dir_name_is_still_sweepable(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(jobs, "OUTPUT_DIR_RETENTION_HOURS", 24.0)
    monkeypatch.setattr(jobs, "_non_terminal_submission_ids", lambda ids: set())
    d = _make_dir(tmp_path, "llmbench_submission_notanid_x")
    old = time.time() - 999 * 3600
    os.utime(d, (old, old))
    jobs._sweep_stale_output_dirs()
    assert not d.exists()


# --------------------------------------------------------------------------
# time-cap ENFORCEMENT — hard-exit a module that ignores its abort
#
# The gap this closes: setting abort_event only ASKS a module to stop. One that
# never observes it (a C-level hang, guidellm's teardown deadlock) blocks the
# fork forever while the heartbeat thread keeps beating, so the reaper never sees
# it. After a grace window the worker hard-exits, converging into the existing
# missing-heartbeat reaper path. _worker_hard_exit is stubbed so no test exits.
# --------------------------------------------------------------------------

def _wait_for(pred, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


def test_wedged_module_hard_exits_after_grace(monkeypatch):
    monkeypatch.setattr(jobs, "MODULE_ABORT_GRACE_SECONDS", 0.5)
    exits = []
    monkeypatch.setattr(jobs, "_worker_hard_exit", lambda code: exits.append(code))

    cancel = threading.Event()
    # cap 0.2s, and we NEVER call stop() -> the module "never returns"
    abort, timed_out, stop = jobs._start_module_deadline(1, "m", cancel, cap_seconds=0.2)
    try:
        assert _wait_for(lambda: exits), "a module that ignores the cap must hard-exit the fork"
        assert exits == [jobs.WORKER_WEDGE_EXIT_CODE]
        assert timed_out.is_set(), "and it must be recorded as a timeout, not a cancel"
    finally:
        stop()


def test_ignored_cancel_also_hard_exits_but_not_as_timeout(monkeypatch):
    monkeypatch.setattr(jobs, "MODULE_ABORT_GRACE_SECONDS", 0.5)
    exits = []
    monkeypatch.setattr(jobs, "_worker_hard_exit", lambda code: exits.append(code))

    cancel = threading.Event()
    cancel.set()  # user cancel that the module then ignores
    abort, timed_out, stop = jobs._start_module_deadline(1, "m", cancel, cap_seconds=3600)
    try:
        assert _wait_for(lambda: exits), "an ignored cancel must also free the fork"
        assert not timed_out.is_set(), "an ignored cancel is not a time-cap breach"
    finally:
        stop()


def test_module_that_returns_within_grace_is_not_killed(monkeypatch):
    monkeypatch.setattr(jobs, "MODULE_ABORT_GRACE_SECONDS", 5.0)
    exits = []
    monkeypatch.setattr(jobs, "_worker_hard_exit", lambda code: exits.append(code))

    cancel = threading.Event()
    abort, timed_out, stop = jobs._start_module_deadline(1, "m", cancel, cap_seconds=0.2)
    assert _wait_for(lambda: abort.is_set()), "cap should fire"
    stop()  # module noticed abort and returned (loop's finally calls stop)
    time.sleep(0.3)
    assert exits == [], "a module that unwinds within grace must NOT be killed"


def test_grace_zero_reverts_to_cooperative_only(monkeypatch):
    monkeypatch.setattr(jobs, "MODULE_ABORT_GRACE_SECONDS", 0.0)
    exits = []
    monkeypatch.setattr(jobs, "_worker_hard_exit", lambda code: exits.append(code))

    cancel = threading.Event()
    abort, timed_out, stop = jobs._start_module_deadline(1, "m", cancel, cap_seconds=0.2)
    try:
        assert _wait_for(lambda: timed_out.is_set()), "cap still fires"
        time.sleep(0.5)
        assert exits == [], "grace=0 must disable the hard exit"
    finally:
        stop()
