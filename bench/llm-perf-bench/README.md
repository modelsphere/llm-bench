# llm-perf-bench

A small threaded load generator for multi-turn, multi-user chat workloads. The
`agentic` module drives it (through `bench/tests/d_agentic.py`): each virtual
user replays a recorded conversation turn by turn, so the load has the shape of
an agent session — growing context, think time between turns — rather than
independent single requests.

It is embedded rather than installed so the module does not depend on an
external package; the platform imports it from this directory.

Standalone use:

```sh
export LLM_PERF_BASE_URL=http://localhost:8000
export LLM_PERF_MODEL=my-model
python3 llm_benchmark.py --name my-test --desc "what is being measured" \
    --work-dir ./ --test-set ./resources/testset.txt
```

`model_query.py` sends each request: it tries `BASE_URL/v1/messages` first and
falls back to `BASE_URL/v1/chat/completions`. Copy `model_query.py.template` to
adapt it to another API shape.
