# BFCL V4 integration

LLMBench runs selected BFCL V4 cases through the official generator and evaluator
at revision `6ea57973c7a6097fd7c5915698c54c17c5b1b6c8`. These are subsets, not the
full BFCL leaderboard. The integration provides two entry points:

- [`bench.bfcl.smoke`](smoke.py): a standalone, non-streaming check of two fixed cases.
- [`bfcl_v4`](../modules/bfcl_v4.py): the platform module, with streaming generation,
  telemetry, optional endpoint probing and Replay export, and concurrency scans.

Run the shell commands below from the repository root. Deployment instructions
are in the [repository README](../../README.md) and [deployment guide](../../docs/deploying.md).
For programmatic submission and result consumption, see the
[AutoTune API guide](../../docs/api/for-autotune.md).

## Standalone smoke check

Use Python 3.12, Git, and an accessible OpenAI-compatible Chat Completions endpoint:

```sh
python3.12 -m venv /tmp/llmbench-bfcl-venv
bfcl_python=/tmp/llmbench-bfcl-venv/bin/python
bfcl_rev=$("$bfcl_python" -c 'import json; print(json.load(open("bench/bfcl/smoke-manifest.json"))["revision"])')
"$bfcl_python" -m pip install requests "bfcl_eval @ git+https://github.com/ShishirPatil/gorilla.git@${bfcl_rev}#subdirectory=berkeley-function-call-leaderboard"
"$bfcl_python" -m bench.bfcl.smoke \
  --endpoint http://localhost:8000/v1 \
  --model your-served-model-name \
  --output-dir /tmp/llmbench-bfcl-smoke-001
```

Set `BFCL_ENDPOINT_API_KEY` in the process environment if authentication is
required. Without a key, the handler removes the SDK's placeholder Authorization
header. Use a new output directory for each invocation; an existing directory is
rejected.

The smoke manifest selects `simple_python_0` and `multi_turn_base_0`, with
concurrency 2 and temperature 0.001. The command verifies the installed BFCL
revision, runs native tool-call generation without streaming, checks exact case
coverage and inference errors, then invokes official partial evaluation and
checks scoring coverage. Inspect `result/` for generated records and `score/`
for category scores. A successful command does **not** imply that the model
answered either case correctly; inspect the scores and tool-call exchange.

This installation pins BFCL source, but resolves transitive dependencies at
installation time. Use the image build below when a locked dependency
environment is required.

## Worker environment

The [backend Dockerfile](../../platform/backend/Dockerfile) builds a separate
Python 3.12 BFCL environment for Linux AMD64 using
[`requirements.lock`](requirements.lock). The final image sets
`BFCL_PYTHON=/opt/bfcl/bin/python`. The ARM64 build omits this BFCL environment;
provide a compatible interpreter through `BFCL_PYTHON` to run BFCL there or in a
custom worker environment. The runner checks its installed BFCL revision.

The BFCL build downloads dependencies and the pinned source checkout with network
access. Dependency artifacts are hash-checked. Wheels are required except for
`google-search-results==2.4.2`, whose source archive is built using pinned
`setuptools`, `wheel`, and `packaging`. Installation into the final BFCL virtual
environment, source builds, `pip check`, and BFCL CLI checks run in a
`--network=none` stage with build isolation disabled. The BFCL source itself is
installed from the separately fetched, revision-pinned checkout.

For a package mirror or a slower connection, the BFCL build accepts:

```sh
docker build --platform linux/amd64 --target bfcl-builder -f platform/backend/Dockerfile \
  --build-arg BFCL_PIP_INDEX_URL=https://your-mirror/simple \
  --build-arg BFCL_PIP_TIMEOUT=600 \
  -t llmbench-bfcl-ci .
```

These arguments apply to the BFCL dependency stage. The backend environment and
tokenizer have separate build settings described in the Dockerfile.

## Task sets and parameters

