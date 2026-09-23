"""
Convert conversation datasets to a flat JSONL for GuideLLM.

Supports two formats (auto-detected):

  ShareGPT  -- JSON array of {id, conversations: [{from, value}, ...]}
  Oneapi    -- JSON array of {model, messages, usage, ...}
               (dataset/dataset-20260327 style)

Output rows:
  {"prompt": "...", "output_tokens_count": N}

GuideLLM auto-detects both column names; output_tokens_count sets max_tokens per request.

Usage:
    # ShareGPT
    python utils/prepare_dataset.py \\
        --input  dataset/sharegpt/ShareGPT_V3_unfiltered_cleaned_split.json \\
        --output dataset/sharegpt/sharegpt_prompts.jsonl

    # Oneapi (directory of JSONs, claude models only)
    python utils/prepare_dataset.py \\
        --input  dataset/dataset-20260327 \\
        --output dataset/dataset-20260327/prompts.jsonl \\
        --claude-only
"""

import argparse
import json
import random
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def approx_tokens(text: str) -> int:
    """Rough token count: word count * 1.3 (subword inflation)."""
    return max(1, round(len(text.split()) * 1.3))


def is_claude_model(model: str | None) -> bool:
    return isinstance(model, str) and model.lower().startswith("claude")


# ---------------------------------------------------------------------------
# ShareGPT format
# ---------------------------------------------------------------------------

def sharegpt_pairs(conversations: list, first_only: bool) -> list[tuple[str, int]]:
    """Return (prompt, output_tokens_count) pairs from a ShareGPT conversation."""
    pairs = []
    turns = [t for t in conversations if isinstance(t, dict)]
    for i, turn in enumerate(turns):
        if turn.get("from") != "human":
            continue
        prompt = turn.get("value", "").strip()
        if not prompt:
            continue
        output_tokens = 0
        for j in range(i + 1, len(turns)):
            if turns[j].get("from") == "gpt":
                output_tokens = approx_tokens(turns[j].get("value", ""))
                break
        pairs.append((prompt, output_tokens))
        if first_only:
            break
    return pairs


def convert_sharegpt(data: list, out, min_chars: int, max_chars: int, all_turns: bool) -> tuple[int, int]:
    written = skipped = 0
    for record in data:
        pairs = sharegpt_pairs(record.get("conversations", []), first_only=not all_turns)
        for prompt, output_tokens_count in pairs:
            if len(prompt) >= min_chars and (max_chars == 0 or len(prompt) <= max_chars):
                row = json.dumps({"prompt": prompt, "output_tokens_count": output_tokens_count}, ensure_ascii=False) + "\n"
                if isinstance(out, list):
                    out.append(row)
                else:
                    out.write(row)
                written += 1
            else:
                skipped += 1
        if not pairs:
            skipped += 1
    return written, skipped


# ---------------------------------------------------------------------------
# Oneapi format (dataset-20260327)
# ---------------------------------------------------------------------------

def extract_text(content) -> str:
    """Extract plain text from a message content field (str or list of blocks)."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        return " ".join(parts).strip()
    return ""


def convert_oneapi(data: list, out, min_chars: int, max_chars: int, claude_only: bool) -> tuple[int, int]:
    written = skipped = 0
    for record in data:
        if not isinstance(record, dict):
            skipped += 1
            continue
        model = record.get("model")
        if claude_only and not is_claude_model(model):
            skipped += 1
            continue

        messages = record.get("messages") or []
        usage = record.get("usage") or {}
        output_tokens_count = usage.get("output_tokens", 0) or 0

        # Build prompt from all user messages
        user_texts = [
            extract_text(m.get("content", ""))
            for m in messages
            if isinstance(m, dict) and m.get("role") == "user"
        ]
        prompt = "\n".join(t for t in user_texts if t).strip()

        if not prompt or len(prompt) < min_chars or (max_chars != 0 and len(prompt) > max_chars):
            skipped += 1
            continue

        row = json.dumps({"prompt": prompt, "output_tokens_count": output_tokens_count}, ensure_ascii=False) + "\n"
        if isinstance(out, list):
            out.append(row)
        else:
            out.write(row)
        written += 1
    return written, skipped


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------

def detect_format(record: dict) -> str:
    """Return 'sharegpt' or 'oneapi' based on the first record's keys."""
    if "conversations" in record:
        return "sharegpt"
    if "messages" in record and "model" in record:
        return "oneapi"
    return "unknown"


