# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

[0.1.0]: https://github.com/modelsphere/llm-bench/releases/tag/v0.1.0
