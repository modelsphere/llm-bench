# LLMBench platform — developer guide

The platform half of LLMBench: the FastAPI backend (`backend/`), the Dramatiq
worker that runs benchmarks (same code, `app/queue/`), and the Vue frontend
(`frontend/`). The benchmark modules themselves live in `../bench/`.

To *run* LLMBench, see the repository [README](../README.md) (Docker Compose on
one machine) and [docs/deploying.md](../docs/deploying.md) (Helm). This page is
for changing it.

## Local development

Postgres and Redis run in Docker; the backend, worker and frontend run natively
with hot reload.

Prerequisites: Python 3.12 and [uv](https://docs.astral.sh/uv/), Node 22, Docker.

```bash
# 1. Postgres + Redis
docker compose -f platform/backend/docker-compose.yml up -d

# 2. Backend environment (from the lockfile — the same versions CI tests)
cd platform/backend
uv sync --frozen --extra dev

# 3. Schema, first admin, starter benchmark. There is no default admin password.
export ADMIN_EMAIL=me@example.com ADMIN_USERNAME=me ADMIN_PASSWORD='pick-one'
PYTHONPATH=../.. uv run python seed.py

# 4. API + worker together (Ctrl-C stops both); API on http://localhost:8090
uv run python dev.py

# 5. Frontend, in another terminal; http://localhost:5173, proxies /api
cd platform/frontend && npm ci && npm run dev
```

`dev.py` sets `DEBUG=true` for readable tracebacks and puts the repository root
on `PYTHONPATH` (the backend imports `bench/`). Settings are environment
variables (or a `.env` file in `platform/backend/`); see `app/core/config.py`.
With `DEBUG` off the backend refuses to start on the placeholder
`SECRET_KEY` / `PLATFORM_SECRET_KEY`, so a deployment cannot ship them by accident.

To benchmark something without a GPU, run the mock model and submit it:

```bash
uv run --project platform/backend python mock_server.py --port 8001 --model mock \
    --ttft-ms 300 --tpot-ms 20 --output-tokens 256
# submit http://localhost:8001/v1, model "mock", to perf-suite-v1 from the UI
```

## Tests

```bash
# Backend: real Postgres + Redis on :55432 / :56379 (SQLite would lie about locking)
docker run -d --name llmbench-test-pg -e POSTGRES_PASSWORD=pw -e POSTGRES_DB=app -p 55432:5432 postgres:16-alpine
docker run -d --name llmbench-test-redis -p 56379:6379 redis:7-alpine
cd platform/backend && PYTHONPATH=../.. uv run pytest tests/ -q

# Benchmark modules, against a self-started mock server — no GPU, no network
uv run --project platform/backend python -m pytest tests/modules/ -q

# Frontend: type-check + build, and the en / zh-CN locale key parity CI enforces
cd platform/frontend && npm run build
```

`tests/modules/test_guidellm_measurement.py` checks the numbers themselves —
TTFT, inter-token latency and request counts against a mock with known timing,
including reasoning models — and is the gate on any guidellm upgrade.

## Schema changes

Migrations are Alembic revisions in `backend/alembic/versions/`, on top of
`001_initial`. Generate one against a real Postgres and read what it produced;
CI runs `alembic upgrade head`, `alembic check`, a full downgrade and an upgrade
again, so the models and the migrations cannot drift apart.

```bash
cd platform/backend
PYTHONPATH=../.. uv run alembic revision --autogenerate -m "what changed"
```

Identities never go in a migration (it cannot ask for a password); reference
data may. `seed.py` owns the first admin, the starter benchmark and the service
account.

## Layout

```
platform/
  backend/
    app/
      api/         FastAPI routers
      core/        settings, auth, encryption, preflight, card normalization
      datasets/    the rolling replay-dataset collector and its log sources
      db/          SQLAlchemy models
      queue/       Dramatiq broker, the run_submission actor, heartbeats
    alembic/       migrations
    seed.py        first admin, starter benchmark, service account
    dev.py         API + worker for local development
  frontend/
    src/           api/ stores/ router/ views/ components/ locales/
  cli.py           a small command-line client (login, list, submit, tail)
  examples/        scripted API usage
```

## API Reference

The platform exposes a REST API for queries and submissions. All endpoints return JSON unless noted.

**Base URL.** Through the frontend (the chart, `docker compose`) the API is under `/api`: `http://localhost:8080/api`. Calling the backend directly, there is no prefix: `http://localhost:8000` for `uvicorn`, `http://localhost:8090` for `python dev.py`. Examples below use `$BASE`.

**Auth.** Every endpoint except the few public ones below requires an `Authorization: Bearer <token>` header, where the token is either a login JWT or a personal API key (`llmb_…`, minted on the API Keys page) — both carry exactly the account's role. The SSE endpoint takes the token as a query param (`?token=…`) instead, because browser `EventSource` can't set headers.

| Visibility | Endpoints |
|------------|-----------|
| Public (no auth) | `GET /health`, `GET /metrics`, `GET /modules`, `GET /modules/{name}` |
| Requires login | `/benchmarks*`, `/leaderboard/*`, `/submissions/*`, `GET /auth/me` |
| Admin, or a service account on its own benchmarks | `/benchmarks/admin/benchmarks` (create, import, list, update, lock, export, delete) |
| Admin, or a service account | `/replay-datasets` reads and `POST /replay-datasets/profiles/{id}/build` |
| Admin only | `/auth/admin/*`, `/card-types` writes, `/benchmarks/admin/benchmarks/group-tags`, collection-profile edits, `GET /admin/version` |

**Roles.** `user` submits and reads. `admin` manages everything. `super_admin` also manages roles. `service` is an automation account — another platform such as LLM AutoTune — created by the deployment (`seed.py`, from `SERVICE_USERNAME` + `SERVICE_API_KEY`), never granted from the UI. It manages only the benchmarks it created, may read and trigger rolling-dataset builds, may call the preflight endpoints with its API key, and is exempt from the per-user active-submission cap because the platform behind it bounds its own concurrency.

### System

- `GET /health` — Health check. Returns `{"status":"ok"}` when DB and Redis are reachable, else `503`.
- `GET /metrics` — Prometheus metrics (plain text).

### Authentication

- `POST /auth/register` — Register a new user (`username` ≥ 3 chars, `password` ≥ 8 chars). Returns `201`.
  ```json
  { "email": "user@example.com", "username": "user", "password": "secret123" }
  ```
  Response: `{ "access_token": "...", "token_type": "bearer", "role": "user" }`

- `POST /auth/login` — Log in (**JSON body with email/password**).
  ```json
  { "email": "user@example.com", "password": "secret123" }
  ```
  Response: `{ "access_token": "...", "token_type": "bearer", "role": "user" }`

- `GET /auth/me` — Get current user. Requires `Authorization: Bearer <token>`.
  Response: `{ "id": 1, "email": "...", "username": "...", "role": "user" }`
- `POST /auth/change-password` — The signed-in user changes their own password. Body `{ "current_password": "...", "new_password": "..." }` (new password ≥ 8 chars). Verifies the current password; returns `204` on success.

#### Example: Log in as admin and query a submission

```bash
BASE="http://localhost:8080/api"   # or http://localhost:8000 against uvicorn directly

# 1. Log in and save the token
TOKEN=$(curl -s -X POST "$BASE/auth/login" \
  -H "Content-Type: application/json" \
  -d "{\"email\":\"$ADMIN_EMAIL\",\"password\":\"$ADMIN_PASSWORD\"}" | jq -r '.access_token')

# 2. Query submission #42
curl -s "$BASE/submissions/42" \
  -H "Authorization: Bearer $TOKEN" | jq
```

### Modules (Public)

- `GET /modules` — List available test modules.
  Response: `{ "modules": [ ModuleDescriptor, ... ] }`

- `GET /modules/{name}` — Get a single module descriptor.
  Each `ModuleDescriptor` has: `name`, `display_name`, `description`, `params_schema`,
  `default_params`, `metrics_schema`, plus lifecycle flags `deprecated` (bool) and
  `deprecation_note` (string).

### Benchmarks (Requires login)

- `GET /benchmarks` — List all **active** benchmarks.
  Response: `{ "benchmarks": [...] }`

- `GET /benchmarks/{slug}` — Get benchmark detail including modules and metric configs.

### Leaderboard (Requires login)

- `GET /leaderboard/{slug}` — Get the leaderboard for a benchmark (only `done` submissions, ranked by `score_total`).
  Response:
  ```json
  {
    "benchmark_slug": "...",
    "benchmark_name": "...",
    "current_config_hash": "...",
    "total_submissions": 10,
    "modules": [{ "name": "...", "order_index": 0 }],
    "module_names": ["..."],
    "rows": [
      {
        "rank": 1,
        "submission_id": 42,
        "created_at": "...",
        "username": "...",
        "endpoint_model": "...",
        "score_total": 0.95,
        "passed": true,
        "config_hash": "abc123",
        "runs": [
          {
            "module_name": "...",
            "order_index": 0,
            "score": 0.95,
            "passed": true,
            "metrics": { "accuracy": 0.95 }
          }
        ]
      }
    ]
  }
  ```

### Submissions

- `POST /submissions/benchmarks/{slug}/submit` — Submit a model to a benchmark. Returns `202`, status `queued`.
  ```json
  {
    "endpoint_url": "https://...",
    "model": "gpt-4",
    "api_key": "...",
    "contributor": "display name (optional)",
    "cards_per_machine": 8, "machine_count": 1, "card_type": "H100",
    "source_url": "https://autotune.example.com/runs/57 (optional)"
  }
  ```
  `api_key` is encrypted at rest and never returned. `contributor` is the name the
  submission is listed under, when it should not be the account name.

  The hardware fields are optional, but they are what makes the card-normalized
  throughput metrics (`*_tpm_card_norm`) comparable across deployments of
  different sizes; without them an endpoint is assumed to be at the module's
  baseline card count.

  `source_url` is an optional link back to the system that produced the submission (e.g. an
  LLM AutoTune run page). The platform treats it as opaque: it checks only that the
  value starts with `http://` or `https://` and is at most 500 characters (`422` otherwise),
  and never parses or rewrites it beyond that. Absent and empty both mean "no link", and
  neither is an error. It is shown on the submission detail page.

- `POST /submissions/modules/{name}/submit` — Submit a single module ad-hoc (same body). Ad-hoc submissions are private.

- `GET /submissions/me` — List the current user's submissions (array of `SubmissionResponse`, newest first).

- `GET /submissions/{submission_id}` — Get submission detail.
  - Benchmark submissions are readable by every signed-in user (read-only; cancel and worker logs stay owner/admin-only).
  - Ad-hoc module submissions are always private (owner/admin only).
  - Returns full per-module run results and benchmark metadata:
    ```json
    {
      "id": 42,
      "user_id": 1,
      "benchmark_id": 2,
      "module_name": null,
      "endpoint_url": "...",
      "endpoint_model": "...",
      "status": "done",
      "score_total": 0.95,
      "passed": true,
      "error": null,
      "benchmark_config_hash": "abc123",
      "contributor": "team-a",
      "created_at": "...",
      "started_at": "...",
      "finished_at": "...",
      "runs": [
        {
          "id": 1,
          "module_name": "perf_guidellm",
          "params_json": { },
          "status": "done",
          "score": 0.95,
          "passed": true,
          "metrics_json": { },
          "metric_configs_json": [ ],
          "error": null,
          "started_at": "...",
          "finished_at": "...",
          "artifact_path": "...",
          "benchmark_module_id": 7
        }
      ],
      "benchmark_slug": "...",
      "benchmark_name": "..."
    }
    ```

- `POST /submissions/{submission_id}/cancel` — Cancel a `queued` or `running` submission (owner or admin). Returns `409` if already terminal.

- `GET /submissions/{submission_id}/logs/download` — Download the full worker log as a text attachment (owner/admin only). Returns `409` while the run is still in progress.

- `GET /submissions/{submission_id}/logs` — SSE live stream of run progress (see next section). Pass the token as a query param: `?token=<jwt>`.

### Submission status & live progress

There are two ways to track where a submission is:

**1) Poll the status (pull).** `GET /submissions/{id}` returns a `status` field for the overall submission:

