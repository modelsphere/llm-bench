"""Backend test config.

These are *integration* tests: they need a real Postgres and a real Redis,
because the behaviour under test (worker-crash recovery, Dramatiq state
reconciliation) only exists against real engines — SQLite/StubBroker would lie.

Point them at throwaway services with:

    docker run -d --name pg-test  -e POSTGRES_PASSWORD=pw -e POSTGRES_DB=app -p 55432:5432 postgres:16-alpine
    docker run -d --name redis-test -p 56379:6379 redis:7-alpine

Override the defaults with TEST_DATABASE_URL / TEST_DATABASE_URL_SYNC /
TEST_REDIS_URL if your services live elsewhere. If nothing is reachable the
tests skip (they never touch the app's real DATABASE_URL).

IMPORTANT: the env vars below MUST be set before any `import app.*`, because
app.db.models builds its engines and app.queue.* reads Redis config from
settings at import time.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# tests/ -> backend -> platform -> repo root. Put the repo root on the path so
# `import bench` resolves, and the backend dir so `import app` resolves, exactly
# as the worker image's PYTHONPATH does. Production also sets BENCH_MODULES_PATH.
_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT / "platform" / "backend"))
sys.path.insert(0, str(_REPO_ROOT))
os.environ.setdefault("BENCH_MODULES_PATH", str(_REPO_ROOT / "bench"))

os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://postgres:pw@localhost:55432/app"
)
os.environ["DATABASE_URL_SYNC"] = os.environ.get(
    "TEST_DATABASE_URL_SYNC", "postgresql://postgres:pw@localhost:55432/app"
)
os.environ["REDIS_URL"] = os.environ.get("TEST_REDIS_URL", "redis://localhost:56379/15")

# Tests run against throwaway services with placeholder secrets; DEBUG=true so
# the app's fail-closed production-secrets guard (_assert_production_secrets)
# lets the TestClient lifespan start.
os.environ["DEBUG"] = "true"

# On CI the throwaway Postgres/Redis are provisioned as job services, so an
# unreachable service is an infrastructure failure — fail loudly at collection
# instead of letting every per-file `skipif` green-wash the job with 0 tests.
if os.environ.get("CI"):
    import psycopg2
    import redis as _redis

    psycopg2.connect(os.environ["DATABASE_URL_SYNC"]).close()
    _redis.from_url(os.environ["REDIS_URL"]).ping()
