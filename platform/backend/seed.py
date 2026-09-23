#!/usr/bin/env python3
"""Bring a database to a usable state: schema, first admin, starter benchmark,
and — optionally — a service account for another platform.

Runs as the chart's migrate Job after every install and upgrade, and by hand
for local development. Every step is idempotent: re-running it changes nothing
that already exists, and never resets a password someone has since changed.

    ADMIN_EMAIL=me@example.com ADMIN_USERNAME=me ADMIN_PASSWORD=... python seed.py

Environment:
    ADMIN_EMAIL, ADMIN_USERNAME, ADMIN_PASSWORD
        Required. There is deliberately no default password: a platform whose
        first admin is `admin / AdminPass123` is a platform anyone can log into.
    SERVICE_USERNAME, SERVICE_API_KEY
        Optional. Creates an account with the `service` role and gives it this
        exact API key — the value the other platform is configured with (e.g.
        LLM AutoTune's AUTOTUNE_LLMBENCH_API_KEY). The key must start with
        `llmb_`; only its hash is stored. Mint one with scripts/gen-prod-secrets.sh.
"""
from __future__ import annotations

import argparse
import os
import secrets
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import yaml  # noqa: E402

from app.core.auth import API_KEY_PREFIX, get_password_hash, hash_api_key  # noqa: E402
from app.core.config import settings  # noqa: E402

SEED_BENCHMARK = "perf-suite-v1.yaml"
# A seeded key is a secret someone generated for this deployment, not a
# placeholder; refuse anything short enough to have been typed by hand.
MIN_SERVICE_KEY_LENGTH = len(API_KEY_PREFIX) + 32


def _session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    return sessionmaker(create_engine(settings.DATABASE_URL_SYNC, echo=False))()


def _repo_root() -> Path:
    here = Path(__file__).resolve().parent
    for candidate in (here, *here.parents):
        if (candidate / "benchmarks").is_dir():
            return candidate
    raise FileNotFoundError("no benchmarks/ directory above seed.py")


def run_migrations() -> None:
    from alembic import command
    from alembic.config import Config
    ini = os.path.join(os.path.dirname(os.path.abspath(__file__)), "alembic.ini")
    command.upgrade(Config(ini), "head")
    print("[seed] migrations applied")


def ensure_admin(email: str, username: str, password: str) -> int:
    """The first admin. Matched on email OR username so a re-run never creates
    a second account — and never rewrites the password of the one it finds."""
    from app.db.models import User, UserRole

    session = _session()
    try:
        user = (session.query(User)
                .filter((User.email == email) | (User.username == username)).first())
        if user is not None:
            print(f"[seed] admin {user.username!r} exists — left as it is")
            return user.id
        user = User(email=email, username=username,
                    password_hash=get_password_hash(password), role=UserRole.ADMIN)
        session.add(user)
        session.commit()
        print(f"[seed] created admin {username!r} ({email})")
        return user.id
    finally:
        session.close()


def ensure_seed_benchmark(owner_id: int) -> None:
    """benchmarks/perf-suite-v1.yaml, created once, owned by the first admin."""
    from sqlalchemy.orm import selectinload

    from app.api.benchmarks import _compute_config_hash
    from app.db.models import Benchmark, BenchmarkModule, BenchmarkStatus

    data = yaml.safe_load((_repo_root() / "benchmarks" / SEED_BENCHMARK).read_text())
    session = _session()
    try:
        if session.query(Benchmark).filter_by(slug=data["slug"]).first() is not None:
            print(f"[seed] benchmark {data['slug']!r} exists — left as it is")
            return
        bench = Benchmark(
            slug=data["slug"], name=data["name"], description=data.get("description", ""),
            version=str(data.get("version", "1")),
            status=BenchmarkStatus(data.get("status", "active")),
            created_by_user_id=owner_id,
        )
        session.add(bench)
        session.flush()
        for i, mod in enumerate(data["modules"]):
            session.add(BenchmarkModule(
                benchmark_id=bench.id, module_name=mod["module_name"],
                params_json=mod.get("params", {}),
                metric_configs_json=mod.get("metric_configs", []),
                weight=mod["weight"], order_index=mod.get("order_index", i),
                skip_if_prev_failed=bool(mod.get("skip_if_prev_failed", False)),
            ))
        session.commit()
        # Hash what the database holds (Numeric weights come back as Decimals),
        # so it matches what the API computes for the same benchmark later.
        session.expire_all()
        reloaded = (session.query(Benchmark).options(selectinload(Benchmark.modules))
                    .filter_by(id=bench.id).one())
        reloaded.config_hash = _compute_config_hash(reloaded)
        session.commit()
        print(f"[seed] created benchmark {data['slug']!r}")
    finally:
        session.close()