| Field | Values |
|-------|--------|
| submission `status` | `queued` · `running` · `done` · `failed` · `canceled` |
| per-module `runs[].status` | `pending` · `running` · `done` · `failed` |

The `runs[]` array is pre-created at submit time, one per module, so a client knows the total
step count and per-step status from the very first poll.

**2) SSE live stream (push) — with fine-grained progress.** Subscribe to
`GET /submissions/{id}/logs?token=<jwt>`. The worker pushes an event each time a module finishes
or makes progress. Each message is `data: <json>\n\n`, where `json.event` is one of:

| `event` | Payload fields | Meaning |
|---------|----------------|---------|
| `connected` | `submission_id` | Stream opened |
| `progress` | `module_name`, `fraction` (0..1), `message` | Fine-grained progress within a module — drives the progress bar |
| `run_complete` | `run_id`, `module_name`, `status`, `score`, `passed`, `metrics`, `metric_configs`, `error` | One module finished |
| `retry` | `attempt`, `max_retries` | Worker redelivery retry |
| `done` | `status` (`done`/`failed`/`canceled`) | Submission reached a terminal state; the stream then closes |

Example:

```bash
curl -N "$BASE/submissions/42/logs?token=$TOKEN"
# data: {"event": "connected", "submission_id": 42}
# data: {"event": "progress", "module_name": "perf_guidellm", "fraction": 0.4, "message": "warming up"}
# data: {"event": "run_complete", "run_id": 1, "module_name": "perf_guidellm", "status": "done", "score": 0.95, ...}
# data: {"event": "done", "status": "done"}
```

