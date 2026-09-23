# Replay tooling

The pieces behind the `replay` module and the rolling-dataset collector:

| File | What it does |
|---|---|
| `log_replay_tool.py` | Standalone replay CLI, and the record loader the module uses |
| `bodylog_convert.py` | Gateway bodylog record → replay record, with the `--clean` rules that make captured traffic replayable against a bare engine |
| `dataset_feed.py` | The on-disk layout of rolling datasets: builds, the `latest.json` pointer, pins, frozen copies |
| `jsonl_io.py` | gzip-transparent JSONL reading and writing |
| `analyze_causal_schedule.py` | Reconstructs conversation structure (which request continues which) from a capture |

Replaying a capture by hand, outside the platform:

```bash
python bench/replay_test/log_replay_tool.py replay \
    --input-jsonl bench/examples/replay-smoke.jsonl \
    --url http://localhost:8000/v1/chat/completions \
    --header "Authorization: Bearer $API_KEY" \
    --override-model my-model \
    --concurrency 8 \
    --output replay_results.jsonl
```

Converting raw gateway bodylog files into a replay dataset:
`scripts/convert_bodylog_dataset.py`.