def ensure_service_account(username: str, api_key: str) -> None:
    """A `service` user holding exactly `api_key`. If the user exists with a
    different key, the key is ADDED, not swapped: rotating is add-new, move the
    consumer, then delete the old key from the API Keys page."""
    from app.db.models import ApiKey, User, UserRole

    if not api_key.startswith(API_KEY_PREFIX) or len(api_key) < MIN_SERVICE_KEY_LENGTH:
        raise SystemExit(
            f"[seed] SERVICE_API_KEY must start with {API_KEY_PREFIX!r} and be at least "
            f"{MIN_SERVICE_KEY_LENGTH} characters — generate one with scripts/gen-prod-secrets.sh"
        )
    session = _session()
    try:
        user = session.query(User).filter_by(username=username).first()
        if user is None:
            user = User(
                email=f"{username}@service.invalid", username=username,
                # Never logs in with a password; this one exists only because
                # the column is required, and nobody knows it.
                password_hash=get_password_hash(secrets.token_urlsafe(32)),
                role=UserRole.SERVICE,
            )
            session.add(user)
            session.flush()
            print(f"[seed] created service account {username!r}")
        elif user.role != UserRole.SERVICE:
            raise SystemExit(
                f"[seed] {username!r} exists with role {user.role.value!r}; refusing to "
                "turn a person's account into a service account — pick another SERVICE_USERNAME"
            )
        key_hash = hash_api_key(api_key)
        if session.query(ApiKey).filter_by(key_hash=key_hash).first() is None:
            session.add(ApiKey(user_id=user.id, name="seeded",
                               key_prefix=api_key[:12], key_hash=key_hash))
            print(f"[seed] installed the service API key for {username!r}")
        session.commit()
    finally:
        session.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="Migrate and seed the LLMBench database")
    ap.add_argument("--admin-email", default=os.environ.get("ADMIN_EMAIL", ""))
    ap.add_argument("--admin-username", default=os.environ.get("ADMIN_USERNAME", ""))
    ap.add_argument("--admin-password", default=os.environ.get("ADMIN_PASSWORD", ""))
    ap.add_argument("--service-username", default=os.environ.get("SERVICE_USERNAME", "autotune"))
    ap.add_argument("--service-api-key", default=os.environ.get("SERVICE_API_KEY", ""))
    ap.add_argument("--skip-migrations", action="store_true")
    args = ap.parse_args()

    missing = [n for n, v in (("ADMIN_EMAIL", args.admin_email),
                              ("ADMIN_USERNAME", args.admin_username),
                              ("ADMIN_PASSWORD", args.admin_password)) if not v]
    if missing:
        raise SystemExit(f"[seed] set {', '.join(missing)} — there is no default admin")

    if not args.skip_migrations:
        run_migrations()
    admin_id = ensure_admin(args.admin_email, args.admin_username, args.admin_password)
    ensure_seed_benchmark(admin_id)
    if args.service_api_key:
        ensure_service_account(args.service_username, args.service_api_key)
    print("[seed] done")


if __name__ == "__main__":
    main()
