#!/usr/bin/env python3
"""Start FastAPI (uvicorn) + Dramatiq worker together; Ctrl-C kills both."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

_backend_dir = Path(__file__).resolve().parent
_repo_root = _backend_dir.parent.parent
_venv_python = str(_backend_dir / ".venv" / "bin" / "python")

# Strip any inherited HTTP(S) proxy so the load generator talks to LLM
# endpoints directly. Routing guidellm through a local HTTP/2 proxy caps
# concurrency at ~16 streams/connection and silently tanks uptime — the
# excess requests fail client-side with LocalProtocolError before sending.
_PROXY_VARS = (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "all_proxy",
)
env = {
    **{k: v for k, v in os.environ.items() if k not in _PROXY_VARS},
    "PYTHONPATH": f"{_repo_root}:{_backend_dir}",
    # Local dev wants readable 500 tracebacks. DEBUG now defaults OFF (so public
    # deploys fail safe), so opt back in here — overridable if explicitly set.
    "DEBUG": os.environ.get("DEBUG", "true"),
}

# Verify the strip actually took: warn about which proxy vars were inherited,
# then assert none survived into the child env we're about to launch with.
_inherited = {k: os.environ[k] for k in _PROXY_VARS if k in os.environ}
if _inherited:
    print(
        "Stripped inherited proxy vars (load generator will connect direct): "
        + ", ".join(f"{k}={v}" for k, v in _inherited.items()),
        flush=True,
    )
_leaked = [k for k in _PROXY_VARS if k in env]
assert not _leaked, f"proxy vars leaked into child env: {_leaked}"
print(f"Proxy check OK — no proxy vars in worker env ({len(_inherited)} stripped).", flush=True)

procs: list[subprocess.Popen] = []


def _run_migrations() -> None:
    """Bring the DB schema to head (alembic upgrade head) before starting services.

    Keeps the local DB in sync with the models automatically, so a freshly
    pulled migration can't leave the API/worker querying columns that don't yet
    exist (the classic `column ... does not exist` startup crash).

    Run with a short libpq ``lock_timeout`` so that a *stale* connection holding
    a table lock (e.g. a leaked `idle in transaction` backend from a previous
    dev run) makes the migration fail fast with a clear message instead of
    silently hanging the whole startup. Skip with DEV_SKIP_MIGRATIONS=1.
    """
    if os.environ.get("DEV_SKIP_MIGRATIONS") == "1":
        print("DEV_SKIP_MIGRATIONS=1 — skipping `alembic upgrade head`.", flush=True)
        return
    print("Running DB migrations (alembic upgrade head)…", flush=True)
    # libpq reads PGOPTIONS; alembic's sync engine connects via libpq, so this
    # bounds how long the DDL will wait for a contended lock.
    mig_env = {**env, "PGOPTIONS": "-c lock_timeout=15s"}
    try:
        subprocess.run(
            [_venv_python, "-m", "alembic", "upgrade", "head"],
            cwd=_backend_dir, env=mig_env, check=True, timeout=300,
        )
    except subprocess.TimeoutExpired:
        sys.exit(
            "Migration timed out (>300s). A stale DB connection is likely holding "
            "a table lock — look for `idle in transaction` backends "
            "(SELECT * FROM pg_stat_activity) or restart Postgres, then retry."
        )
    except subprocess.CalledProcessError as exc:
        sys.exit(
            f"`alembic upgrade head` failed (exit {exc.returncode}) — not starting "
            "API/worker against an out-of-date schema. If the cause is a lock "
            "timeout, a leaked dev connection is holding the table lock; clear it "
            "(pg_terminate_backend) or restart Postgres, then retry."
        )
    print("DB schema up to date.", flush=True)


def _shutdown(signum=None, frame=None) -> None:
    print("\nShutting down…", flush=True)
    for p in procs:
        if p.poll() is None:
            p.terminate()
    for p in procs:
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()
    sys.exit(0)


signal.signal(signal.SIGINT, _shutdown)
signal.signal(signal.SIGTERM, _shutdown)

_log_level = os.environ.get("LOGLEVEL", "INFO").lower()

# Apply pending DB migrations before launching anything that talks to the DB.
_run_migrations()

api = subprocess.Popen(
    [_venv_python, "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8090", "--log-level", _log_level],
    cwd=_backend_dir,
    env=env,
)
procs.append(api)
print(f"FastAPI started (PID {api.pid})  →  http://localhost:8090", flush=True)

worker = subprocess.Popen(
    [
        _venv_python, "-m", "dramatiq",
        "app.queue.broker",
        "app.queue.jobs",
        "--threads", "4",
    ],
    cwd=_backend_dir,
    env=env,
)
procs.append(worker)
print(f"Dramatiq worker started (PID {worker.pid})", flush=True)
print("Press Ctrl-C to stop both.", flush=True)

# Wait; if either process dies unexpectedly, shut everything down.
while True:
    for p in procs:
        if p.poll() is not None:
            print(f"Process PID {p.pid} exited with code {p.returncode}. Shutting down.", flush=True)
            _shutdown()
    try:
        procs[0].wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass
