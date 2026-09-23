#!/usr/bin/env python3
"""
llmbench — CLI for the LLM Benchmark Platform.

Usage:
    llmbench login --email EMAIL [--password PASSWORD]
    llmbench submit --benchmark SLUG --url URL --model MODEL [--api-key KEY] [--module NAME]
    llmbench tail <submission-id>
    llmbench benchmarks
    llmbench modules
    llmbench whoami

Environment:
    LLMBENCH_API   API base URL (default: http://localhost:8001)
    LLMBENCH_TOKEN Path to saved token file (default: ~/.llmbench_token)
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import time
import urllib.request
import urllib.error


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

API = os.environ.get("LLMBENCH_API", "http://localhost:8001")
TOKEN_FILE = os.environ.get("LLMBENCH_TOKEN", os.path.expanduser("~/.llmbench_token"))


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _req(method: str, path: str, token: str | None = None, body: dict | None = None) -> dict:
    url = f"{API}{path}"
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        body_bytes = exc.read()
        try:
            err = json.loads(body_bytes).get("detail", body_bytes.decode())
        except Exception:
            err = body_bytes.decode()
        sys.exit(f"Error {exc.code}: {err}")


def _get_token() -> str | None:
    if os.path.exists(TOKEN_FILE):
        return open(TOKEN_FILE).read().strip()
    return None


def _save_token(token: str) -> None:
    with open(TOKEN_FILE, "w") as f:
        f.write(token)
    os.chmod(TOKEN_FILE, 0o600)


def _clear_token() -> None:
    if os.path.exists(TOKEN_FILE):
        os.remove(TOKEN_FILE)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_login(args: argparse.Namespace) -> None:
    email = args.email
    if args.password:
        password = args.password
    else:
        password = getpass.getpass("Password: ")

    resp = _req("POST", "/auth/login", body={"email": email, "password": password})
    token: str = resp["access_token"]
    _save_token(token)
    print(f"Logged in as {email} (role={resp['role']})")
    print(f"Token saved to {TOKEN_FILE}")


def cmd_logout(_args: argparse.Namespace) -> None:
    _clear_token()
    print("Logged out.")


def cmd_whoami(args: argparse.Namespace) -> None:
    token = _get_token()
    if not token:
        sys.exit("Not logged in. Run: llmbench login")
    resp = _req("GET", "/auth/me", token=token)
    print(f"id={resp['id']}  username={resp['username']}  role={resp['role']}  email={resp['email']}")


def cmd_modules(args: argparse.Namespace) -> None:
    token = _get_token()
    if not token:
        sys.exit("Not logged in. Run: llmbench login")
    resp = _req("GET", "/modules", token=token)
    for m in resp["modules"]:
        print(f"  {m['name']:<30} {m['display_name']}")


def cmd_benchmarks(args: argparse.Namespace) -> None:
    token = _get_token()
    if not token:
        sys.exit("Not logged in. Run: llmbench login")
    resp = _req("GET", "/benchmarks", token=token)
    for b in resp["benchmarks"]:
        print(f"  {b['slug']:<30} [{b['status']}]  {b['name']}")
        for bm in b.get("modules", []):
            print(f"    - {bm['module_name']:<28} weight={bm['weight']}")


def cmd_submit(args: argparse.Namespace) -> None:
    token = _get_token()
    if not token:
        sys.exit("Not logged in. Run: llmbench login")

    api_key = args.api_key or ""
    payload = {
        "endpoint_url": args.url,
        "model": args.model,
        "api_key": api_key,
    }

    if args.module:
        path = f"/modules/{args.module}/submit"
    elif args.benchmark:
        path = f"/benchmarks/{args.benchmark}/submit"
    else:
        sys.exit("Specify either --benchmark or --module")

    resp = _req("POST", path, token=token, body=payload)
    sub = resp
    print(f"Submission #{sub['id']} queued.")
    print(f"  Status: {sub['status']}")
    print(f"  Endpoint: {sub['endpoint_url']} / {sub['endpoint_model']}")
    print(f"\nTrack progress: llmbench tail {sub['id']}")


def cmd_tail(args: argparse.Namespace) -> None:
    token = _get_token()
    if not token:
        sys.exit("Not logged in. Run: llmbench login")

    last_status = None
    try:
        while True:
            resp = _req("GET", f"/submissions/{args.submission_id}", token=token)
            s = resp
            status = s["status"]

            if status != last_status:
                if last_status is not None:
                    print()
                last_status = status

            # Build a compact one-line summary
            runs = s.get("runs", [])
            scores = []
            for r in runs:
                if r["status"] == "done" and r["score"] is not None:
                    scores.append(f"{r['module_name']}={r['score']:.4f}")

            parts = [f"[{status.upper()}]"]
            if s.get("score_total") is not None:
                parts.append(f"score={s['score_total']:.4f}")
            if s.get("passed") is not None:
                parts.append(f"passed={'✓' if s['passed'] else '✗'}")
            if scores:
                parts.append(" ".join(scores))
            if s.get("error"):
                parts.append(f"error={s['error'][:60]}")

            print(f"\rSubmission #{s['id']}  {'  '.join(parts)}  ", end="", flush=True)

            if status in ("done", "failed", "canceled"):
                print()  # newline after final state
                break

            time.sleep(3)
    except KeyboardInterrupt:
        print()
        sys.exit(0)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(prog="llmbench", description="LLM Benchmark Platform CLI")
    sub = parser.add_subparsers(required=True)

    # login
    p_login = sub.add_parser("login", help="Login to the platform")
    p_login.add_argument("--email", required=True, help="Email address")
    p_login.add_argument("--password", help="Password (omit to prompt)")
    p_login.set_defaults(func=cmd_login)

    # logout
    p_logout = sub.add_parser("logout", help="Clear stored credentials")
    p_logout.set_defaults(func=cmd_logout)

    # whoami
    p_who = sub.add_parser("whoami", help="Show current user")
    p_who.set_defaults(func=cmd_whoami)

    # modules
    p_mods = sub.add_parser("modules", help="List available test modules")
    p_mods.set_defaults(func=cmd_modules)

    # benchmarks
    p_bench = sub.add_parser("benchmarks", help="List benchmarks")
    p_bench.set_defaults(func=cmd_benchmarks)

    # submit
    p_sub = sub.add_parser("submit", help="Submit an endpoint for benchmarking")
    p_sub.add_argument("--benchmark", metavar="SLUG", help="Benchmark slug (e.g. perf-suite-v1)")
    p_sub.add_argument("--module", metavar="NAME", help="Single module name (ad-hoc)")
    p_sub.add_argument("--url", required=True, metavar="URL", help="API base URL (e.g. https://api.example.com/v1)")
    p_sub.add_argument("--model", required=True, help="Model name (e.g. Kimi-K2.5)")
    p_sub.add_argument("--api-key", metavar="KEY", default="", help="API key (omit to use none)")
    p_sub.set_defaults(func=cmd_submit)

    # tail
    p_tail = sub.add_parser("tail", help="Watch a submission's progress")
    p_tail.add_argument("submission_id", type=int, metavar="ID", help="Submission ID")
    p_tail.set_defaults(func=cmd_tail)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
