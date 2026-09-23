"""A run says which module of its benchmark produced it.

A benchmark can include the same module twice — two sweeps of different shapes,
say. A client reading results (LLM AutoTune does) needs to know which run came
from which, and the order runs happen to be listed in is not a contract. Each
run therefore carries `benchmark_module_id`.

Needs a real test Postgres; skips otherwise.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.core.auth import create_access_token, get_password_hash
from app.core.config import settings
from app.core.security import encrypt_api_key
from app.db.models import (
    Base,
    Benchmark,
    BenchmarkModule,
    BenchmarkStatus,
    ModuleRunStatus,
    Submission,
    SubmissionRun,
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


pytestmark = pytest.mark.skipif(not _services_available(), reason="needs a test Postgres")


@pytest.fixture(scope="module", autouse=True)
def _schema():
    Base.metadata.create_all(_sync_engine)
    try:
        yield
    finally:
        import asyncio

        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(_async_engine.dispose())


def test_two_runs_of_the_same_module_name_their_benchmark_module():
    with TestClient(app) as client:     # startup syncs the module registry
        s = get_sync_session()
        try:
            suffix = uuid.uuid4().hex[:10]
            user = User(email=f"ri-{suffix}@example.com", username=f"ri_{suffix}",
                        password_hash=get_password_hash("RunIdentity1"), role=UserRole.USER)
            s.add(user)
            s.flush()
            bench = Benchmark(slug=f"ri-{suffix}", name="two sweeps", status=BenchmarkStatus.ACTIVE,
                              created_by_user_id=user.id)
            s.add(bench)
            s.flush()
            first = BenchmarkModule(benchmark_id=bench.id, module_name="case_truncation",
                                    params_json={"n": 1}, weight=0.5, order_index=0)
            second = BenchmarkModule(benchmark_id=bench.id, module_name="case_truncation",
                                     params_json={"n": 2}, weight=0.5, order_index=1)
            s.add_all([first, second])
            s.flush()
            sub = Submission(user_id=user.id, benchmark_id=bench.id, endpoint_url="http://x/v1",
                             endpoint_model="m", endpoint_api_key_enc=encrypt_api_key(""),
                             status=SubmissionStatus.DONE)
            s.add(sub)
            s.flush()
            # Deliberately inserted out of benchmark order.
            s.add_all([
                SubmissionRun(submission_id=sub.id, module_name="case_truncation",
                              benchmark_module_id=second.id, params_json={"n": 2},
                              status=ModuleRunStatus.DONE),
                SubmissionRun(submission_id=sub.id, module_name="case_truncation",
                              benchmark_module_id=first.id, params_json={"n": 1},
                              status=ModuleRunStatus.DONE),
            ])
            s.commit()
            ids = {"first": first.id, "second": second.id, "sub": sub.id, "user": user.id}
        finally:
            s.close()

        token = create_access_token({"sub": str(ids["user"]), "role": "user"})
        body = client.get(f"/submissions/{ids['sub']}", headers={"Authorization": f"Bearer {token}"})
        assert body.status_code == 200, body.text
        by_module = {r["benchmark_module_id"]: r["params_json"]["n"] for r in body.json()["runs"]}
        assert by_module == {ids["first"]: 1, ids["second"]: 2}
