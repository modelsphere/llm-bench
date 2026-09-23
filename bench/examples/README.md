# Example datasets

Small, hand-written inputs the modules default to, so every module runs on a
fresh checkout with nothing downloaded. They prove the pipeline works; they
say nothing about how a real service performs.

| File | Used by | What it is |
|---|---|---|
| `hallucination.json` | `hallucination` | A question answerable only from the document it comes with |
| `tool_call.json` | `tool_call_success` | A request with one tool that the answer needs |
| `replay-smoke.jsonl` | `replay` (fixed source) | 20 short requests in the replay format |

`model` in each request is replaced with the model under test at run time.

For real measurements, point the modules at your own data: a captured request
for the two probes (`dataset_path`), and a replay file or a rolling collection
profile for `replay` — see `docs/replay-datasets.md`. The academic suites
behind `opencompass` are public and fetched with
`scripts/fetch_academic_datasets.py`.