### Admin Endpoints

- `GET /admin/version` — Backend image build info + the DB's current alembic revision, for verifying a deploy.
- `GET /benchmarks/admin/benchmarks` — List all benchmarks including drafts (a service account sees only its own).
- `POST /benchmarks/admin/benchmarks` — Create a new benchmark.
- `PUT /benchmarks/admin/benchmarks/{id}` — Update a benchmark.
- `DELETE /benchmarks/admin/benchmarks/{id}` — Delete a benchmark (blocked with `409` if it has submissions).
- `PUT /benchmarks/admin/benchmarks/{id}/lock` — Toggle benchmark lock.
- `GET /benchmarks/admin/benchmarks/{id}/export` — Export benchmark as YAML.
- `POST /benchmarks/admin/benchmarks/import` — Import benchmark from YAML (`multipart/form-data`, field name `file`).

#### User Management (Admin Only)

- `GET /auth/admin/users` — List all users (includes registration time). Admins and super admins may view it.
- `PATCH /auth/admin/users/{id}/role` — **Super admin only.** Grant/revoke admin for a user. Body `{ "role": "admin" }` or `{ "role": "user" }`. Guards: can't demote the last admin; neither `super_admin` nor `service` can be granted or modified via the API (this also blocks super admins from revoking each other). A super admin is set by direct DB access inside the backend container; a service account by `seed.py`. Plain admins can view the user list but cannot change anyone's role.

