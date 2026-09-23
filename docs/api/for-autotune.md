# What LLM AutoTune relies on

[LLM AutoTune](https://github.com/modelsphere/llm-autotune) measures every
configuration it tries by submitting the running endpoint to an LLMBench
benchmark. This page is the contract between the two: the routes, fields and
names AutoTune reads. Changing any of them is a breaking change for AutoTune and
belongs in the changelog. Anything not listed here is free to change.

`GET /api/openapi.json` is the authoritative schema; this page says which parts
of it another platform depends on, and why.

## Identity

AutoTune authenticates with the API key of a **service account**
(`Authorization: Bearer llmb_…`), seeded by the deployment — see
[deploying.md](../deploying.md#connecting-llm-autotune). Login with a password
is supported (`POST /auth/login`, body `{"email", "password"}`) but not needed.

A service account may:

| | |
|---|---|
| submit and read submissions | like any user, without the per-user active-submission cap |
| create, import, update, lock, export and delete benchmarks | only ones it created (`created_by_user_id` = itself); others answer 403 |
| list benchmarks in the admin view | sees only its own |
| read collection profiles and builds, trigger a build | yes |
| call `POST /submissions/preflight` with its key | yes (a person's key may not) |
| create or edit profiles, freeze datasets, manage users or card types | no |

## Routes

| Call | Used for |
|---|---|
| `GET /benchmarks` | Which benchmarks exist and which modules each runs (`benchmarks[].slug`, `benchmarks[].modules[].module_name`, `.params_json`). Only **active** benchmarks are listed; a draft or archived one also answers 404 on submit. |
| `GET /auth/me` | Which account the key belongs to (`id`), to tell AutoTune's own benchmarks from anyone else's. |
| `GET /benchmarks/{slug}` | One benchmark whatever its status, with `id`, `is_locked` and `created_by_user_id`; `404` when the slug is free. |
| `POST /benchmarks/admin/benchmarks/import` | Creating AutoTune's own benchmarks from a template — the same YAML `…/{id}/export` writes. |
| `PUT /benchmarks/admin/benchmarks/{id}/lock` | Locking them, so the configuration a campaign measured against cannot move. The route **toggles**; call it only on an unlocked benchmark. |
| `POST /submissions/preflight` | Checking an endpoint answers before committing a run. `{"endpoint_url", "model", "api_key"}` → `{ok, checks[]}`. |
| `POST /submissions/benchmarks/{slug}/submit` | One run. Returns `202` with `id`. |
| `GET /submissions/{id}` | Polling it. |
| `POST /submissions/{id}/cancel` | Giving up on it. `409` once it is already terminal. |
| `GET /replay-datasets/profiles`, `…/profiles/{id}/builds`, `…/builds/{id}` | Pinning a campaign to one rolling-dataset build. |
| `POST /replay-datasets/profiles/{id}/build` | Asking for a fresh build. `409` while one is already running. |

**Busy is not failure.** `429` (active-submission cap — not applied to service
accounts) and `503` (a cordon during a disruptive upgrade) mean "try again
later"; a client should retry them, not record a failed run.

## The submit body

```json
{
  "endpoint_url": "http://10.0.0.5:30000/v1",
  "model": "served-model-name",
  "api_key": "",
  "cards_per_machine": 4,
  "machine_count": 1,
  "card_type": "H100",
  "description_summary": "tp=4 chunked_prefill_size=8192",
  "description_detail": "markdown: the full configuration",
  "source_url": "https://autotune.example.com/runs/1234"
}
```

- **The hardware fields drive card normalization.** The worker adds
  `*_tpm_card_norm` metrics — throughput rescaled to the module's baseline card
  count — using `cards_per_machine × machine_count`. Send the cards the
  configuration actually occupies (tp × dp × pp), not the size of the box, or
  configurations of different widths will not compare. Omitted, an endpoint is
  assumed to be at the baseline already.
- `source_url` must be `http(s)://…`, at most 500 characters; anything else is
  a 422.

## Reading a result

`GET /submissions/{id}`:

| Field | Meaning |
|---|---|
| `status` | `queued` · `running` · `done` · `failed` · `canceled` |
| `passed` | `true` only if every module ran and passed. **`null` when no module produced a result** — treat that as a failure, not a pass. |
| `score_total` | Weighted mean of the module scores. |
| `runs[]` | One per module of the benchmark, created at submit time. |
| `runs[].benchmark_module_id` | Which module of the benchmark produced this run. A benchmark may contain the same module twice (two sweeps of different shapes); this is how to tell them apart. Do not rely on the order of `runs`. |
| `runs[].module_name`, `.status`, `.passed`, `.score`, `.error` | Per module. |
| `runs[].metrics_json` | Raw metrics; see below. |
| `runs[].metric_configs_json` | The rules that judged them: `{key, role, min_val, max_val, …}`, `role` ∈ `score` · `redline` · `display`. |
| `runs[].params_json` | The parameters the module ran with (credentials redacted for non-admins). |

## Names

**Modules** a benchmark can contain, as they appear in `module_name`:
`perf_guidellm`, `perf_guidellm_sweep`, `replay`, `functional_acceptance`,
`case_truncation`, `opencompass`, `agentic`, `hallucination`,
`tool_call_success`.

**Metrics AutoTune reads**, by module:

| Module | Keys |
|---|---|
| `perf_guidellm_sweep` | `input_tps`, `output_tps`, `total_tps_mean`, `request_output_tps`, `ttft_p50_ms` / `_p90_ms` / `_p99_ms`, `itl_p99_ms`, `tpot_p99_ms`, `uptime`, `reported_concurrency`, `reported_level_meets_slo`, `http_status_<code>`, plus a nested group per concurrency level (`c8`: {…}) |
| `replay` | `input_tpm`, `uncached_input_tpm`, `cached_tpm`, `output_tpm`, `cache_hit_rate`, `ttft_p50_ms` / `_p99_ms`, `total_time_p99_ms`, `uptime`, `error_rate`, `unfinished_rate`, `http_200_count`, `judge_good_acc_rate`, `judge_halluc_clear_rate`, `dataset_id`, `dataset_sha256` |
| `functional_acceptance` | `pass_rate`, and one `0/1` metric per check |
| any throughput module | `input_tpm_card_norm`, `output_tpm_card_norm`, `total_tpm_card_norm`, `cached_tpm_card_norm` — added by the worker, display-only |

Notes that matter to a client:

- **TTFT percentiles are p50, p90, p99 and mean.** There is no p95.
- **TTFT is time to the first token of any kind** — for a reasoning model, the
  first reasoning token, not the first answer token.
- `http_status_<code>` counts every HTTP response the load generator received,
  including requests in flight at the edges of the measured window, so it can
  exceed `measured_requests`.
- `dataset_id` and `dataset_sha256` identify the exact replay dataset a run
  used. The hash is the first **16** hex characters of the build's SHA-256; the
  build API reports all 64. Compare on the shorter.
- A module run for a rolling profile records the resolved build in
  `params_json.dataset_resolved`.

## Benchmarks from templates

AutoTune creates the benchmarks it needs by importing a template — a benchmark
export YAML — with its own slug, name and description, and locks it. The import
format is exactly what `GET /benchmarks/admin/benchmarks/{id}/export` produces:

```yaml
slug: autotune-screen-v1
name: …
description: …
version: "1"
status: active
group_tags: []
modules:
  - module_name: perf_guidellm_sweep
    order_index: 0
    weight: 1.0
    skip_if_prev_failed: false
    params: { … }            # validated against the module's params schema
    metric_configs: [ … ]
```

An import whose slug exists answers 409. Because a service account manages only
what it created, a 409 on a slug owned by someone else is final — pick another
slug rather than trying to adopt it.
