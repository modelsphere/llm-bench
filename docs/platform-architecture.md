# LLM Benchmark Platform — Architecture

## Overview

A modular, multi-tenant web platform for evaluating LLM services. Admins compose benchmarks from pluggable test modules; users submit their LLM endpoints and receive scored results ranked on a public leaderboard.

The system has two distinct layers:

```
bench/               platform/frontend/
  Module layer         Vue 3 SPA
  (pure Python,       (TypeScript,
  no deps)             Tailwind)

platform/backend/
  FastAPI + SQLAlchemy + Dramatiq
```

The `bench/` module layer is a library imported by the backend worker — it has no platform dependencies and can be run standalone.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Frontend | Vue 3, TypeScript, Tailwind CSS, Pinia, Vue Router, Axios |
| Backend | FastAPI, SQLAlchemy 2.x (async), Alembic, Pydantic |
| Database | PostgreSQL (async via asyncpg, sync via psycopg2) |
| Task Queue | Dramatiq + Redis |
| Auth | JWT (python-jose), bcrypt 4.x, Fernet encryption |
| Python | 3.12+ with `uv` for package management |

---

## Architecture

### Backend (`platform/backend/`)

```
app/
  api/          FastAPI routers (auth, modules, benchmarks, submissions, leaderboard)
  core/         Config (pydantic-settings), auth (JWT), security (Fernet)
  db/           SQLAlchemy 2.x models + async session dependency
  schemas/      Pydantic request/response models
  queue/        Dramatiq broker + job actors
alembic/versions/
  001_initial   All tables in their final form, plus reference data: the
                card-type list and test_modules (from
                bench.modules.MODULE_REGISTRY). Later schema changes are new
                revisions on top; CI keeps models and migrations in agreement.
seed.py         Run after migrating: the first admin (from required env —
                there is no default password), the perf-suite-v1 benchmark
                (from benchmarks/perf-suite-v1.yaml), and optionally a
                `service` account for another platform (SERVICE_API_KEY).
```

**API routes:**

| Method | Path | Auth | Description |
|---|---|---|---|
| POST | `/auth/register` | — | Register new user |
| POST | `/auth/login` | — | Login, returns JWT |
| GET | `/auth/me` | JWT | Current user profile |
| GET | `/modules` | JWT | List all module descriptors |
| GET | `/modules/{name}` | JWT | Single module details |
| GET | `/benchmarks` | JWT | List active benchmarks |
| GET | `/benchmarks/{slug}` | JWT | Benchmark detail |
| POST | `/benchmarks/{slug}/submit` | JWT | Submit endpoint for benchmark |
| POST | `/modules/{name}/submit` | JWT | Submit ad-hoc single module |
| GET | `/submissions/me` | JWT | User's submission history |
| GET | `/submissions/{id}` | JWT | Submission detail with runs |
| GET | `/submissions/{id}/logs` | JWT | SSE stream of run events via Redis pub/sub |
| POST | `/submissions/{id}/cancel` | JWT | Cancel queued/running |
| GET | `/leaderboard/{slug}` | JWT | Ranked leaderboard for benchmark |
| POST | `/benchmarks/admin/benchmarks` | Admin | Create benchmark |
| GET | `/benchmarks/admin/benchmarks` | Admin | List all benchmarks |
| PUT | `/benchmarks/admin/benchmarks/{id}` | Admin | Update benchmark |
| GET | `/health` | — | Health check |

---

### Data Models

