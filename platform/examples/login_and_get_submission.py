#!/usr/bin/env python3
"""Example: log in and fetch a submission's details from the k8s backend.

Talks to the deployed platform over its public HTTP API (the same surface the
web UI uses). nginx strips the ``/api`` prefix in front of the backend, so every
route the UI hits at ``/api/...`` is served by FastAPI at ``/...``. This script
targets the ``/api`` base directly.

What it does:
  1. POST /api/auth/login          -> JWT   (or use an existing personal API key)
  2. GET  /api/submissions/{id}    -> submission detail (status, score, per-module runs)
     ...or, with --mine:
     GET  /api/submissions/me      -> list YOUR submissions (summary rows)

Auth note: the platform accepts EITHER a JWT (from /auth/login) or a personal
API key (``Authorization: Bearer <key>``) on every submission endpoint — an API
key confers the same identity/permissions as a login. For CI/automation, prefer
a personal API key (create one in the UI, or via POST /api/auth/api-keys) and
pass it through --api-key, skipping the email/password step entirely.

Access: benchmark submissions are readable by every signed-in user (read-only
platform policy — cancel and worker logs stay owner/admin-only). Ad-hoc module
submissions are always private to their owner; a non-owner gets 403.

Usage:
    # Fetch one submission by id (email + password)
    python login_and_get_submission.py \
        --base http://localhost:8080/api \
        --email you@example.com --password 'secret' \
        --id 123

    # Same, using a personal API key instead of login
    python login_and_get_submission.py --base http://localhost:8080/api \
        --api-key llmb_... --id 123

    # List your own submissions (find an id to fetch)
    python login_and_get_submission.py --api-key llmb_... --mine

Only stdlib is used, so there are no dependencies to install.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request


def _request(method: str, url: str, *, token: str | None = None, body: dict | None = None) -> tuple[int, dict | list | str]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read().decode("utf-8", "replace")
            ct = resp.headers.get("Content-Type", "")
            return resp.status, (json.loads(raw) if "json" in ct and raw else raw)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, raw


def die(msg: str, payload: object = None) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    if payload is not None:
        print(json.dumps(payload, indent=2, default=str), file=sys.stderr)
    sys.exit(1)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base", default="http://localhost:8080/api",
                   help="Platform base URL (the API lives under <base>/api). "
                        "Default is the test k8s cluster ingress.")
    # Auth: either login (email+password) or a personal API key.
    p.add_argument("--email")
    p.add_argument("--password")
    p.add_argument("--api-key", help="Personal API key (llmb_...); used instead of email/password.")
    # What to fetch.
    p.add_argument("--id", type=int, help="Submission id to fetch.")
    p.add_argument("--mine", action="store_true", help="List your own submissions instead of fetching one by id.")
    args = p.parse_args()

    if not args.mine and args.id is None:
        die("Pass --id <submission_id> to fetch one, or --mine to list your submissions.")

    api = args.base.rstrip("/") + "/api"

    # --- 1. Authenticate ---------------------------------------------------
    if args.api_key:
        token = args.api_key
        print("Using personal API key for auth.")
    else:
        if not (args.email and args.password):
            die("Provide --api-key, or both --email and --password.")
        status, data = _request("POST", f"{api}/auth/login",
                                 body={"email": args.email, "password": args.password})
        if status != 200:
            die(f"Login failed (HTTP {status})", data)
        token = data["access_token"]
        print(f"Logged in as {args.email} (role={data.get('role')}).")

    # --- 2a. List your own submissions ------------------------------------
    if args.mine:
        status, subs = _request("GET", f"{api}/submissions/me", token=token)
        if status != 200:
            die(f"Failed to list submissions (HTTP {status})", subs)
        if not subs:
            print("You have no submissions yet.")
            return
        print(f"\n{len(subs)} submission(s):\n")
        print(f"{'id':>6}  {'status':<9}  {'score':>8}  {'benchmark/module':<28}  created_at")
        print("-" * 80)
        for s in subs:
            target = s.get("benchmark_slug") or f"module:{s.get('module_name')}" or "-"
            score = s.get("score_total")
            print(f"{s['id']:>6}  {s['status']:<9}  "
                  f"{(f'{score:.2f}' if isinstance(score, (int, float)) else '-'):>8}  "
                  f"{target:<28}  {s.get('created_at')}")
        print("\nRe-run with --id <n> to see full detail for one.")
        return

    # --- 2b. Fetch one submission's detail --------------------------------
    status, s = _request("GET", f"{api}/submissions/{args.id}", token=token)
    if status == 404:
        die(f"Submission {args.id} not found.", s)
    if status == 403:
        die("Access denied — you can only read your own submissions "
            "(or public/admin ones).", s)
    if status != 200:
        die(f"Failed to fetch submission (HTTP {status})", s)

    print(f"\n=== Submission {s['id']} ===")
    print(json.dumps({
        "id": s["id"],
        "status": s["status"],
        "benchmark": s.get("benchmark_slug") or s.get("benchmark_name"),
        "module_name": s.get("module_name"),
        "endpoint_url": s.get("endpoint_url"),
        "endpoint_model": s.get("endpoint_model"),
        "score_total": s.get("score_total"),
        "passed": s.get("passed"),
        "error": s.get("error"),
        "created_at": s.get("created_at"),
        "started_at": s.get("started_at"),
        "finished_at": s.get("finished_at"),
    }, indent=2, default=str))

    runs = s.get("runs", [])
    if runs:
        print(f"\n--- {len(runs)} module run(s) ---")
        for r in runs:
            print(json.dumps({
                "module": r["module_name"],
                "status": r["status"],
                "score": r.get("score"),
                "passed": r.get("passed"),
                "error": r.get("error"),
                "metrics": r.get("metrics_json"),
            }, indent=2, default=str))


if __name__ == "__main__":
    main()
