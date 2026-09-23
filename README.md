# LLMBench

Point it at an OpenAI-compatible endpoint; it runs a benchmark suite against it
and puts the result on a leaderboard.

A **benchmark** is an ordered list of **modules**, each with locked parameters
and its own scoring rules. Anyone with an account submits an endpoint (URL,
model name, key); a worker runs every module against it and records the raw
metrics, a per-module score, and pass/fail against the benchmark's redlines.
Submissions to the same benchmark configuration rank against each other.

## What it measures

| Module | What it does |
|---|---|
| `perf_guidellm_sweep` | Throughput and latency (TTFT, TPOT, ITL at p50/p90/p99) across concurrency levels, via [guidellm](https://github.com/vllm-project/guidellm). Grid mode, or **auto** mode: it searches for the highest concurrency that still meets your SLO — doubling, then binary search, then a confirmation run — with each probe isolated in its own process. |
| `perf_guidellm` | The same measurement at one concurrency level. |
| `replay` | Replays captured production requests and reports what the endpoint actually does with real traffic: errors, TTFT by input length, truncation, repetition, throughput, cache hits, and an optional LLM judge. The dataset can be a fixed file or a **rolling** one rebuilt from live gateway traffic, with every run pinned to — and recording — the exact build it replayed. |
| `functional_acceptance` | 56 pass/fail checks: streaming, tool calling, reasoning switches, structured output, sampling-parameter boundaries, auth, multimodal, request validation. Detects a bare engine vs a gateway and adjusts. [Catalog.](docs/functional-acceptance-checks.md) |
| `opencompass` | Academic accuracy: AIME 2025, GPQA-Diamond, IFEval, MMLU-Pro, HLE, LiveCodeBench, SimpleQA, LongBench v2. |
| `case_truncation` | Very long outputs, checked for truncation. |
| `agentic` | Multi-turn agent sessions under concurrent virtual users. |
| `hallucination`, `tool_call_success` | Replays one request many times and checks every answer. |

Throughput is also reported **card-normalized** — rescaled to a common GPU count
from the hardware the submitter declares — so an 8-GPU and a 2-GPU deployment
can be compared.

## Quickstart — one machine, no GPU

```bash
git clone https://github.com/modelsphere/llm-bench && cd llm-bench
cp .env.example .env            # fill in the four secrets; the file says how
docker compose --profile mock up -d --build
```

Open <http://localhost:8080> and sign in with `ADMIN_EMAIL` / `ADMIN_PASSWORD`
from `.env`. The `mock` profile runs a fake model: submit
`http://mock-llm:8000/v1`, model `mock`, to **perf-suite-v1** and watch it go
through every module. The numbers mean nothing about any real model; the point
is to see the whole pipeline work. Drop `--profile mock` once you are
benchmarking real endpoints.

For the academic suites, fetch the public datasets first:
`uv run --project platform/backend python scripts/fetch_academic_datasets.py`.

## On Kubernetes

```bash
scripts/gen-prod-secrets.sh --out secrets.prod.yaml
helm upgrade --install llm-bench deploy/helm/llm-bench -n llm-bench --create-namespace -f secrets.prod.yaml
```

[docs/deploying.md](docs/deploying.md) covers sizing, storage, the rolling
dataset collector, upgrades that don't kill running benchmarks, and backups.

## Used by LLM AutoTune

[LLM AutoTune](https://github.com/modelsphere/llm-autotune) searches for better
serving configurations by launching them and measuring each one here. It talks
to LLMBench as a **service account** — seeded by the deployment, able to manage
only the benchmarks it created. Setup is two values, one on each side:
[Connecting LLM AutoTune](docs/deploying.md#connecting-llm-autotune). What it
depends on is written down in [docs/api/for-autotune.md](docs/api/for-autotune.md).

## Documentation

| | |
|---|---|
| [docs/platform-architecture.md](docs/platform-architecture.md) | How it fits together: API, worker, crash recovery, scoring, datasets |
| [docs/deploying.md](docs/deploying.md) | Helm install, operations, upgrades |
| [docs/replay-datasets.md](docs/replay-datasets.md) | Rolling replay datasets, end to end |
| [docs/functional-acceptance-checks.md](docs/functional-acceptance-checks.md) | The 56 checks |
| [docs/api/for-autotune.md](docs/api/for-autotune.md) | The contract another platform relies on |
| [platform/README.md](platform/README.md) | Developer guide and API reference |

## Layout

```text
bench/        the benchmark modules and the tests they drive (a plain library)
benchmarks/   benchmark definitions; perf-suite-v1 is seeded on install
platform/     backend/ (FastAPI + Dramatiq worker), frontend/ (Vue), cli.py
deploy/helm/  the Helm chart
scripts/      dataset fetching and conversion, secrets, operations
tests/        module tests, run against a mock server — no GPU, no network
docs/
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[Apache License 2.0](LICENSE).