```
users
  id, email, username, password_hash, role (admin|user), created_at

test_modules          ← synced from bench/modules/ on backend startup
  name (PK), display_name, description,
  params_schema_json (pydantic JSON schema),
  default_params_json, version

benchmarks
  id, slug, name, description, version,
  status (draft|active|archived),
  group_tags (JSONB list of "Top/Mid/Low" paths — display grouping only),
  created_by_user_id → users.id, created_at

benchmark_modules     ← module instances within a benchmark
  id, benchmark_id → benchmarks.id,
  module_name → test_modules.name,
  params_json (admin-locked), weight, order_index

submissions           ← a user's test run
  id, user_id → users.id,
  benchmark_id → benchmarks.id | NULL,
  module_name → test_modules.name | NULL,
  endpoint_url, endpoint_model,
  endpoint_api_key_enc (Fernet),
  extra_params (JSONB) — optional submission-level knobs,
                         e.g. {"concurrency_override": N}
  description_summary — optional one-liner (≤100 chars) by the submitter,
                        shown on the leaderboard + detail page
  description_detail  — optional markdown (≤5000 chars), rendered on the
                        submission detail page
  source_url          — optional http(s) link (≤500 chars) back to the system
                        that produced the submission; shown on the detail page
  status (queued|running|done|failed|canceled),
  score_total, passed, error,
  started_at, finished_at, created_at

submission_runs       ← per-module execution within a submission
  id, submission_id → submissions.id,
  module_name, params_json,
  status (pending|running|done|failed),
  score, passed, metrics_json, error,
  artifact_path, started_at, finished_at
```

---

### Module Registry Sync (Startup Hook)

On every backend startup, the FastAPI lifespan hook imports `bench.modules.MODULE_REGISTRY` and upserts each entry into `test_modules`. This keeps the DB descriptors in sync with the latest module code without requiring a migration.

```
bench.modules.MODULE_REGISTRY
    → app/main.py lifespan
    → get_async_session
    → UPSERT INTO test_modules
```

---

### Auth Flow

1. User registers or logs in → receives JWT with `{sub: user_id, role: "admin"|"user"}`
2. Frontend stores JWT in `localStorage`
3. Axios interceptor attaches `Authorization: Bearer <token>` to every request
4. Backend `get_current_user` dependency decodes token, queries `users` table, returns `User` model
5. `require_admin` enforces admin-only endpoints; `require_service_or_admin` plus
   `ensure_can_manage` let a `service` account (another platform, e.g. LLM
   AutoTune) manage only the benchmarks it created
6. API keys stored in submissions are encrypted at rest with Fernet (`PLATFORM_SECRET_KEY`)

---

### Submission Flow

```
User submits benchmark
    → POST /benchmarks/{slug}/submit
    → FastAPI: create Submission (status=queued)
    → Dramatiq: enqueue run_submission message
    → Worker (one of P processes): dequeue, run modules sequentially
        → write heartbeat key to Redis (TTL 60s, re-written every 20s)
        → get_module(name).run(endpoint, params, output_dir)
        → write SubmissionRun rows per module
        → aggregate weighted score → Submission.score_total
        → delete heartbeat key
```

For single-module ad-hoc submissions, the same flow runs with `benchmark_id=NULL`.

**Extra params (`extra_params`).** The submit body carries an optional, nested
`extra_params` object — a forward-compatible bag for submission-level knobs, so
new options can be added without changing the top-level request shape or the DB
schema (it is stored as JSONB on `submissions.extra_params`). Today it holds two
**mutually exclusive** keys (schema-enforced — setting both is a 422):

- `concurrency_override` — one integer that overrides the `concurrency` param of
  **every** module in the run that has one. The API clamps it to
  `[1, MAX_ALLOWED_CONCURRENCY]` (a `pydantic-settings` value, default 128) on
  submit; the worker then rebuilds each module's validated params before
  `run()`, clamping again to that module's own declared range, and logs the
  effective value per module. The set of modules a value affects is derived live
  from each module's schema (`TestModule.concurrency_param_name`, which also
  aliases differently-named fields such as opencompass's `max_workers`) and
  surfaced as `supports_concurrency_override` on the module/benchmark APIs, so
  the submit page lists the affected modules automatically. Because the override
  is a per-submission value applied to freshly-built params (and the few modules
  that shell concurrency out to env vars save/restore them), it never leaks into
  later runs on the same long-lived worker process.
- `module_concurrency_overrides` — the per-module alternative: a
  `{module_name: int}` map; modules absent from the map keep their configured
  concurrency. Values get the same two-stage clamp; the submit endpoints 400 on
  module names not part of the submission. Both mechanisms funnel through one
  resolution point (`app.core.module_caps.resolve_concurrency_override`) used by
  the worker (apply + record) and the per-run report on the detail API, so they
  can never disagree. The submit page renders the choice as a radio group
  (module defaults / one global value / per-module values).