#### Example: Create a benchmark

A minimal-but-complete request: one `perf_guidellm` module, scored on output TPS, with uptime as a redline. Field reference is below the body.

```bash
# Obtain $TOKEN as an admin (or use a service account's API key) first
curl -s -X POST "$BASE/benchmarks/admin/benchmarks" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "slug": "throughput-demo",
    "name": "Throughput Demo",
    "description": "Single-module GuideLLM throughput benchmark.",
    "status": "active",
    "modules": [
      {
        "module_name": "perf_guidellm",
        "weight": 1.0,
        "order_index": 0,
        "params_json": {
          "backend": "guidellm",
          "concurrency": 8,
          "input_tokens": 1024,
          "output_tokens": 512,
          "max_seconds": 120
        },
        "metric_configs": [
          { "key": "output_tps",  "role": "score",   "formula": "ratio_capped", "baseline": 200, "weight": 1.0 },
          { "key": "uptime",      "role": "redline", "min_val": 0.95 },
          { "key": "ttft_p99_ms", "role": "display" }
        ]
      }
    ]
  }' | jq
```

Field reference:

| Field | Notes |
|-------|-------|
| `slug` | URL id; lowercase letters / digits / hyphens only (`^[a-z0-9-]+$`), globally unique |
| `status` | `draft` (default, hidden from the UI) / `active` (shows in lists + leaderboard) / `archived` |
| `modules[].module_name` | Must be one of `GET /modules` |
| `modules[].params_json` | Module params; missing fields fall back to the module default — see `params_schema` from `GET /modules/{name}` |
| `modules[].weight` | Module weight 0–1; **weights should sum to 1.0 across modules**. `order_index` sets display order |
| `modules[].metric_configs[]` | Per-metric evaluation rules (below) |

Each `metric_configs[]` entry:

- `key` — metric name, from the module's `metrics_schema` (`GET /modules/{name}`).
- `role` — `score` (counts toward the score) / `redline` (pass/fail gate only) / `display` (informational).
- A `score` rule uses `formula` + `baseline`: e.g. `ratio_capped` = `min(value / baseline, 1.0)`.
- A `redline` rule uses `min_val` / `max_val` thresholds.

Returns `201` with the full `BenchmarkResponse` (including the generated `id` and `config_hash`). A duplicate slug returns `409`.

---

## Functional acceptance checks

The `functional_acceptance` module's 56 checks — what each one sends and what
it accepts — are listed in [docs/functional-acceptance-checks.md](../docs/functional-acceptance-checks.md).
