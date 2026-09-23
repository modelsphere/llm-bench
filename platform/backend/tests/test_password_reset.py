"""Integration tests for the admin-approved forgot-password flow.

Needs a real test Postgres (see tests/conftest.py for the docker one-liner);
skips otherwise. Drives the app through its HTTP surface with FastAPI's
TestClient and seeds/inspects rows via the sync session — same style as the
other DB-integration tests here.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

# conftest.py has already pointed settings at the throwaway test DB before this
# import runs, so building engines here is safe.
from app.core.auth import create_access_token, get_password_hash
from app.core.config import settings
from app.db.models import (
    Base,
    PasswordResetRequest,
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


def _redis_available() -> bool:
    try:
        import redis

        redis.from_url(settings.REDIS_URL).ping()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _services_available(),
    reason="needs a test Postgres (see tests/conftest.py for the docker one-liner)",
)

# Rate-limit tests additionally need Redis; without it the limiter fails open
# (no 429), so those assertions can't hold.
redis_required = pytest.mark.skipif(
    not _redis_available(), reason="rate limiting needs a test Redis"
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


# --- helpers ---------------------------------------------------------------

_PASSWORD = "OriginalPass1"


def _mk_user(role: UserRole) -> User:
    """Create a user with a unique email/username and return the fresh row."""
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


def _auth_header(user: User, *, minted_ago: int = 0) -> dict[str, str]:
    """Bearer header for `user`. `minted_ago` backdates the token's iat by that
    many seconds — needed when a test asserts session invalidation, because the
    resolve check deliberately grants same-second tokens a grace window (see
    resolve_user_from_token), and the whole test flow runs within one second."""
    data = {"sub": str(user.id), "role": user.role.value}
    if minted_ago:
        data["iat"] = datetime.now(UTC) - timedelta(seconds=minted_ago)
    token = create_access_token(data)
    return {"Authorization": f"Bearer {token}"}


def _open_request_for(user_id: int) -> PasswordResetRequest | None:
    s = get_sync_session()
    try:
        return (
            s.query(PasswordResetRequest)
            .filter(
                PasswordResetRequest.user_id == user_id,
                PasswordResetRequest.used_at.is_(None),
                PasswordResetRequest.canceled_at.is_(None),
            )
            .order_by(PasswordResetRequest.created_at.desc())
            .first()
        )
    finally:
        s.close()


# --- forgot-password (public) ---------------------------------------------

def test_forgot_password_is_generic_and_only_files_for_plain_users(client):
    user = _mk_user(UserRole.USER)
    admin = _mk_user(UserRole.ADMIN)

    unknown_resp = client.post(
        "/auth/forgot-password", json={"email": "nobody-xyz@example.com"}
    )
    user_resp = client.post("/auth/forgot-password", json={"email": user.email})
    admin_resp = client.post("/auth/forgot-password", json={"email": admin.email})

    # All three return the identical generic 202 — no enumeration of existence
    # or eligibility.
    assert unknown_resp.status_code == 202
    assert user_resp.status_code == 202
    assert admin_resp.status_code == 202
    assert unknown_resp.json() == user_resp.json() == admin_resp.json()

    # A row is created only for the eligible plain user.
    assert _open_request_for(user.id) is not None
    assert _open_request_for(admin.id) is None


def test_forgot_password_dedupes(client):
    user = _mk_user(UserRole.USER)
    client.post("/auth/forgot-password", json={"email": user.email})
    client.post("/auth/forgot-password", json={"email": user.email})

    s = get_sync_session()
    try:
        count = (
            s.query(PasswordResetRequest)
            .filter(PasswordResetRequest.user_id == user.id)
            .count()
        )
    finally:
        s.close()
    assert count == 1


# --- approve (super_admin only) -------------------------------------------

def test_approve_requires_super_admin(client):
    user = _mk_user(UserRole.USER)
    admin = _mk_user(UserRole.ADMIN)
    client.post("/auth/forgot-password", json={"email": user.email})
    req = _open_request_for(user.id)

    # A plain admin cannot approve.
    r = client.post(
        f"/auth/admin/password-resets/{req.id}/approve", headers=_auth_header(admin)
    )
    assert r.status_code == 403


def test_approve_refuses_privileged_targets(client):
    super_admin = _mk_user(UserRole.SUPER_ADMIN)
    admin = _mk_user(UserRole.ADMIN)

    # Manufacture a request row pointing at an admin (the public endpoint would
    # never make one, but the approve guard must still refuse it).
    s = get_sync_session()
    try:
        req = PasswordResetRequest(user_id=admin.id)
        s.add(req)
        s.commit()
        s.refresh(req)
        req_id = req.id
    finally:
        s.close()

    r = client.post(
        f"/auth/admin/password-resets/{req_id}/approve",
        headers=_auth_header(super_admin),
    )
    assert r.status_code == 403


# --- reset (public) --------------------------------------------------------

def test_reset_happy_path_and_session_invalidation(client):
    user = _mk_user(UserRole.USER)
    super_admin = _mk_user(UserRole.SUPER_ADMIN)

    # A JWT the user already holds, minted before the reset.
    old_header = _auth_header(user, minted_ago=5)
    assert client.get("/auth/me", headers=old_header).status_code == 200

    client.post("/auth/forgot-password", json={"email": user.email})
    req = _open_request_for(user.id)
    approve = client.post(
        f"/auth/admin/password-resets/{req.id}/approve",
        headers=_auth_header(super_admin),
    )
    assert approve.status_code == 200
    token = approve.json()["token"]
    assert approve.json()["path"] == f"/reset-password?token={token}"

    new_password = "BrandNewPass2"
    reset = client.post(
        "/auth/reset-password", json={"token": token, "new_password": new_password}
    )
    assert reset.status_code == 204

    # New password works, old one doesn't.
    login = client.post(
        "/auth/login", json={"email": user.email, "password": new_password}
    )
    assert login.status_code == 200
    assert client.post(
        "/auth/login", json={"email": user.email, "password": _PASSWORD}
    ).status_code == 401

    # The pre-reset JWT is now rejected (session invalidation).
    assert client.get("/auth/me", headers=old_header).status_code == 401

    # A token minted AFTER the reset must still work — guards against the iat/
    # password_changed_at second-granularity off-by-one (a fresh login in the
    # same wall-clock second as the reset must not be spuriously rejected).
    fresh = {"Authorization": f"Bearer {login.json()['access_token']}"}
    assert client.get("/auth/me", headers=fresh).status_code == 200

    # A token can't be reused.
    assert client.post(
        "/auth/reset-password", json={"token": token, "new_password": "Another3ok"}
    ).status_code == 400


def test_reset_rejects_unknown_token(client):
    r = client.post(
        "/auth/reset-password",
        json={"token": "llmr_not-a-real-token", "new_password": "Whatever1"},
    )
    assert r.status_code == 400


def test_reset_rejects_expired_and_canceled(client):
    user = _mk_user(UserRole.USER)
    super_admin = _mk_user(UserRole.SUPER_ADMIN)
    client.post("/auth/forgot-password", json={"email": user.email})
    req = _open_request_for(user.id)
    token = client.post(
        f"/auth/admin/password-resets/{req.id}/approve",
        headers=_auth_header(super_admin),
    ).json()["token"]

    # Force expiry in the DB.
    s = get_sync_session()
    try:
        row = s.get(PasswordResetRequest, req.id)
        row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        s.commit()
    finally:
        s.close()
    assert client.post(
        "/auth/reset-password", json={"token": token, "new_password": "Whatever1"}
    ).status_code == 400


def test_reject_blocks_later_use(client):
    user = _mk_user(UserRole.USER)
    super_admin = _mk_user(UserRole.SUPER_ADMIN)
    client.post("/auth/forgot-password", json={"email": user.email})
    req = _open_request_for(user.id)
    token = client.post(
        f"/auth/admin/password-resets/{req.id}/approve",
        headers=_auth_header(super_admin),
    ).json()["token"]

    reject = client.post(
        f"/auth/admin/password-resets/{req.id}/reject", headers=_auth_header(super_admin)
    )
    assert reject.status_code == 204

    # A canceled token no longer resets.
    assert client.post(
        "/auth/reset-password", json={"token": token, "new_password": "Whatever1"}
    ).status_code == 400


# --- change-password: invalidates other sessions, reissues current ---------

def test_change_password_invalidates_and_reissues(client):
    user = _mk_user(UserRole.USER)
    old_header = _auth_header(user, minted_ago=5)
    assert client.get("/auth/me", headers=old_header).status_code == 200

    new_password = "ChangedPass9"
    resp = client.post(
        "/auth/change-password",
        headers=old_header,
        json={"current_password": _PASSWORD, "new_password": new_password},
    )
    assert resp.status_code == 200
    reissued = resp.json()["access_token"]

    # The token used to make the change is now invalid (older sessions killed)...
    assert client.get("/auth/me", headers=old_header).status_code == 401
    # ...but the reissued token keeps the current session alive.
    assert client.get(
        "/auth/me", headers={"Authorization": f"Bearer {reissued}"}
    ).status_code == 200
    # New password logs in, old one doesn't.
    assert client.post(
        "/auth/login", json={"email": user.email, "password": new_password}
    ).status_code == 200
    assert client.post(
        "/auth/login", json={"email": user.email, "password": _PASSWORD}
    ).status_code == 401


# --- rate limiting (needs Redis) -------------------------------------------

@redis_required
def test_login_locks_account_after_repeated_failures(client):
    user = _mk_user(UserRole.USER)
    # Unique forwarded IP so this test's per-IP counter can't collide with others.
    ip = {"X-Forwarded-For": f"10.0.0.{uuid.uuid4().int % 250 + 1}"}
    bad = {"email": user.email, "password": "wrong-password"}

    # Up to the failure cap, wrong passwords return 401.
    for _ in range(settings.LOGIN_EMAIL_MAX_FAILURES):
        assert client.post("/auth/login", json=bad, headers=ip).status_code == 401

    # Beyond it the account is locked — even the CORRECT password is refused with
    # 429 until the window elapses.
    locked = client.post(
        "/auth/login", json={"email": user.email, "password": _PASSWORD}, headers=ip
    )
    assert locked.status_code == 429
    assert "Retry-After" in locked.headers


@redis_required
def test_register_is_ip_rate_limited(client):
    ip = {"X-Forwarded-For": f"10.1.0.{uuid.uuid4().int % 250 + 1}"}

    def _body():
        s = uuid.uuid4().hex[:12]
        return {"email": f"{s}@example.com", "username": f"u_{s}", "password": "GoodPass123"}

    # Exhaust the per-IP allowance.
    for _ in range(settings.REGISTER_IP_MAX_ATTEMPTS):
        assert client.post("/auth/register", json=_body(), headers=ip).status_code == 201
    # The next signup from the same IP is throttled.
    blocked = client.post("/auth/register", json=_body(), headers=ip)
    assert blocked.status_code == 429