**Card-normalized TPM.** Perf modules that declare a
`TestModule.card_norm_baseline` (perf_guidellm, perf_guidellm_sweep,
replay — all 8) get display-only `*_tpm_card_norm` metrics computed by
the worker after each run: raw TPM (or TPS × 60) × baseline ÷ total card count
(`cards_per_machine × machine_count` from the submission's hardware section;
assumed to be at the baseline when absent). Computed platform-side because the
bench layer never sees the submission; injected via
`ModuleResult.extra_display_configs`, so they can't affect scoring/redlines.
Pure math lives in `app/core/card_normalize.py`. The submission-detail page
explains them with hover tooltips (`metricTips.*` i18n namespace — the general
mechanism for metric explanations).

To add a future option: add a field to `SubmissionExtraParams`, normalize it in
`_normalize_extra_params`, and read it where it applies — no migration needed.

**Descriptions (`description_summary` / `description_detail`).** The submit body
also carries two optional, submitter-authored description fields for the service
under test and the optimizations behind the run:

- `description_summary` — one line, capped at 100 characters (code points, so
  the cap is the same for ASCII and CJK). Shown in the leaderboard's "Summary"
  column and in the submission-detail header.
- `description_detail` — markdown, capped at 5000 characters. Rendered on the
  submission detail page. Rendering goes through `marked` + DOMPurify
  (`frontend/src/utils/markdown.ts`) — sanitization is mandatory, since details
  are shown to other users on public submission pages.

Both are trimmed on the backend and stored as NULL when blank; the caps live as
constants in the submission schema module and are mirrored by `maxlength`
attributes on the submit forms.

**Provenance (`source_url`).** The submit body also takes one optional URL
pointing back at the system that generated the submission — for programmatic
submitters (e.g. LLM AutoTune) this is the run page holding the engine config
and the search behind the numbers. Without it, a leaderboard row is three ids
and a score, with no way back to what produced them.

The value is deliberately **opaque**: the platform validates only that it starts
with `http://` or `https://` and fits the 500-char cap (422 otherwise, since it
is rendered as a link), and never parses, normalizes or rewrites it. That is
what keeps the field useful to any submitter rather than to one integration.
Absent and empty both mean "no link" and neither is an error.

It shows up as a "Source" link in the submission-detail header.

---

### Worker Resilience

Long-running benchmark modules (especially `perf_guidellm`) can OOM, segfault in native dependencies, or otherwise terminate the worker abruptly. The platform isolates these failures so a single bad task never takes down the queue.

**Process isolation.** Dramatiq runs as `--processes P --threads T` (defaults: `WORKER_PROCESSES=4`, `WORKER_THREADS=1` — one task per OS process). A fatal failure kills only its own process; the dramatiq master respawns it automatically within seconds. Both knobs are configurable via Helm (`app.worker.processes`, `app.worker.threads`) and env vars (`WORKER_PROCESSES`, `WORKER_THREADS`).

**Heartbeat + reaper.** Each worker writes `worker:submission:<id>` to Redis with a 60s TTL when it picks up a task and **re-writes the whole key** every 20s from a daemon thread (`app/queue/heartbeat.py`). The backend runs an async reaper (`app/core/reaper.py`) every 30s that scans `RUNNING` submissions: any with no heartbeat (past a 90s grace period) is marked `FAILED` with `error="Worker process crashed unexpectedly"`, in-flight `SubmissionRun` rows are also marked `FAILED`, and `run_complete` + `done` SSE events are published so open UI tabs update immediately.

> The refresh must `SET`, not `EXPIRE`. Redis here is a single replica with no persistence, so a Redis restart drops every heartbeat; `EXPIRE` is a silent no-op on a missing key, so an EXPIRE-only refresh could never recreate it and the reaper would mark **every in-flight submission** `FAILED` while its worker ran happily to completion (the cancel-watcher only reacts to `CANCELED`, so nothing stops it). Guarded by `tests/test_deploy_skew.py`.

**Orphaned-QUEUED recovery.** The same reaper re-enqueues submissions stuck `QUEUED` with no queue marker (`queued:submission:<id>`), which is how a Redis restart's lost Dramatiq messages are recovered. It claims each submission with `SET NX` **before** sending: the reaper runs in every backend replica, so an unguarded check-then-act lets two replicas both re-enqueue and the submission runs twice concurrently against the user's endpoint. A failed send releases the claim.

