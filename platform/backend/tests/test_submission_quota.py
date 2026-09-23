"""Integration tests for the per-user active-submission quota.

Needs a real test Postgres AND Redis (a successful submit enqueues to
Dramatiq); skips otherwise. Same TestClient + sync-session seeding style as
test_password_reset.py. The limit is monkeypatched down to 2 so tests don't
have to seed 8 rows.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.core.auth import create_access_token, get_password_hash
from app.core.config import settings
from app.db.models import (
    Base,
    Submission,
    SubmissionStatus,
    TestModule,
    User,
    UserRole,
    get_sync_session,
)
from app.db.models import _async_engine, _sync_engine  # type: ignore
from app.main import app


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
        import asyncio

        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            _async_engine.dispose()
        )


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def small_limit(monkeypatch):
    """Drop the quota to 2 so tests seed 2 rows, not 8."""
    monkeypatch.setattr(settings, "MAX_ACTIVE_SUBMISSIONS_PER_USER", 2)
    return 2


# --- helpers ----------------------------------------------------------------

_PASSWORD = "QuotaPass1"


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


def _mk_module() -> str:
    """Insert a minimal TestModule row and return its (unique) name."""
    name = f"quota_mod_{uuid.uuid4().hex[:8]}"
    s = get_sync_session()
    try:
        s.add(
            TestModule(
                name=name,
                display_name="Quota test module",
                description="synthetic module for quota tests",
                params_schema_json={},
                default_params_json={},
            )
        )
        s.commit()
        return name
    finally:
        s.close()


def _seed_submission(user: User, module_name: str, status: SubmissionStatus) -> int:
    s = get_sync_session()
    try:
        sub = Submission(
            user_id=user.id,
            benchmark_id=None,
            module_name=module_name,
            endpoint_url="http://localhost:9",
            endpoint_model="m",
            endpoint_api_key_enc="enc",
            status=status,
        )
        s.add(sub)
        s.commit()
        s.refresh(sub)
        return sub.id
    finally:
        s.close()


def _submit(client: TestClient, user: User, module_name: str):
    return client.post(
        f"/submissions/modules/{module_name}/submit",
        headers=_auth_header(user),
        json={
            "endpoint_url": "http://localhost:9",
            "model": "m",
            "api_key": "k",
        },
    )


# --- enforcement ------------------------------------------------------------

def test_below_limit_is_accepted(client, small_limit):
    user = _mk_user()
    mod = _mk_module()
    _seed_submission(user, mod, SubmissionStatus.QUEUED)

    assert _submit(client, user, mod).status_code == 202


def test_at_limit_is_blocked_with_actionable_detail(client, small_limit):
    user = _mk_user()
    mod = _mk_module()
    id1 = _seed_submission(user, mod, SubmissionStatus.QUEUED)
    id2 = _seed_submission(user, mod, SubmissionStatus.RUNNING)

    resp = _submit(client, user, mod)
    assert resp.status_code == 429
    detail = resp.json()["detail"]
    assert f"(2/{small_limit})" in detail
    assert str(id1) in detail and str(id2) in detail


def test_terminal_statuses_do_not_count(client, small_limit):
    user = _mk_user()
    mod = _mk_module()
    for st in (
        SubmissionStatus.DONE,
        SubmissionStatus.FAILED,
        SubmissionStatus.CANCELED,
    ):
        _seed_submission(user, mod, st)

    assert _submit(client, user, mod).status_code == 202


def test_admin_is_exempt(client, small_limit):
    admin = _mk_user(UserRole.ADMIN)
    mod = _mk_module()
    for _ in range(small_limit + 1):
        _seed_submission(admin, mod, SubmissionStatus.QUEUED)

    assert _submit(client, admin, mod).status_code == 202


# --- GET /submissions/quota -------------------------------------------------

def test_quota_endpoint_reports_usage(client, small_limit):
    user = _mk_user()
    mod = _mk_module()
    active_id = _seed_submission(user, mod, SubmissionStatus.RUNNING)
    _seed_submission(user, mod, SubmissionStatus.DONE)  # must not count

    body = client.get("/submissions/quota", headers=_auth_header(user)).json()
    assert body == {
        "limit": small_limit,
        "active": 1,
        "exempt": False,
        "active_ids": [active_id],
    }


def test_quota_endpoint_marks_admin_exempt(client, small_limit):
    admin = _mk_user(UserRole.ADMIN)
    body = client.get("/submissions/quota", headers=_auth_header(admin)).json()
    assert body["exempt"] is True
