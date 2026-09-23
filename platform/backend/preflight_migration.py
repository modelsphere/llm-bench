"""Refuse to migrate while a long-lived transaction can block the DDL.

Why this exists
---------------
On 2026-07-23 a one-line migration (ADD COLUMN submissions.run_attempts) took the
site down for ~25 minutes. It was never slow — `ADD COLUMN ... DEFAULT NOT NULL`
is catalog-only on PG 11+ — it was *waiting*. Two worker backends had been
`idle in transaction` for 2h43m and 1h51m holding ACCESS SHARE on `submissions`;
the ALTER needed ACCESS EXCLUSIVE and queued behind them.

The damage came from what happens next: a **pending** ACCESS EXCLUSIVE request
blocks every *subsequent* lock request on that table. So every ordinary query
piled up behind the migration, backend readiness probes failed, the API went
0/1, and helm eventually failed its post-upgrade hook — all while the migration
itself had done nothing at all.

This is structural, not a one-off: the worker holds one session for the whole
submission, so a transaction on `submissions` routinely lives as long as a module
run (hours). Any migration touching a hot table can hit it.

So: look before leaping. Refusing to start costs a failed deploy that names its
own cause. Starting and blocking costs an outage that looks like a slow
migration, which is the wrong thing to go debugging.

Behaviour
---------
Exits 0 when it is safe to migrate (or when it cannot tell — see below).
Exits 1 with a diagnosis and the exact remediation command when a transaction
older than the threshold is holding a lock we would contend with.

Config (env):
  MIGRATION_PREFLIGHT           1/0     enable (default 1)
  MIGRATION_PREFLIGHT_MAX_AGE   seconds transaction age to refuse at (default 300)
  MIGRATION_PREFLIGHT_WAIT      seconds keep re-checking this long before giving
                                        up, so a transaction about to end is
                                        waited out rather than failing the deploy
                                        (default 120)

Fails OPEN, deliberately: if the check itself errors (no driver, permissions, a
Postgres version without some column) it logs and exits 0. A broken *guard* must
not become a new way to block deploys — the migration's own behaviour is
unchanged by that, and it is what we had before this existed.
"""
from __future__ import annotations

import os
import sys
import time

# Only these tables matter: they are the hot ones the worker holds long
# transactions against. A long transaction elsewhere cannot block a migration
# that never touches its table, and refusing on it would be a false positive.
WATCHED_TABLES = ("submissions", "submission_runs", "benchmarks", "benchmark_modules")

ENABLED = os.getenv("MIGRATION_PREFLIGHT", "1").strip().lower() not in ("0", "false", "no")
MAX_AGE_SECONDS = float(os.getenv("MIGRATION_PREFLIGHT_MAX_AGE", "300"))
WAIT_SECONDS = float(os.getenv("MIGRATION_PREFLIGHT_WAIT", "120"))
POLL_SECONDS = 5.0

# Transactions older than MAX_AGE that hold a lock on a watched table. Matching on
# the LOCK rather than on `state` is what makes this precise: an idle-in-transaction
# session that touched nothing we care about is harmless, while a long *active*
# transaction holding ACCESS SHARE on submissions blocks the DDL just as surely as
# an idle one. pg_stat_activity alone cannot tell those apart.
QUERY = """
SELECT a.pid,
       EXTRACT(EPOCH FROM (now() - a.xact_start))::bigint AS age_s,
       a.state,
       a.application_name,
       COALESCE(string_agg(DISTINCT c.relname, ', '), '?') AS tables,
       LEFT(REGEXP_REPLACE(COALESCE(a.query, ''), '\\s+', ' ', 'g'), 100) AS query
FROM pg_stat_activity a
JOIN pg_locks l ON l.pid = a.pid
LEFT JOIN pg_class c ON c.oid = l.relation
WHERE a.datname = current_database()
  AND a.pid <> pg_backend_pid()
  AND a.xact_start IS NOT NULL
  AND a.xact_start < now() - make_interval(secs => %(max_age)s)
  AND c.relname = ANY(%(tables)s)
GROUP BY a.pid, a.xact_start, a.state, a.application_name, a.query
ORDER BY age_s DESC
"""


def _dsn() -> str | None:
    """libpq DSN from whatever the app config exposes."""
    try:
        from app.core.config import settings
        url = getattr(settings, "DATABASE_URL", None) or getattr(settings, "database_url", None)
    except Exception:
        url = None
    url = url or os.getenv("DATABASE_URL")
    if not url:
        return None
    # Strip the SQLAlchemy driver marker: postgresql+asyncpg:// -> postgresql://
    if "+" in url.split("://", 1)[0]:
        scheme, rest = url.split("://", 1)
        url = scheme.split("+", 1)[0] + "://" + rest
    return url