**Cancel-watcher safety.** Each in-flight submission spawns a daemon thread that polls for `CANCELED` status. The watcher uses its **own** short-lived SQLAlchemy session per poll — never the task's session — because SQLAlchemy `Session` is not thread-safe. The watcher is bounded by both a `cancel_event` and a `stop_event`; `_run_modules` always joins the thread in a `finally` block so threads do not leak across submissions.

**Top-level guard.** `run_submission` wraps its body in a `BaseException` handler that opens a fresh session and marks the submission `FAILED` with a `"Worker crashed"` error before re-raising — a belt-and-suspenders backstop for the (rare) Python-level escape that the heartbeat reaper would otherwise have to catch a heartbeat-interval later.

---

### Frontend (`platform/frontend/src/`)

```
api/client.ts     Axios instance + JWT interceptor + all typed API calls
stores/
  auth.ts         Pinia: token, user, login, logout, fetchMe
  benchmarks.ts   Pinia: benchmarks list, modules list, leaderboard
  submissions.ts  Pinia: submission polling
router/index.ts   Vue Router with auth guard + admin guard
views/
  Login.vue, Register.vue
  BenchmarkList.vue       Active benchmarks card grid
  BenchmarkDetail.vue     Benchmark info + leaderboard
  BenchmarkSubmit.vue     Submit endpoint for a benchmark
  ModuleSubmit.vue        Ad-hoc single module run
  SubmissionDetail.vue    Polling + per-run metrics
  MySubmissions.vue      User's submission history
  admin/BenchmarkList.vue Admin benchmark table
  admin/BenchmarkEditor.vue  Create/edit benchmark with module builder
components/
  LeaderboardTable.vue   Sortable ranked table
  MetricCard.vue         Score + pass/fail display
  ModuleParamForm.vue    Dynamic form from JSON schema
  SubmissionCharts.vue   Score + latency bar charts (Chart.js)
```

**Navigation guards:**
- Routes with `meta: { auth: true }` → redirect to `/login` if unauthenticated
- Routes with `meta: { admin: true }` → redirect to `/benchmarks` if non-admin

**Submission streaming:** `SubmissionDetail.vue` opens an `EventSource` to `GET /submissions/{id}/logs` (SSE via Redis pub/sub) for live run completion events. A 3-second polling fallback ensures reliability even if SSE fails. After completion, `SubmissionCharts.vue` renders per-module score and latency charts using Chart.js.

---

## Key Design Decisions

### 1. Module params locked at benchmark level

Users only supply their endpoint URL, model name, and API key. Module hyperparameters (concurrency, timeouts, etc.) are admin-defined and immutable per benchmark. This ensures fair, reproducible comparisons across all submissions.

### 2. Bench module layer is a pure library

`bench/modules/` has zero platform dependencies. It can be unit-tested in isolation (`pytest tests/modules/`), run standalone, and is imported as a library by the Dramatiq worker. Adding a new test type requires only dropping a new file into `bench/modules/` and restarting the backend.

### 3. Async everywhere on the hot path

FastAPI uses `asyncpg` + SQLAlchemy 2.x async throughout. The Dramatiq worker uses a separate synchronous SQLAlchemy session since Dramatiq actors run in-process threads, not async tasks.

### 4. JSONB for all flexible params

`params_schema_json`, `params_json`, `metrics_json` are all `JSONB`. PostgreSQL JSONB is indexed and allows partial updates without schema migrations. Schemas are stored as pydantic JSON schema dicts — the same format used to generate the frontend's dynamic `ModuleParamForm`.

### 5. SQLAlchemy Enum `values_callable`

Python `enum.Enum` members are `ADMIN`/`USER` but PostgreSQL stores lowercase `"admin"`/`"user"`. All `Enum` columns use `values_callable=lambda cls: [e.value for e in cls]` so SQLAlchemy writes and reads the lowercase string values.

### 6. Weighted score aggregation

Benchmark `score_total = Σ(module_score × module_weight) / Σ(weights)`. Weights sum to 1.0 (enforced by frontend form validation). Individual module scores are normalized 0.0–1.0 by each module's own scoring logic.