All three manifests use the same pinned BFCL revision and temperature 0.001.
The module defaults to the smoke task set, `cases_per_category: 1`, and
`concurrency: 2`.

| `task_set` | Categories | Measured cases |
| --- | --- | --- |
| `bfcl-v4-smoke-v1` | `simple_python`, `multi_turn_base` | Two fixed cases; `cases_per_category` must be 1 and `case_selection` must be `head` |
| `bfcl-v4-tool-use-v1` | `simple_python`, `multiple`, `parallel`, `irrelevance`, `multi_turn_base` | `5 × cases_per_category` |
| `bfcl-v4-multi-turn-long-context-v1` | `multi_turn_long_context` | `cases_per_category` |

The tool-use and long-context sets accept 1–200 cases per category and support
`case_selection: head` (the default) or `case_selection: tail`. Head selects the
contiguous IDs `<category>_0` through `<category>_<N-1>`. Tail selects the last N
records in each category's pinned BFCL dataset file, preserving the original
dataset order rather than sorting IDs. Smoke rejects tail to preserve its fixed
cases.

For tail, a separate `select` runner phase checks the installed BFCL revision and
resolves the IDs before generation. A concurrency scan resolves the selection
once successfully and reuses the same IDs across all subsequent points. Selection
time counts toward the attempt and scan budgets; failures, timeouts and
cancellation retain the select phase's available runner diagnostics.

The resolved IDs are written to each attempt's `manifest.json` and participate
in its `protocol_hash`. Default head selection preserves the existing scan
protocol hash; tail selection participates in the scan protocol hash explicitly.

`allow_request_failures` defaults to false. When enabled, cases interrupted by
recorded HTTP or transport failures are scored as incorrect in the full case set.
Use `case_results.jsonl` and `summary.json` for these adjusted scores. The raw
BFCL files under `score/` retain the upstream evaluator's scores.
Uptime includes all generation HTTP attempts, including failed retries. Use an
`uptime` redline to set the required success rate for a benchmark.

Importable benchmark examples are available for
[tool use](../../benchmarks/bfcl-v4-tool-use-v1.yaml) and
[long-context multi-turn](../../benchmarks/bfcl-v4-multi-turn-long-context-v1.yaml).
The tool-use example selects one case per category at concurrency 2. The
long-context example selects **200 cases at concurrency 1**, despite the module's
one-case default. Adjust the case count and time budget for the endpoint being
tested. Long-context input grows through tool results and subsequent turns.

Key runtime parameters:

| Parameter | Default | Meaning |
| --- | --- | --- |
| `concurrency` | 2 | Concurrent cases, from 1 to 128; requests within a case remain ordered |
| `request_timeout_seconds` | 120 | Logical model-call deadline, including any configured retry backoff; range 1–3600 seconds |
| `max_seconds` | 1800 | Total budget for one point, including probe, generation, and evaluation; range 1–86400 seconds |
| `request_retries` | 0 | Up to 3 retries for HTTP 429 or 5xx, only before a stream has been opened |
| `probe_endpoint` | `false` | Run the separate endpoint probe before measured generation |
| `export_replay_trace` | `false` | Export measured request bodies and a provenance index |
| `required_metrics` | `[accuracy]` | Reject a point if a listed metric is absent, non-numeric, or non-finite |

The streaming handler requests `stream_options.include_usage=true`. It does not
retry partial streams, transport failures without a retryable HTTP status, or
other HTTP errors. SDK retries are disabled. There is no synthetic warmup and no
automatic endpoint cache reset.

`min_ttft_coverage`, `min_usage_coverage`, and `min_cache_usage_coverage` default
to zero and accept values in [0, 1]. The usage threshold applies separately to
input and output coverage. Both shipped benchmark YAMLs additionally require
`passed_cases_per_hour` and `ttft_p90_ms`, and set `min_ttft_coverage: 1.0`.
These point-level checks run before scan-level aggregate metrics exist; configure
aggregate requirements as benchmark metric rules instead.

