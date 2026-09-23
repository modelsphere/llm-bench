# Contributing

Thanks for looking. Issues and pull requests are welcome; for anything larger
than a fix, open an issue first so the approach can be agreed before you write
it.

## Setting up

Python 3.12 with [uv](https://docs.astral.sh/uv/), Node 22, Docker, and — for
the chart — Helm. The developer guide is [platform/README.md](platform/README.md).

## Before you open a pull request

```bash
uvx ruff@0.15.15 check .
uv run --project platform/backend python -m pytest tests/modules/ -q
(cd platform/backend && PYTHONPATH=../.. uv run pytest tests/ -q)   # needs Postgres + Redis, see the guide
(cd platform/frontend && npm run build)
helm lint deploy/helm/llm-bench --set secrets.secretKey=x --set secrets.platformSecretKey=x \
  --set secrets.adminEmail=a@example.com --set secrets.adminUsername=a \
  --set secrets.adminPassword=x --set postgres.password=x
```

CI runs all of these, plus a migration check (`alembic upgrade`, `alembic check`,
downgrade, upgrade) and a hygiene gate.

## Things that need extra care

- **Anything that changes a number.** A change to a module, the evaluator, or
  the load generator can move every score on every leaderboard without a
  configuration changing — and the leaderboard's config-hash check will not
  notice, because the benchmark itself did not change. Say so in the pull
  request and in the changelog, so operators know to bump their benchmarks'
  `version` (which moves the hash and starts a fresh leaderboard). `tests/modules/test_guidellm_measurement.py` checks measured TTFT,
  inter-token latency and counts against a server with known timing; a guidellm
  upgrade must keep it green.
- **Schema changes** are Alembic revisions, generated against a real Postgres
  and read before committing. Never put identities or credentials in one.
- **Datasets.** Never commit captured traffic, even a few lines: it is someone's
  prompts. Tests use `bench/examples/` or build records in code.
- **Both locales.** UI strings go in `platform/frontend/src/locales/en.json`
  and `zh-CN.json`; CI fails when their keys differ.

## Adding a benchmark module

Subclass `TestModule` in `bench/modules/`, register it in `MODULE_REGISTRY`,
and give it a `ParamsSchema`, metric descriptors and default metric configs.
A module returns raw metrics only — scoring and pass/fail belong to the
benchmark's metric configs, so the same module can be judged differently by
different benchmarks. Add a test that runs it against `mock_server.py`.

## License

Contributions are accepted under the Apache License 2.0, the license this
project is released under.