### 7. Fernet encryption for API keys

`PLATFORM_SECRET_KEY` (32-byte Fernet key) encrypts API keys before storing in `submissions.endpoint_api_key_enc`. Decryption happens only inside the Dramatiq worker process at runtime.

### 8. Replay streams its dataset — memory is bounded by concurrency, not dataset size

The replay datasets are production traffic captures: multi-GB JSONL files (2.6 GB is not unusual) of requests carrying 80k–170k-token prompts. Loading one into RAM OOM-killed workers. The amplifier was record retention — each record held the payload three times over (`request_body` raw string + `request_json` parsed + `raw_payload`, the original log dict that re-contains both) and parsed JSON costs roughly 3–5× its serialized size, so a 2.6 GB file went 8–15 GB resident. Worker pods run several Dramatiq processes (see *Worker Resilience*), so a handful of colocated replay submissions share one container's memory limit and blow it together.

Three properties keep it bounded:

**Lean loading.** The replay run path loads records without the parsed `request_json` / `raw_payload` fields, keeping only the raw request body. Every consumer that needs the parsed form already falls back to re-parsing the body on demand, so this trades a little CPU for a large cut in resident memory. The standalone replay CLI keeps the full load.

**Streaming dispatch.** The batch is *planned* without being loaded — a sizing pass yields a record count plus a factory that returns a fresh lazy iterator (callers needing a second pass simply ask for another). Requests are then dispatched through a **bounded in-flight window** — a small multiple of `concurrency` — topped up as futures complete, so each finished request's record becomes garbage immediately. **Peak memory is O(window), not O(dataset)**: the dataset never has to fit in RAM. Results are kept as one small dict per position, with an empty dict meaning "never dispatched" — the contract the aggregator uses to split attempted from not-started when a time budget cuts the batch short.

**A response-size ceiling.** Each request buffers its streamed SSE events before processing them, so one non-terminating generation could grow that buffer without bound. An absolute cap applies even when no per-request generation cap is configured; tripping it stops reading (closing the stream also aborts the request server-side) and marks the request as capped.

Tuning knobs are env vars read at import time (`REPLAY_*`) — like the rest of the replay module's settings, they must be set on the worker and the worker restarted; mutating `os.environ` at run time has no effect.

**Why streaming is safe on slow storage.** The datasets often live on a network filesystem that can stall for seconds (see `docs/deploying.md`), so it is worth being explicit about the I/O tradeoff:

- Both the sizing pass and the replay pass are **large sequential reads** — the access pattern a network FS or spinning disk handles best. Streaming introduces no random I/O, no re-seeking, and no re-reading of records.
- Sizing costs **one extra full pass** on the uncapped path (the capped path always needed it). That pass reads in binary with a large buffer and only counts lines — it never decodes UTF-8 or parses JSON — and it warms the page cache for the pass that follows.
- The batch is **LLM-bound, not disk-bound.** Requests take seconds each at single-digit concurrency, so records are consumed far more slowly than any disk can supply them. Streaming spreads the same total I/O across the run instead of front-loading it into a startup spike, which is if anything gentler on shared storage.
- The one real behavioural change: **an FS stall now lands mid-run**, on the dispatch loop, instead of at startup. That loop also polls cancellation and the time budget, so a long stall delays those checks. The in-flight window is the buffer against this — the pool keeps working through a stall for as long as it has queued records, which is exactly why the window defaults to a *multiple* of concurrency rather than to concurrency itself. Shrink it only with that tradeoff in mind.

### 9. Replay datasets can roll — but a run is always pinned to one build

A replay dataset can be a fixed file, or a benchmark can follow a **collection
profile**: a named, continuously-rebuilt dataset assembled from live gateway
traffic. The traffic comes from a log source (`app/datasets/sources.py`): the
hourly files the gateway's bodylog listener writes (the default), or a
VictoriaLogs store the same records were shipped to. Both yield identical
records, so the rest of the pipeline does not know which one it is reading.

```
bodylog files / VictoriaLogs ──windowed, streamed──▶ dataset-builder pod
                                                       │ convert + sample
                     datasets PVC (RWX)  ◀─────────────┘ atomic publish
                       replay/auto/<profile>/latest.json  ← pointer
                                          builds/<id>.jsonl
         postgres ◀── profiles + build history ── admin "Replay Datasets" page
```

