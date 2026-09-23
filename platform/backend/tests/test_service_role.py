"""The `service` role: what another platform's account may and may not do.

LLM AutoTune (or any automation) talks to LLMBench as a service account whose
API key the deployment seeds. It needs to create and lock the benchmarks it runs,
read and trigger the rolling datasets it pins, and preflight an endpoint before
committing a submission. It must not become an admin by another name: it may
not touch a benchmark somebody else created, see other people's benchmarks in
the admin list, edit collection profiles, or manage users.

Every call here authenticates the way the other platform does — with its API
key, not a website session.

Needs a real test Postgres; skips otherwise.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.core.auth import create_access_token, generate_api_key, get_password_hash
from app.core.config import settings
from app.db.models import (
    ApiKey,
    Base,
    Benchmark,
    BenchmarkStatus,
    User,
    UserRole,
    get_sync_session,
    is_admin_or_above,
    is_service_or_above,
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


def _mk_user(role: UserRole) -> User:
    suffix = uuid.uuid4().hex[:12]
    s = get_sync_session()
    try:
        u = User(email=f"sr-{suffix}@example.com", username=f"sr_{role.value}_{suffix}",
                 password_hash=get_password_hash("ServiceRolePass1"), role=role)
        s.add(u)
        s.commit()
        s.refresh(u)
        s.expunge(u)
        return u
    finally:
        s.close()


def _key_header(user: User) -> dict[str, str]:
    """An API key for `user`, the credential an automated caller holds."""
    full, prefix, key_hash = generate_api_key()
    s = get_sync_session()
    try:
        s.add(ApiKey(user_id=user.id, name="test", key_prefix=prefix, key_hash=key_hash))
        s.commit()
    finally:
        s.close()
    return {"Authorization": f"Bearer {full}"}


def _jwt_header(user: User) -> dict[str, str]:
    token = create_access_token({"sub": str(user.id), "role": user.role.value})
    return {"Authorization": f"Bearer {token}"}


def _mk_benchmark(creator: User) -> int:
    s = get_sync_session()
    try:
        b = Benchmark(slug=f"sr-{uuid.uuid4().hex[:8]}", name="service role test",
                      status=BenchmarkStatus.ACTIVE, config_hash="abcd1234",
                      created_by_user_id=creator.id)
        s.add(b)
        s.commit()
        return b.id
    finally:
        s.close()


def _new_benchmark_body() -> dict:
    return {
        "slug": f"svc-{uuid.uuid4().hex[:8]}",
        "name": "made by a service account",
        "status": "active",
        "modules": [{"module_name": "case_truncation", "params_json": {},
                     "metric_configs": [], "weight": 0, "order_index": 0}],
    }


# --- the ranking ---------------------------------------------------------------


def test_service_sits_between_user_and_admin():
    assert is_service_or_above(UserRole.SERVICE)
    assert not is_admin_or_above(UserRole.SERVICE)
    assert not is_service_or_above(UserRole.USER)
    assert is_service_or_above(UserRole.ADMIN)


# --- benchmarks: its own, and only its own ------------------------------------------


def test_a_service_account_creates_and_locks_its_own_benchmark(client):
    svc = _mk_user(UserRole.SERVICE)
    headers = _key_header(svc)

    created = client.post("/benchmarks/admin/benchmarks", headers=headers, json=_new_benchmark_body())
    assert created.status_code == 201, created.text
    bid = created.json()["id"]
    assert created.json()["created_by_user_id"] == svc.id

    locked = client.put(f"/benchmarks/admin/benchmarks/{bid}/lock", headers=headers)
    assert locked.status_code == 200 and locked.json()["is_locked"] is True
    assert client.get(f"/benchmarks/admin/benchmarks/{bid}/export", headers=headers).status_code == 200


def test_a_service_account_cannot_touch_someone_elses_benchmark(client):
    admin, svc = _mk_user(UserRole.ADMIN), _mk_user(UserRole.SERVICE)
    theirs = _mk_benchmark(admin)
    headers = _key_header(svc)

    for method, path in (("put", f"/benchmarks/admin/benchmarks/{theirs}/lock"),
                         ("get", f"/benchmarks/admin/benchmarks/{theirs}/export"),
                         ("delete", f"/benchmarks/admin/benchmarks/{theirs}")):
        resp = getattr(client, method)(path, headers=headers)
        assert resp.status_code == 403, (method, path, resp.text)
        assert "only the benchmarks it created" in resp.json()["detail"]
    resp = client.put(f"/benchmarks/admin/benchmarks/{theirs}", headers=headers, json={"name": "mine now"})
    assert resp.status_code == 403


def test_the_admin_list_shows_a_service_account_only_its_own(client):
    admin, svc = _mk_user(UserRole.ADMIN), _mk_user(UserRole.SERVICE)
    theirs, mine = _mk_benchmark(admin), _mk_benchmark(svc)

    listed = client.get("/benchmarks/admin/benchmarks", headers=_key_header(svc)).json()["benchmarks"]
    ids = {b["id"] for b in listed}
    assert mine in ids and theirs not in ids
    # An admin still sees everything, the service account's included.
    all_ids = {b["id"] for b in client.get("/benchmarks/admin/benchmarks",
                                           headers=_jwt_header(admin)).json()["benchmarks"]}
    assert {mine, theirs} <= all_ids


def test_an_ordinary_user_key_still_cannot_manage_benchmarks(client):
    user = _mk_user(UserRole.USER)
    resp = client.post("/benchmarks/admin/benchmarks", headers=_key_header(user), json=_new_benchmark_body())
    assert resp.status_code == 403


def test_group_tags_remain_admin_only(client):
    svc = _mk_user(UserRole.SERVICE)
    resp = client.put("/benchmarks/admin/benchmarks/group-tags", headers=_key_header(svc),
                      json={"assignments": []})
    assert resp.status_code == 403


# --- rolling datasets: read and trigger, never configure --------------------------------


def test_a_service_account_reads_profiles_but_cannot_edit_them(client):
    svc = _mk_user(UserRole.SERVICE)
    headers = _key_header(svc)
    assert client.get("/replay-datasets/profiles", headers=headers).status_code == 200
    created = client.post("/replay-datasets/profiles", headers=headers, json={
        "name": f"p{uuid.uuid4().hex[:6]}", "display_name": "x",
        "source_type": "bodylog_files", "source_url": "file:///data/bodylog",
    })
    assert created.status_code == 403
    assert client.get("/replay-datasets/frozen", headers=headers).status_code == 403


# --- preflight: a service key is accepted, a person's key is not ------------------------


def test_preflight_accepts_a_service_key_and_refuses_a_personal_one(client):
    body = {"endpoint_url": "http://127.0.0.1:9", "model": "m", "api_key": ""}
    svc, user = _mk_user(UserRole.SERVICE), _mk_user(UserRole.USER)
    # Reachability of the endpoint is not the point — the auth gate is. A
    # refused connection still comes back as a 200 report with a failed check.
    assert client.post("/submissions/preflight", headers=_key_header(svc), json=body).status_code == 200
    assert client.post("/submissions/preflight", headers=_key_header(user), json=body).status_code == 403
    assert client.post("/submissions/preflight", headers=_jwt_header(user), json=body).status_code == 200


# --- users: a service account manages nothing about people ---------------------------------


def test_a_service_account_cannot_list_or_promote_users(client):
    svc, victim = _mk_user(UserRole.SERVICE), _mk_user(UserRole.USER)
    headers = _key_header(svc)
    assert client.get("/auth/admin/users", headers=headers).status_code == 403
    assert client.patch(f"/auth/admin/users/{victim.id}/role", headers=headers,
                        json={"role": "admin"}).status_code == 403