## Metrics and scoring

The official evaluator determines case success. Overall `accuracy` is successful
cases divided by measured cases; each category also exposes
`accuracy_<category>` and `case_count_<category>`. Generation and grading wall
times are reported separately. `passed_cases_per_hour` uses successful cases and
the measured generation wall time, excluding grading and the optional probe.

TTFT starts at the HTTP attempt and ends at the first nonempty text content or
tool function name/arguments. Reasoning-only chunks and tool IDs alone do not
count as first output. Latency percentiles, TTFT coverage, and input/output usage
coverage use successful measured HTTP attempts. The aggregate `usage_coverage`
instead uses all measured HTTP attempts, including failed retries. HTTP
attempt/error/retry counts are also reported.

- `input_tpm` and `output_tpm` are the corresponding token/s rates multiplied by
  60. Their denominator is total measured generation wall time, including
  concurrent execution and multi-turn waiting, excluding probe and grading.
  Partial usage reporting can produce partial rates; inspect coverage alongside
  them. Missing samples or unavailable wall time produce `null`.
- `total_tpm` is their sum, available only when input and output usage coverage
  are both 100% among successful measured requests. Scans require a finite
  `total_tpm` to consider a point valid.
- `cache_hit_rate` is **token-weighted**: cached input tokens divided by input
  tokens across successful requests reporting both values. It is not a fraction
  of requests. `cache_usage_coverage` is the fraction of successful requests with
  valid paired values. Missing cache usage is not treated as zero; an explicitly
  reported zero cached-token count is a valid sample. A zero input-token
  denominator produces a `null` hit rate. BFCL also reports `_first` and
  `_subsequent` variants, split by request index within each case.

The worker applies the benchmark's `metric_configs` to compute score and pass/fail.
The module default and shipped YAMLs score accuracy and have **no accuracy
redline**. Add an explicit `role: redline` rule to enforce a quality floor. TPM
metrics are displayed by default, but can be configured as score or redline
terms; `total_tpm` also drives scan selection. Display-only defaults do not
prevent such benchmark overrides.

The `llmbench-stream-v2` handler enables the official evaluator's
`underscore_to_dot` setting, matching the dot-to-underscore conversion in OpenAI
tool schemas. Its version participates in the attempt protocol hash. Results
from older handler versions can have different accuracy for dotted function
names and should be rerun before comparison.

## Optional endpoint probe

With `probe_endpoint: true`, the official runner first generates the fixed
`multi_turn_base_0` case at concurrency 1, without scoring it. The resulting
`probe/probe.json` reports whether it observed native tool calls, a successful
request containing a tool result with a previously observed `tool_call_id`, and
complete input/output usage samples.

These are observations from that execution: `false` can mean the model never
issued a tool call. HTTP or runner errors fail the point and retain diagnostics.
Probe traffic is excluded from measured accuracy, performance metrics, and
Replay export, but consumes the point's time budget. In a scan, it runs only for
the first point. Keep it disabled during cold-cache measurements and validate
the endpoint separately.

## Concurrency scans

Set `concurrency_grid: [1, 3, 5, 6, 8]` in module parameters to execute 2–16
strictly increasing, unique integer points in [1, 128]. One module run executes
the points serially against the same endpoint. Each point uses its own attempt
directory and timing/telemetry. The scalar `concurrency` parameter is ignored.
Endpoint cache is shared across points without reset or synthetic warmup.

`max_seconds` caps each point. `max_total_seconds`, when supplied, bounds the
whole scan; otherwise the budget is `max_seconds × len(concurrency_grid)`.
**Both explicit and implicit scan budgets must be at most 169,200 seconds
(47 hours)**, leaving one hour below the worker actor's 48-hour limit. An explicit
total budget can be smaller than the sum of point caps; each point is limited
by the remaining scan budget. The worker derives its cooperative watchdog from
the declared budget.