**The pointer file is the source of truth; the database is a mirror.** That is
what keeps `bench/` a pure library: `bench/replay_test/dataset_feed.py` resolves
a profile by reading one small JSON file, so the worker, the standalone CLI and
an operator with `cat` all agree on which build is current. The DB rows
(`replay_dataset_profiles`, `replay_dataset_builds`) hold collection *policy* and
history, and a benchmark still runs when they are mid-migration.

**Publishing is atomic in both steps.** The dataset is staged on the same
filesystem, fsync'd, and `os.replace`d into `builds/`; only then is a new pointer
fsync'd and `os.replace`d onto `latest.json`. A reader sees the old build or the
new one — never a partial file, never a pointer to a dataset that isn't fully on
disk.

**A run pins one build, before the module starts.** `_resolve_dataset_feed` in
the worker resolves the profile once — at the start of *that module*, not at
submission start, so a replay queued behind a 16h opencompass run resolves when
it actually begins — rewrites `dataset_path`, and records the build on
`run.params_json['dataset_resolved']`. This is not a convenience: `ReplayTest`
re-opens its dataset by path several times per run (a sizing pass, the bounded
preflight scan, and every `make_iter()` in the streaming dispatch), so a publish
landing between two passes would otherwise have the run *size* against one
dataset and *replay* another. Pinning makes a mid-run publish a non-event, and
the recorded provenance is what makes a rotating dataset acceptable at all — any
score can be traced to the exact bytes behind it.

The pin is a **hard link** into `pins/`, and the run reads from that path. A
replay can run for hours, and retention deleting `builds/<id>.jsonl` between two
of its re-opens would fail a healthy benchmark. Retention already avoids that by
policy (an age floor, a keep count, a query for in-flight pins) but policy is
configurable; a link makes it structural, since unlink drops one name and the
inode survives while the pin exists. It is a link and **not a copy** on purpose —
datasets are 0.25–3GB and several replays share a worker pod, so copying would
multiply both the volume's read I/O and the pod's ephemeral disk for no added
safety. The pin is released as soon as its own module finishes (not at the end
of the submission — a later module can run for many more hours), and a sweeper
mirroring `_sweep_stale_output_dirs` collects pins left by a worker that died.
Note a pin's age comes from its filename: hard links share inode timestamps, so
`st_mtime` there is when the *dataset* was written, not when the pin was taken.

**A bad collection never displaces a good dataset.** Publication is gated on a
minimum record count, a minimum number of non-empty input-length buckets, and a
full re-read of the staged file through the real replay loader. A failure leaves
the previous pointer untouched.

**Bounded everywhere.** The window is split into sub-windows and each is
reservoir-sampled to a quota (shortfall carried forward), so memory is O(one
slice) and the network is O(sample), not O(window) — a day of traffic is many GB
of request bodies. A free-space precondition refuses to start a build that could
fill the shared datasets volume, and retention keeps the newest N builds while
never deleting the current one, one pinned by an in-flight submission, or one
young enough that a run may have just resolved it.

**Staleness warns, it does not block.** A build older than the profile's
`max_age_hours` still runs; the worker logs a warning, the run records
`stale: true`, and the admin page shows the profile amber. Availability was the
deliberate choice — the mitigation is provenance and visibility, not refusal.
A *missing* feed (nothing ever published) does fail the run, loudly: quietly
replaying some other fixed file would produce a plausible number that means
nothing.

Collected records carry the request and `resp_meta.usage`; response bodies are
dropped and credential headers stripped, so continuously mirroring production
traffic onto a shared volume doesn't also accumulate production secrets.

**Datasets are stored gzipped** (`.jsonl.gz`), which measured 3.3-3.6x on real
collected builds — the difference between a rolling feed sitting comfortably on
the shared volume and crowding out the curated datasets already there. Readers
dispatch on the extension (`bench/replay_test/jsonl_io.py`), so a `.jsonl` and a
`.jsonl.gz` are interchangeable everywhere and existing datasets keep working
untouched. Decompression lands twice per run — the sizing pass and the streaming
dispatch — at roughly 3.5s per 500MB, which is noise beside an LLM-bound batch.
gzip rather than zstd because it is in the standard library and therefore works
in every image, on the dev box, and in a bare checkout with nothing to install.

