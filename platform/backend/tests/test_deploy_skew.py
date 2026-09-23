"""Integration tests for the two Redis-contract invariants a rolling deploy leans on.

Both bugs these guard only ever fire during a deploy, which is exactly why they
survived so long: the unit under test looks correct in isolation and the failure
needs a second actor (a Redis restart, or a second backend replica) to appear.

  1. The per-submission heartbeat must survive a Redis restart. Redis here is a
     single replica with no volume, so a reschedule wipes every key. A refresh
     loop that only EXPIREs can never bring the key back, and the reaper then
     FAILs every in-flight submission while its worker happily runs to
     completion — the worst outcome available, because the run still burns the
     full drain budget and the user just sees FAILED.

  2. The reaper's orphaned-QUEUED recovery must dispatch a submission exactly
     once even though the loop runs in every backend replica.

Skips automatically if no test Postgres/Redis is reachable — see conftest.py.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from dramatiq import Message

# app.* imports are safe here: conftest.py has already pointed settings at the
# test DB/Redis before collection imported this module.
from app.core.config import settings
from app.core.security import encrypt_api_key
from app.db.models import (
    Base,
    Submission,
    SubmissionStatus,
    User,
    _async_engine,
    _sync_engine,
    get_sync_session,
)
# Aliased so the name doesn't start with "Test" — otherwise pytest tries to
# collect it as a test case and warns.
from app.db.models import TestModule as ModuleRow
from app.queue import heartbeat as heartbeat_mod
from app.queue.heartbeat import HeartbeatMiddleware, heartbeat_key


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
def _schema():
    Base.metadata.create_all(_sync_engine)
    try:
        yield
    finally:
        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            _async_engine.dispose()
        )


@pytest.fixture()
def redis_sync():
    import redis

    client = redis.from_url(settings.REDIS_URL, decode_responses=False)
    try:
        yield client
    finally:
        client.close()


@pytest_asyncio.fixture()
async def async_pool():
    """asyncpg pools are bound to the loop that created them, and pytest-asyncio
    gives each test a fresh loop — so a connection pooled by an earlier test
    raises "attached to a different loop" here. Dispose on both sides to keep
    every pool inside a single test's loop.
    """
    await _async_engine.dispose()
    try:
        yield
    finally:
        await _async_engine.dispose()


# --------------------------------------------------------------------------- #
# 1. Heartbeat survives a Redis wipe
# --------------------------------------------------------------------------- #
def test_heartbeat_refresh_recreates_key_after_redis_wipe(monkeypatch, redis_sync):
    """The refresh loop must re-SET the key, not just EXPIRE it.

    EXPIRE on a missing key is a silent no-op, so an EXPIRE-only refresh can
    never recover from the Redis restart this test simulates.
    """
    monkeypatch.setattr(heartbeat_mod, "HEARTBEAT_INTERVAL_SECONDS", 0.1)

    submission_id = 900_000 + int(uuid.uuid4().int % 10_000)
    key = heartbeat_key(submission_id)
    redis_sync.delete(key)

    middleware = HeartbeatMiddleware()
    message = Message(
        queue_name="default",
        actor_name="run_submission",
        args=(submission_id,),
        kwargs={},
        options={},
    )

    middleware.before_process_message(None, message)
    try:
        assert redis_sync.exists(key), "heartbeat key not written on message pickup"

        # The Redis pod reschedules: unpersisted, so every key vanishes.
        redis_sync.delete(key)
        assert not redis_sync.exists(key)

        deadline = time.time() + 5.0
        while time.time() < deadline and not redis_sync.exists(key):
            time.sleep(0.05)

        assert redis_sync.exists(key), (
            "heartbeat never came back after a Redis wipe — the refresh loop is "
            "EXPIRE-only again, and the reaper will FAIL every draining submission"
        )
        # The value must be restored too, not just the TTL: it is what tells an
        # operator which pod/pid owns a submission.
        payload = json.loads(redis_sync.get(key))
        assert payload["actor"] == "run_submission"
        assert payload["pid"] > 0

        ttl = redis_sync.ttl(key)
        assert 0 < ttl <= heartbeat_mod.HEARTBEAT_TTL_SECONDS
    finally:
        middleware.after_process_message(None, message)

    assert not redis_sync.exists(key), "heartbeat key outlived the message"


def test_heartbeat_ignores_non_target_actors(redis_sync):
    """Only run_submission is heartbeated; other actors must not leak keys."""
    middleware = HeartbeatMiddleware()
    message = Message(
        queue_name="default",
        actor_name="some_other_actor",
        args=(12345,),
        kwargs={},
        options={},
    )
    middleware.before_process_message(None, message)
    assert not redis_sync.exists(heartbeat_key(12345))
    middleware.after_process_message(None, message)


# --------------------------------------------------------------------------- #
# 2. Reaper dispatches an orphaned QUEUED submission exactly once
# --------------------------------------------------------------------------- #
MODULE_NAME = "test_fake_skew"


def _ensure_module_row(session) -> None:
    if session.get(ModuleRow, MODULE_NAME) is None:
        session.add(
            ModuleRow(
                name=MODULE_NAME,
                display_name="Fake Skew (test only)",
                description="x",
                params_schema_json={},
                default_params_json={},
                metrics_schema_json={},
            )
        )
        session.commit()


def _make_orphaned_queued(session) -> int:
    """A submission that Redis lost the message for: QUEUED, old enough to be
    past the reaper's grace window, and with no queue marker."""
    from app.core.reaper import GRACE_SECONDS

    _ensure_module_row(session)

    uniq = uuid.uuid4().hex[:12]
    user = User(
        email=f"skew-{uniq}@test.local",
        username=f"skew-{uniq}",
        password_hash="x",
        role="user",
    )
    session.add(user)
    session.flush()

    # ck_submission_one_of requires exactly one of benchmark_id / module_name.
    # Recovery only ever reads the id, but the row still has to be legal.
    sub = Submission(
        user_id=user.id,
        module_name=MODULE_NAME,
        endpoint_url="http://unused.local/v1",
        endpoint_model="fake-model",
        endpoint_api_key_enc=encrypt_api_key("unused"),
        status=SubmissionStatus.QUEUED,
        created_at=datetime.utcnow() - timedelta(seconds=GRACE_SECONDS * 3),
    )
    session.add(sub)
    session.flush()
    sub_id = sub.id
    session.commit()
    return sub_id