def _blockers(conn) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(QUERY, {"max_age": MAX_AGE_SECONDS, "tables": list(WATCHED_TABLES)})
        return cur.fetchall()


def _pending_migrations() -> bool | None:
    """True if `alembic upgrade head` would actually apply something.

    The lock check only earns its keep when DDL is about to run. Guarding an
    up-to-date database would block deploys that were never going to touch the
    schema — and, worse, would refuse the very deploy that ships a fix for the
    long transactions it is complaining about. That is not hypothetical: it is
    exactly the state this was first run in.

    Returns None when it cannot tell, so the caller can fall back to checking
    (better a needless check than a missed one).
    """
    try:
        from alembic.config import Config
        from alembic.runtime.migration import MigrationContext
        from alembic.script import ScriptDirectory
        from sqlalchemy import create_engine

        cfg = Config(os.getenv("ALEMBIC_CONFIG", "alembic.ini"))
        script = ScriptDirectory.from_config(cfg)
        heads = set(script.get_heads())

        engine = create_engine(_dsn(), poolclass=None)
        try:
            with engine.connect() as conn:
                current = set(MigrationContext.configure(conn).get_current_heads())
        finally:
            engine.dispose()

        if current == heads:
            print(f"[preflight] database already at head ({', '.join(sorted(heads)) or '-'})")
            return False
        print(
            f"[preflight] pending migration(s): current={', '.join(sorted(current)) or 'none'} "
            f"-> head={', '.join(sorted(heads))}"
        )
        return True
    except Exception as exc:
        print(f"[preflight] could not determine pending migrations ({type(exc).__name__}: {exc})")
        return None


def main() -> int:
    if not ENABLED:
        print("[preflight] disabled via MIGRATION_PREFLIGHT=0 — proceeding")
        return 0

    dsn = _dsn()
    if not dsn:
        print("[preflight] no DATABASE_URL available — skipping check (fail-open)")
        return 0

    try:
        import psycopg2
    except Exception as exc:  # pragma: no cover - driver always present in the image
        print(f"[preflight] psycopg2 unavailable ({exc}) — skipping check (fail-open)")
        return 0

    # Only guard when DDL is actually going to run. `alembic upgrade head` on an
    # up-to-date database takes no table locks, so refusing on a long transaction
    # would block a deploy for a migration that does not exist.
    if _pending_migrations() is False:
        print("[preflight] no migration to apply — lock check not needed")
        return 0

    deadline = time.monotonic() + WAIT_SECONDS
    rows: list[tuple] = []
    try:
        conn = psycopg2.connect(dsn, connect_timeout=10)
        conn.autocommit = True
        try:
            while True:
                rows = _blockers(conn)
                if not rows:
                    print(
                        f"[preflight] no transaction older than {MAX_AGE_SECONDS:.0f}s holds a "
                        f"lock on {', '.join(WATCHED_TABLES)} — safe to migrate"
                    )
                    return 0
                if time.monotonic() >= deadline:
                    break
                print(
                    f"[preflight] {len(rows)} long transaction(s) still holding locks; "
                    f"waiting up to {deadline - time.monotonic():.0f}s more…"
                )
                time.sleep(POLL_SECONDS)
        finally:
            conn.close()
    except Exception as exc:
        print(f"[preflight] check failed ({type(exc).__name__}: {exc}) — proceeding anyway (fail-open)")
        return 0

    print("", file=sys.stderr)
    print("=" * 72, file=sys.stderr)
    print("REFUSING TO MIGRATE: a long-lived transaction would block the DDL.", file=sys.stderr)
    print("=" * 72, file=sys.stderr)
    for pid, age_s, state, app, tables, query in rows:
        print(
            f"  pid={pid} age={age_s // 60}m{age_s % 60:02d}s state={state!r} "
            f"app={app or '-'}\n    tables={tables}\n    query={query}",
            file=sys.stderr,
        )
    print(
        "\nWhy this matters: the migration would take ACCESS EXCLUSIVE on one of\n"
        "these tables. A PENDING exclusive lock blocks every SUBSEQUENT query on\n"
        "that table, so the whole API stalls behind a migration that has not yet\n"
        "done anything. That is how the 2026-07-23 outage happened.\n"
        "\nThe worker holds one DB session for a whole submission, so these are\n"
        "most likely in-flight runs rather than leaks. Options:\n"
        "  1. Wait for the runs to finish, then redeploy.\n"
        "  2. Deploy with --cordon (stops new submissions, drains, then migrates).\n"
        "  3. If they really are stuck, terminate just the holders:\n"
        "       SELECT pg_terminate_backend(pid) FROM pg_stat_activity\n"
        "       WHERE datname=current_database() AND state='idle in transaction'\n"
        "         AND xact_start < now() - interval '10 minutes';\n"
        "  4. Override for this deploy: MIGRATION_PREFLIGHT=0 (accepts the risk).\n",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