Two sharp edges the layout has to respect, both of which had a bug caught by a
test: a staged build is named `<id>.jsonl.gz.part` and published by *rename*, so
the staging name must resolve to the same codec as its final name; and build ids
must be recovered with a double-suffix strip, since `Path.stem` on
`b1.jsonl.gz` yields `b1.jsonl` and would make every retention comparison miss.

**Which dataset a run replayed is a metric, not just a param.** The worker stamps
`dataset_id` and `dataset_sha256` (string, display-only) onto the run's metrics
from the same provenance block it pinned, so the two can never disagree. With a
rolling dataset this is the difference between comparable and incomparable
numbers: two submissions started minutes apart can legitimately have replayed
different builds, and the metrics view is where people actually compare runs.

Defaults keep every existing benchmark byte-identical: `dataset_source` defaults
to `fixed`, and a benchmark saved before this feature has none of the new keys.

---

### 10. Benchmark grouping is a frontend concern

The benchmarks page groups cards by `Benchmark.group_tags`, a flat list of
`Top/Mid/Low` paths (1–3 levels, top-down: a lower level cannot exist
without the ones above it). The backend only stores and validates the string
shape (`normalize_group_tags`); it has no group entity, no hierarchy, and the
field is deliberately outside the config hash and editable while locked. The
tree, the "Not grouped" bucket, and the cards / list / flat view toggle are
all built client-side (`utils/groupTags.ts`). A benchmark with several tags
appears under each group. Pitfall: tag matching is case-sensitive, so
"LLM" and "llm" are two groups — the editor autocompletes from tags already
in use to keep spellings converging.

Because there is no group entity, editing the grouping *as a whole* is a
cross-benchmark operation: renaming a group means rewriting that path prefix on
every benchmark carrying it. That is what the admin benchmarks page's "Groups"
tab does — it edits a client-side draft of every benchmark's tag list (tree on
the left, tick-box membership on the right) and saves the rows that changed
through one transactional endpoint, so a rename can never be half-applied and
split one group in two. Consequence worth knowing: a group created there with
no benchmarks in it has nowhere to be stored and disappears on save (the tab
says so before you save).

## Project Layout

```
llm-bench/
  bench/
    modules/             MODULE_REGISTRY and the nine modules (one file each)
    tests/               The test implementations the modules drive
                         (functional/ → the functional_acceptance 10+46
                          checks; see docs/functional-acceptance-checks.md)
    replay_test/         Replay dataset tooling: conversion, the rolling feed
    examples/            Tiny datasets the modules default to
  benchmarks/
    perf-suite-v1.yaml   Benchmark definition (5 modules, weights, defaults)
  platform/
    backend/
      app/
        api/              FastAPI routers
        core/             config, auth, security
        db/               models, session
        schemas/          Pydantic models
        queue/            Dramatiq broker + jobs + heartbeat middleware
        core/reaper.py    Async crashed-worker reaper
      alembic/versions/   Migrations
      docker-compose.yml  postgres + redis for local development
      Dockerfile
    frontend/
      src/
        api/              Axios client
        stores/           Pinia stores
        router/           Vue Router
        views/            Page components
        components/       Shared components
      vite.config.ts      Proxy to backend
```

---

## Development Workflow

See [platform/README.md](../platform/README.md): Postgres and Redis in Docker,
`seed.py` with your own admin credentials, then `python dev.py`.

---

## CLI (`platform/cli.py`)

```bash
# Login
python platform/cli.py login --email "$ADMIN_EMAIL" --password "$ADMIN_PASSWORD"

# List benchmarks and modules
python platform/cli.py benchmarks
python platform/cli.py modules

# Submit an endpoint (full benchmark or single module)
python platform/cli.py submit --benchmark perf-suite-v1 \
    --url http://localhost:8001/v1 --model mock

# Tail live output
python platform/cli.py tail <submission-id>

# Who am I / logout
python platform/cli.py whoami
python platform/cli.py logout
```

Token is stored in `~/.llmbench_token` (mode 0600).