class _BarrieredRedis:
    """Forces both reaper cycles to finish their EXISTS on `target_key` before
    either acts on the result.

    Plain asyncio.gather does NOT reliably reproduce the two-replica race — the
    coroutines can happen to run their EXISTS→send→SET straight through without
    interleaving, so the test passes against the buggy code. The barrier pins
    the interleaving that a real pair of replicas hits by chance.
    """

    def __init__(self, inner, barrier: asyncio.Barrier, target_key: str) -> None:
        self._inner = inner
        self._barrier = barrier
        self._target_key = target_key

    async def exists(self, key):
        result = await self._inner.exists(key)
        if key == self._target_key:
            # Hold here until the other reaper has also looked and seen "absent".
            await asyncio.wait_for(self._barrier.wait(), timeout=5)
        return result

    def __getattr__(self, name):
        return getattr(self._inner, name)


@pytest.mark.asyncio
async def test_orphaned_queued_is_dispatched_once_by_concurrent_reapers(monkeypatch, async_pool):
    """Two backend replicas run the reaper loop. Only one may re-enqueue.

    Without the SET NX claim both observe the marker missing and both send,
    running the submission twice concurrently against the user's endpoint.
    """
    import redis.asyncio as redis_lib

    from app.core.reaper import _recover_orphaned_queued
    from app.queue import jobs as jobs_mod

    session = get_sync_session()
    try:
        sub_id = _make_orphaned_queued(session)
    finally:
        session.close()

    sent: list[int] = []
    monkeypatch.setattr(jobs_mod.run_submission, "send", lambda i: sent.append(i))

    marker = f"{jobs_mod.QUEUE_MARKER_PREFIX}{sub_id}"
    redis_client = redis_lib.from_url(settings.REDIS_URL)
    await redis_client.delete(marker)
    try:
        barrier = asyncio.Barrier(2)
        replica_a = _BarrieredRedis(redis_client, barrier, marker)
        replica_b = _BarrieredRedis(redis_client, barrier, marker)

        await asyncio.gather(
            _recover_orphaned_queued(replica_a),
            _recover_orphaned_queued(replica_b),
        )

        assert sent.count(sub_id) == 1, (
            f"submission {sub_id} dispatched {sent.count(sub_id)}x by concurrent "
            "reapers — the SET NX claim is gone and runs will double-execute "
            "against the user's endpoint"
        )
        assert await redis_client.exists(marker), (
            "winner did not leave the marker set — the next cycle would "
            "re-dispatch this submission again"
        )
    finally:
        await redis_client.delete(marker)
        await redis_client.aclose()
        _cleanup_submission(sub_id)


@pytest.mark.asyncio
async def test_claim_is_released_when_enqueue_fails(monkeypatch, async_pool):
    """A failed send must not leave the marker behind.

    The marker is what tells the next cycle "a message is live". If a send
    failure left it set, the submission would sit QUEUED forever and the reaper
    would never look at it again.
    """
    import redis.asyncio as redis_lib

    from app.core.reaper import _recover_orphaned_queued
    from app.queue import jobs as jobs_mod

    session = get_sync_session()
    try:
        sub_id = _make_orphaned_queued(session)
    finally:
        session.close()

    def _boom(_i):
        raise RuntimeError("broker unreachable")

    monkeypatch.setattr(jobs_mod.run_submission, "send", _boom)

    marker = f"{jobs_mod.QUEUE_MARKER_PREFIX}{sub_id}"
    redis_client = redis_lib.from_url(settings.REDIS_URL)
    await redis_client.delete(marker)
    try:
        await _recover_orphaned_queued(redis_client)
        assert not await redis_client.exists(marker), (
            "claim survived a failed enqueue — this submission is now stuck "
            "QUEUED forever, invisible to every later reaper cycle"
        )
    finally:
        await redis_client.delete(marker)
        await redis_client.aclose()
        _cleanup_submission(sub_id)


def _cleanup_submission(sub_id: int) -> None:
    session = get_sync_session()
    try:
        sub = session.get(Submission, sub_id)
        if sub is not None:
            user_id = sub.user_id
            session.delete(sub)
            session.commit()
            user = session.get(User, user_id)
            if user is not None:
                session.delete(user)
                session.commit()
    finally:
        session.close()