The worker supplies the submission's frozen metric rules. Point selection checks
finite `total_tpm`, the required point-level score/redline metrics, and point-level
redlines. It then selects the eligible point with the highest `total_tpm`, breaking
ties in favor of lower concurrency. The representative point's metrics become
top-level scalars; each attempted point remains under `c<N>` with `valid`,
`eligible`, and any point error. Unattempted points have no `c<N>` entry.

The following metrics are created after the point loop, so their score/redline
rules are evaluated only by the worker after aggregation:

| Metric | Meaning |
| --- | --- |
| `reported_concurrency` | Representative point, including a diagnostic fallback when no point qualifies |
| `sweep_complete` | 1 when every planned point is valid and the scan was not canceled; a valid point can still fail a quality redline |
| `sweep_canceled` | 1 when user cancellation invalidates the scan |
| `sweep_timed_out` | 1 when the scan's total or worker time budget expires; an individual point timeout alone does not set this flag |
| `sweep_points_attempted` | Number of recorded points, including a point interrupted by cancellation |
| `sweep_points_completed` | Number of valid points |
| `sweep_points_planned` | Number of configured grid points |

If no point is eligible, the fastest valid point is retained for diagnostics and
its point-level rules fail final evaluation. With no valid point, the scan
returns an error. Failed points and budget expiry do not automatically reject an
otherwise eligible representative: add scan-level redlines when complete
execution is required. For example, a benchmark module can use:

```yaml
params:
  task_set: bfcl-v4-tool-use-v1
  concurrency_grid: [1, 3, 5, 6, 8]
  max_seconds: 1800
  max_total_seconds: 9000
metric_configs:
  - key: total_tpm
    role: score
    formula: passthrough
  - key: accuracy
    role: redline
    min_val: 0.8
  - key: sweep_complete
    role: redline
    min_val: 1
  - key: sweep_timed_out
    role: redline
    max_val: 0
```

Accepted cancellation always prevents ranking, even if a point already passed
or a run result was committed before cancellation. The submission has
`passed=false`; the scan run is `failed`, with `passed=false`, `sweep_canceled=1`,
and `sweep_complete=0`. Partial diagnostics are retained. Cancellation and final
completion are serialized; cancellation of an already completed submission is
rejected. For cancellations after artifact creation, use the persisted API
verdict rather than an archive's earlier metrics.

Submission overrides use `extra_params.bfcl_concurrency_grid` on a standalone
BFCL benchmark. They cannot be combined with scalar concurrency overrides.
Optional `expected_benchmark_config_hash` rejects a mismatch at submission time;
the worker also checks the stored benchmark hash before executing a queued scan.
Effective parameters and submission-time scoring rules are persisted. Worker
retries restart execution, preserve those rules, and discard prior runtime
display entries.

## Artifacts and Replay comparisons

Each point attempt records its resolved `manifest.json`, `params.json`,
`identity.json`, official `result/` and `score/` files, request/case events,
`case_results.jsonl`, `summary.json`, and `artifacts.json` as those stages
complete. Failed attempts can contain only partial outputs. A scan adds its own
`scan.json` and an artifact index covering the point directories. Temporary
`config.json` files are removed and excluded from published archives.

Docker Compose mounts shared artifact storage for the API and worker. Helm
requires `bfclArtifacts.enabled=true` and `bfclArtifacts.existingClaim` naming
an existing shared RWX PVC. With `BFCL_ARTIFACT_DIR` configured on both services,
the submission owner or an administrator can download the archive referenced by
the run through `GET /submissions/{submission_id}/runs/{run_id}/artifacts`,
relative to the API base URL. Storage lifecycle policy controls archive retention.

