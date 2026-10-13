# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- A service account creates collection profiles and manages the ones it
  created, as with benchmarks (migration `002_profile_owner`); profiles report
  `created_by_user_id`. LLM AutoTune's **Datasets** page uses this.
- `POST /replay-datasets/probe` counts what an unsaved source and filters
  match, and `POST /replay-datasets/values` lists the models or backends a
  store has seen, with request counts, so an editor can offer them.
- **Import a dataset file** as a frozen dataset (`PUT /replay-datasets/frozen/{name}`,
  the file as the request body), and **download** a frozen dataset
  (`GET /replay-datasets/frozen/{name}/download`). Together they move a dataset
  between installations: a build downloaded from one replays the same requests
  in another. An import is checked record by record before it appears, keeps
  the hash it had where it was built, and is always frozen. **Import…** and
  **Download** in the admin UI's frozen datasets; at most `REPLAY_IMPORT_MAX_GB`
  (20). The frontend's nginx streams these without a size limit or buffering.
- Frozen datasets record where they came from (`origin`: `upload` for an
  import).

### Changed

- Service accounts may list frozen datasets, so LLM AutoTune can offer them as
  replay datasets.
- A `forwarded_to` filter without a port matches every port on that host.
  On VictoriaLogs it now also matches the backend with or without a scheme,
  and in `peer`, as the file source already did.

### Fixed

- An import that was stopped, or whose page was closed, could hold its name
  with nothing in the list: the check of every record keeps running after the
  upload, and a second import of the name then failed. Imports in progress are
  now listed (`importing` in `GET /replay-datasets/frozen`: uploading, or
  checking records, with progress), and a second import of the same name says
  it is already being imported. Stopping an upload throws the partial file
  away; an import whose backend died frees its name within two minutes; a
  meta file left without its dataset no longer takes the name. The admin UI
  shows imports in progress, and the import dialog can stop an upload or close
  while the check finishes.
- On an installation with a datasets volume but no collector, deleting a
  frozen dataset answered 503: the backend now deletes it itself, as it
  writes an import itself. The chart sets `REPLAY_FROZEN_ROOT` whenever
  `datasets.enabled` is on, not only with the collector, so a custom
  `datasets.mountPath` keeps imports on the volume.
- `helm upgrade` migrates the schema before any pod is replaced (a
  `pre-upgrade` Job runs the preflight and `alembic upgrade head`; the migrate
  Job still seeds afterwards). The new backend answered 500 from the old schema
  until the post-upgrade Job caught up.
- A changed setting restarts the backend, worker and dataset collector: their
  pods now carry checksums of the ConfigMap and the Secret. Before, an upgrade
  that only changed a value (turning datasets on, a new key) left them on the
  old one.
- The backend and frontend keep serving for 8 seconds after they are told to
  stop (`preStop`), so a rolling upgrade drops no request.
- Listing profiles failed once a VictoriaLogs profile existed: responses
  left out `source_type`, so the URL failed the file source's validation.
- A VictoriaLogs probe reports the sample's prompt tokens, read from the
  flattened `resp_meta.usage.prompt_tokens`.

## [0.2.0] - 2026-10-10

Versioned with LLM AutoTune 0.2.0, which it is tested with.

### Changed

- `perf_guidellm` samples a fixed number of requests, like the sweep module:
  it stops after concurrency × `requests_per_concurrency` (default 20)
  requests, with `max_seconds` as the cap. A fixed sample keeps a result's
  precision the same however fast the endpoint is. Clear
  `requests_per_concurrency` for the old duration-only test.

### Fixed

- The chart's postgres starts on a freshly formatted block volume (Ceph RBD,
  EBS, a persistent disk): it keeps its data in a `pgdata` subdirectory, as
  initdb refuses a mount point holding `lost+found`. A database an earlier
  chart initialised at the volume root stays where it is.
- The install notes give the frontend's NodePort when
  `service.frontend.type` is `NodePort`, instead of telling you to
  port-forward.

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

[Unreleased]: https://github.com/modelsphere/llm-bench/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/modelsphere/llm-bench/compare/v0.1.2...v0.2.0
[0.1.2]: https://github.com/modelsphere/llm-bench/releases/tag/v0.1.2
[0.1.0]: https://github.com/modelsphere/llm-bench/releases/tag/v0.1.0