# ---------------------------------------------------------------------------
# Main convert entry point
# ---------------------------------------------------------------------------

def convert(
    input_paths: list[Path],
    output_path: Path,
    min_chars: int,
    max_chars: int,
    all_turns: bool,
    claude_only: bool,
    shuffle: bool = False,
    seed: int = 42,
) -> None:
    total_written = total_skipped = 0
    rows: list[str] = []

    for path in input_paths:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, list) or not data:
            print(f"  Skipping {path.name}: empty or not a JSON array", file=sys.stderr)
            continue

        fmt = detect_format(data[0])
        if fmt == "sharegpt":
            w, s = convert_sharegpt(data, rows, min_chars, max_chars, all_turns)
        elif fmt == "oneapi":
            w, s = convert_oneapi(data, rows, min_chars, max_chars, claude_only)
        else:
            print(f"  Skipping {path.name}: unrecognised format", file=sys.stderr)
            continue

        print(f"  {path.name}: written={w} skipped={s} (format={fmt})")
        total_written += w
        total_skipped += s

    if shuffle:
        random.seed(seed)
        random.shuffle(rows)
        print(f"Shuffled {len(rows)} rows (seed={seed})")

    with output_path.open("w", encoding="utf-8") as out:
        for row in rows:
            out.write(row)

    print(f"Done. Total written: {total_written}  skipped: {total_skipped}  -> {output_path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert ShareGPT or Oneapi conversation datasets to JSONL for GuideLLM"
    )
    parser.add_argument(
        "--input", required=True,
        help="Path to a JSON file or a directory containing multiple JSON files",
    )
    parser.add_argument("--output", required=True, help="Destination JSONL file")
    parser.add_argument(
        "--claude-only", action="store_true",
        help="(Oneapi format) Only include records where model starts with 'claude'",
    )
    parser.add_argument(
        "--all-turns", action="store_true",
        help="(ShareGPT format) Extract every human turn, not just the first",
    )
    parser.add_argument(
        "--min-chars", type=int, default=10,
        help="Minimum prompt length in characters (default: 10)",
    )
    parser.add_argument(
        "--max-chars", type=int, default=0,
        help="Maximum prompt length in characters, 0 = no limit (default: 0). "
             "Rough guide: ~4 chars per token, so 2048 tokens ≈ 8192 chars.",
    )
    parser.add_argument(
        "--shuffle", action="store_true",
        help="Shuffle output rows (use with --seed for reproducibility)",
    )
    parser.add_argument(
        "--seed", type=int, default=42,
        help="Random seed for shuffle (default: 42)",
    )
    args = parser.parse_args()

    input_path = Path(args.input)
    output_path = Path(args.output)

    if not input_path.exists():
        print(f"ERROR: input not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    if input_path.is_dir():
        input_paths = sorted(input_path.glob("*.json"))
        if not input_paths:
            print(f"ERROR: no .json files found in {input_path}", file=sys.stderr)
            sys.exit(1)
        print(f"Found {len(input_paths)} JSON files in {input_path}")
    else:
        input_paths = [input_path]

    output_path.parent.mkdir(parents=True, exist_ok=True)
    convert(input_paths, output_path, args.min_chars, args.max_chars, args.all_turns, args.claude_only, args.shuffle, args.seed)


if __name__ == "__main__":
    main()
