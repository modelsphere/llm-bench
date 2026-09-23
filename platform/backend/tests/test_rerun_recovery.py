"""Integration test: a submission whose worker was killed mid-run is recovered.

Scenario this guards (the deploy-rollout case):

  1. A worker picks up a submission and starts running it (status RUNNING,
     a SubmissionRun in RUNNING with partial/garbage metrics).
  2. The worker pod is SIGKILLed (helm rollout, OOM, node DNS blip...). No
     in-process exception fires — the DB is simply left mid-flight. The reaper
     may also have flipped the submission to FAILED by the time recovery runs.
  3. Dramatiq's Redis broker redelivers the un-acked message to a new worker,
     which calls run_submission(id) again.

We don't reproduce the broker's redelivery timing (minutes, flaky) — we
reproduce the *exact DB state a kill leaves* and then invoke the actor body,
which is precisely what the new worker does. That isolates the part that is
OUR code (the reset-and-rerun reconciliation in _run_submission_body) and
asserts the recovered submission ends in the correct state, exactly once, with
no stale results carried over.

Skips automatically if no test Postgres/Redis is reachable — see conftest.py.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest
from pydantic import BaseModel

# app.* imports are safe here: conftest.py has already pointed settings at the
# test DB/Redis before collection imported this module.
from app.core.config import settings
from app.core.security import encrypt_api_key
from app.db.models import (
    Base,
    ModuleRunStatus,
    Submission,
    SubmissionRun,
    SubmissionStatus,
    User,
    _async_engine,
    _sync_engine,
    get_sync_session,
)
# Aliased so the names don't start with "Test" — otherwise pytest tries to
# collect these classes as test cases and warns.
from app.db.models import TestModule as ModuleRow
from bench.modules import MODULE_REGISTRY
from bench.modules.base import MetricConfig, ModuleResult, TestModule as BenchModuleBase

MODULE_NAME = "test_fake_recover"


# --------------------------------------------------------------------------- #
# A fake bench module: no LLM endpoint, deterministic metric, and a per-process
# call counter so we can prove how many times it actually executed.
# --------------------------------------------------------------------------- #
class _FakeParams(BaseModel):
    pass


class FakeRecoverModule(BenchModuleBase):
    name = MODULE_NAME
    display_name = "Fake Recover (test only)"
    description = "Test module: returns a fixed metric without calling any endpoint."
    ParamsSchema = _FakeParams
    default_metric_configs = [
        MetricConfig(key="quality", role="score", formula="passthrough", weight=1.0)
    ]

    run_count = 0  # incremented once per successful run() in this process

    def run(self, endpoint, params, output_dir, progress_cb=None, cancel_event=None):
        type(self).run_count += 1
        if progress_cb is not None:
            progress_cb(0.5, "halfway")
        # A value distinct from the "stale" sentinel we seed the killed run with,
        # so we can tell a fresh result apart from a carried-over one.
        return ModuleResult(metrics={"quality": 0.85})


def _services_available() -> bool:
    try:
        import psycopg2
        import redis

        psycopg2.connect(settings.DATABASE_URL_SYNC).close()
        redis.from_url(settings.REDIS_URL).ping()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _services_available(),
    reason="needs a test Postgres + Redis (see tests/conftest.py for docker one-liners)",
)


@pytest.fixture(scope="module", autouse=True)
def _schema_and_registry():
    """Create the schema once and register the fake module for the test session."""
    Base.metadata.create_all(_sync_engine)
    MODULE_REGISTRY[MODULE_NAME] = FakeRecoverModule
    try:
        yield
    finally:
        MODULE_REGISTRY.pop(MODULE_NAME, None)
        # Drop async engine connections so the process exits cleanly.
        import asyncio

        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            _async_engine.dispose()
        )


@pytest.fixture()
def session():
    s = get_sync_session()
    try:
        yield s
    finally:
        s.close()


def _ensure_module_row(session):
    if session.get(ModuleRow, MODULE_NAME) is None:
        session.add(
            ModuleRow(
                name=MODULE_NAME,
                display_name="Fake Recover (test only)",
                description="x",
                params_schema_json={},
                default_params_json={},
                metrics_schema_json={},
            )
        )
        session.commit()


def _make_killed_submission(session, leftover_status: SubmissionStatus):
    """Seed exactly what a SIGKILLed worker leaves behind: a submission in
    `leftover_status` with a half-written, stale SubmissionRun."""
    _ensure_module_row(session)

    # The test DB persists across runs (it's a throwaway service, not dropped
    # between invocations); a random suffix keeps email/username unique.
    uniq = uuid.uuid4().hex[:12]
    user = User(
        email=f"rerun-{uniq}@test.local",
        username=f"rerun-{uniq}",
        password_hash="x",
        role="user",
    )
    session.add(user)
    session.flush()

    sub = Submission(
        user_id=user.id,
        module_name=MODULE_NAME,
        endpoint_url="http://unused.local/v1",
        endpoint_model="fake-model",
        endpoint_api_key_enc=encrypt_api_key("unused"),
        status=leftover_status,
        # Older than the reaper's grace window: this is a genuinely stuck run.
        started_at=datetime.utcnow() - timedelta(seconds=300),
        error="Worker crashed: SIGKILL (will retry)",
    )
    session.add(sub)
    session.flush()

    run = SubmissionRun(
        submission_id=sub.id,
        module_name=MODULE_NAME,
        params_json={},
        status=ModuleRunStatus.RUNNING,
        started_at=datetime.utcnow() - timedelta(seconds=300),
        # Stale partial data from the attempt that was killed. If recovery
        # "resumed" instead of restarting, this sentinel would survive.
        metrics_json={"quality": 0.0, "stale": True},
        score=0.0,
        passed=False,
        error="partway through when killed",
    )
    session.add(run)
    session.commit()
    return sub.id, run.id


@pytest.mark.parametrize(
    "leftover_status",
    [SubmissionStatus.RUNNING, SubmissionStatus.FAILED],
    ids=["killed-left-RUNNING", "reaper-marked-FAILED"],
)
def test_killed_submission_reruns_to_completion(session, leftover_status):
    from app.queue.jobs import run_submission

    sub_id, run_id = _make_killed_submission(session, leftover_status)
    FakeRecoverModule.run_count = 0

    # This is exactly what the redelivered message does on a fresh worker.
    run_submission(sub_id)

    # Re-read from a clean session so we see committed state, not identity-map.
    session.expire_all()
    sub = session.get(Submission, sub_id)
    runs = session.query(SubmissionRun).filter_by(submission_id=sub_id).all()

    # --- ran exactly once during recovery ---------------------------------- #
    assert FakeRecoverModule.run_count == 1

    # --- submission state is correct & coherent ---------------------------- #
    assert sub.status == SubmissionStatus.DONE
    assert sub.error is None
    assert sub.score_total is not None
    assert sub.passed is not None
    assert sub.started_at is not None and sub.finished_at is not None
    assert sub.finished_at >= sub.started_at
    # --- run rows: no duplicates, fresh result, stale data discarded ------- #
    assert len(runs) == 1, "recovery must reuse the run row, not create a duplicate"
    r = runs[0]
    assert r.id == run_id
    assert r.status == ModuleRunStatus.DONE
    assert r.metrics_json == {"quality": 0.85}
    assert "stale" not in (r.metrics_json or {})
    assert r.error is None
    assert r.started_at is not None and r.finished_at is not None


def test_done_submission_is_not_rerun(session):
    """A submission already DONE must be left untouched (no accidental rerun)."""
    from app.queue.jobs import run_submission

    sub_id, _ = _make_killed_submission(session, SubmissionStatus.RUNNING)
    # First recovery completes it.
    run_submission(sub_id)
    session.expire_all()
    assert session.get(Submission, sub_id).status == SubmissionStatus.DONE

    # A spurious second delivery must be a no-op.
    FakeRecoverModule.run_count = 0
    run_submission(sub_id)
    assert FakeRecoverModule.run_count == 0
    session.expire_all()
    assert session.get(Submission, sub_id).status == SubmissionStatus.DONE


def test_resurrection_is_bounded_by_run_attempts(session):
    """A killed worker's message is redelivered forever otherwise.

    Dramatiq's own `retries` never bounds this: a broker redelivery after a hard
    worker death arrives as a FRESH delivery with retries=0, so
    SUBMISSION_MAX_RETRIES is never consumed. Meanwhile the reaper has already
    marked the submission FAILED. The two recovery paths don't know about each
    other, so the submission resurrects and re-runs every module from scratch —
    observed in the wild twice, ~9h per cycle. submissions.run_attempts is the
    row-local counter that stops it.
    """
    from app.queue.jobs import SUBMISSION_MAX_ATTEMPTS, run_submission

    sub_id, _ = _make_killed_submission(session, SubmissionStatus.FAILED)

    # Pretend the allowance is already spent, as it would be after repeated kills.
    sub = session.get(Submission, sub_id)
    sub.run_attempts = SUBMISSION_MAX_ATTEMPTS
    sub.status = SubmissionStatus.FAILED
    session.commit()

    FakeRecoverModule.run_count = 0
    run_submission(sub_id)

    session.expire_all()
    sub = session.get(Submission, sub_id)
    assert FakeRecoverModule.run_count == 0, "the exhausted submission must not run again"
    assert sub.status == SubmissionStatus.FAILED
    assert "attempts" in (sub.error or "").lower(), (
        "the give-up reason must say why, not look like a generic crash"
    )


def test_attempts_are_counted_before_the_run_so_a_hard_death_still_burns_one(session):
    """The counter must increment up-front — a worker that dies mid-run never
    gets to record anything afterwards, which is precisely the case it bounds."""
    from app.queue.jobs import run_submission

    sub_id, _ = _make_killed_submission(session, SubmissionStatus.RUNNING)
    FakeRecoverModule.run_count = 0

    run_submission(sub_id)

    session.expire_all()
    assert session.get(Submission, sub_id).run_attempts == 1


def test_terminal_submission_does_not_consume_an_attempt(session):
    """A spurious redelivery of a DONE run must be a no-op, not an attempt burn —
    otherwise repeated redeliveries could exhaust a healthy submission."""
    from app.queue.jobs import run_submission

    sub_id, _ = _make_killed_submission(session, SubmissionStatus.RUNNING)
    run_submission(sub_id)
    session.expire_all()
    assert session.get(Submission, sub_id).status == SubmissionStatus.DONE
    attempts_after_success = session.get(Submission, sub_id).run_attempts

    run_submission(sub_id)  # spurious redelivery
    session.expire_all()
    assert session.get(Submission, sub_id).run_attempts == attempts_after_success