For a cache off/on comparison, repeat the same protocol against two verified
service configurations. Compare the attempt manifest hashes for corresponding
points, and record model/deployment identity, cache settings, and execution
order separately. A scan's `scan.json` hash also covers its effective parameters
and frozen metric configs. Different concurrency points have different attempt
hashes. Compare category accuracy, request counts, token usage, and cache
coverage alongside throughput.

For fixed-request comparisons, enable `export_replay_trace` and copy the
resulting `trace.jsonl` into the [Replay dataset storage](../../docs/replay-datasets.md).
Use the same file, concurrency, sample count, and request options on both Replay
candidates. The file hash is `summary.json` → `metrics.trace_sha256`;
`trace-index.jsonl` records case/request indices, protocol hash, and request-body
hashes. Probe requests are excluded. Replay schedules the captured requests
independently; it does not reproduce the original per-case causal sequence.
Use case-ordered execution when testing multi-turn prefix-cache reuse.

## Offline integration contract

CI builds the locked Linux AMD64 BFCL environment, then runs the official
generator/evaluator against a loopback fixture without external networking.
The commands require an AMD64 Docker host or working AMD64 emulation:

```sh
docker build --platform linux/amd64 --target bfcl-builder -f platform/backend/Dockerfile \
  -t llmbench-bfcl-ci .
docker run --platform linux/amd64 --rm --network none -v "$PWD:/app:ro" -w /app \
  llmbench-bfcl-ci /opt/bfcl/bin/python -m bench.bfcl.contract_test
```

The contract compares streaming and non-streaming official scores on the smoke
subset and exercises all five tool-use categories with two cases each. It also
checks authentication, tool-call ID round trips, concurrent case overlap,
ordered requests within a case, usage/cache telemetry, and the optional probe.
It covers dotted function names in single-tool, tool-selection, and parallel
calls. This fixture check does not replace validation against the target model
endpoint.

## Failure diagnostics without artifact storage

The submission's downloadable worker log includes attempt identity, concurrency,
case counts, timeout/retry settings, phase start/end, runner PID/exit code and
30-second request/case progress summaries. Failed requests include HTTP status,
exception type, safe error codes, and up to five examples with case/request IDs
and stack-frame locations. Stack source text, local variables, exception messages
from upstream, prompts and response bodies are not included.

Each attempt atomically updates `state.json`; the child records
`runner_state.json`, `runner_error.json`, and the parent records
`process_state.json`. Generation coverage failures identify the category and
missing/failed case IDs. Missing or partially written diagnostic files are
reported without replacing the original failure. Cancellation and timeout also
write terminal snapshots.

When a runner is terminated before it can write an error file, the failure
snapshot still includes `runner_state` (the last stage) and `process_state`
(PID and exit code), when available. Negative exit codes identify termination
signals; they do not establish that the process ran out of memory. Parent-side
setup or metric validation errors use `diagnostic.code: parent_failure` rather
than reporting a missing runner error.

Concurrency scans update `scan_state.json` before and after every point and
immediately checkpoint the submission log to the database. A failed point's
snapshot is retained in `metrics.cN.diagnostic` when the module result is saved,
even if a later point succeeds and the overall submission passes. Failure
snapshots include the phase, elapsed time, event counts, failure samples and
runner diagnostic; `cN/failure.json` retains the local point failure. These logs
and result metadata do not require `BFCL_ARTIFACT_DIR`. Local JSON files follow
the worker's existing output-directory retention policy; configured artifact
storage remains necessary for durable access to all raw artifacts.

Scan checkpoint log entries contain compact point status, validity, eligibility,
and error summaries. Full metrics and diagnostics remain in `scan_state.json`
and the module result rather than being repeated at every checkpoint.

For `generate: ValueError`, first inspect `diagnostic.stage` and the error code:
`generation_inference_failed` means generated records contain failures;
`generation_case_coverage_mismatch` means selected cases are missing or duplicated;
`completion_stream_incomplete` identifies a response without a complete finish.
Unknown upstream exceptions retain their type and stack-frame locations.
