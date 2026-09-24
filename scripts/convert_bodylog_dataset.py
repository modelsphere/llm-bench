#!/usr/bin/env python3
"""Convert a gateway *bodylog* JSONL capture → replay JSONL for the benchmark loader.

Thin CLI over `bench.replay_test.bodylog_convert`, which holds the conversion
rules and is shared with the platform's rolling dataset collector
(`app/datasets/builder.py`) so the two can never drift. Read that module's
docstring for the bodylog → MatchedRequest mapping and the full `--clean`
rule list.

Already-converted replay JSONL is also accepted as input (detected per record),
so a converted dataset can be re-processed — e.g. to apply `--clean` when the
raw capture is no longer around. Records whose request body was truncated at
capture time (`req_body_truncated` true, or missing/un-parseable `req_body`)
cannot be replayed and are skipped with a warning (the count is reported, never
silently dropped).

Usage:
    python scripts/convert_bodylog_dataset.py \
        dataset/replay/bodylog_2026-06-25_00h.jsonl \
        dataset/replay/bodylog_2026-06-25_00h.replay.jsonl \
        [--limit 1000] [--clean --max-model-len 262144]

The output JSONL is compatible with bench.replay_test.log_replay_tool.load_extract_jsonl.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bench.replay_test.bodylog_convert import (  # noqa: E402
    CONTEXT_HEADROOM_TOKENS,
    TTFT_INPUT_BUCKETS,
    ConvertStats,
    StripPolicy,
    convert_record,
    dumps_record,
)

# Drop reasons that predate --clean; reported separately for continuity.
_SKIP_REASONS = ("truncated", "no_body", "bad_json", "no_messages")
_SKIP_LABELS = {
    "truncated": "truncated req_body",
    "no_body": "missing req_body",
    "bad_json": "bad JSON",
    "no_messages": "no chat messages",
}


def convert(
    input_path: Path,
    output_path: Path,
    limit: int = 0,
    max_model_len: int = 0,
    strip: StripPolicy | None = None,
) -> int:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    source_file = input_path.name
    clean = max_model_len > 0
    budget = max_model_len - CONTEXT_HEADROOM_TOKENS
    stats = ConvertStats()

    with input_path.open("r", encoding="utf-8") as f, \
            output_path.open("w", encoding="utf-8") as out:
        for line_no, line in enumerate(f, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            stats.total += 1
            if limit > 0 and stats.written >= limit:
                break

            try:
                rec = json.loads(stripped)
            except json.JSONDecodeError as exc:
                print(f"  ⚠  line {line_no}: invalid JSON line ({exc}) — skipping", file=sys.stderr)
                stats.drops["bad_json"] += 1
                continue
            if not isinstance(rec, dict):
                print(f"  ⚠  line {line_no}: not a JSON object — skipping", file=sys.stderr)
                stats.drops["bad_json"] += 1
                continue

            record, reason = convert_record(
                rec, source_file=source_file, line_no=line_no,
                clean=clean, budget=budget, strip=strip, fixes=stats.fixes,
            )
            if record is None:
                stats.drops[reason] += 1
                if reason in ("bad_json", "no_messages"):
                    print(f"  ⚠  line {line_no}: {_SKIP_LABELS[reason]} — skipping", file=sys.stderr)
                continue

            out.write(dumps_record(record) + "\n")
            stats.note_written(
                ((rec.get("raw_payload") if "request_body" in rec and "req_body" not in rec else rec)
                 or {}).get("resp_meta", {}).get("usage", {}).get("prompt_tokens")
            )

    skipped = sum(stats.drops[r] for r in _SKIP_REASONS)
    print(f"Converted {stats.written}/{stats.total} records → {output_path}")
    if skipped:
        detail = ", ".join(f"{stats.drops[r]} {_SKIP_LABELS[r]}" for r in _SKIP_REASONS)
        print(f"  skipped {skipped}: {detail}")
    if clean:
        print(
            f"  clean: budget={budget} tok (max_model_len={max_model_len} "
            f"- {CONTEXT_HEADROOM_TOKENS} headroom)"
        )
        clean_drops = {k: v for k, v in stats.drops.items() if k not in _SKIP_REASONS}
        if clean_drops:
            detail = "  ".join(f"{k}={v}" for k, v in sorted(clean_drops.items()))
            print(f"  clean: dropped {sum(clean_drops.values())}: {detail}")
        if stats.fixes:
            detail = "  ".join(f"{k}={v}" for k, v in sorted(stats.fixes.items()))
            print(f"  clean: fixed in place: {detail}")
        if not clean_drops and not stats.fixes:
            print("  clean: nothing to drop or fix")
    if stats.written:
        parts = [f"{human}={stats.buckets.get(human, 0)}" for human, _, _ in TTFT_INPUT_BUCKETS]
        if stats.bucket_unknown:
            parts.append(f"unknown={stats.bucket_unknown}")
        print("  input-length buckets (by captured prompt_tokens): " + "  ".join(parts))
    return stats.written


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert gateway bodylog JSONL (or re-process converted replay JSONL) to replay JSONL"
    )
    parser.add_argument("input", type=Path, help="Input bodylog or converted replay JSONL file")
    parser.add_argument("output", type=Path, help="Output replay JSONL file")
    parser.add_argument("--limit", type=int, default=0, help="Max records to convert (0 = all)")
    parser.add_argument(
        "--clean", action="store_true",
        help="Drop/repair requests a strict backend would 4xx/5xx "
             "(original-4xx records, over-context prompts, broken/external images, "
             "bad tool schemas, non-string content; clamps max_tokens to fit the "
             "context window). Requires --max-model-len.",
    )
    parser.add_argument(
        "--max-model-len", type=int, default=0, metavar="TOKENS",
        help="Target backend's context window in tokens (e.g. 262144); used by --clean",
    )
    parser.add_argument(
        "--strip-secrets", action="store_true",
        help="Drop the captured response body and strip credential headers "
             "(Authorization, Cookie, x-api-key, …) from raw_payload — the policy "
             "the platform's rolling collector always applies. Off by default so "
             "existing offline conversions stay byte-identical.",
    )
    args = parser.parse_args()

    if not args.input.exists():
        parser.error(f"Input file not found: {args.input}")
    if args.clean and args.max_model_len <= CONTEXT_HEADROOM_TOKENS:
        parser.error(f"--clean requires --max-model-len > {CONTEXT_HEADROOM_TOKENS}")
    if args.max_model_len and not args.clean:
        parser.error("--max-model-len only makes sense with --clean")

    convert(
        args.input, args.output, args.limit,
        max_model_len=args.max_model_len if args.clean else 0,
        strip=StripPolicy.for_feed() if args.strip_secrets else None,
    )


if __name__ == "__main__":
    main()
