"""Integration tests for the bulk group-tag endpoint (admin Groups tab).

Grouping has no group entity: a group exists exactly as long as some benchmark
carries its path. Renaming or deleting one is therefore a rewrite across every
benchmark that carried it, which is why the tab saves through a single
transactional call instead of N per-benchmark updates. What is asserted here is
what that endpoint has to guarantee for the tab to be safe to use:

1. It replaces whole tag lists across several benchmarks in one request, with
   the same normalisation a single update applies.
2. It is all-or-nothing: a stale id (benchmark deleted while the tab was open)
   rejects the batch instead of half-renaming a group.
3. Grouping stays presentation — a locked benchmark can still be re-tagged and
   its config_hash does not move.
4. Its path is not swallowed by the "/{benchmark_id}" route declared next to it.

Needs a real test Postgres; skips otherwise. Same style as
test_leaderboard_privacy.py.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.core.auth import create_access_token, get_password_hash
from app.core.config import settings
from app.db.models import (
    Base,
    Benchmark,
    BenchmarkStatus,
    User,
    UserRole,
    get_sync_session,
)
from app.db.models import _async_engine, _sync_engine  # type: ignore
from app.main import app

BULK_URL = "/benchmarks/admin/benchmarks/group-tags"


def _services_available() -> bool:
    try:
        import psycopg2

        psycopg2.connect(settings.DATABASE_URL_SYNC).close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _services_available(),
    reason="needs a test Postgres (see tests/conftest.py for the docker one-liner)",
)


@pytest.fixture(scope="module", autouse=True)
def _schema():
    Base.metadata.create_all(_sync_engine)
    try:
        yield
    finally:
        import asyncio

        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            _async_engine.dispose()
        )


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


# --- helpers ----------------------------------------------------------------


def _mk_user(role: UserRole = UserRole.ADMIN) -> User:
    suffix = uuid.uuid4().hex[:12]
    s = get_sync_session()
    try:
        u = User(
            email=f"gt-{role.value}-{suffix}@example.com",
            username=f"gt_{role.value}_{suffix}",
            password_hash=get_password_hash("GroupTagPass1"),
            role=role,
        )
        s.add(u)
        s.commit()
        s.refresh(u)
        s.expunge(u)
        return u
    finally:
        s.close()


def _auth_header(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token({'sub': str(user.id), 'role': user.role.value})}"}


def _mk_benchmark(creator: User, tags: list[str], *, locked: bool = False) -> int:
    s = get_sync_session()
    try:
        b = Benchmark(
            slug=f"gt-{uuid.uuid4().hex[:8]}",
            name="Group tag test",
            status=BenchmarkStatus.ACTIVE,
            config_hash="abcd1234",
            is_locked=locked,
            group_tags=tags,
            created_by_user_id=creator.id,
        )
        s.add(b)
        s.commit()
        return b.id
    finally:
        s.close()


def _stored(benchmark_id: int) -> Benchmark:
    s = get_sync_session()
    try:
        b = s.get(Benchmark, benchmark_id)
        s.expunge(b)
        return b
    finally:
        s.close()


# --- tests ------------------------------------------------------------------


def test_replaces_tag_lists_across_benchmarks_in_one_call(client):
    admin = _mk_user()
    # A rename in the tab looks like this: every benchmark under the old path
    # is sent back with the new one, and a benchmark dropped from the group is
    # sent with the remaining tags only.
    moved = _mk_benchmark(admin, ["Old/Serving", "Hardware"])
    dropped = _mk_benchmark(admin, ["Old/Serving"])

    resp = client.put(
        BULK_URL,
        headers=_auth_header(admin),
        json={
            "assignments": [
                # Whitespace is normalised exactly as a single update does.
                {"benchmark_id": moved, "group_tags": [" New / Serving ", "Hardware"]},
                {"benchmark_id": dropped, "group_tags": []},
            ]
        },
    )

    # A 422 here would mean "group-tags" was parsed as a benchmark id by the
    # "/{benchmark_id}" route declared beside this one.
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"updated": 2}
    assert _stored(moved).group_tags == ["New/Serving", "Hardware"]
    assert _stored(dropped).group_tags == []


def test_unknown_id_rejects_the_whole_batch(client):
    admin = _mk_user()
    kept = _mk_benchmark(admin, ["Keep"])

    resp = client.put(
        BULK_URL,
        headers=_auth_header(admin),
        json={
            "assignments": [
                {"benchmark_id": kept, "group_tags": ["Renamed"]},
                {"benchmark_id": 10**9, "group_tags": ["Renamed"]},
            ]
        },
    )

    assert resp.status_code == 404
    # Half a rename would split one group in two, so nothing may be applied.
    assert _stored(kept).group_tags == ["Keep"]


def test_locked_benchmark_is_retaggable_and_keeps_its_config_hash(client):
    admin = _mk_user()
    locked = _mk_benchmark(admin, ["Before"], locked=True)
    before = _stored(locked).config_hash

    resp = client.put(
        BULK_URL,
        headers=_auth_header(admin),
        json={"assignments": [{"benchmark_id": locked, "group_tags": ["After"]}]},
    )

    assert resp.status_code == 200, resp.text
    stored = _stored(locked)
    assert stored.group_tags == ["After"]
    # Grouping is presentation: it must not re-group existing leaderboards.
    assert stored.config_hash == before


def test_malformed_path_rejects_the_batch(client):
    admin = _mk_user()
    kept = _mk_benchmark(admin, ["Keep"])

    resp = client.put(
        BULK_URL,
        headers=_auth_header(admin),
        json={
            "assignments": [
                {"benchmark_id": kept, "group_tags": ["Fine"]},
                {"benchmark_id": kept + 1, "group_tags": ["/Mid"]},  # id is irrelevant: validation runs first
            ]
        },
    )

    assert resp.status_code == 422
    assert _stored(kept).group_tags == ["Keep"]


def test_non_admin_cannot_retag(client):
    admin = _mk_user()
    user = _mk_user(UserRole.USER)
    benchmark = _mk_benchmark(admin, ["Keep"])

    resp = client.put(
        BULK_URL,
        headers=_auth_header(user),
        json={"assignments": [{"benchmark_id": benchmark, "group_tags": ["Mine"]}]},
    )

    assert resp.status_code == 403
    assert _stored(benchmark).group_tags == ["Keep"]
