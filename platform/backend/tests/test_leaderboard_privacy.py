"""Integration tests for leaderboard + submission visibility semantics.

Policy (since 2026-07): every signed-in user can view leaderboard rows AND
any benchmark submission's detail page, read-only. Mutations stay gated:
cancel is owner/admin-only, and ad-hoc module submissions (never on a
leaderboard) remain private to their owner.

- Draft benchmarks 404 for non-admins (existence-hiding); archived stay
  visible.
- Leaderboard rows always carry submission_id (detail link) for every viewer.
- GET /submissions/{id}: 200 for any signed-in user on benchmark submissions,
  403 for non-owners on ad-hoc module submissions.
- POST /submissions/{id}/cancel: 403 for non-owners.

Needs a real test Postgres; skips otherwise. Same style as
test_password_reset.py.
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
    Submission,
    SubmissionStatus,
    User,
    UserRole,
    get_sync_session,
)
from app.db.models import _async_engine, _sync_engine  # type: ignore
from app.main import app


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

_PASSWORD = "LbPrivPass1"


def _mk_user(role: UserRole = UserRole.USER) -> User:
    suffix = uuid.uuid4().hex[:12]
    s = get_sync_session()
    try:
        u = User(
            email=f"{role.value}-{suffix}@example.com",
            username=f"{role.value}_{suffix}",
            password_hash=get_password_hash(_PASSWORD),
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
    token = create_access_token({"sub": str(user.id), "role": user.role.value})
    return {"Authorization": f"Bearer {token}"}


def _mk_benchmark(
    creator: User,
    status: BenchmarkStatus = BenchmarkStatus.ACTIVE,
) -> str:
    slug = f"lb-priv-{uuid.uuid4().hex[:8]}"
    s = get_sync_session()
    try:
        s.add(
            Benchmark(
                slug=slug,
                name=f"Privacy test {slug}",
                status=status,
                config_hash="abcd1234",
                created_by_user_id=creator.id,
            )
        )
        s.commit()
        return slug
    finally:
        s.close()


def _bench_id(slug: str) -> int:
    s = get_sync_session()
    try:
        return s.query(Benchmark).filter(Benchmark.slug == slug).one().id
    finally:
        s.close()


def _mk_done_submission(user: User, slug: str, score: float) -> int:
    s = get_sync_session()
    try:
        sub = Submission(
            user_id=user.id,
            benchmark_id=_bench_id(slug),
            module_name=None,
            endpoint_url="http://localhost:9",
            endpoint_model=f"model-of-{user.username}",
            endpoint_api_key_enc="enc",
            description_summary=f"summary-of-{user.username}",
            status=SubmissionStatus.DONE,
            score_total=score,
            passed=True,
        )
        s.add(sub)
        s.commit()
        s.refresh(sub)
        return sub.id
    finally:
        s.close()


def _mk_module_submission(user: User) -> int:
    """Ad-hoc single-module submission — never on a leaderboard, stays private."""
    s = get_sync_session()
    try:
        sub = Submission(
            user_id=user.id,
            benchmark_id=None,
            module_name="perf_guidellm",
            endpoint_url="http://localhost:9",
            endpoint_model=f"model-of-{user.username}",
            endpoint_api_key_enc="enc",
            status=SubmissionStatus.DONE,
        )
        s.add(sub)
        s.commit()
        s.refresh(sub)
        return sub.id
    finally:
        s.close()


def _get(client: TestClient, slug: str, viewer: User):
    return client.get(f"/leaderboard/{slug}", headers=_auth_header(viewer))


# --- status visibility ------------------------------------------------------

def test_draft_hidden_from_users_visible_to_admins(client):
    admin = _mk_user(UserRole.ADMIN)
    user = _mk_user()
    slug = _mk_benchmark(admin, status=BenchmarkStatus.DRAFT)

    assert _get(client, slug, user).status_code == 404
    assert _get(client, slug, admin).status_code == 200


def test_archived_stays_visible(client):
    admin = _mk_user(UserRole.ADMIN)
    user = _mk_user()
    slug = _mk_benchmark(admin, status=BenchmarkStatus.ARCHIVED)

    assert _get(client, slug, user).status_code == 200


# --- read-only visibility for all users --------------------------------------

def test_leaderboard_rows_always_link_to_details(client):
    admin = _mk_user(UserRole.ADMIN)
    alice, bob = _mk_user(), _mk_user()
    slug = _mk_benchmark(admin)
    alice_id = _mk_done_submission(alice, slug, 0.9)
    bob_id = _mk_done_submission(bob, slug, 0.8)

    body = _get(client, slug, bob).json()
    rows = {row["rank"]: row for row in body["rows"]}
    assert len(rows) == 2

    # Another user's row is fully visible INCLUDING its detail link.
    other = rows[1]  # alice ranks first (0.9) and is not the viewer
    assert other["submission_id"] == alice_id
    assert other["username"] == alice.username
    assert other["endpoint_model"] == f"model-of-{alice.username}"
    assert other["description_summary"] == f"summary-of-{alice.username}"
    assert other["score_total"] == 0.9

    assert rows[2]["submission_id"] == bob_id


def test_any_user_can_read_benchmark_submission_detail(client):
    admin = _mk_user(UserRole.ADMIN)
    alice, bob = _mk_user(), _mk_user()
    slug = _mk_benchmark(admin)
    alice_id = _mk_done_submission(alice, slug, 0.9)

    resp = client.get(f"/submissions/{alice_id}", headers=_auth_header(bob))
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == alice_id
    assert body["benchmark_slug"] == slug


def test_module_submissions_stay_private(client):
    alice, bob = _mk_user(), _mk_user()
    admin = _mk_user(UserRole.ADMIN)
    sub_id = _mk_module_submission(alice)

    assert client.get(f"/submissions/{sub_id}", headers=_auth_header(bob)).status_code == 403
    assert client.get(f"/submissions/{sub_id}", headers=_auth_header(alice)).status_code == 200
    assert client.get(f"/submissions/{sub_id}", headers=_auth_header(admin)).status_code == 200


def test_viewers_cannot_cancel_others_submissions(client):
    admin = _mk_user(UserRole.ADMIN)
    alice, bob = _mk_user(), _mk_user()
    slug = _mk_benchmark(admin)
    alice_id = _mk_done_submission(alice, slug, 0.9)

    # Read access does not grant mutation: non-owner cancel is refused outright.
    resp = client.post(f"/submissions/{alice_id}/cancel", headers=_auth_header(bob))
    assert resp.status_code == 403
