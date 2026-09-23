"""Guards against the 2026-07-23 outage: a transaction left open across a module run.

The worker commits before calling module.run() precisely so the connection is not
held for the (hours-long) duration. That worked until SQLAlchemy's default
expire_on_commit=True quietly undid it: commit() expires every loaded object, so
the next attribute access issues a refresh SELECT and OPENS A NEW TRANSACTION,
which nothing closed until the module returned. The connection then sat `idle in
transaction`, which blocks DDL on `submissions` (a pending ACCESS EXCLUSIVE lock
queues every subsequent query behind it) and pins autovacuum.

The failure is silent — no error, no log line, just a connection quietly holding a
transaction — so it needs a test that fails loudly.
"""
from __future__ import annotations

import threading

from app.db import models
from app.queue import jobs


def test_sync_session_factory_disables_expire_on_commit():
    """The whole class of bugs comes back the moment this flips to True."""
    kw = getattr(models._sync_session_factory, "kw", {})
    assert kw.get("expire_on_commit") is False, (
        "sync sessions must not expire on commit: an attribute read afterwards "
        "issues a refresh SELECT that re-opens a transaction, which the worker "
        "then holds for the entire module run"
    )


def test_progress_callback_holds_no_orm_objects():
    """The callback fires for the whole module run, so it must not be able to
    reach the shared session at all — ints only, by construction."""
    import inspect

    params = list(inspect.signature(jobs._make_progress_cb).parameters)
    assert params[0] == "submission_id", (
        f"_make_progress_cb takes {params[0]!r}; it must take a plain submission_id, "
        "not a mapped object whose attribute access would hit the session"
    )

    cb = jobs._make_progress_cb(1, 2, "m", threading.Event())
    for cell in (cb.__closure__ or ()):
        val = cell.cell_contents
        assert not isinstance(val, models.Base), (
            f"progress callback closes over a mapped ORM object ({type(val).__name__}); "
            "touching it during a run re-opens a transaction on the shared session"
        )


def test_progress_callback_cancels_from_the_event_only():
    """Cancellation must come from cancel_event (set by the watcher in its own
    session), never from a stale attribute on the shared object."""
    ev = threading.Event()
    cb = jobs._make_progress_cb(1, 2, "m", ev)

    published = []
    jobs._publish_progress_event = lambda *a, **k: published.append(a)  # type: ignore[assignment]

    cb(0.5, "halfway")           # not cancelled -> returns normally
    assert published, "progress must still be published"

    ev.set()
    try:
        cb(0.6, "later")
    except jobs.SubmissionCancelled:
        pass
    else:
        raise AssertionError("setting cancel_event must abort the module")


def test_module_loop_commits_before_running_a_module():
    """A commit must sit between the run-row setup and module_cls().run(), or the
    setup's transaction rides along for the whole module."""
    import inspect

    src = inspect.getsource(jobs._run_module_loop)
    before = src.split("module_cls().run(")[0]
    tail = before[before.rindex("_make_progress_cb"):]
    assert "session.commit()" in tail, (
        "no session.commit() between building the progress callback and "
        "module_cls().run() — the setup transaction would stay open for the "
        "entire module run"
    )
