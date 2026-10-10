# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- The chart's postgres starts on a freshly formatted block volume (Ceph RBD,
  EBS, a persistent disk): it keeps its data in a `pgdata` subdirectory, as
  initdb refuses a mount point holding `lost+found`. A database an earlier
  chart initialised at the volume root stays where it is.

## [0.1.2] - 2026-09-29

Versioned with LLM AutoTune 0.1.2, which it is tested with; there is no 0.1.1.

### Changed

- `docker compose up` runs the released images (`LLMBENCH_VERSION`, default
  0.1.2) instead of building the checkout; `--build` still builds it.
- The mock model (`mock_server.py`) is fast by default: 20 ms to the first
  token, 1 ms per token, 256-token replies, and requests for more capped at
  4096 tokens. A `perf-suite-v1` run against it takes about two minutes; it
  took about half an hour, mostly functional checks streaming 64K-token
  replies.
- `scripts/gen-prod-secrets.sh` needs only openssl (it needed Python's
  `cryptography`), and with `--env` writes the `.env` file Compose reads.

- The backend image carries the tokenizer the throughput modules size prompts
  with (Qwen3-0.6B's), so a benchmark downloads nothing at run time. It used to
  fetch it from huggingface.co at first use: a cluster without a route there
  failed every throughput run, and a stalled download could hang a run for up
  to an hour. `scripts/fetch_tokenizer.py` prepares another for `PROCESSOR_PATH`.

### Fixed

- The install notes no longer warn that 5.1 GB of datasets are missing: every
  module runs on the example data in the image.
- The migrate job no longer prints a harmless bcrypt traceback.
- `secrets.example.yaml` pointed at an old chart path.

## [0.1.0] - 2026-09-24

### Added

- First public release of LLMBench: a web platform that runs benchmark suites
  against OpenAI-compatible LLM endpoints and ranks the results. Nine modules
  (GuideLLM load and concurrency sweeps with SLO-driven auto search,
  production-traffic replay, 56 functional acceptance checks, academic suites,
  long-output truncation, agentic multi-turn load, hallucination and tool-call
  probes), per-benchmark scoring rules, crash-recovering workers, a Helm chart
  and a one-machine Docker Compose setup.
- Rolling replay datasets collected from the gateway's bodylog files or from
  VictoriaLogs.
- A `service` role for platforms that submit on their own (LLM AutoTune).
- Images `4pdosc/llm-bench-backend` and `4pdosc/llm-bench-frontend` on Docker
  Hub, for amd64 and arm64.
- Released under the Apache License 2.0.

[0.1.2]: https://github.com/modelsphere/llm-bench/releases/tag/v0.1.2
[0.1.0]: https://github.com/modelsphere/llm-bench/releases/tag/v0.1.0
